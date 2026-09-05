"""mcp.probe — stdio probe for installed MCP servers.

Spec: internal/spec-mcp-administration-2026-05-10.md §5.5 (Health monitoring).

The probe spawns an MCP server (or its evolve wrapper) over stdio, sends
the MCP ``initialize`` JSON-RPC message, waits for the server to respond
with its ``serverInfo`` block, then kills the process. The point is to
verify three things at once:

  1. The command can be spawned (binary on PATH, args parseable, wrapper
     script exists).
  2. Required credentials resolved — the wrapper exits non-zero with a
     specific exit code (64) when a keystore key is missing.
  3. The MCP server reaches the ``initialize`` exchange in finite time.

Phase C ships stdio probes only. HTTP-transport probes are out of scope
until the credential-binding design for header-based auth is settled
(spec open question 1).

The implementation uses a thread to read the first line off stdout so we
can apply a real timeout without blocking on a process that never closes
its pipes (an MCP server is long-running by nature — communicate() would
hang).
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso


# JSON-RPC initialize payload. The MCP protocol version string is what
# the current first-party Anthropic servers expect; servers that respect
# the spec will accept this with the empty capabilities and reply with
# their own serverInfo + capabilities. If the server rejects this
# version we still record a non-OK probe (the bot can't talk to it
# either) but classify it as ``protocol_mismatch`` so the operator
# knows it's not a credential or spawn issue.
_PROTOCOL_VERSION = "2024-11-05"

_INIT_MESSAGE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "evolve-mcp-probe", "version": "0.1"},
    },
}


_ERROR_CLASSES = {
    "ok",                   # serverInfo received
    "spawn_failed",         # subprocess.Popen raised OSError
    "credential_invalid",   # wrapper exited 64 (missing keystore key)
    "early_exit",           # process exited before responding
    "timeout",              # no response within timeout
    "bad_response",         # got data but not valid JSON-RPC
    "protocol_mismatch",    # server rejected the protocol version
    "other",
}


@dataclass
class ProbeResult:
    """Outcome of one probe run."""

    ok: bool
    bot_id: str
    server_id: str
    probed_at: str            # ISO-8601 UTC
    latency_ms: int
    error_class: str          # one of _ERROR_CLASSES
    error_detail: str = ""    # short human-readable
    server_info: dict[str, Any] = field(default_factory=dict)  # parsed serverInfo when ok

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── stdout reader thread ──────────────────────────────────────────────────────

def _drain_first_line(stream, out_queue: "queue.Queue[str]") -> None:
    """Read one line from a binary stream and push to the queue.

    Daemon-thread helper. Stops on first line OR end-of-stream OR error.
    """
    try:
        line = stream.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        out_queue.put(line)
    except Exception:  # noqa: BLE001 — defensive; thread can't crash the caller
        try:
            out_queue.put("")
        except Exception:  # pragma: no cover
            pass


# ── Public entry point ────────────────────────────────────────────────────────

def probe_stdio(
    command: str,
    args: list[str],
    *,
    bot_id: str,
    server_id: str,
    timeout: float = 10.0,
    env: "dict[str, str] | None" = None,
) -> ProbeResult:
    """Run one stdio probe against the given (command, args).

    Returns a ProbeResult with ``ok=True`` only if the server returned a
    valid JSON-RPC response with a ``result.serverInfo`` block.
    """
    started = time.monotonic()
    probed_at = _utc_now_iso()

    try:
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )
    except OSError as exc:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=0,
            error_class="spawn_failed",
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    response_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    reader = threading.Thread(
        target=_drain_first_line, args=(proc.stdout, response_q), daemon=True,
    )
    reader.start()

    try:
        payload = (json.dumps(_INIT_MESSAGE) + "\n").encode("utf-8")
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            _kill(proc)
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_class="early_exit",
                error_detail=f"stdin write failed: {exc}",
            )

        try:
            line = response_q.get(timeout=timeout)
        except queue.Empty:
            line = ""

        latency_ms = int((time.monotonic() - started) * 1000)

        # Did the process exit early?
        rc = proc.poll()
        if not line:
            # No stdout — check exit code for the wrapper's missing-key signal
            if rc == 64:
                stderr_bytes = b""
                try:
                    if proc.stderr is not None:
                        stderr_bytes = proc.stderr.read() or b""
                except Exception:  # noqa: BLE001
                    pass
                detail = stderr_bytes.decode("utf-8", errors="replace").strip()[:240]
                return ProbeResult(
                    ok=False, bot_id=bot_id, server_id=server_id,
                    probed_at=probed_at, latency_ms=latency_ms,
                    error_class="credential_invalid",
                    error_detail=detail or "wrapper script reported missing keystore key",
                )
            if rc is not None and rc != 0:
                stderr_bytes = b""
                try:
                    if proc.stderr is not None:
                        stderr_bytes = proc.stderr.read() or b""
                except Exception:  # noqa: BLE001
                    pass
                detail = stderr_bytes.decode("utf-8", errors="replace").strip()[:240]
                return ProbeResult(
                    ok=False, bot_id=bot_id, server_id=server_id,
                    probed_at=probed_at, latency_ms=latency_ms,
                    error_class="early_exit",
                    error_detail=f"rc={rc}; {detail}" if detail else f"rc={rc}",
                )
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at, latency_ms=latency_ms,
                error_class="timeout",
                error_detail=f"no response within {timeout:.1f}s",
            )

        # Parse the line as JSON-RPC
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at, latency_ms=latency_ms,
                error_class="bad_response",
                error_detail=f"json: {exc.msg}; first 80 chars: {line[:80]!r}",
            )

        if "error" in msg:
            err = msg.get("error") or {}
            err_msg = (err.get("message") if isinstance(err, dict) else "") or ""
            if "protocol" in err_msg.lower() or "version" in err_msg.lower():
                err_class = "protocol_mismatch"
            else:
                err_class = "bad_response"
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at, latency_ms=latency_ms,
                error_class=err_class,
                error_detail=f"server error: {err_msg[:200]}",
            )

        result = msg.get("result") or {}
        server_info = result.get("serverInfo") or {}
        if not isinstance(server_info, dict) or "name" not in server_info:
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at, latency_ms=latency_ms,
                error_class="bad_response",
                error_detail="response missing result.serverInfo.name",
            )

        return ProbeResult(
            ok=True, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class="ok",
            server_info={
                "name": str(server_info.get("name") or ""),
                "version": str(server_info.get("version") or ""),
                "protocol_version": str(result.get("protocolVersion") or ""),
            },
        )
    finally:
        _kill(proc)


def probe_http(
    url: str,
    headers: "dict[str, str] | None",
    *,
    bot_id: str,
    server_id: str,
    timeout: float = 10.0,
) -> ProbeResult:
    """Run one http-transport probe against the given URL.

    POSTs the MCP initialize JSON-RPC message; expects a response that
    parses as JSON-RPC with ``result.serverInfo`` (per the protocol).
    Classifies into the same error_class taxonomy as probe_stdio() so
    the monitor's signal logic doesn't need a transport-specific branch.

    Auth-related failures (401, 403) are classified as
    ``credential_invalid`` so the signal fires the same way the stdio
    wrapper's exit-64 does.
    """
    import urllib.request
    import urllib.error

    started = time.monotonic()
    probed_at = _utc_now_iso()

    body = (json.dumps(_INIT_MESSAGE) + "\n").encode("utf-8")
    req_headers = dict(headers or {})
    req_headers.setdefault("Content-Type", "application/json")
    req_headers.setdefault("Accept", "application/json, text/event-stream")
    req = urllib.request.Request(url, data=body, method="POST", headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        if exc.code in (401, 403):
            return ProbeResult(
                ok=False, bot_id=bot_id, server_id=server_id,
                probed_at=probed_at, latency_ms=latency_ms,
                error_class="credential_invalid",
                error_detail=f"HTTP {exc.code} from server",
            )
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class="bad_response",
            error_detail=f"HTTP {exc.code}",
        )
    except urllib.error.URLError as exc:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_class="spawn_failed",
            error_detail=f"url_error: {exc.reason}",
        )
    except (OSError, TimeoutError) as exc:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_class="timeout" if isinstance(exc, TimeoutError) else "spawn_failed",
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    text = payload.decode("utf-8", errors="replace")
    # Server-Sent Events transport prefixes data lines with "data: ";
    # strip if present so the JSON-RPC parse below works either way.
    first_line = ""
    for raw in text.splitlines():
        if raw.startswith("data: "):
            first_line = raw[len("data: "):]
            break
        if raw.strip():
            first_line = raw
            break
    if not first_line:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class="bad_response",
            error_detail=f"empty body (status={status})",
        )
    try:
        msg = json.loads(first_line)
    except json.JSONDecodeError as exc:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class="bad_response",
            error_detail=f"json: {exc.msg}; first 80: {first_line[:80]!r}",
        )

    if "error" in msg:
        err = msg.get("error") or {}
        err_msg = (err.get("message") if isinstance(err, dict) else "") or ""
        if "protocol" in err_msg.lower() or "version" in err_msg.lower():
            err_class = "protocol_mismatch"
        elif "auth" in err_msg.lower() or "unauthor" in err_msg.lower():
            err_class = "credential_invalid"
        else:
            err_class = "bad_response"
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class=err_class,
            error_detail=f"server error: {err_msg[:200]}",
        )

    result = msg.get("result") or {}
    server_info = result.get("serverInfo") or {}
    if not isinstance(server_info, dict) or "name" not in server_info:
        return ProbeResult(
            ok=False, bot_id=bot_id, server_id=server_id,
            probed_at=probed_at, latency_ms=latency_ms,
            error_class="bad_response",
            error_detail="response missing result.serverInfo.name",
        )

    return ProbeResult(
        ok=True, bot_id=bot_id, server_id=server_id,
        probed_at=probed_at, latency_ms=latency_ms,
        error_class="ok",
        server_info={
            "name": str(server_info.get("name") or ""),
            "version": str(server_info.get("version") or ""),
            "protocol_version": str(result.get("protocolVersion") or ""),
        },
    )


def _kill(proc: subprocess.Popen) -> None:
    """Best-effort terminate; SIGKILL if it doesn't go down."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    except Exception:  # noqa: BLE001
        pass


# ── Health log (jsonl per (bot, server)) ──────────────────────────────────────

def health_dir(shared_dir: Path) -> Path:
    return shared_dir / "mcp" / "health"


def health_log_path(shared_dir: Path, bot_id: str, server_id: str) -> Path:
    return health_dir(shared_dir) / bot_id / f"{server_id}.jsonl"


def append_health(result: ProbeResult, shared_dir: Path) -> None:
    """Append one probe result to the per-(bot, server) jsonl log."""
    path = health_log_path(shared_dir, result.bot_id, result.server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result.to_dict(), sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_recent(
    shared_dir: Path,
    bot_id: str,
    server_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read up to ``limit`` most recent probe results for (bot, server).

    Returns the entries newest-first. Returns [] when no log file exists.
    """
    path = health_log_path(shared_dir, bot_id, server_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    # Walk from the end so we can stop after ``limit``
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def last_n_status(
    shared_dir: Path,
    bot_id: str,
    server_id: str,
    *,
    n: int = 3,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return (entries, ok_count, fail_count) over the last n probes."""
    recent = read_recent(shared_dir, bot_id, server_id, limit=n)
    ok = sum(1 for e in recent if e.get("ok"))
    fail = len(recent) - ok
    return recent, ok, fail

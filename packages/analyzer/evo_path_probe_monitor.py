"""evo_path_probe_monitor — synthetic end-to-end probe for the "evo" keyword path.

Why this exists
───────────────
The "evo" keyword path — a member bot's gateway plugin calling the admin
server's ``/api/evo/dispatch`` so a user typing ``evo help`` (or any ``evo
…`` subcommand) gets a real response — has broken pod-wide more than once:

  * device-auth on-by-default 401'd the plugin's cookieless TCP RPC
    (the 2026-06-24 outage; interim PR #3257);
  * the admin-daemon unix socket bound a hardcoded macOS path that never
    existed on Linux, so every socket client hit connect ENOENT (#3160);
  * the same socket landed mode 0660 ``evolve:evolve`` and the Linux evo
    gateway (uid not in group ``evolve``) hit connect EACCES (#3223).

EVERY time, the break was found by a human typing ``evo help`` and noticing
the bot answered with generic OpenClaw help instead of the evo surface —
NEVER by a monitor. There was no probe for this path. This module is it.

What it does
────────────
A pure-Python monitor (no LLM) that exercises the evo path the same way the
plugin does and asserts it works, then fires/resolves a Signal:

  * **TCP transport** — a black-box, *cookieless* HTTP ``POST`` to
    ``127.0.0.1:5050/api/evo/dispatch``, byte-for-byte the shape
    ``packages/plugin/src/better/EvoDispatchClient.ts`` sends (no ``Cookie``
    header, no device token). PASS = HTTP 200 with a well-formed
    ``DispatchResult``: ``mode`` is a string AND (``system_append`` non-empty
    OR ``direct_send_message`` non-empty). FAIL = HTTP 401 / connection error
    / non-JSON / ``mode`` missing / empty envelope / exception.

  * **unix-socket transport** — connects to ``{shared_dir}/admin-daemon.sock``
    (the socket the plugin's other clients — RosterTools, the directory
    digest — use) and round-trips an HTTP request, mirroring the daemon's own
    liveness self-probe (``unix_socket_server._probe_socket_alive``). This
    surfaces the socket-perms / socket-path break class (the Linux ENOENT /
    EACCES / ECONNREFUSED failures). The assertion here is *transport
    reachability*: a connection-class failure is RED, but an HTTP response of
    any status (including the 401 the ``evolve``-uid probe legitimately gets
    when the daemon trusts only the ``evo`` peer uid) proves the socket is
    serving and is GREEN. Dispatch-envelope correctness is the TCP probe's
    job; this one isolates "is the socket the plugin connects to even
    reachable."

The faithfulness IS the value
─────────────────────────────
The probe MUST make a real request over the wire with no cookie. It must NOT
call the in-process ``dispatch()`` Python function: the entire #3257 outage
lived in the Flask ``_enforce_device_auth`` ``before_request`` gate, which an
in-process call bypasses — so an in-process probe would have stayed GREEN
through the whole outage. Replicating the plugin's request shape (POST, path,
JSON body, NO cookie, loopback TCP) is the only thing that catches it.

Signal
──────
Producer ``evo_path_probe``; type ``evo_path_down``; pod-scoped, severity
``alert``. ONE pod-wide Signal per transport — the gate/transport is pod-wide,
so a break is an Evolve bug, not per-bot drift; firing per-bot would be noise.
The signature (``evo_path_probe:evo_path_down:<transport>``) is stable across
runs and bots, so ``observe()`` find-or-creates: re-running a RED probe bumps
the existing Signal's count rather than spawning a new one (count-agnostic, no
per-run spam). ``sweep_resolve`` at the end auto-archives a transport's Signal
the moment it recovers.

Delivery
────────
Installed as a 30-minute infra job (``ai.openclaw.evolve.evo_path_probe_monitor``)
by ``deploy.install_evolve_infra_jobs`` via the platform_profile seam — launchd
on macOS, systemd on Linux, no hardcoded paths/labels. Runs on BOTH pods so
platform/auth divergence is caught. Also runnable as a post-deploy Gate-2 check
with ``--gate`` (exits non-zero on RED; the default scheduled run exits 0 even
on RED so it does not also trip ``cron_exit_monitor`` — the Signal IS the
alert). Prints a one-line JSON run-summary to stdout on every run, so
``monitor_coverage``'s producer-liveness layer can tell when the probe itself
goes silent.

Spec: docs/spec-reports-2026-06-12.md (signal-producer quality + UX).
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import http.client
import json
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolve_config import (
    CANONICAL_SHARED_DIR,
    get_members,
    get_primary,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "evo_path_probe"
SIGNAL_TYPE = "evo_path_down"

# Faithful to packages/plugin/src/better/EvoDispatchClient.ts:
#   const ADMIN_PORT = 5050; const ADMIN_HOST = "127.0.0.1";
# The plugin hardcodes these; the probe mirrors them so it exercises exactly
# the path the plugin does. Overridable for tests / non-default pods.
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 5050
DISPATCH_PATH = "/api/evo/dispatch"
SOCKET_FILENAME = "admin-daemon.sock"

# Synthetic probe identity. A stable, obviously-synthetic sender id so the
# dispatch identity resolver has something to key on without colliding with a
# real channel user. ``evo help`` is role-agnostic and returns a stable,
# non-empty envelope (system_append + direct_send_message) on every role.
PROBE_CHANNEL = "telegram"          # the channel the keyword path passes
PROBE_SENDER_EXTERNAL_ID = "evo-path-probe"
PROBE_RAW_TEXT = "evo help"

# Mirrors EvoDispatchClient's req.setTimeout(10_000, ...).
DEFAULT_TIMEOUT_SECONDS = 10.0

# Unix-socket statuses that prove the daemon is serving the request (the
# transport is healthy) even though the dispatch envelope was not asserted.
# 401/403 are EXPECTED when the probe's own uid is not the socket's trusted
# peer (the daemon trusts only the ``evo`` uid post-cutover) — that is an auth
# outcome, not a socket break, so it must not RED the transport probe.
_SOCKET_TRANSPORT_OK_STATUSES = frozenset({200, 401, 403})


@dataclass
class ProbeOutcome:
    """The result of probing one transport."""

    transport: str                  # "tcp" | "unix-socket"
    ok: bool
    http_status: int | None = None
    error: str | None = None        # short error class, e.g. "connect:ENOENT"
    detail: str = ""                # one-line human-readable cause
    latency_ms: int | None = None
    envelope_ok: bool = False       # well-formed DispatchResult observed
    mode: str | None = None

    @property
    def transport_label(self) -> str:
        return "TCP" if self.transport == "tcp" else "unix socket"

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "ok": self.ok,
            "http_status": self.http_status,
            "error": self.error,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "envelope_ok": self.envelope_ok,
            "mode": self.mode,
        }


def _probe_request_body(bot_id: str) -> str:
    """The exact JSON body EvoDispatchClient.dispatch() POSTs."""
    return json.dumps(
        {
            "bot_id": bot_id,
            "channel": PROBE_CHANNEL,
            "sender_external_id": PROBE_SENDER_EXTERNAL_ID,
            "raw_text": PROBE_RAW_TEXT,
        }
    )


def _errno_class(exc: OSError) -> str:
    """A short, stable label for an OS error (errno name when available)."""
    name = errno.errorcode.get(exc.errno or 0) if isinstance(exc, OSError) else None
    return f"connect:{name}" if name else f"connect:{type(exc).__name__}"


# ── DispatchResult envelope assertion (shared by TCP + socket-200 paths) ──────


def _evaluate_dispatch_envelope(payload: Any) -> tuple[bool, str | None, str]:
    """Apply the PASS criteria to a parsed dispatch response body.

    PASS = ``mode`` is a string AND (``system_append`` non-empty OR
    ``direct_send_message`` non-empty). Returns ``(envelope_ok, mode, detail)``.
    """
    if not isinstance(payload, dict):
        return False, None, "response body is not a JSON object"
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        return False, None, "DispatchResult.mode missing or not a string"
    system_append = payload.get("system_append")
    direct_send = payload.get("direct_send_message")
    has_text = bool(
        (isinstance(system_append, str) and system_append.strip())
        or (isinstance(direct_send, str) and direct_send.strip())
    )
    if not has_text:
        return False, mode, "empty envelope (no system_append / direct_send_message)"
    return True, mode, "well-formed DispatchResult"


# ── TCP probe: faithful EvoDispatchClient replica ─────────────────────────────


def probe_tcp(
    *,
    bot_id: str,
    host: str = ADMIN_HOST,
    port: int = ADMIN_PORT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeOutcome:
    """POST /api/evo/dispatch over loopback TCP with NO device cookie.

    This is the byte-for-byte plugin call. The absence of a ``Cookie`` header
    is deliberate and load-bearing: it is what makes the probe see the
    device-auth 401 the #3257 outage produced. Do not add one.
    """
    body = _probe_request_body(bot_id)
    body_bytes = body.encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
    }
    t0 = time.monotonic()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", DISPATCH_PATH, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read()
    except (socket.timeout, TimeoutError):
        ms = int((time.monotonic() - t0) * 1000)
        return ProbeOutcome(
            "tcp", False, error="timeout", latency_ms=ms,
            detail=f"TCP request to {host}:{port} timed out after {timeout:.0f}s",
        )
    except OSError as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return ProbeOutcome(
            "tcp", False, error=_errno_class(exc), latency_ms=ms,
            detail=f"TCP transport error to {host}:{port}: {exc}",
        )
    finally:
        conn.close()
    ms = int((time.monotonic() - t0) * 1000)

    if status != 200:
        cause = (
            "device-auth gate (the plugin sends no cookie)"
            if status in (401, 403)
            else f"unexpected HTTP {status}"
        )
        return ProbeOutcome(
            "tcp", False, http_status=status, error=f"http:{status}",
            latency_ms=ms, detail=f"HTTP {status} on {DISPATCH_PATH} — {cause}",
        )
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return ProbeOutcome(
            "tcp", False, http_status=status, error="non-json", latency_ms=ms,
            detail="HTTP 200 but response body was not valid JSON",
        )
    envelope_ok, mode, detail = _evaluate_dispatch_envelope(payload)
    return ProbeOutcome(
        "tcp", envelope_ok, http_status=status,
        error=None if envelope_ok else "bad-envelope",
        latency_ms=ms, envelope_ok=envelope_ok, mode=mode, detail=detail,
    )


# ── unix-socket probe: faithful transport-reachability check ──────────────────


def _parse_http_over_socket(data: bytes) -> tuple[int | None, Any]:
    """Parse a raw HTTP/1.x response read off a stream socket.

    Returns ``(status_code_or_None, parsed_json_body_or_None)``. A None status
    means the bytes were not a recognizable HTTP response (garbled / non-HTTP).
    """
    if not data:
        return None, None
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
        return None, None
    try:
        status = int(parts[1])
    except ValueError:
        return None, None
    payload: Any = None
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
    return status, payload


def probe_unix_socket(
    socket_path: str | Path,
    *,
    bot_id: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeOutcome:
    """Round-trip an HTTP request over the admin-daemon unix socket.

    Catches the socket-perms / socket-path break class (ENOENT when the path
    is wrong/absent, EACCES when perms lock the caller out, ECONNREFUSED when
    nothing is listening). A connection-class failure is RED; any HTTP
    response (incl. the 401/403 the ``evolve``-uid probe gets when the daemon
    trusts only the ``evo`` peer) proves the socket is serving and is GREEN —
    auth over the socket is the TCP probe's concern, not the transport probe's.
    """
    socket_path = str(socket_path)
    if not Path(socket_path).exists():
        return ProbeOutcome(
            "unix-socket", False, error="connect:ENOENT",
            detail=f"admin-daemon.sock not found at {socket_path} (ENOENT) — "
            "socket unbound or path wrong",
        )
    body = _probe_request_body(bot_id)
    body_bytes = body.encode("utf-8")
    # HTTP/1.0 so the server closes the connection after the response and our
    # read-to-EOF terminates cleanly — same shape the daemon's own
    # _probe_socket_alive uses. Host header is cosmetic on the unix socket.
    raw_req = (
        f"POST {DISPATCH_PATH} HTTP/1.0\r\n"
        f"Host: admin-daemon\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"\r\n"
    ).encode("utf-8") + body_bytes

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.monotonic()
    try:
        s.connect(socket_path)
        s.sendall(raw_req)
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except socket.timeout:
        ms = int((time.monotonic() - t0) * 1000)
        return ProbeOutcome(
            "unix-socket", False, error="timeout", latency_ms=ms,
            detail=f"unix socket {socket_path} did not respond within {timeout:.0f}s",
        )
    except OSError as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return ProbeOutcome(
            "unix-socket", False, error=_errno_class(exc), latency_ms=ms,
            detail=f"unix socket {socket_path} connect/transport error: {exc}",
        )
    finally:
        with contextlib.suppress(OSError):
            s.close()
    ms = int((time.monotonic() - t0) * 1000)

    status, payload = _parse_http_over_socket(data)
    if status is None:
        return ProbeOutcome(
            "unix-socket", False, error="non-http", latency_ms=ms,
            detail="connected, but the response was not a valid HTTP reply",
        )
    if status not in _SOCKET_TRANSPORT_OK_STATUSES:
        # 404 (route gone) / 5xx (dispatch crashed) over a reachable socket —
        # the transport is up but the server is broken; worth a RED.
        return ProbeOutcome(
            "unix-socket", False, http_status=status, error=f"http:{status}",
            latency_ms=ms,
            detail=f"socket reachable but {DISPATCH_PATH} returned HTTP {status}",
        )
    # Transport healthy. If we happened to get a 200, assert the envelope too
    # for the richest detail (a trusted-peer pod), but never RED on it.
    envelope_ok, mode, env_detail = (False, None, "")
    if status == 200:
        envelope_ok, mode, env_detail = _evaluate_dispatch_envelope(payload)
    detail = (
        f"socket reachable; {DISPATCH_PATH} returned HTTP {status}"
        + (f" with a {env_detail}" if status == 200 else "")
    )
    return ProbeOutcome(
        "unix-socket", True, http_status=status, latency_ms=ms,
        envelope_ok=envelope_ok, mode=mode, detail=detail,
    )


# ── Signal emission ───────────────────────────────────────────────────────────


def pod_label() -> str:
    """A short, human pod label for Signal titles (e.g. "mini", "evolve-vps-pod").

    The hostname is the most reliable pod identifier available in the analyzer
    layer; falls back to the platform name if it is unavailable.
    """
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    if host:
        return host.split(".")[0]
    try:
        from platform_profile import get_profile
        return get_profile().name
    except Exception:  # noqa: BLE001 — label is cosmetic; never fail the probe
        return "pod"


def _human_title(o: ProbeOutcome, pod: str) -> str:
    """Legible one-line title, e.g.
    "evo keyword path down — HTTP 401 on /api/evo/dispatch (mini, TCP)".
    """
    if o.http_status is not None:
        cause = f"HTTP {o.http_status} on {DISPATCH_PATH}"
    elif o.error == "connect:ENOENT":
        cause = "admin-daemon.sock missing (ENOENT)"
    elif o.error and o.error.startswith("connect:EACCES"):
        cause = "admin-daemon.sock permission denied (EACCES)"
    elif o.error and o.error.startswith("connect:ECONNREFUSED"):
        cause = "connection refused (daemon down)"
    elif o.error == "timeout":
        cause = "request timed out"
    elif o.error == "non-json":
        cause = "non-JSON response"
    elif o.error == "non-http":
        cause = "non-HTTP response"
    elif o.error == "bad-envelope":
        cause = "empty/malformed DispatchResult"
    else:
        cause = o.error or "unknown failure"
    return f"evo keyword path down — {cause} ({pod}, {o.transport_label})"


def _signal_body(o: ProbeOutcome, pod: str) -> str:
    return (
        f"The evo keyword path is broken on {pod} over the {o.transport_label} "
        f"transport. A black-box probe replicating the gateway plugin's "
        f"cookieless call to {DISPATCH_PATH} failed:\n\n"
        f"  {o.detail}\n\n"
        f"Users typing `evo help` (or any `evo …` subcommand) on this pod get "
        f"generic OpenClaw help instead of the evo surface. This is a pod-wide "
        f"break of a shared path, not per-bot drift. Likely causes: the "
        f"device-auth gate 401'ing the plugin's cookieless RPC (the #3257 "
        f"shape), the admin daemon being down, or an admin-daemon.sock "
        f"path/permission break."
    )


def emit_signals(
    shared_dir: Path,
    outcomes: list[ProbeOutcome],
    *,
    pod: str | None = None,
    dry_run: bool = False,
) -> set[str]:
    """Fire a pod-wide Signal per RED transport; sweep_resolve recovered ones.

    Returns the set of kept (still-firing) signatures.
    """
    pod = pod or pod_label()
    kept: set[str] = set()
    for o in outcomes:
        signature = make_signature(PRODUCER, SIGNAL_TYPE, o.transport)
        if o.ok:
            continue
        kept.add(signature)
        if dry_run:
            continue
        try:
            signals_store.observe(
                shared_dir,
                signature=signature,
                producer=PRODUCER,
                type=SIGNAL_TYPE,
                severity="alert",
                flavor="maintenance",
                scope="pod",
                # Group the TCP + socket findings of one outage into one
                # incident in the Alerts UI without merging their dedup.
                incident_key=f"{SIGNAL_TYPE}:{pod}",
                title=_human_title(o, pod),
                body=_signal_body(o, pod),
                details={
                    "pod": pod,
                    "transport": o.transport,
                    "endpoint": DISPATCH_PATH,
                    "http_status": o.http_status,
                    "error": o.error,
                    "detail": o.detail,
                    "latency_ms": o.latency_ms,
                    "probe": "black-box cookieless plugin-faithful request",
                    # Severity-framework fields (mirrors integration_probe):
                    # a pod-wide user-facing path fully down.
                    "vector": "operations",
                    "magnitude": 3,
                    "severity_active": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 — emit is best-effort
            print(
                f"[evo_path_probe] observe() failed for {signature}: {exc}",
                file=sys.stderr,
            )
    if not dry_run:
        try:
            signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                types={SIGNAL_TYPE},
                reason="evo path probe recovered",
            )
        except Exception as exc:  # noqa: BLE001 — sweep is best-effort
            print(
                f"[evo_path_probe] sweep_resolve() failed: {exc}",
                file=sys.stderr,
            )
    return kept


# ── Orchestration ─────────────────────────────────────────────────────────────


def _resolve_probe_bot(config: dict[str, Any], explicit: str | None) -> str | None:
    """Pick a representative bot to address the dispatch at.

    An explicit ``--bot`` wins; otherwise the primary bot (always present and
    role-stable), then the first member. Returns None when no bot resolves —
    the caller skips the probe rather than addressing dispatch at a bot that
    does not exist (which would produce a false RED).
    """
    if explicit:
        return explicit
    primary = get_primary(config)
    if primary:
        return primary
    members = get_members(config)
    return members[0] if members else None


def run_probe(
    shared_dir: Path,
    *,
    bot_id: str,
    host: str = ADMIN_HOST,
    port: int = ADMIN_PORT,
    socket_path: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    pod: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Probe both transports, emit/sweep Signals, return a summary dict."""
    pod = pod or pod_label()
    if socket_path is None:
        socket_path = Path(shared_dir) / SOCKET_FILENAME

    outcomes = [
        probe_tcp(bot_id=bot_id, host=host, port=port, timeout=timeout),
        probe_unix_socket(socket_path, bot_id=bot_id, timeout=timeout),
    ]
    kept = emit_signals(shared_dir, outcomes, pod=pod, dry_run=dry_run)
    red = [o for o in outcomes if not o.ok]
    return {
        "producer": PRODUCER,
        "pod": pod,
        "bot_id": bot_id,
        "ok": not red,
        "transports": {o.transport: o.to_dict() for o in outcomes},
        "signals_firing": sorted(kept),
        "red_count": len(red),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Black-box probe for the evo keyword path (/api/evo/dispatch).",
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=CANONICAL_SHARED_DIR,
        help="Pod shared directory (default: platform-keyed canonical shared dir).",
    )
    parser.add_argument(
        "--network", default=None,
        help="Path to network.json (default: platform-keyed canonical config).",
    )
    parser.add_argument(
        "--bot", default=None,
        help="Bot to address dispatch at (default: primary, then first member).",
    )
    parser.add_argument("--admin-host", default=ADMIN_HOST)
    parser.add_argument("--admin-port", type=int, default=ADMIN_PORT)
    parser.add_argument(
        "--socket-path", default=None,
        help="admin-daemon.sock path (default: {shared_dir}/admin-daemon.sock).",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--gate", action="store_true",
        help="Gate-2 mode: exit non-zero when any transport is RED. The default "
        "(scheduled) run exits 0 even on RED so it does not also trip "
        "cron_exit_monitor — the Signal is the alert.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Probe and print; do not write Signals or sweep.",
    )
    args = parser.parse_args(argv)

    bot_id = args.bot
    if not bot_id:
        try:
            config = load_config(args.network)
            bot_id = _resolve_probe_bot(config, None)
        except Exception as exc:  # noqa: BLE001 — config unreadable
            summary = {
                "producer": PRODUCER, "skipped": True,
                "reason": f"could not read network.json: {exc}",
            }
            print(json.dumps(summary, indent=2), flush=True)
            return 0
    if not bot_id:
        summary = {
            "producer": PRODUCER, "skipped": True,
            "reason": "no primary/member bot resolved — nothing to probe",
        }
        print(json.dumps(summary, indent=2), flush=True)
        return 0

    summary = run_probe(
        args.shared_dir,
        bot_id=bot_id,
        host=args.admin_host,
        port=args.admin_port,
        socket_path=args.socket_path,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    # The summary is the LAST thing printed on a successful run, so its stdout
    # mtime is monitor_coverage's producer-liveness signal. Keep it last.
    print(json.dumps(summary, indent=2), flush=True)

    if args.gate and not summary["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

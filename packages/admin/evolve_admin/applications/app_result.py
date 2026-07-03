"""app_result — the structured execution-result contract for app scripts.

Single source of truth for the **execution-integrity floor** the forge bakes
into bots (spec
[docs/spec-app-invocation-just-works-2026-06-29.md](../../../../docs/spec-app-invocation-just-works-2026-06-29.md)
§2.2; the OQ-2 re-resolution
[docs/blocker-app-integrity-harness-oq2-2026-06-30.md](../../../../docs/blocker-app-integrity-harness-oq2-2026-06-30.md)).

Why this exists
---------------

OpenClaw exposes **no** synchronous tool-exec hook Evolve can wrap around an
``agent_invokes`` app script (the blocker doc proves this exhaustively), and
Evolve never forks OpenClaw — it is an external public-npm product we admin,
not own (memory: *evolve-is-admin-layer-on-uncontrolled-openclaw*). So the
strongest integrity Evolve can deliver from the layer it 100% controls — the
forge — is a **self-reporting wrapper**: a forge-written launcher that runs the
real app command, captures its **real** exit code + stderr (the proven capture
shape is ``oc_cli._run_oc_subprocess``), and emits a machine-parseable,
sentinel-delimited **result block** describing the true outcome. The bot
narrates from that block instead of inventing an outcome.

This module defines that block — the format, the canonical serializer
(:func:`format_result_block`, used by the launcher), the fail-closed parser
(:func:`parse_result_block`, used by downstream consumers, tests, and a future
OpenClaw hook), and the launcher source (:data:`WRAPPER_SOURCE`) the forge
writes into each bot.

Hook-upgradeable (load-bearing)
-------------------------------

The block format is **exactly** what a future OpenClaw ``before_tool_call`` /
``after_tool_call`` / ``PostToolUse`` hook could read and enforce *un-fakeably*
— see ``docs/contract-app-result-block-2026-06-30.md``. The launcher is the
floor we can ship today; the hook snaps on top later with no format change and
no rebuild. Until that hook exists, the floor is **not un-fakeable**: a bot
that bypasses the wrapper, or whose process is killed before the block is
emitted, can still misreport. The parser's fail-closed rule (absent / garbled
block ⇒ *failure*, never success) bounds that residual; it does not erase it.

Honesty residual is intentional and documented; this raises the bar a lot
(confabulating success now means contradicting a visible structured ``failed``
block) without claiming the un-fakeable contract only the hook can give.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── The contract ──────────────────────────────────────────────────────────────
#
# Versioned in the sentinel itself so a future reader can branch on it. Bump
# the trailing /N and add a parser branch rather than mutating /1 in place — a
# silently-changed shape is the exact drift this contract exists to prevent
# (same discipline as triggerProtocols' code-registered stdout_protocol).

SCHEMA_VERSION = "evolve.app_result/1"

# Sentinels are matched as WHOLE LINES by the parser. The launcher JSON-escapes
# the child's captured stdout/stderr into a single-line JSON value, so a child
# that prints these very strings cannot forge a second block: its bytes land
# inside a quoted string, never as a bare sentinel line. (Injection defense.)
SENTINEL_OPEN = "<<<<<EVOLVE_APP_RESULT/1"
SENTINEL_CLOSE = "EVOLVE_APP_RESULT/1>>>>>"

# Per-stream cap kept inside the block (bytes of the decoded text). Generous
# enough for a real failure message; bounded so a runaway script can't bloat
# the channel. Mirrored verbatim in WRAPPER_SOURCE (pinned by a test).
STREAM_CAP = 20_000

# Stable status vocabulary. "ok" iff the wrapped command exited 0 and did not
# time out; "failed" otherwise (non-zero exit, timeout, spawn error, or — at
# the parser — an absent / garbled / contradictory block).
STATUS_OK = "ok"
STATUS_FAILED = "failed"

# Where the forge writes the launcher in each bot, relative to the workspace
# root. Under ``evolve/`` because that subtree carries the evolve-user write
# ACL (workspace/evolve/...) — the forge (evolve user) can write it directly,
# with no bot-owned-file write and so no dependency on the privileged-write
# helper. App-agnostic: one launcher per bot wraps any app command.
WRAPPER_WORKSPACE_RELPATH = "evolve/app_result/run.py"


@dataclass
class AppResult:
    """A parsed (or fail-closed) app execution outcome.

    ``is_success`` is the ONE question callers should ask: it is True only for a
    well-formed ``ok`` block whose exit code agrees. Every degenerate case —
    block absent, malformed, ambiguous, or self-contradictory (``ok`` with a
    non-zero exit) — yields ``is_success == False`` with a ``reason``. Fail
    closed: the bot never treats "I couldn't tell" as success.
    """

    status: str                       # STATUS_OK | STATUS_FAILED
    exit_code: int | None = None      # the wrapped command's real exit code
    timed_out: bool = False
    error_summary: str | None = None  # plain-language failure line (None on ok)
    stdout: str = ""                  # captured child stdout (decoded, capped)
    stderr: str = ""                  # captured child stderr (decoded, capped)
    duration_ms: int | None = None
    command: list[str] | None = None  # the wrapped argv (for hook/audit mapping)
    reason: str = ""                  # why a block was rejected; "" when clean
    raw: dict[str, Any] | None = None  # the decoded JSON, when one was present

    @property
    def is_success(self) -> bool:
        return self.status == STATUS_OK and not self.reason


def format_result_block(payload: dict[str, Any]) -> str:
    """Serialize *payload* into the canonical sentinel-delimited block.

    The launcher emits byte-identical output (its ``_emit`` mirrors this); a
    round-trip test pins the equivalence. Single-line JSON (no embedded literal
    newlines — they are escaped) is what makes the whole-line sentinel match
    unambiguous.
    """
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return f"\n{SENTINEL_OPEN}\n{body}\n{SENTINEL_CLOSE}\n"


# Whole-line sentinel match. ``[ \t]*`` tolerates trailing spaces a pipe might
# introduce; ``\r?`` tolerates CRLF. DOTALL so the JSON capture spans (a
# malformed multi-line body is still captured, then rejected at json.loads).
_BLOCK_RE = re.compile(
    r"^" + re.escape(SENTINEL_OPEN) + r"[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"\r?\n" + re.escape(SENTINEL_CLOSE) + r"[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def parse_result_block(captured: str | bytes) -> AppResult:
    """Parse the launcher's captured stdout into an :class:`AppResult`.

    **Fail-closed** is the whole point: any ambiguity resolves to failure, so a
    bot that derives its reply from this can never be tricked into reporting
    success when the truth is unknown. Specifically:

      * **No block** → failed (``reason="absent"``). A script that crashed
        before emitting, was killed, or never ran the wrapper looks like this.
      * **>1 block** → failed (``reason="ambiguous"``). The honest launcher
        emits exactly one; multiple means tampering or a bug — refuse to guess.
      * **Unparseable JSON / missing or bad ``status``** → failed
        (``reason="malformed"``).
      * **``status: ok`` but a non-zero exit code** → failed
        (``reason="contradictory"``). Catches a forged/garbled "success" that
        disagrees with its own exit code.
      * **Well-formed ``ok`` with exit 0** → success.

    Tolerates non-UTF8 bytes (decoded with replacement) and arbitrary leading /
    trailing noise around the block.

    **Producer-trust boundary (read before reusing this elsewhere).** This
    parser is safe on output from a *trusted producer* — one that is the sole
    emitter of the sentinel *delimiter lines* and that encapsulates the wrapped
    command's own output (so the command's bytes can never become delimiter
    lines). The launcher is exactly that: it captures child stdout out-of-band
    and JSON-escapes it, so a child that prints the sentinel lands inside a
    quoted string, never as a structural delimiter. A future OpenClaw
    ``after_tool_call`` hook is also that — it captures the tool's stdout
    out-of-band and emits the block itself.

    What this parser deliberately does NOT do is *authenticate* the delimiter
    lines: if some future consumer scraped arbitrary attacker-controlled channel
    text and handed it here, an attacker who supplies the outer ``OPEN``/``CLOSE``
    lines AND a same-line ``"status":"ok"`` JSON would be believed. That is a
    producer-authenticity property, not something content inspection can
    recover (a legitimate block's ``stdout`` field may itself contain the
    sentinel token). Consumers must only parse blocks they captured from the
    wrapper / tool boundary — never raw channel text. The hook upgrade path
    preserves this (it captures out-of-band), so the un-fakeable contract is
    unaffected.
    """
    text = _decode(captured)
    matches = _BLOCK_RE.findall(text)

    if not matches:
        return AppResult(
            status=STATUS_FAILED,
            reason="absent",
            error_summary=(
                "The app produced no result block, so its outcome is unknown — "
                "treating it as a failure."
            ),
        )
    if len(matches) > 1:
        return AppResult(
            status=STATUS_FAILED,
            reason="ambiguous",
            error_summary=(
                "The app produced more than one result block; refusing to guess "
                "which is real — treating it as a failure."
            ),
        )

    body = matches[0]
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return AppResult(
            status=STATUS_FAILED,
            reason="malformed",
            error_summary=(
                "The app's result block was not valid JSON — treating it as a "
                "failure."
            ),
        )
    if not isinstance(data, dict):
        return AppResult(
            status=STATUS_FAILED,
            reason="malformed",
            error_summary="The app's result block was not an object.",
        )

    status = data.get("status")
    if status not in (STATUS_OK, STATUS_FAILED):
        return AppResult(
            status=STATUS_FAILED,
            reason="malformed",
            error_summary="The app's result block had no valid status.",
            raw=data,
        )

    raw_exit = data.get("exit_code")
    exit_code = raw_exit if isinstance(raw_exit, int) else None
    timed_out = bool(data.get("timed_out"))
    error_summary = data.get("error_summary")
    error_summary = error_summary if isinstance(error_summary, str) else None
    cmd = data.get("command")
    command = [str(c) for c in cmd] if isinstance(cmd, list) else None
    duration = data.get("duration_ms")
    duration_ms = duration if isinstance(duration, int) else None

    # Contradiction guard: a truthful "ok" never carries a non-zero exit or a
    # timeout. Disagreement ⇒ fail closed rather than trust the self-label.
    if status == STATUS_OK and ((exit_code is not None and exit_code != 0) or timed_out):
        return AppResult(
            status=STATUS_FAILED,
            exit_code=exit_code,
            timed_out=timed_out,
            reason="contradictory",
            error_summary=(
                "The app reported success but its exit code says otherwise — "
                "treating it as a failure."
            ),
            stdout=_decode(data.get("stdout")),
            stderr=_decode(data.get("stderr")),
            duration_ms=duration_ms,
            command=command,
            raw=data,
        )

    return AppResult(
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        error_summary=error_summary if status == STATUS_FAILED else None,
        stdout=_decode(data.get("stdout")),
        stderr=_decode(data.get("stderr")),
        duration_ms=duration_ms,
        command=command,
        reason="",
        raw=data,
    )


# ── bot_guidance — the in-context honesty instruction ─────────────────────────

# The section TITLE is stable so the forge can replace-in-place idempotently
# across re-forges (no duplicate sections).
BOT_GUIDANCE_SECTION = "Reporting app outcomes truthfully"


def result_harness_bot_guidance(
    wrapper_relpath: str = WRAPPER_WORKSPACE_RELPATH,
) -> dict[str, str]:
    """Return the ``{section, content}`` bot_guidance block for the harness.

    This is Evolve's integrity lever *without* a hook: better **context**, not
    removed agency (principle-just-works). It teaches the bot to run app
    commands through the wrapper and to derive its reply from the structured
    result — and, critically, to fail closed in prose (a missing/unreadable
    block is a failure, never an assumed success), mirroring the parser.

    Phrasing deliberately avoids the freelance-validator's at-risk prose
    markers and never names a ``scripts/<x>.py`` path, so adding it does not
    itself flag the app as at-risk.
    """
    content = (
        "This bot's apps are run through Evolve's result wrapper at "
        f"`{wrapper_relpath}`. The wrapper runs the real app command, captures "
        "its true exit status, and prints one machine-readable result block:\n\n"
        f"    {SENTINEL_OPEN}\n"
        '    {"status": "ok|failed", "exit_code": N, "error_summary": "...", ...}\n'
        f"    {SENTINEL_CLOSE}\n\n"
        "When you run an app command, run it through the wrapper:\n\n"
        f"    python3 {wrapper_relpath} -- <the real command and its arguments>\n\n"
        "Then read the result block before you reply, and report from it:\n"
        "  - `status: ok` — the command succeeded. Report from its output.\n"
        "  - `status: failed` — the command did NOT succeed. Tell the user "
        "plainly that it didn't work, using `error_summary`. Never claim "
        "success and never invent a result.\n"
        "  - block missing or unreadable — treat it as a failure too. Never "
        "assume success when you cannot see a clear `ok` outcome.\n\n"
        "This is how the bot stays honest about what actually happened."
    )
    return {"section": BOT_GUIDANCE_SECTION, "content": content}


# ── The launcher the forge writes into each bot ───────────────────────────────
#
# Standalone: imports only the Python stdlib, so it runs as the bot user in the
# bot's workspace with no access to the evolve_admin package. It is the floor's
# entire trust surface, so it is a FIXED, tested artifact (not LLM-generated):
# the forge writes it verbatim. A test execs this very source against fixture
# children (success / non-zero / crash / timeout / sentinel-injection /
# non-UTF8) and asserts parse_result_block reads each truthfully.
#
# The sentinel / schema / cap literals below MUST match the constants above; a
# test asserts the source contains them (drift guard).

WRAPPER_SOURCE = '''#!/usr/bin/env python3
"""Evolve app-result wrapper — GENERATED, DO NOT EDIT.

Execution-integrity floor for app scripts. Runs a real app command, captures
its true exit code + stdout/stderr, and emits ONE sentinel-delimited result
block describing the outcome, so the assistant narrates from the truth instead
of inventing it.

Contract + rationale: docs/contract-app-result-block-2026-06-30.md
Spec: docs/spec-app-invocation-just-works-2026-06-29.md (§2.2).

Usage:
    python3 run.py [--timeout SECONDS] [--cwd DIR] -- <command> [args...]

This wrapper ALWAYS exits 0 once it has emitted a block: the truth lives in the
block, not in this process's exit status. That deliberately replaces the raw
"(agent) failed" exec chip (which leaks a traceback into chat and which the
post-hoc app_script_failure_audit counts) with a clean structured failure.
"""

import json
import os
import signal
import subprocess
import sys
import time

SENTINEL_OPEN = "<<<<<EVOLVE_APP_RESULT/1"
SENTINEL_CLOSE = "EVOLVE_APP_RESULT/1>>>>>"
SCHEMA_VERSION = "evolve.app_result/1"
STREAM_CAP = 20000
DEFAULT_TIMEOUT = 600.0


def _decode(raw):
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _cap(text):
    # Returns (capped_text, was_truncated). Caps on characters after decode;
    # close enough to the byte budget for a diagnostic and avoids splitting a
    # multibyte sequence.
    if len(text) > STREAM_CAP:
        return text[:STREAM_CAP], True
    return text, False


def _emit(payload):
    # One JSON line between whole-line sentinels. The child's captured streams
    # are JSON string values here, so any sentinel-looking bytes the child
    # printed are escaped inside a quoted string and cannot forge a second
    # block. This wrapper writes NOTHING else to stdout (the child's raw stdout
    # is captured, never passed through), so exactly one block exists.
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write("\\n" + SENTINEL_OPEN + "\\n")
    sys.stdout.write(body)
    sys.stdout.write("\\n" + SENTINEL_CLOSE + "\\n")
    sys.stdout.flush()


def _first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:400]
    return ""


def _parse_args(argv):
    timeout = DEFAULT_TIMEOUT
    cwd = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
            i += 2
            continue
        if a == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
            continue
        # First non-flag token starts the command.
        break
    return timeout, cwd, argv[i:]


def main(argv):
    timeout, cwd, cmd = _parse_args(argv)

    if not cmd:
        _emit({
            "schema": SCHEMA_VERSION,
            "status": "failed",
            "exit_code": None,
            "timed_out": False,
            "error_summary": "No command was given to the result wrapper.",
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_ms": 0,
            "command": [],
        })
        return 0

    start = time.time()
    timed_out = False
    out_b = b""
    err_b = b""

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as exc:
        _emit({
            "schema": SCHEMA_VERSION,
            "status": "failed",
            "exit_code": None,
            "timed_out": False,
            "error_summary": "The app could not be started: " + str(exc)[:300],
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_ms": int((time.time() - start) * 1000),
            "command": [str(c) for c in cmd],
        })
        return 0

    try:
        out_b, err_b = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the whole process group (the child may have spawned its own
        # children); fall back to killing just the child.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except Exception:
            out_b, err_b = b"", b""
        rc = proc.returncode

    duration_ms = int((time.time() - start) * 1000)
    out_text, out_trunc = _cap(_decode(out_b))
    err_text, err_trunc = _cap(_decode(err_b))

    if timed_out:
        status = "failed"
        summary = "The app timed out after %ds and was stopped." % int(timeout)
    elif rc == 0:
        status = "ok"
        summary = None
    else:
        status = "failed"
        summary = "The app exited with an error (status %s)." % rc
        detail = _first_line(err_text) or _first_line(out_text)
        if detail:
            summary = summary + " Details: " + detail

    _emit({
        "schema": SCHEMA_VERSION,
        "status": status,
        "exit_code": rc,
        "timed_out": timed_out,
        "error_summary": summary,
        "stdout": out_text,
        "stderr": err_text,
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
        "duration_ms": duration_ms,
        "command": [str(c) for c in cmd],
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as _exc:  # never leave the caller without a block
        try:
            _emit({
                "schema": SCHEMA_VERSION,
                "status": "failed",
                "exit_code": None,
                "timed_out": False,
                "error_summary": "The result wrapper failed: " + str(_exc)[:300],
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "duration_ms": None,
                "command": [],
            })
        finally:
            sys.exit(0)
'''


def write_wrapper(workspace_root: Path) -> Path:
    """Write the launcher into *workspace_root* at :data:`WRAPPER_WORKSPACE_RELPATH`.

    Idempotent and content-addressed: rewrites only when the on-disk source
    differs from :data:`WRAPPER_SOURCE` (so re-forges don't needlessly churn
    the file, and an out-of-date wrapper from an earlier forge is healed). The
    destination is under ``workspace/evolve/`` which carries the evolve-user
    write ACL, so this is a plain ``write_text`` — no sudo / bot-owned-file
    write, hence no dependency on the privileged-write helper.

    Returns the wrapper path. Raises on a genuine I/O / permission failure so
    the caller can log it; the caller treats baking as best-effort.
    """
    dest = workspace_root / WRAPPER_WORKSPACE_RELPATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing: str | None = None
    if dest.exists():
        try:
            existing = dest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = None
    if existing == WRAPPER_SOURCE:
        return dest
    dest.write_text(WRAPPER_SOURCE, encoding="utf-8")
    return dest


def merge_bot_guidance(existing: Any) -> list:
    """Return *existing* bot_guidance with the harness section present once.

    Replaces an existing section with the same title in place (idempotent
    across re-forges) rather than appending a duplicate; preserves every other
    section and their order. Tolerant of a None / non-list input (treated as
    empty) and non-dict entries (passed through untouched).
    """
    block = result_harness_bot_guidance()
    out: list = []
    replaced = False
    if isinstance(existing, list):
        for section in existing:
            if (
                isinstance(section, dict)
                and section.get("section") == BOT_GUIDANCE_SECTION
            ):
                out.append(block)
                replaced = True
            else:
                out.append(section)
    if not replaced:
        out.append(block)
    return out

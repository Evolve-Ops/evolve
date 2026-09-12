"""Atlas — one-shot LLM dispatch via the bot's local OpenClaw agent.

Implements the ``openclaw_headless`` transport from
``docs/principle-apps-inherit-bot-llm.md``. All Atlas LLM work (classifier,
recap pattern detection, research scope/strategy checks, research synthesis)
routes through this module instead of calling provider APIs directly.

The bot's configured provider, model, tier-walk fallback, ``daily_cap_usd``
auto-trip, ``cost_watchdog``, and prompt caching govern the call. No API key
lives in workspace files; no ``api.anthropic.com`` URL appears in Atlas code.

Reference implementation pattern:
    packages/analyzer/app_audit_tier3.py::_dispatch_via_oc_full
which is the same shape used by the Evolve heartbeat / tier-3 audit dispatch.
Both the process-group kill (so leaked openclaw-agent workers die with the
parent on timeout) and the stderr-DEVNULL behaviour (so OC's ~1MB of plugin
chatter per call doesn't pipe-deadlock) are inherited from there. The
cost-recovery-via-TurnObserver dance is NOT replicated — atlas's spend rolls
up via the bot's own cost system already.
"""
from __future__ import annotations

import json
import os
import shutil
import signal as _signal
import subprocess
import sys

# Same OC binary lookup as setup_wizard / deploy / oc_cli. The forge installs
# openclaw via Homebrew, so the bin lives under one of these paths.
_OC_CANDIDATES = (
    "/opt/homebrew/bin/openclaw",
    "/usr/local/bin/openclaw",
    "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw",
    "/usr/local/lib/node_modules/openclaw/bin/openclaw",
    "/usr/lib/node_modules/openclaw/bin/openclaw",
)

DEFAULT_TIMEOUT_S = 30
_OC_BIN: str | None = None

# Tag prepended to every dispatched prompt so Layer C (agent-freelance-bypass
# Phase 2 plugin interceptor) can exclude script-internal LLM calls from
# trigger-matching. Without this, atlas's broad ``dm_research`` trigger
# (``pattern: ".*"`` for any DM-shaped message) matches every classifier
# prompt and runs ``atlas_research.py`` as a side-effect on each call — costs
# a wasted script invocation and pollutes stderr with trigger-fire chatter.
# The manifest's ``event_triggers[].match.exclude_pattern`` MUST include this
# marker as one of its alternations, otherwise the trigger fires anyway and
# the dispatch still works but is wasteful.
_INTERNAL_PROMPT_MARKER = "[atlas:internal]\n"

# Dedicated session key for headless dispatches. Must NOT be the default
# ``agent:main:main`` — that session is shared with the bot's user-facing
# conversation and accumulates app-audit prompts, heartbeat polls, and other
# inbound traffic across all dispatches. On atlas, the default session bloated
# to 62 messages including a 63KB app-audit blob, and post-agent plugin
# processing then hung forever on every new dispatch (observed 2026-06-07:
# 60s wrapper timeout vs ~13s with a fresh key). Using a dedicated key keeps
# headless prompts on their own session and lets prompt caching still hit
# across successive calls.
_INTERNAL_SESSION_KEY = "agent:main:atlas-internal"


def _log(msg: str) -> None:
    print(f"[atlas:oc_dispatch] {msg}", file=sys.stderr)


def _resolve_openclaw_bin() -> str | None:
    global _OC_BIN
    if _OC_BIN is not None:
        return _OC_BIN
    found = shutil.which("openclaw")
    if not found:
        for cand in _OC_CANDIDATES:
            if shutil.os.path.exists(cand):
                found = cand
                break
    _OC_BIN = found
    return _OC_BIN


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole session, escalate to SIGKILL if it doesn't die.

    Modeled on packages/analyzer/app_audit_tier3.py::_kill_process_group.
    On POSIX, ``start_new_session=True`` made proc.pid the session leader,
    which on macOS/Linux means proc.pid is also the process-group ID. So
    ``killpg(pid, signal)`` hits the whole tree (openclaw → openclaw-agent
    workers → plugin processes). SIGTERM first to give in-flight network
    I/O a chance to flush, then SIGKILL after a short grace period if the
    process is still alive. Silent on ProcessLookupError — the group may
    have already drained between the timeout fire and our kill.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _extract_text_and_tokens(payload: dict) -> tuple[str, int]:
    """Pull assistant text + token count from ``openclaw agent --json`` output.

    Two known shapes (see app_audit_tier3._extract_text_and_tokens for the
    canonical version with version notes):
      - 2026.5.22+:  {"payloads":[{"text":"..."}], "meta":{"agentMeta":{"usage":{"input":N,"output":N}}}}
      - pre-2026.5: {"text":"...", "usage":{"input_tokens":N, "output_tokens":N}}
    Both are handled so a mid-upgrade pod doesn't silently return empty text.
    """
    text = ""
    payloads = payload.get("payloads")
    if isinstance(payloads, list) and payloads:
        first = payloads[0] if isinstance(payloads[0], dict) else {}
        text = (first.get("text") or "").strip()
    if not text:
        text = (payload.get("text") or payload.get("message") or "").strip()

    tokens = 0
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    usage_new = agent_meta.get("usage") if isinstance(agent_meta.get("usage"), dict) else {}
    if usage_new:
        tokens = int(usage_new.get("input", 0)) + int(usage_new.get("output", 0))
    if not tokens:
        usage_old = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = int(usage_old.get("input_tokens", 0)) + int(usage_old.get("output_tokens", 0))
    return text, tokens


def dispatch(prompt: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> tuple[str, dict]:
    """Send a one-shot prompt to the bot's local OpenClaw agent.

    Returns ``(text, telemetry)``.

    ``text`` is the assistant's reply (stripped). Empty string on failure.

    ``telemetry`` is a dict with keys:
      - ``tokens`` (int): input + output tokens, 0 if unknown.
      - ``cost_usd`` (float): estimated cost using Haiku rates ($1/MTok in,
        $5/MTok out — a conservative pessimistic estimate; real cost lives on
        the bot's daily_total_usd which is what ``daily_cap_usd`` governs).
      - ``error`` (str): empty on success; a short reason string on failure.

    cwd="/tmp" because openclaw calls libuv's uv_cwd() at startup; if the
    inherited CWD is unreadable the binary exits with EACCES before parsing
    argv. Cron launches Atlas with cwd=/Users/<bot>/.openclaw/workspace which
    the bot can read, so this is belt-and-suspenders — but cheap belt.

    No --system flag: openclaw agent has no --system option (verified against
    2026.4.29/2026.5.12). Fold any framing into the message body.
    """
    binary = _resolve_openclaw_bin()
    if not binary:
        return "", {"tokens": 0, "cost_usd": 0.0, "error": "openclaw binary not found"}

    # Prefix every prompt with the Layer-C-exclusion marker. See
    # _INTERNAL_PROMPT_MARKER docstring for the rationale.
    # --session-key isolates headless dispatch traffic from the bot's main
    # user-facing session; see _INTERNAL_SESSION_KEY docstring.
    cmd = [
        binary, "agent",
        "--local", "--agent", "main",
        "--session-key", _INTERNAL_SESSION_KEY,
        "--json",
        "--timeout", str(timeout_s),
        "--message", _INTERNAL_PROMPT_MARKER + prompt,
    ]
    # stderr=DEVNULL prevents a pipe-buffer deadlock: OC emits ~1MB of
    # diagnostic stderr per dispatch (plugin chatter, Layer C trigger
    # decisions, session-summary output, compaction-safeguard notes). With
    # `capture_output=True`, the OS pipe (default ~64KB on macOS) fills and
    # the child blocks writing stderr forever — observed as a 60s timeout
    # on a call whose actual LLM work completes in under 3s. Atlas doesn't
    # consume OC's stderr; routing it to DEVNULL is the clean fix.
    #
    # start_new_session=True + killpg on timeout puts the child in its own
    # process group so we can take down the whole tree (openclaw → openclaw-
    # agent workers → plugin processes) when the wrapper times out. Without
    # this, subprocess.run(timeout=N) only SIGKILLs the immediate child; the
    # agent worker keeps running and eventually finishes its (already billed)
    # LLM call before idling forever. Same zombie-accumulation pattern
    # observed in app_audit_tier3 forensics (2026-05-20).
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True, cwd="/tmp",
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout_s + 30)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                stdout, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout = ""
            return "", {"tokens": 0, "cost_usd": 0.0,
                        "error": f"dispatch timeout after {timeout_s}s; killed process group"}
    except OSError as exc:
        if proc is not None:
            _kill_process_group(proc)
        return "", {"tokens": 0, "cost_usd": 0.0, "error": f"dispatch OSError: {exc}"}

    if proc.returncode != 0:
        _log(f"openclaw exit={proc.returncode} (stderr suppressed for pipe-deadlock safety)")
        return "", {"tokens": 0, "cost_usd": 0.0,
                    "error": f"openclaw exit={proc.returncode}"}

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Some openclaw modes print plain text — fall back to raw stdout.
        return stdout.strip(), {"tokens": 0, "cost_usd": 0.0, "error": ""}

    text, tokens = _extract_text_and_tokens(payload)
    # Pessimistic Haiku estimate (input cheap, output expensive). True cost is
    # tracked by the bot's daily_total_usd via OC's TurnObserver — this
    # estimate is only for per-app logs (budget tracking, audit roll-up).
    # We don't have an input/output split from the merged token count, so
    # treat all tokens as output for a conservative upper bound.
    cost_usd = tokens * 5.0 / 1_000_000
    return text, {"tokens": tokens, "cost_usd": cost_usd, "error": ""}


def parse_json_reply(text: str) -> dict | None:
    """Parse a JSON-only reply from the agent. Returns None on failure.

    Helper for the common pattern: prompt instructs "Return JSON ONLY", and
    the caller wants a dict back. Atlas's classifier, recap pattern detector,
    and research scope/strategy checks all share this shape.
    """
    if not text:
        return None
    s = text.strip()
    # The agent sometimes wraps JSON in markdown fences despite instructions.
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:].strip()
        # strip trailing fence on a separate line
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None

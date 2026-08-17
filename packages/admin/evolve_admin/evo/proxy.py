"""evo proxy — route admin-UI chat through evo's OC gateway.

Phase 4.1 of the evo OC-native architecture
(docs/spec-evo-oc-native-2026-05-19.md §3). Replaces the legacy
hand-rolled Haiku-fallback path in ``/api/home/chat`` with a thin
proxy to evo's actual OC agent. The admin UI now talks to the SAME
agent that handles Telegram + has the SAME SOUL/MEMORY/TOOLS context
+ can invoke the SAME 12 tools shipped in #1273 / #1279 / #1282.

Wire-up:

  admin UI chat drawer
      ─POST─> /api/home/chat                      (handler — kept compat shape)
              └─ build page-context summary
              └─ send_to_evo(...)
                  └─ subprocess: `openclaw agent --json
                       --agent main --message "..." --session-id ...`
                  └─ OC gateway: dispatch → model turn → tool calls
                  └─ parses JSON result; returns the agent's text reply

Why subprocess instead of direct WS-RPC: OC's gateway speaks JSON-RPC
over WS. Building a WS client in Python is heavier than this proxy
needs, and the openclaw CLI already handles connection lifecycle,
auth, session routing, and JSON envelope parsing. We'll graduate to
WS when we need streaming (Phase 4.2) — that's the right trigger.

Page context (the per-page summary protocol — case for case how this
fixes the three failure modes the operator surfaced 2026-05-19):

  * Each admin UI page contributes a tiny summary blob in the JSON
    body: ``{page_id, view, summary, elided_counts, tool_pointers}``.
  * The proxy wraps that summary as a ``<page-context>`` XML block at
    the top of the user message. evo's AGENTS.md teaches the model to
    read the block as ground truth about what the operator sees.
  * Summary is intentionally short (a few hundred tokens) — the full
    page data lives in tools. The summary names the available tool +
    elision counts so the model knows when to fetch.
  * "Don't say no without checking" — when the operator asks about
    something specific that isn't in the summary, the model is
    instructed to call the relevant tool before claiming the data
    doesn't exist.

This is the "summary-with-fetch-on-demand" approach the operator
approved over always-on full context: prompt-cache friendlier,
token-efficient, and (with the AGENTS.md rule) not brittle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Default agent. OC's primary bot defines `agents.defaults` with the
# session model + bootstrap files; --agent main resolves to that. If a
# pod ever has multiple agents on the same bot, this argument turns
# into a parameter, but v1 pods are 1:1 (bot↔agent).
_DEFAULT_AGENT = "main"

# Subprocess deadline. Most agent turns finish in 5–30 s (model latency
# + tool calls). Long multi-tool turns can run a few minutes — operators
# reported the prior 90 s cap firing on real workloads. The
# client-side ceiling is 5 min; we land ~30 s below it so the server
# returns its own "didn't respond" text just before the client aborts,
# giving the operator a readable error bubble instead of a raw fetch
# abort. The CLI exits 0 + JSON on success; any non-zero exit surfaces
# as a proxy error.
_SUBPROCESS_TIMEOUT_S = 270

# Page-context summary cap. Summaries are intentionally small — full
# data lives in tools the model can call. This cap protects the proxy
# from a page accidentally posting megabytes of state in the
# ``state`` field of page_context. Truncation surfaces as a notice in
# the page-context block so the model knows.
_SUMMARY_MAX_CHARS = 4000

# Recent-action ring size. Closes "what did you just do?", "operator
# said 'the first one' — which was first?", and reference-resolution
# gaps generally. Read from OC's session jsonl rather than a separate
# server-side store — the jsonl is the system of record for tool
# calls. 5 entries × ~80 chars each ≈ 400 chars of block budget.
_RECENT_ACTIONS_RING = 5

# Default location for OC's session storage. Pods can override via
# env var (matches the existing OC_SESSIONS_DIR convention used by
# evo's gateway plist when the deploy is non-standard).
_OC_SESSIONS_DIR_DEFAULT = "/Users/evolve/.openclaw/agents/main/sessions"


@dataclass(frozen=True)
class ProxyResult:
    """Shape returned by ``send_to_evo``.

    ``text`` is the agent's prose reply (what to show in the chat
    bubble). ``session_id`` is whatever OC ended up using — usually
    what we sent in, but if OC rerouted, we surface its choice so the
    client can use the same id on the next turn for context
    continuity. ``model`` is the model OC actually invoked (handy for
    cost-display + debugging). ``usage`` is the token+cost dict from
    OC's agent meta (may be empty when OC didn't report usage).

    ``error`` is set when the subprocess failed; ``text`` carries an
    operator-facing message even in error cases so the chat doesn't
    show a blank bubble.

    ``inspector_event`` (Phase 4 of the surface-aware help-style spec —
    docs/spec-surface-aware-help-style-2026-05-22.md §7) is set when
    the outgoing-text inspector substituted or modified the text. The
    proxy's caller may surface a yellow-banner hint to the operator
    using this. ``None`` means the inspector took no action — the text
    passed through unchanged.
    """

    text: str
    session_id: str
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    run_id: str | None = None
    inspector_event: Any = None   # InspectorEvent | None — typed lazily to avoid cycle


# ── openclaw binary discovery ────────────────────────────────────────────────

_OPENCLAW_BIN: str | None = None


def _openclaw_bin() -> str:
    """Resolve the absolute openclaw binary path. Cached after first
    lookup. Matches deploy.py's ``_openclaw_bin`` so subprocess calls
    use the same path the sudoers rules grant."""
    global _OPENCLAW_BIN
    if _OPENCLAW_BIN is None:
        found = shutil.which("openclaw")
        if found is None:
            for candidate in (
                "/opt/homebrew/bin/openclaw",
                "/usr/local/bin/openclaw",
            ):
                if Path(candidate).exists():
                    found = candidate
                    break
        _OPENCLAW_BIN = found or "openclaw"
    return _OPENCLAW_BIN


# ── Session id derivation ────────────────────────────────────────────────────


# Sanitize ``request_id`` to letters/digits/underscore/hyphen ONLY.
# Note we exclude '.' even though the body-side session_id pattern
# permits it — the fallback path is best kept dot-free so traversal
# sequences like ``..`` can't ever appear in the derived session_id
# even if upstream validation drifts.
_ANON_FALLBACK_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def derive_session_id(
    page_id: str | None,
    *,
    prefix: str = "admin-ui",
    request_id: str | None = None,
) -> str:
    """Derive a stable session_id for an admin-UI chat thread.

    Each admin UI page gets its own thread so the operator can keep
    distinct conversations about Alerts vs Cost vs Security vs the
    Dashboard. The id is deterministic for a given (prefix, page_id)
    so reloading the page resumes the same OC session — OC's session
    store keys off session_id, so we get bootstrap-file injection +
    short-term memory across reloads "for free."

    Anonymous fallback (#1367 follow-up): when ``page_id`` is missing,
    a ``request_id`` (typically a per-browser cookie value the route
    handler reads from the request) keys a *stable* anon thread —
    ``admin-ui-anon-<hash(request_id)>`` — so consecutive turns from
    the same browser land on the SAME OC session. Without
    ``request_id`` we fall back to a uuid4 per call; that's the legacy
    behavior, kept for callers that genuinely want isolation per
    invocation (eg a CLI repl spawning short-lived threads).

    The previous unconditional uuid4 behavior fragmented one operator-
    perceived chat into many OC sessions whenever the client omitted
    ``session_id`` (eg the legacy Chat-page send path for session
    records pre-dating ``oc_session_id``). The Chat-page client now
    always sends a stable ``session_id`` (Part 1 of the #1367 fix);
    this parameter is defense-in-depth for other surfaces and for
    transient client-side states where the body field is missing.

    Per-page session lifecycle (rotate-on-clear, MEMORY sharing across
    sessions) lands in Phase 4.3 — for v1 the id is stable per page
    and per pod and never auto-rotates.
    """
    if page_id:
        # Replace characters that confuse session file paths
        safe = page_id.replace("/", "-").replace(" ", "-").lower()
        return f"{prefix}-{safe}"
    if request_id:
        # Stable per-browser anon thread. Sanitize ``request_id`` to
        # the same charset the route's _SAFE_SESSION_ID_RE allows so
        # an upstream cookie value never escapes the session-id
        # filesystem-safe charset. Strip any leading prefix collision.
        safe_req = _ANON_FALLBACK_SAFE_RE.sub("-", request_id)[:48].strip("-")
        if safe_req:
            log.info(
                "derive_session_id: stable anon fallback (request_id) — "
                "request_id=%r → %s-anon-%s",
                request_id, prefix, safe_req,
            )
            return f"{prefix}-anon-{safe_req}"
    log.warning(
        "derive_session_id: minting per-request uuid anon id — "
        "no page_id, no request_id. Each turn from this caller will "
        "start a FRESH OC session (no short-term memory across turns). "
        "If this fires repeatedly for one operator, investigate why "
        "the client isn't sending session_id or a stable browser id."
    )
    return f"{prefix}-anon-{uuid.uuid4().hex[:8]}"


# ── Session-context formatting ───────────────────────────────────────────────


def format_session_context(session_context: dict[str, Any] | None) -> str:
    """Render the per-session framing block.

    Spec §3.7 lever #1. Closes:

      * identity gap — operator_id is named, not implicit
      * self-identity gap — the bot is told which bot it IS (and whether
        it is the admin bot) so it stops confabulating member-bot
        authorization walls on cross-bot questions
      * authority gap — model sees the operator's authority tier in-context
        (the tier was already passed in the body but never reached the model)
      * temporal staleness — local_time anchors "this morning", "an hour ago"
      * reference resolution — recent_actions ring resolves "the first one"
        / "what we just discussed" / "did I already snooze that?"
      * audit recall — "what did you just do?" answerable without operator's
        page

    Shape:

        {
          "bot_id": "evolve",                    (optional; the bot's own id —
                                                  renders a "Bot: …" line when set)
          "is_admin_bot": True,                  (optional; when truthy the Bot line
                                                  adds the admin/most-privileged framing)
          "operator_id": "pod_admin",            (string, defaults to "pod_admin")
          "authority": "ask" | "auto-small" | "auto",
          "local_time": "2026-05-19T14:32:00-07:00",   (ISO; frontend's clock)
          "session_age_seconds": int,            (optional; renders as humanized)
          "user_turn_count": int,                (optional; prior user turns on this thread;
                                                  when >= 1 the renderer emits a
                                                  "this is turn N of an ongoing thread"
                                                  line — see #1402 follow-up)
          "recent_actions": [                    (optional; list of dicts)
            {"tool": "pod_state(bots)", "outcome": "ok", "when": "1m ago",
             "summary": "7 bots, evolve has scan_needed chip"},
            ...
          ]
        }

    Returns the empty string when ``session_context`` is None or has no
    usable content — the caller drops the block entirely rather than
    sending an empty wrapper.
    """
    if not isinstance(session_context, dict):
        return ""

    operator_id = (session_context.get("operator_id") or "").strip() or "pod_admin"
    authority = (session_context.get("authority") or "").strip().lower()
    if authority not in {"ask", "auto-small", "auto"}:
        authority = "ask"
    # Tier preference — operator's per-turn model pick from the admin-UI
    # composer (Auto / Fast / Standard / Power). Spec:
    # docs/spec-user-tier-control-2026-05-26.md §"session-context block
    # (informational only)". Only render when non-default. Auto is the
    # implicit baseline; surfacing it on every turn would churn the
    # prompt cache for nothing. Routing is enforced by the plugin's
    # before_model_resolve hook — this line is purely so the model can
    # acknowledge the operator's intent in its reply ("Using Power for
    # this — let me think harder.") when relevant.
    tier_preference = (session_context.get("tier_preference") or "").strip().lower()
    if tier_preference not in {"fast", "standard", "power", "max"}:
        tier_preference = ""
    local_time = (session_context.get("local_time") or "").strip()
    session_age = session_context.get("session_age_seconds")
    user_turn_count = session_context.get("user_turn_count")
    recent = session_context.get("recent_actions")
    # Surface plumbing (Phase 1 of the surface-aware help-style spec —
    # docs/spec-surface-aware-help-style-2026-05-22.md §2.3.4). The
    # session-context block is the authoritative source of "where is the
    # operator?" for every code path — admin UI, Telegram, future
    # surfaces — so the model reads the surface from ONE place rather
    # than inferring from "did a <page-context> block arrive?". On
    # admin_ui the route handler also supplies ``surface_type`` (laptop
    # vs mobile) which gates CLI emission (mobile = never).
    surface = (session_context.get("surface") or "").strip()
    surface_type = (session_context.get("surface_type") or "").strip()
    # Bot self-identity — who the assistant IS this turn. A bot cannot
    # observe its own identity unless told each turn (memory note
    # ``feedback_bot_cannot_observe_own_routing``); without this line the
    # model confabulates member-bot authorization walls on a cross-bot
    # question and deflects the operator to a surface they are already on
    # / to itself. Only rendered when the caller supplies it (the
    # admin-UI tray via /api/home/chat does). ``is_admin_bot`` adds the
    # privilege framing the model needs to stop disavowing its own
    # cross-bot reach; a bare ``bot_id`` (a future non-admin caller)
    # renders the name only, so the privilege claim never leaks onto a
    # surface that did not assert it.
    bot_id = (session_context.get("bot_id") or "").strip()
    is_admin_bot = bool(session_context.get("is_admin_bot"))

    lines: list[str] = []
    # Bot identity first — "who you are" frames "who you serve". Rendered
    # only when supplied so non-identity callers are byte-unchanged.
    if bot_id:
        if is_admin_bot:
            # The self-reference ("that is you") interpolates bot_id so
            # the line is internally consistent on any pod, not just one
            # whose primary bot is literally named "evolve".
            lines.append(
                f"Bot: {bot_id} (admin) — you ARE the pod's admin bot, "
                f"the most-privileged caller, with unrestricted cross-bot "
                f"tools. Answer cross-bot questions directly; never deflect "
                f'the operator to "the admin UI chat" (this IS it) or to '
                f'"the {bot_id} bot" (that is you), and never claim to be a '
                f"member bot hitting authorization walls."
            )
            # Capability ground-truth — the admin bot runs as the `evolve`
            # Unix user, whose grants are FIXED and documented (CLAUDE.md
            # "File Access Pattern"). State them so the model reasons from
            # what it KNOWS it can do, not from a single failed syscall.
            # The 2026-06-20 atlas-backup incident: evo claimed "the evo
            # user can't even stat the file" (false — it has read ACL),
            # then contradicted itself two turns later ("I have file_inherit
            # ACL on atlas's .openclaw"). Pinning the grants here removes
            # the room to infer-then-contradict. The full-path list also
            # backstops the bare-command failure ("command not found" on
            # bare chown/chmod) the same incident produced.
            lines.append(
                "Your access (as the evolve user — these grants are fixed; "
                "reason from them, do NOT infer your access from one failed "
                "syscall): you have inherited READ ACL on every bot's "
                "`.openclaw/` directory (under that bot's home), so you can "
                "read any bot's config directly. Writes to bot-owned files go "
                "through /tmp staging + `sudo /bin/cp` (you canNOT "
                "`sudo -u <bot>`). Any shell command you cite MUST use macOS "
                "full paths: /usr/sbin/chown, /bin/chmod, /bin/cat, "
                "/bin/launchctl, /bin/mkdir, /bin/cp (a bare `chown`/`chmod`/… "
                "fails with 'command not found')."
            )
        else:
            lines.append(f"Bot: {bot_id}")
    # Identity + authority — always render even when nothing else is set;
    # the cost is small and the framing is critical.
    lines.append(f"Operator: {operator_id} (authority tier: {authority})")
    # Tier preference — only render when the operator explicitly picked
    # Fast / Standard / Power. See note above for why Auto is omitted.
    if tier_preference:
        lines.append(f"Tier preference: {tier_preference}")
        # Machine-readable routing directive. The human-readable line above
        # is purely informational (so the model can acknowledge the pick);
        # ROUTING is enforced by the plugin's before_model_resolve hook.
        # For evo's admin home chat that hook runs inside the long-running
        # gateway daemon, NOT the thin `openclaw agent` CLI client the proxy
        # spawns — so the per-turn EVOLVE_TIER_PREFERENCE env var (set on the
        # client) is invisible to it, and the operator's pick was silently
        # dropped (home-chat Max routing bug). This directive travels in the
        # message envelope, which the gateway always receives, so the plugin
        # can read the per-turn tier there.
        #
        # SECURITY: this directive pins a premium/cost tier and bypasses the
        # operator-only chip + per-day max cap, so it must be unforgeable by
        # untrusted body text appended after this block (a chat message,
        # quoted email, fetched doc, member-bot inbound). Two properties make
        # it safe, mirrored by ModelRouter.parseTierDirective:
        #   (1) It lives INSIDE this server-emitted <session-context> block,
        #       which send_to_evo always prepends BEFORE the raw user body.
        #       The plugin anchors its parse to the FIRST such block, so a
        #       copy of the token in the user body (always after the first
        #       </session-context>) is ignored.
        #   (2) It carries a fresh per-turn nonce the user cannot predict
        #       (never echoed into user-visible context). The plugin rejects
        #       a bare `[evolve-routing] tier=…` with no well-formed nonce,
        #       so a guessed/copy-pasted legacy token cannot self-escalate.
        # Keep the token shape in sync with ModelRouter._TIER_DIRECTIVE_RE.
        _nonce = uuid.uuid4().hex
        lines.append(f"[evolve-routing nonce={_nonce}] tier={tier_preference}")
    # Surface line — only render when supplied. Absent surface preserves
    # the legacy "no <page-context> → Telegram" inference for callers
    # that haven't been updated yet.
    if surface:
        if surface_type:
            lines.append(f"Surface: {surface} / {surface_type}")
        else:
            lines.append(f"Surface: {surface}")
    if local_time:
        lines.append(f"Operator's local time: {local_time}")
    # Turn-N signal — load-bearing for the "fresh session" confabulation
    # fix (#1402 follow-up). When the JSONL on disk already shows at
    # least one prior user message, this is turn 2+ and the model's
    # context window ALREADY contains the prior turns (OC's JSONL
    # replay). The explicit count + "scroll back" instruction is the
    # in-prompt lever telling the model to use what it has rather than
    # disavow it. ``user_turn_count`` is the count of PRIOR user turns
    # written to the JSONL before the route handler invoked the agent,
    # so the current turn number is ``user_turn_count + 1``.
    if (
        isinstance(user_turn_count, (int, float))
        and int(user_turn_count) >= 1
    ):
        current_turn = int(user_turn_count) + 1
        # The counter-rail clause ("Treat your own prior replies as
        # memory of what you said, not as state-of-truth …") is the
        # 2026-05-23 follow-up to #1402's "fresh session" fix. #1402
        # told the model "your prior replies ARE in your context"; this
        # follow-up balances that with "but they are memory, not fact"
        # — see diagnosis-evo-recalls-prior-turns-as-ground-truth-
        # 2026-05-23.md. Without this, the model treats its 2-day-old
        # *"I staged a patch at /tmp/…"* assertion as current truth and
        # recommends `cp`ing from a path it never verified still
        # exists.
        if isinstance(session_age, (int, float)) and session_age > 0:
            age_str = _humanize_duration(int(session_age))
            lines.append(
                f"This is turn {current_turn} of an ongoing thread, "
                f"started {age_str} ago. Scroll back — the operator's "
                f"prior messages and your prior replies ARE in your "
                f"context above this block. Treat your own prior "
                f"replies as memory of what you said, not as "
                f"state-of-truth that is still valid. Any file path, "
                f"staged artifact, or config value you mentioned in "
                f"an earlier turn must be re-verified via a tool call "
                f"before you act or recommend on it."
            )
        else:
            lines.append(
                f"This is turn {current_turn} of an ongoing thread. "
                f"Scroll back — the operator's prior messages and your "
                f"prior replies ARE in your context above this block. "
                f"Treat your own prior replies as memory of what you "
                f"said, not as state-of-truth that is still valid. "
                f"Any file path, staged artifact, or config value you "
                f"mentioned in an earlier turn must be re-verified "
                f"via a tool call before you act or recommend on it."
            )
    elif isinstance(session_age, (int, float)) and session_age > 0:
        # Legacy path — kept for callers that pass session_age_seconds
        # without user_turn_count (none in production today, but the
        # field has been in the dataclass docstring since the original
        # session-context shipped, so we don't break that contract).
        lines.append(
            f"This chat thread is {_humanize_duration(int(session_age))} old."
        )
    if isinstance(recent, list) and recent:
        lines.append("Your recent actions in this thread (most recent first):")
        for a in recent[:_RECENT_ACTIONS_RING]:
            if isinstance(a, dict):
                tool = a.get("tool", "?")
                when = a.get("when") or "recently"
                outcome = a.get("outcome") or "ok"
                summary = a.get("summary") or ""
                line = f"  - {when}: `{tool}` → {outcome}"
                if summary:
                    line += f" — {summary[:120]}"
                lines.append(line)
            elif isinstance(a, str):
                lines.append(f"  - {a}")

    body = "\n".join(lines).rstrip()
    if not body:
        return ""
    return f"<session-context>\n{body}\n</session-context>"


def _humanize_duration(seconds: int) -> str:
    """Compact duration string ("5m", "2h", "3d"). Matches Team_bot_a-style
    elsewhere in the codebase; the model is robust to either form, but
    Team_bot_a-style is shorter and easier to scan in the block."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


# ── Recent-actions ring — read from OC's session jsonl ───────────────────────


def read_recent_actions(
    session_id: str,
    *,
    limit: int = _RECENT_ACTIONS_RING,
    sessions_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Pull the last N tool-call/result pairs from OC's session jsonl.

    OC's session log is the system of record for tool invocations on
    this session. Reading it (rather than maintaining a parallel ring
    in the proxy) avoids drift + works across admin-server restarts.

    Returns a list of ``{tool, outcome, when, summary}`` dicts ordered
    most-recent-first. Empty list when the session file doesn't exist
    yet (first turn) or can't be read. Never raises — the caller's
    session-context block still renders without recent-actions if
    this returns empty.

    The summary truncates the tool_result text to ~140 chars so the
    block stays small. The model can refetch full data via the tool
    pointer in page-context if it needs more.
    """
    if not session_id:
        return []
    sd = Path(sessions_dir or _OC_SESSIONS_DIR_DEFAULT)
    jsonl_path = sd / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    try:
        raw = jsonl_path.read_text()
    except (OSError, PermissionError):
        return []

    # Pair tool_use with its tool_result via toolCallId. OC writes them
    # as separate messages in chronological order; we walk forward,
    # remember tool_uses by id, then attach the matching tool_result
    # when it lands.
    pending: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "message":
            continue
        msg = rec.get("message") or {}
        timestamp_ms = msg.get("timestamp") or 0
        content = msg.get("content") or []
        role = msg.get("role")

        if role == "assistant":
            # Look for tool_use blocks
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    name = c.get("name") or "?"
                    pending[c.get("id", "")] = {
                        "tool": _ring_tool_label(name, c.get("input")),
                        "started_ms": timestamp_ms,
                    }
        elif role == "toolResult":
            tool_call_id = msg.get("toolCallId") or ""
            entry = pending.pop(tool_call_id, None)
            if entry is None:
                continue
            is_error = bool(rec.get("isError")) or msg.get("status") == "error"
            entry["outcome"] = "error" if is_error else "ok"
            entry["finished_ms"] = timestamp_ms
            # Pull a short summary from the result text
            text = ""
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "")[:160]
                    break
            entry["summary"] = _summarize_result_text(text)
            completed.append(entry)

    # Most-recent-first
    completed.sort(key=lambda e: e.get("finished_ms") or 0, reverse=True)
    out: list[dict[str, Any]] = []
    now_ms = int(__import__("time").time() * 1000)
    for entry in completed[:limit]:
        finished = entry.get("finished_ms") or 0
        age_s = max(0, (now_ms - finished) // 1000) if finished else 0
        out.append({
            "tool": entry["tool"],
            "outcome": entry["outcome"],
            "when": f"{_humanize_duration(age_s)} ago" if age_s else "just now",
            "summary": entry.get("summary", ""),
        })
    return out


def read_run_tool_calls(
    session_id: str,
    run_id: str | None,
    *,
    sessions_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read tool-call + tool-result pairs for one ``run_id`` from OC's
    session JSONL.

    Sibling of ``read_recent_actions`` — same file, but scoped to a
    single ``run_id`` (one operator turn). Returns most-recent-first:

        [{"tool": "proposal_action(apply)",
          "outcome": "ok" | "error",
          "summary": "<short result text>"}, ...]

    Phase 1 of the surface-aware help-style spec
    (docs/spec-surface-aware-help-style-2026-05-22.md) + the empty-reply
    diagnosis (docs/diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md):
    when OC's agent loop terminates mid-flight (file-lock contention,
    rate-limit, etc.), ``payloads[0].text`` is empty but the tool calls
    already ran. The proxy uses this helper to synthesize a yellow
    informational bubble naming what happened instead of surfacing
    ``(evo returned an empty reply)`` as a red error.

    Returns ``[]`` when ``run_id`` is missing, the session file doesn't
    exist, or no records match — caller falls back to the legacy
    placeholder in that case.
    """
    if not session_id or not run_id:
        return []
    sd = Path(sessions_dir or _OC_SESSIONS_DIR_DEFAULT)
    jsonl_path = sd / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    try:
        raw = jsonl_path.read_text()
    except (OSError, PermissionError):
        return []

    # OC's session jsonl records ``runId`` on each ``message`` envelope.
    # Filter to messages from this run, pair tool_use with toolResult
    # via toolCallId (same algorithm as ``read_recent_actions`` but
    # without the global limit / time horizon).
    pending: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "message":
            continue
        if rec.get("runId") and rec.get("runId") != run_id:
            continue
        msg = rec.get("message") or {}
        content = msg.get("content") or []
        role = msg.get("role")
        timestamp_ms = msg.get("timestamp") or 0

        if role == "assistant":
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    name = c.get("name") or "?"
                    pending[c.get("id", "")] = {
                        "tool": _ring_tool_label(name, c.get("input")),
                        "started_ms": timestamp_ms,
                    }
        elif role == "toolResult":
            tool_call_id = msg.get("toolCallId") or ""
            entry = pending.pop(tool_call_id, None)
            if entry is None:
                continue
            is_error = bool(rec.get("isError")) or msg.get("status") == "error"
            entry["outcome"] = "error" if is_error else "ok"
            entry["finished_ms"] = timestamp_ms
            text = ""
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "")[:200]
                    break
            entry["summary"] = _summarize_result_text(text)
            completed.append(entry)

    # Surface unpaired tool_use as ``no_result`` so the synthesized
    # message can name "N tool calls but no result recorded" — the
    # file-lock-contention case the diagnosis describes.
    for tool_call_id, entry in pending.items():
        entry["outcome"] = "no_result"
        entry["summary"] = "(no result recorded — OC session jsonl write race)"
        completed.append(entry)

    completed.sort(
        key=lambda e: e.get("finished_ms") or e.get("started_ms") or 0,
        reverse=True,
    )
    return [
        {
            "tool": e["tool"],
            "outcome": e.get("outcome", "ok"),
            "summary": e.get("summary", ""),
        }
        for e in completed
    ]


# ── Gateway liveness probe (for fallback detection) ─────────────────────────


def _evo_gateway_status() -> tuple[bool, int | None]:
    """Quick (≤1.5s) probe of evo's gateway.

    Returns ``(live, port)``. ``port`` is the resolved evo gateway port even
    when unreachable, so callers can include it in operator-facing text.
    ``port`` is ``None`` only when network.json itself can't be loaded.

    Used by ``send_to_evo`` to upgrade a generic subprocess-failure error
    (timeout, non-zero rc, empty stdout) into a structured ``gateway_down``
    response that the Chat UI can route to the diagnostic LLM fallback.
    The probe is best-effort: any exception → ``(False, port_if_known)``.
    """
    port: int | None = None
    try:
        from evolve_admin.config import load_network, DEFAULT_NETWORK_CONFIG
        network = load_network(DEFAULT_NETWORK_CONFIG)
        # Resolve the primary's gateway port via primary_bot_id (it lives on the
        # primary's bots[] entry now); legacy top-level `evolve` block honoured
        # as a fallback for pre-S1 pods (evo-account-separation S1).
        try:
            from primary_bot import primary_bot_gateway_port  # type: ignore
            port = primary_bot_gateway_port(network) or 19030
        except Exception:
            port = (network.get("evolve") or {}).get("gateway_port", 19030)
    except Exception:
        return False, None
    if not port:
        return False, None
    try:
        import urllib.request as _urllib
        with _urllib.urlopen(
            f"http://localhost:{port}/evolve/status", timeout=1.5,
        ) as r:
            r.read(1)  # consume one byte to confirm the response
            return True, port
    except Exception:
        return False, port


def _gateway_down_result(
    session_id: str, model: str | None, run_id: str | None, port: int | None,
) -> "ProxyResult":
    """Build a structured ``gateway_down`` ProxyResult.

    Centralizes the operator-facing copy + error code so every subprocess
    failure path (timeout, non-zero rc, empty stdout) renders identically
    when the underlying cause is "evo's gateway isn't responding." Chat
    UI keys off ``error == "gateway_down"`` to offer the diagnostic-LLM
    fallback.
    """
    port_str = f" on port {port}" if port else ""
    return ProxyResult(
        text=(
            f"Evo's gateway isn't responding{port_str}. The bot is down.\n\n"
            "You can talk to Evolve's diagnostic LLM in the meantime — it "
            "has no member-bot tools but can help diagnose the outage."
        ),
        session_id=session_id,
        model=model, run_id=run_id,
        error="gateway_down",
    )


def _synthesize_empty_reply_text(tool_calls: list[dict[str, Any]]) -> str:
    """Build the yellow-bubble synthesized text for an empty-reply turn
    where tool calls did happen.

    Spec: docs/spec-surface-aware-help-style-2026-05-22.md §8 Phase 1 +
    docs/diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md
    Priority 1. The intent is not to recreate evo's closing prose; it's
    to give the operator a ground-truth list of what just ran so they
    don't have to guess "did the work happen?" when the loop dies
    mid-flight without a closing text turn.
    """
    if not tool_calls:
        return ""
    n = len(tool_calls)
    n_ok = sum(1 for c in tool_calls if c.get("outcome") == "ok")
    n_err = sum(1 for c in tool_calls if c.get("outcome") == "error")
    n_no_result = sum(1 for c in tool_calls if c.get("outcome") == "no_result")
    parts: list[str] = []
    parts.append(
        f"evo ran {n} tool call{'s' if n != 1 else ''} but didn't produce "
        "a closing summary. Verify result below:"
    )
    # Counts line — quick scan
    counts: list[str] = []
    if n_ok:
        counts.append(f"{n_ok} succeeded")
    if n_err:
        counts.append(f"{n_err} returned an error")
    if n_no_result:
        counts.append(
            f"{n_no_result} have no recorded result (OC session jsonl write race)"
        )
    if counts:
        parts.append("- " + "; ".join(counts) + ".")
    # Per-call summary — cap at 10 to keep the bubble readable; the
    # operator can re-ask if they need more.
    for call in tool_calls[:10]:
        tool = call.get("tool", "?")
        outcome = call.get("outcome", "ok")
        summary = call.get("summary") or ""
        line = f"- `{tool}` → {outcome}"
        if summary:
            line += f" ({summary[:120]})"
        parts.append(line)
    if n > 10:
        parts.append(f"- ... and {n - 10} more tool call(s) — re-ask for details.")
    return "\n".join(parts)


def read_thread_stats(
    session_id: str,
    *,
    sessions_dir: Path | None = None,
) -> dict[str, int]:
    """Pull thread-level stats from OC's session jsonl.

    Sibling of ``read_recent_actions`` — same file, different aggregation.
    Returns ``{age_seconds, user_turn_count}``:

      * ``age_seconds`` — seconds from the first message's timestamp to
        now. Lets the session-context block render "this chat thread is
        N old", which is the in-prompt signal that distinguishes turn-1
        ("fresh ask") from turn-N ("ongoing thread; scroll back for
        the referent of the operator's bare reference").
      * ``user_turn_count`` — count of ``message`` records with
        ``role == "user"``. ``user_turn_count >= 1`` means at least one
        prior user turn happened (this is turn 2+); ``== 0`` means
        this really is turn 1 and the model should NOT see a "thread
        is N old" line.

    Returns ``{age_seconds: 0, user_turn_count: 0}`` when the session
    file doesn't exist, can't be read, or has no parseable timestamps.
    Callers can use the values directly — no None handling needed.

    Added 2026-05-21 (#1402 follow-up) to close the "fresh session"
    confabulation: the session-context block was previously identical
    on turn 1 and turn 2, so the model had no prompt-level signal that
    its replayed prior turns were the referent for "C" / "Option B" /
    bare follow-ups.
    """
    if not session_id:
        return {"age_seconds": 0, "user_turn_count": 0}
    sd = Path(sessions_dir or _OC_SESSIONS_DIR_DEFAULT)
    jsonl_path = sd / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return {"age_seconds": 0, "user_turn_count": 0}

    try:
        raw = jsonl_path.read_text()
    except (OSError, PermissionError):
        return {"age_seconds": 0, "user_turn_count": 0}

    first_ts_ms = 0
    user_turn_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "message":
            continue
        msg = rec.get("message") or {}
        ts_ms = msg.get("timestamp") or 0
        if isinstance(ts_ms, (int, float)) and ts_ms > 0 and not first_ts_ms:
            first_ts_ms = int(ts_ms)
        if msg.get("role") == "user":
            user_turn_count += 1

    if first_ts_ms:
        now_ms = int(time.time() * 1000)
        age_seconds = max(0, (now_ms - first_ts_ms) // 1000)
    else:
        age_seconds = 0
    return {"age_seconds": int(age_seconds), "user_turn_count": user_turn_count}


def _strip_oc_namespace(name: str) -> str:
    """OC namespaces MCP tools as ``<server>__<tool-name-with-hyphens>``.
    Strip the server prefix + convert hyphens back to dots so the
    recent-actions ring matches the names in AGENTS.md's page-tool map."""
    if "__" in name:
        name = name.split("__", 1)[1]
    return name.replace("-", ".")


def _ring_tool_label(name: str, arguments: Any) -> str:
    """Label for one recent-actions ring entry.

    Since the B7 Phase 2 tool diet (facades.py), the model calls
    enum-dispatch facades — every ``bot_action`` call arrives under the
    same advertised name, and only the enum argument says WHICH action
    ran. A bare facade name in the ring would make "did I already
    snooze that?" unanswerable, so facade calls are recorded as
    ``facade(value)`` (e.g. ``bot_action(restart)``,
    ``pod_state(bots)``). Legacy canonical names (deprecated aliases,
    pre-diet session history) and non-facade tools pass through the
    namespace strip unchanged.
    """
    label = _strip_oc_namespace(name)
    try:
        from evolve_admin.evo.tools.facades import FACADES
    except ImportError:
        return label
    spec = FACADES.get(label)
    if spec is not None and isinstance(arguments, dict):
        value = arguments.get(spec.param)
        if isinstance(value, str) and value.strip():
            return f"{label}({value.strip()})"
    return label


def _summarize_result_text(text: str) -> str:
    """Tiny one-line summary of a tool's result text. We don't try to
    parse JSON here — model has the full data via re-call if it needs
    more. Goal is to anchor recall ("ok, last call returned data") not
    to expose the data verbatim.

    Tools return JSON-encoded dicts; pull a few high-signal keys when
    they're present, otherwise prefix-snip."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:140] + ("…" if len(text) > 140 else "")
    if not isinstance(obj, dict):
        return text[:140]
    # Common high-signal fields across our tools
    for key in ("count", "error", "to_state", "to_status", "signal_id",
                "proposal_id", "summary", "ok"):
        if key in obj:
            val = obj[key]
            return f"{key}={val}" if not isinstance(val, str) else f"{key}={val[:80]}"
    # Fallback — show the keys
    keys = list(obj.keys())[:5]
    return f"keys=[{', '.join(keys)}]"


# ── Page-context summary formatting ──────────────────────────────────────────


def format_page_context(page_context: dict[str, Any] | None) -> str:
    """Render the page-context blob as a ``<page-context>`` XML block.

    Inputs come from the admin-UI JS frontend. Shape per spec §3.5:

        {
          "page_id": "alerts",
          "page_label": "Alerts",       (operator-facing, optional)
          "view": "firing",             (sub-view of the page, optional)
          "summary": "free text"        (markdown OK)
          | {
              "headline": "...",                       (one-line summary)
              "counts": { ... },                        (key totals)
              "items": [ { ... }, ... ],                (top-N visible)
              "elided_count": int,                      (N - len(items))
              "available_actions": [                    (UI affordances)
                  { "label": "Take this on",
                    "description": "applies the proposal" },
                  ...
              ],
              "tool_pointers": [
                  { "tool": "pod_state(query=\\"signals.firing\\")",
                    "for": "the full firing-alerts list" },
                  ...
              ]
            }
        }

    The ``<page-context>`` tag always carries a ``surface`` attribute
    (defaulting to ``admin_ui``) so the model knows the operator is at
    a desktop in a browser with direct UI affordances — NOT on
    Telegram. Without this signal evo has been suggesting things like
    "run ``evo fail`` to log this" or fabricating UI navigation paths
    ("Dashboard → team_bot_b → Config") to operators who are already on the
    page with the inline action buttons. Both observed 2026-05-19.

    Returns the empty string when ``page_context`` is None or has no
    usable content — the caller drops the block entirely rather than
    sending an empty wrapper that adds tokens for nothing.

    Truncates to ``_SUMMARY_MAX_CHARS`` to defend against
    accidental-payload-bloat from a page that forgot to summarize.
    """
    if not isinstance(page_context, dict):
        return ""
    page_id = (page_context.get("page_id") or "").strip()
    page_label = (page_context.get("page_label") or "").strip()
    view = (page_context.get("view") or "").strip()
    surface = (page_context.get("surface") or "admin_ui").strip()
    # surface_type (Phase 1 — surface-aware help-style spec §2.3.3):
    # the client-side viewport classifier emits ``"laptop"`` /
    # ``"mobile"``. Rendered as a sibling attribute so the model has
    # a cheap scan-target alongside ``surface`` (e.g. attrs like
    # ``surface="admin_ui" surface_type="mobile"``). Optional —
    # callers that don't supply it (Telegram dispatcher, tests) get
    # the legacy single-attribute shape.
    surface_type = (page_context.get("surface_type") or "").strip()
    summary = page_context.get("summary")

    # Nothing useful to inject → no block. Avoids paying tokens for
    # an empty wrapper.
    if not page_id and not page_label and not summary:
        return ""

    attrs: list[str] = []
    # surface attribute first — it's the most important framing signal
    # ("the operator is at a desktop in a browser" vs Telegram). Always
    # present when we emit a page-context block at all.
    attrs.append(f'surface="{_xml_quote(surface)}"')
    if surface_type:
        attrs.append(f'surface_type="{_xml_quote(surface_type)}"')
    if page_id:
        attrs.append(f'page="{_xml_quote(page_id)}"')
    if view:
        attrs.append(f'view="{_xml_quote(view)}"')
    if page_label and not page_id:
        # Fallback — page_label gives the model something to call this
        # surface even when the frontend didn't assign a machine id.
        attrs.append(f'page="{_xml_quote(page_label)}"')

    body = _render_summary_body(summary)
    if len(body) > _SUMMARY_MAX_CHARS:
        body = body[:_SUMMARY_MAX_CHARS] + (
            "\n\n[truncated by proxy — "
            f"original {len(body)} chars exceeds the {_SUMMARY_MAX_CHARS} cap. "
            "Use the named tool below to fetch the full data.]"
        )

    attr_str = (" " + " ".join(attrs)) if attrs else ""
    return f"<page-context{attr_str}>\n{body.rstrip()}\n</page-context>"


def _xml_quote(s: str) -> str:
    """Minimal escape for XML attribute values. The model is robust to
    typos, but a stray quote in the attribute value would break the
    block's parseability for any downstream tooling that reads it."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _render_summary_body(summary: Any) -> str:
    """Render the ``summary`` field — free-text or structured — into the
    body of the XML block.

    Structured shape gets a deterministic rendering: headline first,
    counts as a one-liner, items as a Markdown list, elided count
    explicit, tool pointers as named bullets. The model reads each
    section the same way every turn — predictable, cacheable.

    Free-text passes through verbatim so the frontend can override
    with bespoke prose when a structured shape doesn't fit.
    """
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, dict):
        return ""

    lines: list[str] = []
    headline = summary.get("headline")
    if headline:
        lines.append(str(headline).strip())

    counts = summary.get("counts")
    if isinstance(counts, dict) and counts:
        # "key=value, key=value" — easier for the model to scan than a JSON dump
        kv = ", ".join(f"{k}={v}" for k, v in counts.items())
        lines.append(f"Counts: {kv}.")

    items = summary.get("items")
    if isinstance(items, list) and items:
        lines.append("Top items:")
        for it in items:
            lines.append(f"  - {_render_item(it)}")

    # Per-bot scoreboard. Used by the Security-page server-side pack
    # (security_chat_context). Distinct from ``items`` because it has a
    # consistent per-bot shape that benefits from a dedicated section
    # header — and pages that already use ``items`` for a different
    # purpose (e.g. Recommendations cards) shouldn't have their layout
    # disturbed by adding per-bot rows.
    per_bot = summary.get("per_bot")
    if isinstance(per_bot, list) and per_bot:
        lines.append("Per-bot scoreboard:")
        for row in per_bot:
            lines.append(f"  - {_render_item(row)}")

    # Coalesced findings — one entry per advisory with the affected-bot
    # list inline. Lets the model answer "which bots have X?" without
    # per-bot fan-out. Introduced for the Security-page reshape; future
    # pages can adopt the same field name.
    findings = summary.get("findings")
    if isinstance(findings, list) and findings:
        lines.append(
            "Findings coalesced across bots "
            "(each line is one advisory; `bots:` lists the affected set):"
        )
        for f in findings:
            lines.append(f"  - {_render_finding(f)}")

    # Firing signals — separate concept from audit findings (signals
    # are observation-store entries; findings are audit-cache entries).
    # Producer + occurrence count + bot make the model's job easier than
    # title-only.
    firing = summary.get("firing_signals")
    if isinstance(firing, list) and firing:
        lines.append("Firing signals (from the signal store):")
        for s in firing:
            lines.append(f"  - {_render_signal(s)}")

    # Backup-drift entries — one per bot with the drifted keys inline.
    drift = summary.get("backup_drift")
    if isinstance(drift, list) and drift:
        lines.append("Backup drift (live openclaw.json vs baseline):")
        for d in drift:
            lines.append(f"  - {_render_drift(d)}")

    elided = summary.get("elided_count")
    if isinstance(elided, int) and elided > 0:
        lines.append(
            f"{elided} additional items NOT shown above — fetch the full set "
            "via the tools named below before claiming the operator's "
            "item-of-interest isn't here."
        )

    # Findings/signals elision counts are surfaced separately because
    # they originate from different sources (audit cache vs signal
    # store) and the operator's follow-up tool is different for each.
    elided_f = summary.get("elided_findings_count")
    if isinstance(elided_f, int) and elided_f > 0:
        lines.append(
            f"{elided_f} additional finding(s) NOT shown — call "
            "`pod_state(query=\"audit\", bot_id=…)` for the full per-bot list."
        )
    elided_s = summary.get("elided_signals_count")
    if isinstance(elided_s, int) and elided_s > 0:
        lines.append(
            f"{elided_s} additional firing signal(s) NOT shown — call "
            "`pod_state(query=\"signals.firing\")` for the full list."
        )

    actions = summary.get("available_actions")
    if isinstance(actions, list) and actions:
        lines.append(
            "On-screen actions the operator can click directly (do NOT "
            "invent navigation paths — these buttons are right there):"
        )
        for a in actions:
            if isinstance(a, dict):
                label = a.get("label")
                desc = a.get("description") or a.get("for")
                if label and desc:
                    lines.append(f"  - **{label}** — {desc}")
                elif label:
                    lines.append(f"  - **{label}**")
            elif isinstance(a, str):
                lines.append(f"  - {a}")

    pointers = summary.get("tool_pointers")
    if isinstance(pointers, list) and pointers:
        lines.append("Tools that return this page's full data:")
        for p in pointers:
            if isinstance(p, dict):
                tool = p.get("tool")
                what_for = p.get("for") or p.get("description")
                if tool and what_for:
                    lines.append(f"  - `{tool}` — {what_for}")
                elif tool:
                    lines.append(f"  - `{tool}`")

    return "\n".join(lines)


def _render_item(item: Any) -> str:
    """Render one ``items[]`` entry. Dict → "key=value" pairs joined;
    string → as-is. Anything else falls back to repr(). Keeps the
    summary deterministic regardless of frontend payload variation."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return ", ".join(f"{k}={v}" for k, v in item.items())
    return repr(item)


def _render_finding(f: Any) -> str:
    """Render one coalesced finding for the ``findings`` section.

    Format: ``[severity] title [category=X] bots: a,b,c — remediation``.
    Designed for one-glance scanning: severity first (the model's main
    priority signal), title verbatim, then the affected-bots list
    inline so "which bots have X?" answers without a follow-up call.
    """
    if not isinstance(f, dict):
        return repr(f)
    sev = (f.get("severity") or "info").strip()
    title = (f.get("title") or "").strip()
    cat = (f.get("category") or "").strip()
    bots = f.get("bots") or []
    if not isinstance(bots, list):
        bots = []
    rem = (f.get("remediation") or "").strip()
    parts = [f"[{sev}] {title}"]
    if cat:
        parts.append(f"[category={cat}]")
    if bots:
        parts.append(f"bots: {','.join(str(b) for b in bots)}")
    line = " ".join(parts)
    if rem:
        line += f" — {rem}"
    return line


def _render_signal(s: Any) -> str:
    """Render one firing-signal entry. Same scan-from-left philosophy
    as findings: severity-shaped prefix first, then signal type, then
    detail."""
    if not isinstance(s, dict):
        return repr(s)
    sev_vec = s.get("severity_vector")
    sev_mag = s.get("severity_magnitude")
    sev_chip = ""
    if sev_vec or sev_mag is not None:
        sev_chip = f"[{sev_vec or 'sev'}={sev_mag if sev_mag is not None else '?'}] "
    sig_type = (s.get("type") or "").strip()
    title = (s.get("title") or "").strip()
    bot_id = (s.get("bot_id") or "").strip()
    producer = (s.get("producer") or "").strip()
    occ = s.get("occurrence_count")
    head = f"{sev_chip}{title}"
    if sig_type:
        head = f"{sev_chip}({sig_type}) {title}"
    tail_parts = []
    if bot_id:
        tail_parts.append(f"bot={bot_id}")
    if producer:
        tail_parts.append(f"producer={producer}")
    if isinstance(occ, int) and occ:
        tail_parts.append(f"occ={occ}")
    return head if not tail_parts else f"{head} ({', '.join(tail_parts)})"


def _render_drift(d: Any) -> str:
    """Render one backup-drift entry: bot + drifted-keys list + freshness."""
    if not isinstance(d, dict):
        return repr(d)
    bot = (d.get("bot_id") or "").strip()
    keys = d.get("drifted_keys") or []
    if not isinstance(keys, list):
        keys = []
    stale = bool(d.get("stale_backup"))
    last = d.get("last_backup_at")
    head = bot or "(unknown bot)"
    if keys:
        head += f" — drifted: {','.join(str(k) for k in keys)}"
    extras = []
    if stale:
        extras.append("stale=true")
    if last:
        extras.append(f"last={last}")
    return head if not extras else f"{head} [{'; '.join(extras)}]"


# ── Subprocess invocation ────────────────────────────────────────────────────


def _build_cmd(
    *, message: str, session_id: str, agent: str,
) -> list[str]:
    """Shell command for one agent turn. Centralized so the test fixture
    can stub it and so a future migration to direct WS-RPC has exactly
    one place to update."""
    return [
        _openclaw_bin(), "agent",
        "--agent", agent,
        "--session-id", session_id,
        "--json",
        "--message", message,
    ]


def _parse_agent_json(raw: str) -> tuple[str, str | None, dict[str, Any], str | None]:
    """Parse `openclaw agent --json` output → (text, model, usage, run_id).

    OC's shape (verified on 2026.5.18):
        {
          "runId": "...",
          "status": "ok" | ...,
          "summary": "completed",
          "result": {
            "payloads": [{"text": "...", "mediaUrl": null}],
            "meta": {
              "durationMs": int,
              "agentMeta": {
                "sessionId": "...",
                "model": "claude-sonnet-4-6",
                "contextTokens": int
              },
              ...
            }
          }
        }

    Defensive across shape drift: anything missing degrades to a
    sensible default. We don't raise — the caller's response shape
    must always include a text field for the chat bubble.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ("", None, {}, None)

    run_id = data.get("runId")
    result = data.get("result") or {}
    payloads = result.get("payloads") or []
    first = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
    text = first.get("text") or ""
    meta = result.get("meta") or {}
    agent_meta = meta.get("agentMeta") or {}
    model = agent_meta.get("model")
    usage_raw = meta.get("usage") or agent_meta.get("usage") or {}
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    return (text, model, usage, run_id)


def send_to_evo(
    message: str,
    *,
    session_id: str,
    network_path: Path,
    page_context: dict[str, Any] | None = None,
    session_context: dict[str, Any] | None = None,
    agent: str = _DEFAULT_AGENT,
    timeout_s: int = _SUBPROCESS_TIMEOUT_S,
    tier_preference: str | None = None,
    caller_surface: str | None = None,
    caller_bot_id: str | None = None,
    caller_operator_handle: str | None = None,
) -> ProxyResult:
    """Forward one chat message to evo's agent and return the reply.

    Wraps two context blocks at the top of the user message:

      * ``<session-context>`` — operator identity, authority, local
        time, and a ring of the model's recent tool calls in this
        thread (spec §3.7 lever #1). Closes identity, reference-
        resolution, and audit-recall gaps.

      * ``<page-context>`` — what the operator currently sees on their
        admin-UI page (spec §3.4 / §3.5). Closes page-state gaps.

    Order: session-context FIRST, then page-context. Rationale: the
    session block frames *who* is asking and *what they've recently
    done*; the page block frames *what they're looking at right now*.
    Identity before situation is the standard prompt-engineering
    layering.

    If session_context is supplied without an explicit ``recent_actions``
    field, the proxy reads the last N tool calls from OC's session
    jsonl and injects them automatically — the caller doesn't have to
    track that state themselves.

    ``network_path`` isn't passed to OC directly (OC reads its own
    network.json from the runtime context) — it's threaded through
    for symmetry with the rest of the evo proxy surface and future
    use (e.g. per-pod cwd).

    Returns a ProxyResult. Never raises — every failure path produces
    a ProxyResult with ``error`` set + an operator-facing ``text``.
    """
    msg = (message or "").strip()
    if not msg:
        return ProxyResult(
            text="(empty message)", session_id=session_id, error="empty_message",
        )

    # Auto-fill recent_actions from OC's session jsonl when the caller
    # didn't supply them. The caller can pass an explicit empty list
    # to disable (eg first turn on a fresh thread where we know there
    # are none).
    if isinstance(session_context, dict) and "recent_actions" not in session_context:
        session_context = {
            **session_context,
            "recent_actions": read_recent_actions(session_id),
        }

    # Branch-D follow-through injection (2026-07-28 Backup-page
    # incident). If the PREVIOUS turn on this session was withheld by
    # the inspector's no_evidence_reject, the stub promised the operator
    # a read-only check that nothing else forces to happen — so the
    # proxy injects the promised instruction into THIS turn's context:
    # an <inspector-follow-through> block naming the registered read
    # tool evo must run and quote before answering. Consume-on-read
    # (at most one injection per reject); any failure degrades to no
    # injection, never a blocked turn. See inspector.py
    # record_pending_followup / consume_pending_followup.
    followup_block = ""
    try:
        from evolve_admin.evo.inspector import (
            consume_pending_followup,
            format_followup_nudge,
        )
        _pending = consume_pending_followup(session_id)
        if _pending:
            followup_block = format_followup_nudge(_pending)
    except Exception:  # noqa: BLE001 — follow-through must never break the proxy
        log.exception("send_to_evo: follow-through injection failed; continuing without")
        followup_block = ""

    sc_block = format_session_context(session_context)
    pc_block = format_page_context(page_context)
    prefix_parts: list[str] = [b for b in (sc_block, pc_block, followup_block) if b]
    user_text = ("\n\n".join(prefix_parts) + "\n\n" + msg) if prefix_parts else msg

    cmd = _build_cmd(message=user_text, session_id=session_id, agent=agent)
    # cwd=/tmp: openclaw is Node, calls process.cwd() at startup; if the
    # parent's cwd is unreadable by the running user, Node aborts with
    # EACCES before main(). Same fix as the alerts dispatcher uses.
    # Subprocess env. EVOLVE_NETWORK_PATH lets the plugin find network.json.
    # EVOLVE_TIER_PREFERENCE carries the operator's per-turn tier pick
    # (Auto / Fast / Standard / Power) into the plugin's
    # before_model_resolve hook — see ModelRouter.setUserTier in
    # packages/plugin/src/observer/ModelRouter.ts. Env-per-subprocess
    # gives us per-turn scope for free; the plugin re-reads on every
    # hook fire so "Power for this one turn, then Auto" works.
    _subprocess_env: dict[str, str] = {
        **os.environ,
        "EVOLVE_NETWORK_PATH": str(network_path),
    }
    _tier_pref = (tier_preference or "").strip().lower()
    if _tier_pref in ("fast", "standard", "power", "max"):
        _subprocess_env["EVOLVE_TIER_PREFERENCE"] = _tier_pref
    # "auto" and unknown values: do NOT set the var, so the plugin
    # falls through to classifier-driven routing.

    # Phase 2 authorization framework. The MCP server subprocess reads
    # these env vars to build its ``CallerIdentity`` and gate
    # admin-tier tool calls accordingly. Admin-UI / Telegram-operator
    # callers carry an admin identity; cross-bot relay callers carry
    # ``cross_bot_member`` + the originating bot_id so admin-tier
    # refusals can name the right escalation path. When ``caller_surface``
    # is None we omit the var and the subprocess falls back to its
    # conservative ``cross_bot_member`` default (fail-closed) — so a caller
    # that forgets to declare its surface is treated as non-admin, never
    # silently elevated.
    _surface = (caller_surface or "").strip().lower()
    if _surface in ("admin_ui", "telegram_operator", "cross_bot_member"):
        _subprocess_env["EVOLVE_CALLER_SURFACE"] = _surface
    if caller_bot_id:
        _subprocess_env["EVOLVE_CALLER_BOT_ID"] = str(caller_bot_id)
    if caller_operator_handle:
        _subprocess_env["EVOLVE_CALLER_OPERATOR_HANDLE"] = str(caller_operator_handle)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout_s, cwd="/tmp",
            env=_subprocess_env,
        )
    except subprocess.TimeoutExpired:
        log.warning("send_to_evo: subprocess timed out after %ss", timeout_s)
        # If evo's gateway isn't actually responding, prefer the
        # gateway_down shape so the UI offers the diagnostic-LLM
        # fallback. A subprocess timeout with a live gateway is a
        # saturation/long-tool-call problem — keep the timeout message
        # for that case.
        live, port = _evo_gateway_status()
        if not live:
            return _gateway_down_result(session_id, None, None, port)
        return ProxyResult(
            text=(
                f"evo's gateway didn't respond within {timeout_s}s. "
                "The model may be saturated; retry in a moment."
            ),
            session_id=session_id, error="timeout",
        )
    except FileNotFoundError:
        return ProxyResult(
            text="openclaw binary not found on PATH",
            session_id=session_id, error="openclaw_not_found",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("send_to_evo: unexpected subprocess error")
        return ProxyResult(
            text=f"proxy invocation failed: {exc}",
            session_id=session_id, error=f"subprocess_error: {exc}",
        )

    duration_ms = int((time.time() - started) * 1000)
    log.info(
        "send_to_evo: session=%s rc=%s dur=%dms cmd-bytes=%d",
        session_id, proc.returncode, duration_ms, sum(len(c) for c in cmd),
    )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:500]
        # Non-zero rc with an unresponsive gateway is the classic
        # connection-refused / gateway-down shape (most common cause
        # during the 2026-06-03 OC-upgrade outage). Probe to confirm so
        # we don't mis-label an honest gateway error as a downed gateway.
        live, port = _evo_gateway_status()
        if not live:
            return _gateway_down_result(session_id, None, None, port)
        return ProxyResult(
            text=(
                "evo's gateway returned an error. Stderr tail: "
                f"{stderr or '(empty)'}"
            ),
            session_id=session_id,
            error=f"openclaw_rc={proc.returncode}: {stderr}",
        )

    text, model, usage, run_id = _parse_agent_json(proc.stdout)

    # Phase 4 of the surface-aware help-style spec — Inspector seam.
    # Run the outgoing-text inspector on the assistant reply before it
    # reaches the operator. The inspector handles three known-recurring
    # failure modes (shell-recommendation, permission-tier fabrication,
    # precondition staleness) per the multi-criterion haiku confirmer
    # in §7.4. On any inspector failure, the inspector itself returns
    # the original text + None event, so this call never raises into
    # the proxy path. See packages/admin/evolve_admin/evo/inspector.py.
    inspector_event = None
    if text:
        try:
            from evolve_admin.evo.inspector import inspect_outgoing_text
            text, inspector_event = inspect_outgoing_text(
                text,
                session_context=session_context,
                page_context=page_context,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 — inspector must never break the proxy
            log.exception("send_to_evo: inspector raised; passing original text through")
            inspector_event = None

    if not text:
        # OC returned 0 but no payload. Two failure shapes:
        #
        #   (a) Loop terminated mid-flight after emitting tool calls —
        #       the work usually succeeded but no closing-text turn
        #       ever ran. File-lock contention on the session JSONL is
        #       the recurring proximate cause (see
        #       docs/diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md).
        #       Synthesize a yellow-bubble confirmation listing the
        #       tool calls so the operator has ground truth instead of
        #       a red error.
        #
        #   (b) Model genuinely produced no text and no tool calls —
        #       the truly-empty case. Fall back to the legacy
        #       placeholder.
        #
        # The error code stays ``empty_reply`` in both branches; the
        # route handler uses it to render a yellow ``proxy_warn`` bubble
        # (Phase 1 of the surface-aware help-style spec §8).
        tool_calls = read_run_tool_calls(session_id, run_id)
        if tool_calls:
            synthesized = _synthesize_empty_reply_text(tool_calls)
            log.info(
                "send_to_evo: empty payload but %d tool call(s) ran — "
                "synthesized confirmation. session=%s run_id=%s",
                len(tool_calls), session_id, run_id,
            )
            return ProxyResult(
                text=synthesized,
                session_id=session_id, error="empty_reply",
                model=model, run_id=run_id,
            )
        # Empty stdout from a successful subprocess is ambiguous: the
        # LLM may have genuinely produced no text (true empty reply), or
        # the gateway may have responded with nothing because it's not
        # actually up. Probe to distinguish — only override the legacy
        # placeholder when the probe confirms the gateway is down.
        live, port = _evo_gateway_status()
        if not live:
            return _gateway_down_result(session_id, model, run_id, port)
        return ProxyResult(
            text="(evo returned an empty reply)",
            session_id=session_id, error="empty_reply",
            model=model, run_id=run_id,
        )
    return ProxyResult(
        text=text,
        session_id=session_id,
        model=model,
        usage=usage,
        run_id=run_id,
        inspector_event=inspector_event,
    )

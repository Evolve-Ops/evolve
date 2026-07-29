"""home_chat — conversational LLM layer for the Home page.

The Home chat input first tries the existing ``/api/evo/dispatch``
keyword surface. When the dispatcher doesn't recognize the user's
text, this module falls back to a Haiku call so the operator gets a
real conversational reply instead of the canned "I didn't recognize
that" message.

Key decisions, anchored to the prior conversation:

* **Primary bot's Anthropic key** — calls go out with evo's own
  credentials via :func:`primary_bot.read_primary_bot_anthropic_key`,
  honoring the "per-bot LLM inference, never centralized" principle
  in the operator's memory. The admin server hosts the route but the
  inference is billed against the bot.

* **Haiku 4.5, not Sonnet** — the conversation is mostly summarizing
  pod state + suggesting evo subcommands; Haiku handles that well at
  ~5-10x lower cost. The cost discussion in the design conversation
  established "very cheap status, tokens for guidance + fixing" as
  the priority.

* **Daily call cap** — ``DAILY_CALL_CAP`` per pod, tracked in
  ``{shared_dir}/home-chat-usage.json``. When exceeded, the route
  returns the dispatcher's reply plus a hint to retry tomorrow. Hard
  ceiling — the operator can raise it in network.json.

* **History capped at last 20 turns** — sent to the LLM as
  ``messages`` so the conversation is coherent without growing the
  per-call cost unbounded. Older turns stay in the operator's
  localStorage but don't ride along.

* **Pod-state digest** — server-side recreation of the same data
  the Home narrative consumes (firing signals + pending proposals
  + bot status). Included in the system prompt so the LLM can ground
  its replies in real state without the operator having to repeat
  context every turn.

Endpoint:

    POST /api/home/chat
    Body:
      message    str  — required, the operator's prompt
      history    list — optional, prior [{role: "user"|"assistant", text: str}]
    Returns:
      reply       str  — the response text (rendered as evo's bubble)
      source      "dispatch" | "llm"
      subcommand  str | None  — set when source=="dispatch"
      cost_usd    float | None — set when source=="llm"
      cap_status  {used, remaining, daily_cap}
      error?      str
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# Model + endpoint. Pinned to a specific Haiku revision so the cost
# numbers in the meter stay accurate; operator can override in
# network.json::home_chat.model when a newer revision lands.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Hard caps. Operator override path: network.json::home_chat::daily_cap.
DEFAULT_DAILY_CALL_CAP = 100
DEFAULT_HISTORY_TURNS = 20
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_S = 30

# Haiku 4.5 pricing snapshot (per 1M tokens, USD). Lives here so the
# cost-meter math is auditable; update when Anthropic posts new prices.
# These match the rates documented on https://anthropic.com/pricing as of
# 2026-05; the meter is informational either way (operator can verify
# against the Admin API ingest under Maintenance).
HAIKU_INPUT_PER_MTOK_USD = 0.25
HAIKU_OUTPUT_PER_MTOK_USD = 1.25


# ─────────────────────────────────────────────────────────────────────────────
# Cap tracking
# ─────────────────────────────────────────────────────────────────────────────


def _usage_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "home-chat-usage.json"


def _today_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def read_usage(shared_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Read today's call-count + cumulative cost. Returns a fresh-zero
    dict when the file is missing, malformed, or stale (yesterday's)."""
    p = _usage_path(shared_dir)
    today = _today_key(now)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        data = {}
    if data.get("date") != today:
        return {"date": today, "calls": 0, "cost_usd": 0.0}
    return {
        "date": today,
        "calls": int(data.get("calls") or 0),
        "cost_usd": float(data.get("cost_usd") or 0.0),
    }


def bump_usage(
    shared_dir: Path,
    *,
    cost_usd: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Increment today's call count + cost. Atomic via temp-file rename
    so a crash mid-write can't corrupt the meter."""
    current = read_usage(shared_dir, now=now)
    current["calls"] = int(current["calls"]) + 1
    current["cost_usd"] = round(float(current["cost_usd"]) + float(cost_usd), 6)
    p = _usage_path(shared_dir)
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("home_chat usage write failed: %s", exc)
    return current


def cap_status(
    shared_dir: Path,
    *,
    daily_cap: int = DEFAULT_DAILY_CALL_CAP,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return {used, remaining, daily_cap, cost_today_usd}."""
    usage = read_usage(shared_dir, now=now)
    used = int(usage["calls"])
    return {
        "used": used,
        "remaining": max(0, daily_cap - used),
        "daily_cap": daily_cap,
        "cost_today_usd": round(float(usage["cost_usd"]), 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pod-state digest
# ─────────────────────────────────────────────────────────────────────────────


def _format_breaker_eta(expires_at: str | None, now: datetime | None = None) -> str:
    """Mirror the dashboard's countdown wording. Indefinite trips
    report 'manual reset required'; finite trips render as "in 18h 35m"
    / "in 2d 4h" / "any moment now"."""
    if not expires_at:
        return "indefinite — manual reset required"
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    mins = max(0, int(round((expiry - now).total_seconds() / 60)))
    if mins <= 0:
        return "any moment now"
    if mins < 60:
        return f"reactivates in {mins}m"
    if mins < 1440:
        return f"reactivates in {mins // 60}h {mins % 60}m"
    return f"reactivates in {mins // 1440}d {(mins % 1440) // 60}h"


def _format_breaker_trip_age(
    tripped_at: str | None, now: datetime | None = None
) -> str:
    """Render an age phrase ('22h 15m ago', '35m ago', '3d 4h ago') from
    a tripped_at ISO timestamp. Pre-computed server-side because the
    narrative LLM gets relative-time math wrong across day boundaries
    (issue #2165: 22h-old trip reported as 'about an hour ago')."""
    if not tripped_at:
        return "(trip time unknown)"
    try:
        tripped = datetime.fromisoformat(tripped_at.replace("Z", "+00:00"))
    except ValueError:
        return "(trip time unknown)"
    now = now or datetime.now(timezone.utc)
    mins = max(0, int(round((now - tripped).total_seconds() / 60)))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h {mins % 60}m ago"
    return f"{mins // 1440}d {(mins % 1440) // 60}h ago"


def build_pod_state_digest(
    *,
    firing_signals: list[dict] | None = None,
    pending_proposals: list[dict] | None = None,
    bots: dict[str, dict] | None = None,
    host_health: dict | None = None,
    active_breakers: list[dict] | None = None,
    pause_state: dict | None = None,
    pod_health: dict | None = None,
    max_signals: int = 8,
    max_proposals: int = 5,
    max_health_checks: int = 5,
    now: datetime | None = None,
) -> str:
    """Render a compact text digest of current pod state for the LLM
    system prompt. The shape mirrors what the structured Home narrative
    already shows the operator — keeping the LLM's view of the world
    aligned with what the operator is looking at.

    All inputs are optional so callers can omit pieces they don't have.
    """
    lines: list[str] = []

    # ── Pod-wide pause ──────────────────────────────────────────────
    # Operator clicked "Pause all bots". Every bot is suspended; this
    # outranks every other observation.
    if isinstance(pause_state, dict) and pause_state.get("paused"):
        actor = pause_state.get("paused_by") or pause_state.get("actor") or "?"
        reason = (pause_state.get("reason") or "").strip()
        when = pause_state.get("paused_at") or pause_state.get("ts") or ""
        tail = f" — {reason[:120]}" if reason else ""
        when_str = f" at {when}" if when else ""
        lines.append(f"⚠ Pod-wide pause active (by {actor}{when_str}){tail}")
        lines.append("")

    # ── Tripped breakers ────────────────────────────────────────────
    # A tripped breaker means a bot is suspended — headline material,
    # not a smaller-stuff item. Breakers live in a separate store
    # from Signals (see breakers/store.py), so a breaker trip does
    # NOT show up under "Firing alerts" below.
    if active_breakers:
        lines.append(f"Tripped breakers ({len(active_breakers)}):")
        for r in active_breakers:
            scope = r.get("bot_id") or "?"
            btype = r.get("type") or "?"
            kind = "cost cap" if btype == "cost" else ("full halt" if btype == "full" else btype)
            initiated_by = r.get("initiated_by") or "?"
            trip_age = _format_breaker_trip_age(r.get("tripped_at"), now=now)
            eta = _format_breaker_eta(r.get("expires_at"), now=now)
            reason = (r.get("reason") or "").strip()
            scope_label = "pod-wide" if scope == "pod" else scope
            head = f"  - {scope_label}: {kind} tripped {trip_age} ({initiated_by}, {eta})"
            if reason:
                head += f" — {reason[:120]}"
            lines.append(head)
        lines.append("")

    # ── Urgent outage state ─────────────────────────────────────────
    # Headline-grade outage state that isn't pause/breaker (those rank
    # above). The 2026-06-03 OC-upgrade outage exposed this gap: every
    # gateway was down, the firing-signals section listed them, but the
    # narrative led with "noise across plugins, permissions, and cost
    # tracking" because the LLM treated firing alerts as one bucket. By
    # surfacing down-gateway count + bot names as their own block, the
    # narrative prompt can be instructed to lead with it (see
    # NARRATIVE_SYSTEM_PROMPT). Also visible to the diagnostic LLM, which
    # uses the same digest.
    if bots:
        down_bots = sorted(
            bot_id for bot_id, b in bots.items()
            if isinstance(b, dict) and b.get("live") is False
            # Exclude bots that have never reported a metric — likely
            # not-yet-deployed rather than down. The proper "deployed"
            # signal is harder to derive; last_metric_date is the cheap
            # proxy that matches what the Overview tile shows.
            and b.get("last_metric_date")
        )
        if down_bots:
            names = ", ".join(down_bots[:8])
            tail = f" (+{len(down_bots) - 8} more)" if len(down_bots) > 8 else ""
            lines.append(
                f"⚠ URGENT — {len(down_bots)} bot gateway"
                f"{'s' if len(down_bots) != 1 else ''} DOWN: {names}{tail}"
            )
            lines.append("")

    # ── Pod health checks ───────────────────────────────────────────
    # Mirrors the red "Pod health: X issues detected" banner on the
    # Overview page. These checks (gateway liveness, launchd plists,
    # file perms, repo ownership, …) don't auto-fire Signals, so the
    # narrative needs them piped in explicitly.
    if isinstance(pod_health, dict):
        summary = pod_health.get("summary") or {}
        fail = int(summary.get("fail") or 0)
        warn = int(summary.get("warn") or 0)
        if fail or warn:
            bits = []
            if fail:
                bits.append(f"{fail} fail")
            if warn:
                bits.append(f"{warn} warn")
            lines.append(f"Pod health: {fail + warn} issues ({', '.join(bits)})")
            checks = [
                c for c in (pod_health.get("checks") or [])
                if isinstance(c, dict) and c.get("status") in ("FAIL", "WARN")
            ]
            # FAIL before WARN so the worst hits first.
            checks.sort(key=lambda c: 0 if c.get("status") == "FAIL" else 1)
            for c in checks[:max_health_checks]:
                status = c.get("status", "?")
                category = c.get("category", "?")
                name = c.get("name", "?")
                detail = (c.get("detail") or "").strip().splitlines()[0][:140]
                tail = f" — {detail}" if detail else ""
                lines.append(f"  - [{status}] {category}/{name}{tail}")
            if len(checks) > max_health_checks:
                lines.append(f"  ... and {len(checks) - max_health_checks} more")
            lines.append("")

    # ── Bots ────────────────────────────────────────────────────────
    if bots:
        online = sum(1 for b in bots.values() if (b or {}).get("live"))
        total = len(bots)
        lines.append(f"Bots: {online}/{total} online")
        for bot_id, b in sorted(bots.items()):
            b = b or {}
            status = "online" if b.get("live") else ("active" if b.get("last_metric_date") else "offline")
            extra = []
            if b.get("evolve_synced") is False:
                extra.append("version drift")
            extra_str = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  - {bot_id}: {status}{extra_str}")
        lines.append("")

    # ── Firing signals ──────────────────────────────────────────────
    if firing_signals:
        lines.append(f"Firing alerts ({len(firing_signals)} total):")
        for s in firing_signals[:max_signals]:
            title = (s.get("title") or s.get("type") or "?").strip()
            producer = s.get("producer", "?")
            bot_id = s.get("bot_id")
            fw = s.get("severity_framework") or {}
            vector = fw.get("vector", "?")
            magnitude = fw.get("magnitude", "?")
            scope_label = f" ({bot_id})" if bot_id else ""
            lines.append(
                f"  - [{producer}/{vector}:{magnitude}] {title}{scope_label}"
            )
        if len(firing_signals) > max_signals:
            lines.append(f"  ... and {len(firing_signals) - max_signals} more")
        lines.append("")

    # ── Pending proposals ───────────────────────────────────────────
    if pending_proposals:
        pending = [p for p in pending_proposals if (p or {}).get("status") == "pending"]
        if pending:
            lines.append(f"Pending proposals ({len(pending)}):")
            for p in pending[:max_proposals]:
                summary = (
                    p.get("admin_surface_summary")
                    or p.get("problem")
                    or "(no summary)"
                )
                bot_id = p.get("bot_id", "?")
                urgency = p.get("urgency", "?")
                lines.append(f"  - [{urgency}] {bot_id}: {summary[:120]}")
            if len(pending) > max_proposals:
                lines.append(f"  ... and {len(pending) - max_proposals} more")
            lines.append("")

    # ── Host ────────────────────────────────────────────────────────
    if isinstance(host_health, dict) and host_health.get("available") is not False:
        cpu = host_health.get("cpu_percent")
        mem = (host_health.get("memory") or {}).get("percent")
        disk = (host_health.get("disk") or {}).get("percent")
        if cpu is not None or mem is not None or disk is not None:
            host_bits = []
            if cpu is not None: host_bits.append(f"CPU {int(cpu)}%")
            if mem is not None: host_bits.append(f"Mem {int(mem)}%")
            if disk is not None: host_bits.append(f"Disk {int(disk)}%")
            lines.append("Host: " + " · ".join(host_bits))

    return "\n".join(lines) or "(no pod state available)"


# ─────────────────────────────────────────────────────────────────────────────
# Action catalog — what subcommands actually exist + do
# ─────────────────────────────────────────────────────────────────────────────
# Pulled from the dispatcher's registry at runtime so evo's prompt is
# grounded in reality, not in whatever the LLM imagines a subcommand
# named "wizard" might do. Three layers of filtering:
#
#   1. Stub handlers ("evolve_admin.evo.handlers.stub:*") are excluded.
#      Their handlers return "coming soon" messages; offering them as
#      action buttons is a broken promise.
#   2. Subcommands flagged in _CHAT_INCOMPATIBLE below are excluded.
#      Some commands need channel + sender context (interactive
#      sessions, OAuth callbacks) that the admin chat surface doesn't
#      provide.
#   3. The remaining list is what the prompt advertises AND what
#      extract_suggested_actions() validates against — both layers
#      filter from the same source of truth so the LLM and the UI
#      can't disagree.

# Subcommands that PARSE successfully but don't produce useful output
# in the admin chat surface. Curated list — re-evaluate when handlers
# get adapted to the chat path.
_CHAT_INCOMPATIBLE: frozenset[str] = frozenset({
    # Starts an interactive recommendation session that wants channel
    # context; in the admin chat it routes to a Telegram-flavored path.
    "better",
    # Special-cased in dispatch.py around mutation + audit. Needs
    # shared_dir + caller context not threaded through the chat route.
    "claim",
    # Special-cased to derive user_key from channel + sender_external_id;
    # neither is available from the admin chat surface.
    "profile",
})

# Substring marker used by every stub handler import path.
_STUB_HANDLER_MARKER = ".handlers.stub:"


def build_action_catalog() -> list[dict[str, str]]:
    """Return the subcommands evo can actually offer in the admin chat,
    each with its REAL short_help description.

    Filters: stub handlers + _CHAT_INCOMPATIBLE entries. The result is
    the single source of truth used both for the prompt advertisement
    and for the suggested-action button extraction — so what evo names
    and what the UI surfaces as clickable can't drift.

    Falls back to an empty list when the registry isn't importable
    (defensive — keeps the route working even if evo subcommands move).
    """
    try:
        from ..evo import subcommands as _sc
        out: list[dict[str, str]] = []
        for cmd in _sc._REGISTRY:
            if _STUB_HANDLER_MARKER in (cmd.handler or ""):
                continue
            if cmd.name in _CHAT_INCOMPATIBLE:
                continue
            out.append({
                "name": cmd.name,
                "short_help": (cmd.short_help or "").strip(),
            })
        return out
    except Exception:
        return []


def working_subcommand_names() -> set[str]:
    """Set of subcommand names that work in the admin chat surface.

    Used to validate LLM-suggested action buttons so a hallucinated or
    stub-only command (like the old `evo wizard` recommendation that
    rendered "coming soon" when clicked) never surfaces as a clickable
    dead-end."""
    return {entry["name"] for entry in build_action_catalog()}


def _format_action_catalog_for_prompt(catalog: list[dict[str, str]]) -> str:
    """Render the action catalog as a bulleted list for the prompt.

    Each line: ``  evo <name> — <short_help>``. Indented so it visually
    reads as a subordinate block under the "AVAILABLE COMMANDS" header.
    """
    if not catalog:
        return "  (no subcommands available)"
    return "\n".join(
        f"  evo {entry['name']} — {entry['short_help']}"
        for entry in catalog
    )


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────


SYSTEM_PROMPT_TEMPLATE = """You are evo, the conversational interface for the Evolve pod admin.

You help the pod operator triage and act on what's happening across their bots.
Be concise, factual, and friendly. Match Team_bot_a-style messaging:
- Short header (one line) when there's a finding
- One fact per line, no walls of text
- Conversational close-out, no boilerplate sign-offs

USE THE DATA YOU ALREADY HAVE.

The Current pod state block below is a complete snapshot of every firing
signal, every pending proposal, and every bot's status. When the operator
asks about an alert, an issue, or a specific bot, ANSWER FROM THE DIGEST —
do not tell the operator to run a command to look something up that you
can already see.

Specifically, NEVER say things like:
  ✗  "Run `evo alerts` to see what's firing."
  ✗  "Use `evo summary` for a fresh report."
  ✗  "Check the Alerts page for details."
The operator can SEE you have the data; deflecting reads as evasive. Just
answer with the specifics: the signal title, which bot, what severity,
when it was last observed, and what it likely means.

═══ AVAILABLE COMMANDS — THIS IS YOUR ENTIRE ACTION SURFACE ═══

You can offer one kind of action: a subcommand from the list below.
Write it as `evo <name>` in backticks. The UI renders each mention as
a one-click button under your reply; clicking the button submits
"evo <name>" as the next chat turn. (The operator can also type it
into the prompt below — both work.) That is the WHOLE menu.

The list below is the live registry of subcommands that actually work
in this chat surface — names + the registry's own short_help text:

{action_catalog}

ABSOLUTE RULES — read carefully:

1. NEVER recommend a subcommand whose name isn't in the list above.
   The list is generated from the dispatcher's registry at runtime.
   If a name (`redeploy`, `restart`, `rebuild`, `fix`, `deploy`,
   `restore`, etc.) isn't in the list, it doesn't exist — don't write
   `evo redeploy` or `evo restart` even once.

2. NEVER describe what a subcommand does beyond what its short_help
   says. The short_help is the contract. If `evo wizard`'s short_help
   says "run the Evolve setup wizard for this bot", do NOT tell the
   operator it will "reset personal_bot from scratch" or "walk you through a
   full personal_bot reset" or anything else not in the short_help.

3. NEVER make an offer you can't deliver. If the right fix requires
   redeploying a bot, editing files, resetting permissions, restarting
   services, syncing versions, or anything else not in the list, say
   so plainly — name what the operator needs to do and where:
     • "Redeploy from Dashboard → Personal_bot → Redeploy"
     • "Restore SOUL.md from git history"
     • "Edit exec-approvals.json on the host"
   Then, separately, offer any list-allowed subcommands that DO help —
   `evo fail` to log the issue, `evo alerts` etc.

If you catch yourself about to write "want me to start the redeploy",
STOP. You can't start a redeploy. That kind of offer broken evo's
core promise. Walk the operator to where they can do it; never offer
yourself.

WORKED EXAMPLE — operator asks "can you help fix the personal_bot issues?":
  ✓  Good: "Personal_bot's firing three things right now:
        • MEMORY.md missing — the file moved or got cleared
        • Permission config drift — exec-approvals.json doesn't match baseline
        • SOUL.md structural emptiness — header-only, no body

       The fix for the first two is a redeploy from Dashboard →
       Personal_bot → Redeploy (operator action — I can't trigger that from
       here). For SOUL.md the body needs restoring from git history,
       also an operator-side step.

       What I CAN do from here: `evo fail` to log this so personal_bot's
       diagnostics run and surface more detail. Want me to?"

AUTHORITY — your current tier is "{authority}". This shapes the offer you
make at the end of an answer:
  - "ask"        → describe the issues and offer 1–3 specific subcommand
                   buttons. Do NOT propose to act yourself — let the
                   operator decide.
  - "auto-small" → for low-risk hygiene (snoozes, dismisses, doc-only
                   fixes via the listed subcommands) propose to handle
                   it; for anything else, ask first.
  - "auto"       → for anything with a clear answer that's in your
                   capability list, propose to handle it. Always confirm
                   before running anything destructive.

Whichever tier, "propose to handle it" still means suggesting a
subcommand button — never invent an action outside the capability list.

PHRASING — refer to actions as buttons, not terminal commands:

  ✓  "I can run `evo fail` for you — click the button below."
  ✓  "Want me to check the apps with `evo app-audit`?"
  ✗  "Run `evo fail` to log it." (sounds like a terminal command)

The button under your message IS the way to run the suggestion. Mention
"click the button" or "want me to" when offering, so the operator
isn't searching for a terminal.

If pod state is empty or you genuinely don't have enough information to
answer, say so plainly. Don't invent data, and don't pretend to read
files that aren't in the digest.

──────────────────────────────────────
Current pod state:
{pod_state}
──────────────────────────────────────
"""


def build_system_prompt(
    pod_state_digest: str, authority: str = "ask"
) -> str:
    """Compose the chat system prompt.

    ``authority`` is the operator's current authority tier — "ask" /
    "auto-small" / "auto" — set in the Home page UI. Passed in here so
    the prompt can shape its action offers to match (describe-only vs.
    propose-to-act). Defaults to "ask" (the most conservative) when the
    caller doesn't pass one, so the LLM never assumes blanket permission.

    The action catalog is pulled from the dispatcher's registry at
    runtime via ``build_action_catalog()`` — see that function for the
    stub/incompatible filters. Injecting the live registry here means
    the prompt and the suggested-action validator share a single source
    of truth.
    """
    catalog = build_action_catalog()
    return SYSTEM_PROMPT_TEMPLATE.format(
        pod_state=pod_state_digest,
        authority=authority if authority in {"ask", "auto-small", "auto"} else "ask",
        action_catalog=_format_action_catalog_for_prompt(catalog),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic call (stubbable for tests)
# ─────────────────────────────────────────────────────────────────────────────


# Transport signature: (system_prompt, messages, api_key, model, max_tokens)
#   -> {"text": str, "input_tokens": int, "output_tokens": int}
Transport = Callable[[str, list[dict], str, str, int], dict[str, Any]]


def _default_transport(
    system_prompt: str,
    messages: list[dict],
    api_key: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    req = urllib.request.Request(
        ANTHROPIC_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
        data = json.loads(resp.read())
    text = ""
    blocks = data.get("content") or []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text += b.get("text", "")
    usage = data.get("usage") or {}
    return {
        "text": text.strip(),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimate $/call from token counts. Haiku pricing per 1M tokens."""
    return round(
        (input_tokens / 1_000_000) * HAIKU_INPUT_PER_MTOK_USD
        + (output_tokens / 1_000_000) * HAIKU_OUTPUT_PER_MTOK_USD,
        6,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Narrative — LLM-generated friendly summary for the Home briefing.
#
# Distinct from the chat path: the narrative is auto-fetched on every
# Home visit (no user message), so caching is load-bearing — without it
# every page reload would burn a Haiku call. Cache key is the SHA1 of
# the pod-state digest, so the cached narrative reuses across visits
# until pod state actually changes.
#
# Shares ``DAILY_CALL_CAP`` accounting with the chat path: narrative
# fetches count against the same per-pod cap so a busy chat day doesn't
# silently overspend on narrative regeneration too.
# ─────────────────────────────────────────────────────────────────────────────


DEFAULT_NARRATIVE_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes
DEFAULT_NARRATIVE_MAX_TOKENS = 400  # narrative replies are short prose


NARRATIVE_SYSTEM_PROMPT = """You are evo, the friendly daily-briefing voice on the Evolve pod admin home page.

Write a SHORT summary of the operator's current pod state — 1-2 short
paragraphs, conversational prose, NO bullet lists, NO walls of text.
Match Team_bot_a-style:
- Lead with the headline ("All quiet" / "One thing to look at" / "Three small things across two bots")
- One natural sentence stitching the actual findings together — refer
  to bots by name, keep it conversational
- If there's nothing notable, say so plainly and stop — don't pad
- If there are minor advisory items (version drift, audit cron stale,
  unused bots), close with one casual mention — "also worth a glance
  later" — without alarm

Timing — use the digest's exact strings; never recompute relative time:
- The digest gives you pre-computed phrases like "tripped 22h 15m ago"
  and "reactivates in 13m". Use them verbatim (you may drop trailing
  "0m" — "tripped 22h ago" is fine, "tripped about an hour ago" is NOT).
- Do NOT translate ISO timestamps into your own relative-time phrasing.
  You will get it wrong across day boundaries.
- Do NOT round or paraphrase a precise countdown into a vague one
  ("in 13m" must not become "in just over an hour").

Priority ordering — what counts as the headline:
- A pod-wide pause means every bot is suspended. ALWAYS lead with it.
- A tripped breaker means a bot is suspended right now. ALWAYS lead with
  it, name the bot, and use the digest's trip-age + reactivation strings
  verbatim ("tripped 22h ago, reactivates in 13m").
  Never demote a tripped breaker to "smaller stuff".
- An "⚠ URGENT — N bot gateways DOWN" line in the digest means the pod
  is in an active outage right now. If present, this OUTRANKS pod-health,
  firing alerts, and pending proposals. Lead with it, name the bots,
  say plainly that the pod is degraded. Don't bury it under "noise" or
  "smaller stuff".
- Pod-health FAILs (gateways, launchd, perms) are headline material;
  pod-health WARNs can go in "smaller stuff" unless they cluster.
- Firing alerts and pending proposals come next.
- Version drift, stale crons, idle bots are smaller-stuff material.

Don't enumerate every signal. Group them. Pick the most useful framing.
Avoid technical jargon when a plain word works (say "auth issue" not
"auth_failed", "memory file missing" not "MEMORY.md unreadable").

Don't suggest commands or actions — that's a different surface. Just
the state of things in a friendly voice.

──────────────────────────────────────
Pod state right now:
{pod_state}
──────────────────────────────────────
"""


def build_narrative_prompt(pod_state_digest: str) -> str:
    return NARRATIVE_SYSTEM_PROMPT.format(pod_state=pod_state_digest)


def digest_hash(pod_state_digest: str) -> str:
    """SHA1 of the digest string. Cache invalidates when this changes."""
    return hashlib.sha1((pod_state_digest or "").encode("utf-8")).hexdigest()


def _narrative_cache_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "home-narrative-cache.json"


def read_narrative_cache(shared_dir: Path) -> dict[str, Any] | None:
    """Read the cached narrative or return None on any read error."""
    p = _narrative_cache_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def write_narrative_cache(
    shared_dir: Path,
    *,
    digest_hash_str: str,
    text: str,
    cost_usd: float,
    model: str,
    input_tokens: int,
    output_tokens: int,
    now: datetime | None = None,
) -> None:
    """Atomic write of the narrative cache via temp-file rename."""
    now = now or datetime.now(timezone.utc)
    payload = {
        "digest_hash": digest_hash_str,
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "text": text,
        "cost_usd": round(float(cost_usd), 6),
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }
    p = _narrative_cache_path(shared_dir)
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("home_chat narrative cache write failed: %s", exc)


def cache_is_fresh(
    cache: dict[str, Any] | None,
    *,
    digest_hash_str: str,
    ttl_seconds: int = DEFAULT_NARRATIVE_CACHE_TTL_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Return True when the cache matches the current digest AND was
    generated within the TTL window."""
    if not isinstance(cache, dict):
        return False
    if cache.get("digest_hash") != digest_hash_str:
        return False
    ts = cache.get("generated_at")
    if not isinstance(ts, str):
        return False
    try:
        generated_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - generated_at) < timedelta(seconds=ttl_seconds)


def generate_narrative(
    *,
    pod_state_digest: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_NARRATIVE_MAX_TOKENS,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """One-shot LLM call for the briefing narrative.

    No conversation history (this is auto-fetched, not a chat turn).
    Returns the same {text, input_tokens, output_tokens, cost_usd,
    model} shape as :func:`call_llm` so the route can share usage
    bookkeeping.
    """
    transport = transport or _default_transport
    system_prompt = build_narrative_prompt(pod_state_digest)
    # Single user-side prompt that anchors the request. The system
    # prompt already contains the pod-state context; this just tells
    # the model what to do with it. Kept brief so the in-context
    # cost stays low.
    messages = [{"role": "user", "content": "Briefly summarize the pod state above."}]
    result = transport(system_prompt, messages, api_key, model, max_tokens)
    cost = _estimate_cost_usd(
        result.get("input_tokens", 0), result.get("output_tokens", 0)
    )
    return {
        "text": result.get("text", "").strip(),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": cost,
        "model": model,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic LLM — outage-only fallback when evo's gateway is down.
#
# The admin UI's primary chat path is evo's OC gateway. When that gateway
# stops responding, the operator's chat box becomes useless — they can't
# even ask a clear-headed second pair of eyes "what's going on". This is
# the fallback channel: a small, tool-less Haiku call routed through the
# same pod-wide Anthropic credential the briefing narrative already uses.
#
# Distinct from chat (call_llm) in three ways:
#   1. Different system prompt — explicitly NOT-evo, no tool calls, focused
#      on outage diagnosis.
#   2. No subcommand catalog / authority hint — those are evo concepts.
#   3. The user-facing surface displays a clear banner stating which LLM
#      they're talking to (set by the route layer).
# ─────────────────────────────────────────────────────────────────────────────


DIAGNOSTIC_SYSTEM_PROMPT = """You are Evolve's diagnostic assistant.

The operator's primary bot ("evo") is currently DOWN — its gateway isn't
responding. They've fallen back to this channel because they can't reach
evo. You are NOT evo. Refer to evo in the third person ("evo is down").

You have READ ACCESS to the pod-state digest below — nothing else. You
CANNOT execute tools, run commands, modify any pod state, message bots,
schedule jobs, or approve proposals. Do not pretend otherwise.

What you SHOULD do:
- Diagnose what's likely wrong based on the firing signals and recent
  state.
- Suggest concrete CLI commands the operator can run themselves on the
  pod — format every command in a fenced code block so the operator can
  copy-paste cleanly. Prefer `sudo evolve-admin <subcommand>` and
  `sudo /bin/launchctl ...` shapes; the operator runs commands as
  pod-admin on the mini.
- Point at log paths, daemon names, and shared-dir locations when
  relevant (`/Users/Shared/evolve/...`, `/var/log/...`,
  `~/Library/Logs/...`).
- Be a clear-headed second pair of eyes. Outages are stressful — keep
  the tone calm and concrete.

Style: tight, technical, action-oriented. No pleasantries, no preamble.
Lead with the most likely cause, then a short list of things to try.
Plain text only (no markdown tables); fenced code blocks for commands.

──────────────────────────────────────
Pod state right now:
{pod_state}
──────────────────────────────────────
"""


def build_diagnostic_prompt(pod_state_digest: str) -> str:
    return DIAGNOSTIC_SYSTEM_PROMPT.format(pod_state=pod_state_digest)


def generate_diagnostic_reply(
    *,
    user_message: str,
    history: list[dict],
    pod_state_digest: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_history_turns: int = DEFAULT_HISTORY_TURNS,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """One diagnostic LLM call. Same shape as :func:`call_llm` — distinct
    only in system prompt + lack of authority/subcommand machinery.

    History is preserved across turns so the operator can have a real
    back-and-forth while diagnosing; the route layer is responsible for
    threading it through.
    """
    transport = transport or _default_transport
    system_prompt = build_diagnostic_prompt(pod_state_digest)
    sanitized = _sanitize_history(history, max_turns=max_history_turns)
    messages = list(sanitized) + [{"role": "user", "content": user_message}]
    result = transport(system_prompt, messages, api_key, model, max_tokens)
    cost = _estimate_cost_usd(
        result.get("input_tokens", 0), result.get("output_tokens", 0)
    )
    return {
        "text": result.get("text", ""),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": cost,
        "model": model,
    }


def call_llm(
    *,
    user_message: str,
    history: list[dict],
    pod_state_digest: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_history_turns: int = DEFAULT_HISTORY_TURNS,
    transport: Transport | None = None,
    authority: str = "ask",
) -> dict[str, Any]:
    """Make one Anthropic Messages call. Returns:

        {
          "text": str,
          "input_tokens": int,
          "output_tokens": int,
          "cost_usd": float,
          "model": str,
        }

    ``history`` is a list of ``{role: "user"|"assistant", text: str}``
    dicts in chronological order. Last ``max_history_turns`` are sent
    to the API along with the new user message.

    ``authority`` shapes the prompt's action-offer guidance ("ask" /
    "auto-small" / "auto"). See ``build_system_prompt``.
    """
    transport = transport or _default_transport
    system_prompt = build_system_prompt(pod_state_digest, authority=authority)

    # Build messages: history (capped + normalized) + the new turn.
    sanitized = _sanitize_history(history, max_turns=max_history_turns)
    messages = list(sanitized) + [{"role": "user", "content": user_message}]

    result = transport(system_prompt, messages, api_key, model, max_tokens)
    cost = _estimate_cost_usd(
        result.get("input_tokens", 0), result.get("output_tokens", 0)
    )
    return {
        "text": result.get("text", ""),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": cost,
        "model": model,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Suggested-action extraction
# ─────────────────────────────────────────────────────────────────────────────
# The LLM is instructed to format every command suggestion as `evo X` in
# backticks (see SYSTEM_PROMPT_TEMPLATE). We scan the reply text for that
# pattern, validate each name against the subcommand registry, dedupe,
# and return a list of one-click action descriptors the UI renders as
# buttons under the bubble.
#
# Pattern: backtick + "evo " + subcommand name (lowercase letters/dashes,
# optional trailing args) + closing backtick. The optional args are
# discarded for v1 — every action button just submits "evo <name>" as a
# new chat turn. We can plumb the args through later if a use case needs
# them (e.g. "evo fail <description>").


_SUGGESTION_RE = re.compile(
    r"`evo\s+([a-zA-Z][a-zA-Z0-9_\-]*)(?:\s+[^`]*)?`"
)


def extract_suggested_actions(
    reply_text: str,
    known_subcommands: set[str] | None = None,
) -> list[dict]:
    """Pull suggested-action descriptors from an LLM reply.

    Returns a list of ``{kind, subcommand, label}`` dicts in the order
    the LLM mentioned them. Deduplicated by subcommand name (so a reply
    that suggests `evo alerts` twice produces one button, not two).

    ``known_subcommands`` filters to commands actually in the registry.
    When None, the caller is opting out of validation — every backticked
    suggestion turns into a button. The route always passes a populated
    set; the optional-None branch is for unit tests.
    """
    if not reply_text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for match in _SUGGESTION_RE.finditer(reply_text):
        name = match.group(1).strip().lower()
        if not name or name in seen:
            continue
        if known_subcommands is not None and name not in known_subcommands:
            continue
        seen.add(name)
        out.append({
            "kind": "dispatch",
            "subcommand": name,
            "label": f"Run evo {name}",
        })
    return out


def _sanitize_history(history: list[dict], *, max_turns: int) -> list[dict]:
    """Convert the JS-side ``{role: "user"|"evo", text}`` shape into
    Anthropic Messages API's ``{role: "user"|"assistant", content}``.

    Drops malformed entries; enforces strict alternation user-then-
    assistant (the API rejects consecutive same-role turns); caps at
    ``max_turns`` most-recent turns.
    """
    out: list[dict] = []
    last_role: str | None = None
    for h in (history or []):
        if not isinstance(h, dict):
            continue
        role_raw = (h.get("role") or "").lower()
        if role_raw == "evo":
            role = "assistant"
        elif role_raw == "user":
            role = "user"
        else:
            continue
        text = h.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        # API requires alternating turns; collapse consecutive same-role
        # entries (rare — happens when the UI shows a pending-then-error
        # pair) by keeping only the most recent.
        if role == last_role:
            out[-1] = {"role": role, "content": text}
        else:
            out.append({"role": role, "content": text})
            last_role = role
        if len(out) >= max_turns:
            out = out[-max_turns:]
    return out

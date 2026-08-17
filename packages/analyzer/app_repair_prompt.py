"""app_repair_prompt — channel-aware SYSTEM prompt for in-situ app repair.

When the primary user messages the bot directly via Telegram / Slack /
Discord and says *"what apps are broken?"* or *"help me fix the journal
app"*, the bot's normal session loop drives the conversation. This
module composes the prompt that gives that session loop the right
shape:

* What audience the bot is talking to (operator vs primary user) and
  what channel they're on (admin UI vs Telegram vs …).
* Where the per-finding detail lives in the bot's workspace
  (manifests directory + the field map in the deep-dive footer).
* The proposal-block format (shared with Channel A's admin-UI chat via
  :mod:`analyzer.app_repair_proposals`).
* How to file a proposal cross-bot: ``evo file-proposal …`` via the
  evo gateway.

Channel A (admin-UI chat) packages everything into a per-turn prompt
server-side via ``evolve_admin.applications.repair_chat``. Channel B
(this module) ships the same vocabulary at session_start via a
marker-block injection so the bot's NORMAL session loop already knows
how to interpret repair-conversation cues.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Channel = Literal["telegram", "slack", "discord", "imessage", "admin_ui",
                   "other"]
Audience = Literal["primary_user", "pod_operator", "team_member"]


@dataclass
class AppRepairPromptContext:
    """Inputs for :func:`build_app_repair_system_prompt`.

    Kept tiny on purpose — channel + audience drive most of the
    template's branches; the bot's mission guide and per-bot
    customisations live in OTHER session-prefix blocks. This block
    is shared across every bot in the pod.
    """
    bot_id: str
    channel: Channel = "other"
    audience: Audience = "primary_user"
    # When True, the prompt mentions admin-UI affordances (click-to-
    # apply, the Self-Improvement queue, etc.). Default False because
    # the canonical Channel B audience is on Telegram/Slack with no
    # such surface.
    audience_has_admin_ui: bool = False


def build_app_repair_system_prompt(ctx: AppRepairPromptContext) -> str:
    """Build the SYSTEM prompt block for app-repair conversations.

    Channel A (the admin-UI chat) can adopt this same template for the
    structured-output portion; the audience/channel branches make the
    differences narrow. Channel B's session_start injects the output
    of ``build_app_repair_system_prompt(...)`` as a marker block.

    The output is deterministic — same ctx in → same text out. Tests
    rely on that for parser round-trip assertions.
    """
    audience_line = _audience_line(ctx)
    apply_line = _apply_line(ctx)

    return f"""[APP REPAIR — bot-side awareness for in-situ conversations]

{audience_line}

You have awareness of your own app findings: the session-start [Apps]
block above summarises them, and the [COHERENCE VOCAB] reference —
inlined in this prompt or in `evolve/reference/APPS_GUIDE.md` in your
workspace — tells you what assertion ids and severities mean. For full
detail on any finding, read:

  ~/.openclaw/workspace/manifests/<app>.json

and inspect the fields .coherence.findings[], .reconciliation,
.coherence.last_capability_check, and .last_audit. Do not invent
findings, assertion ids, file paths, or field names — use only what
the manifest and the [COHERENCE VOCAB] reference describe.

When the user wants to propose a fix, EMIT structured blocks in your
reply using EXACTLY this shape:

  <<<repair_proposal action="ACTION_NAME">>>
  {{"key": "value", ...}}
  <<<end>>>

Available actions (one block per proposal; multiple blocks per reply
are fine):

  1. propose_field_edit — change a manifest field (top-level or
     nested via dotted path).
     {{"field": "success_criteria.observable_outcomes",
       "before": <current>, "after": <new>, "rationale": "why"}}

  2. propose_file_edit — describe a needed code change. Applying this
     does NOT auto-edit the file; it queues the change as a
     known_issue on the manifest. Use sparingly.
     {{"path": "scripts/my_app.py",
       "summary": "what needs to change", "rationale": "why"}}

  3. propose_test_exemption — request that the app's test suite be
     marked exempt.
     {{"reason": "why a test isn't appropriate for this app"}}

  4. mark_resolved — when you and the user agree a finding is
     acceptable as-is. Use the finding's `signature` (a 16-hex value
     on each finding entry), not a free-text description.
     {{"signature": "<sig from .coherence.findings[*].signature>",
       "rationale": "why this is fine"}}

  5. done — signal that the conversation is complete.
     {{}}

{apply_line}

Conservatism rules you MUST follow:
  - Be conversational and concise. Describe the issue and ASK before
    proposing — don't fabricate proposals the user didn't ask for.
  - One proposal per block. Multiple blocks per reply are fine when
    you're answering a single ask.
  - Never invent finding signatures, field paths, or file paths — use
    only what appears in the manifest and findings.
  - If you don't know what to do, ask the user a question instead of
    guessing.
  - The user does not directly apply your proposals. The operator
    approves them before any manifest mutation happens. Your reply
    should confirm "filed; the operator will review" after the
    proposal lands."""


def _audience_line(ctx: AppRepairPromptContext) -> str:
    """First line of the prompt — sets audience + channel context."""
    channel_label = {
        "telegram":  "Telegram",
        "slack":     "Slack",
        "discord":   "Discord",
        "imessage":  "iMessage",
        "admin_ui":  "the admin UI",
        "other":     "this channel",
    }.get(ctx.channel, "this channel")

    if ctx.audience == "pod_operator":
        return (
            f"You're talking to the pod operator via {channel_label}. They "
            f"approve proposals directly — use structured proposals freely "
            f"and keep prose short."
        )
    if ctx.audience == "team_member":
        return (
            f"You're talking to a team member via {channel_label}. They "
            f"CAN'T approve repairs themselves — acknowledge what they "
            f"report and offer to flag it for the primary user. Don't "
            f"emit proposal blocks for a team member's request unless "
            f"they're explicitly delegating from the primary."
        )
    # primary_user
    return (
        f"You're talking to your primary user via {channel_label}. Use "
        f"plain English first; propose structured fixes only when the "
        f"user says yes to a specific change. The user approves intent; "
        f"the operator approves the actual manifest write."
    )


def _apply_line(ctx: AppRepairPromptContext) -> str:
    """Tells the bot how a proposal becomes an applied change in this channel."""
    if ctx.channel == "admin_ui":
        return (
            "Apply path: in the admin-UI chat the operator sees each "
            "proposal as a click-to-Apply chip. You don't need to file "
            "anything separately — the server handles routing."
        )
    # Every other channel goes through evo file-proposal cross-bot.
    return (
        "Apply path: after the user confirms a proposal, invoke\n"
        "  evo file-proposal --on-behalf-of " + ctx.bot_id + " "
        "--app <app_id> --action <action> --content <json>\n"
        "via the cross-bot `evo` keyword. This lands a Proposal in the "
        "operator's queue at audience:pod_operator; the operator approves "
        "before any manifest mutation happens. Confirm to the user "
        "\"filed; the operator will review.\""
    )


# ── Marker block extraction ─────────────────────────────────────────────────
# Session_surface injects the app-repair prompt as a marker block so it's
# available to the bot's normal session loop. The prompt itself is composed
# per-session (channel + audience vary); the block is wrapped in the
# evolve-app-repair-prompt markers so the content-scan allowlist treats it
# as a known injection point (no prompt-injection false positive).

APP_REPAIR_BEGIN_MARKER = "<!-- evolve-app-repair-prompt:begin -->"
APP_REPAIR_END_MARKER = "<!-- evolve-app-repair-prompt:end -->"


def wrap_for_session_prefix(prompt_text: str) -> str:
    """Wrap a built prompt in the marker block used for session-prefix
    injection. The markers themselves are not LLM-visible content — they
    let the content scanner allow this injection without firing a
    prompt-injection signal.
    """
    return (
        f"{APP_REPAIR_BEGIN_MARKER}\n"
        f"{prompt_text}\n"
        f"{APP_REPAIR_END_MARKER}"
    )

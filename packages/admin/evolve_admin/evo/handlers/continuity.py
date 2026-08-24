"""``evo continuity`` — short explainer of the Continuity Engine."""

from __future__ import annotations

from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role


_BLURB = (
    "**The Continuity Engine**\n\n"
    "It's what lets your bots deliver on \"I'll get back to you\" promises.\n\n"
    "When a bot commits to acting later — \"I'll check again in 20 minutes\", "
    "\"remind me Tuesday\" — it schedules the follow-up itself with its "
    "built-in `defer` tool: a note to its future self with a fire time.\n\n"
    "A pod-wide runner checks the schedule every couple of minutes and, when "
    "a follow-up comes due, wakes the bot in the original conversation so it "
    "picks up right where it left off. No approval step — the bot only ever "
    "defers things it was already asked to do.\n\n"
    "Each bot's pending follow-ups live in its workspace "
    "(`workspace/evolve/defer-queue.jsonl`), and the Maintenance page shows "
    "the runner's health."
)


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    return DispatchResult(
        subcommand="continuity",
        role=role,
        mode="speak",
        system_append=(
            "IMPORTANT: The user has typed `evo continuity`. "
            "Respond ONLY with the following message, verbatim. "
            "Do not add commentary, framing, or any additional text:\n\n"
            + _BLURB
        ),
        direct_send_message=_BLURB,
    )

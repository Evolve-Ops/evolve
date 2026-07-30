"""``evo continuity`` — short explainer of the Continuity Engine."""

from __future__ import annotations

from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role


_BLURB = (
    "**The Continuity Engine**\n\n"
    "It's what lets me pick work up while you're not around.\n\n"
    "When you mention something deferred — \"I'll do that tonight\", "
    "\"remind me Tuesday\", \"keep working on this overnight\" — I capture it "
    "as a structured task and queue it.\n\n"
    "Small inline actions (append a note, send a ping, check if a URL is up) "
    "can fire on their own when the time comes.\n\n"
    "Bigger work (a full session to keep going on something) waits for your "
    "OK first, then runs while you're idle.\n\n"
    "Nothing fires silently — you can see what's queued and approve, cancel, "
    "or reschedule anything before it runs."
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

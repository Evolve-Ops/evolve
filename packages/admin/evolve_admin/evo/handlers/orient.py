"""Bare ``evo`` — orientation message.

Typed by itself, ``evo`` is ambiguous: a brand-new user expects setup, a
returning user expects a recommendation. So we don't guess. The bare
keyword now responds with a short description of what evo is and points
the user at the next concrete step:

  * wizard never run  → suggest ``evo wizard``
  * wizard has run    → suggest ``evo better``

…and ``evo help`` in both cases for the full surface.

The two action surfaces stay unambiguous: ``evo wizard`` always runs the
setup wizard (re-runnable), ``evo better`` always asks the Better Engine
for a recommendation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role, derive_user_key
from ..wizard import state as _wizard_state


_DESCRIPTION = (
    "**evo**\n\n"
    "I'm your in-chat handle for Evolve — the layer that keeps this bot "
    "set up, healthy, and improving over time. Type `evo` plus a "
    "subcommand and I'll handle it directly, out of the conversation "
    "you're having with the bot.\n"
)


def _next_step_line(wizard_done: bool) -> str:
    if wizard_done:
        return (
            "Quickest next step: `evo better` — I'll surface the single "
            "most valuable thing to do on this bot right now."
        )
    return (
        "Quickest next step: `evo wizard` — a short setup conversation so "
        "I know who you are and what you want this bot to do. Re-runnable "
        "any time you want to redo it."
    )


def render(
    *,
    role: Role,
    bot_id: str,
    network: dict[str, Any],
    channel: str | None,
    sender_external_id: str | None,
) -> DispatchResult:
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    user_key = derive_user_key(
        network,
        bot_id=bot_id,
        channel=channel,
        sender_external_id=sender_external_id,
    )
    wizard_done = _wizard_state.is_onboarded(shared_dir, bot_id, user_key)

    body = (
        _DESCRIPTION
        + "\n"
        + _next_step_line(wizard_done)
        + "\n\n"
        + "For the full menu, run `evo help`."
    )

    return DispatchResult(
        subcommand="evo",
        role=role,
        mode="speak",
        system_append=(
            "IMPORTANT: The user has typed `evo`. "
            "Respond ONLY with the following message, verbatim. "
            "Do not add commentary, framing, or any additional text:\n\n"
            + body
        ),
        direct_send_message=body,
        notes=[
            f"bare evo → orientation "
            f"(wizard_done={wizard_done}, role={role})"
        ],
    )

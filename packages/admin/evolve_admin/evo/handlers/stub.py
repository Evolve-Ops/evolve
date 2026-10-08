"""Stub handlers for subcommands not yet implemented.

These return a "coming soon" message that points the user at the spec and at
the most useful adjacent thing they CAN do today (the admin dashboard, or
``evo better``).
"""

from __future__ import annotations

from typing import Any

from ..dispatch import DispatchResult
from ..identity import Role


def render_wizard_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    msg = (
        "**evo wizard — coming soon**\n\n"
        "The setup wizard is being built. When it lands, running `evo wizard` "
        "will walk you through a short conversation about who you are and what "
        "you want this bot to do, and surface the platform's main capabilities.\n\n"
        "For now, try `evo better` to get a recommendation from the Better "
        "Engine."
    )
    return _speak(msg, "wizard", role)


def render_guide_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    msg = (
        "**evo guide — coming soon**\n\n"
        "When the wizard ships, `evo guide` will let you author or edit a "
        "short note for any other people using this bot — what it's for, how "
        "to use it, what to ask. The bot will read it at every session start "
        "so it stays on-mission."
    )
    return _speak(msg, "guide", role)


def render_apps_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    msg = (
        "**evo apps — coming soon**\n\n"
        "An interactive gallery browser from inside the chat. For now, the "
        "admin dashboard's Apps tab is the place to install apps from the "
        "gallery, and `evo better` will surface install recommendations when "
        "they're a good fit."
    )
    return _speak(msg, "apps", role)


def render_profile_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    """Unused at runtime — `evo profile` is special-cased in dispatch.py so it
    has access to channel + sender_external_id (needed to derive user_key).
    This stub exists only so the registry's handler-import is well-formed."""
    return _speak(
        "Run `evo profile` to see what this bot has recorded about you, or "
        "`evo profile dnt on` to opt out of profile-building entirely.",
        "profile",
        role,
    )


def render_default_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    msg = (
        "**evo default — coming soon**\n\n"
        "When the wizard ships, `evo default <name>` will let you pick what "
        "bare `evo` does for you on this bot. Right now bare `evo` always "
        "resolves to a Better Engine recommendation."
    )
    return _speak(msg, "default", role)


def render_better_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    """Unused in v1 — bare ``evo`` and ``evo better`` route through the
    plugin's legacy direct-Telegram path. Kept here so the registry's handler
    field resolves cleanly if anyone calls into it."""
    msg = (
        "Run `evo` or `evo better` and I'll fetch the top Better Engine "
        "recommendation for this bot."
    )
    return _speak(msg, "better", role)


def render_claim_stub(*, role: Role, bot_id: str, args: str, network: dict[str, Any]) -> DispatchResult:
    """Unused at runtime — `evo claim` is special-cased in dispatch.py so it
    has access to the network dict for mutation + persistence. This stub
    handler exists only so the registry's handler-import is well-formed."""
    return _speak(
        "Run `evo claim <passphrase>` to identify yourself as admin or primary.",
        "claim",
        role,
    )


def _speak(message: str, name: str, role: Role) -> DispatchResult:
    return DispatchResult(
        subcommand=name,
        role=role,
        mode="speak",
        system_append=(
            f"IMPORTANT: The user has typed `evo {name}`. "
            "Respond ONLY with the following message, verbatim. "
            "Do not add commentary, framing, or any additional text:\n\n"
            + message
        ),
        direct_send_message=message,
    )

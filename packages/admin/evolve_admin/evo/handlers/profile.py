"""``evo profile`` handler — view + DNT toggle.

Spec: docs/spec-user-profile-2026-05-07.md §D5, §D5b.

Three subforms parsed out of ``args``:

  * ``""`` (or ``"view"``) — render the user's profile sections back to
    them in chat. Reads bot-side; admin sees the contents only because
    we render them into the LLM's system_append, never into the admin UI.

  * ``"dnt on"`` — flip ``tracking_enabled = false`` on the user's
    profile, wipe all section content + Audit Log, and persist. The
    inferrer hook short-circuits before any LLM call for users with
    DNT on, so token spend stops.

  * ``"dnt off"`` — flip ``tracking_enabled = true`` and persist. No
    backfill from past observations — DNT is a forward-looking switch.

Conversational edits ("remove the marathon training note", "add that
I'm vegetarian") are an additional path in the spec but require the bot
LLM to recognize the pattern and call a tool. That's a separate slice;
this handler ships view + DNT only and tells the user to expect inline
edits soon.

Special-cased in dispatch.py because the handler needs ``channel`` and
``sender_external_id`` to derive the user_key — same reason ``claim``
and ``wizard`` are special-cased.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from evolve_util import now_iso_offset as _now_iso

from ..dispatch import DispatchResult
from ..identity import Role, derive_user_key

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry — called from dispatch.py
# ─────────────────────────────────────────────────────────────────────────────


def handle(
    network: dict[str, Any],
    *,
    bot_id: str,
    channel: str | None,
    sender_external_id: str | None,
    args: str,
    role: Role,
) -> DispatchResult:
    """Resolve an ``evo profile [args]`` invocation."""
    args_clean = (args or "").strip().lower()

    if not channel or sender_external_id is None:
        return _speak(
            _msg_cant_identify(),
            role=role,
        )

    # Resolve identity + bot location
    try:
        from evolve_admin.config import bot_home, get_bot_user
    except ImportError as e:
        logger.warning("evo profile: cannot import config helpers: %s", e)
        return _speak(_msg_internal_error(), role=role)

    user_key = derive_user_key(
        network, bot_id=bot_id, channel=channel, sender_external_id=sender_external_id,
    )
    home = bot_home(bot_id, network)
    bot_user = get_bot_user(bot_id, network)

    if args_clean in ("", "view"):
        return _handle_view(home=home, user_key=user_key, role=role)

    if args_clean in ("dnt on", "dnt-on", "dnt=on"):
        return _handle_dnt_on(
            home=home, user_key=user_key, bot_id=bot_id, bot_user=bot_user, role=role
        )

    if args_clean in ("dnt off", "dnt-off", "dnt=off"):
        return _handle_dnt_off(
            home=home, user_key=user_key, bot_id=bot_id, bot_user=bot_user, role=role
        )

    return _speak(_msg_unknown_subform(args_clean), role=role)


# ─────────────────────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────────────────────


def _handle_view(*, home: Path, user_key: str, role: Role) -> DispatchResult:
    from ..wizard.user_profile_writer import read_user_profile_from_bot

    profile = read_user_profile_from_bot(bot_home=home, user_key=user_key)
    if profile is None:
        return _speak(_msg_no_profile(), role=role)

    if not profile.tracking_enabled:
        return _speak(_msg_dnt_active(), role=role)

    if not profile.has_content():
        return _speak(_msg_empty_profile(), role=role)

    body = _render_profile_body(profile)
    return _speak(body, role=role)


def _render_profile_body(profile) -> str:
    from user_profile import USER_PROFILE_SECTIONS

    lines: list[str] = ["**What I've recorded about you**", ""]
    for section in USER_PROFILE_SECTIONS:
        content = (profile.sections.get(section) or "").strip()
        if not content:
            continue
        lines.append(f"**{section}**")
        lines.append(content)
        lines.append("")

    lines.append("---")
    lines.append(
        "Inline edits (\"remember that I'm vegetarian\", \"forget the marathon "
        "training note\") are coming in the next update. For now, run "
        "`evo profile dnt on` to stop me from learning more, or "
        "`evo profile dnt off` to resume."
    )
    return "\n".join(lines).rstrip()


# ─────────────────────────────────────────────────────────────────────────────
# DNT on / off
# ─────────────────────────────────────────────────────────────────────────────


def _handle_dnt_on(
    *,
    home: Path,
    user_key: str,
    bot_id: str,
    bot_user: str,
    role: Role,
) -> DispatchResult:
    from user_profile import (
        AUDIT_LOG_SECTION,
        UserProfile,
        UserProfileFrontmatter,
        wipe_for_dnt,
    )
    from ..wizard.user_profile_writer import (
        read_user_profile_from_bot,
        write_user_profile_via_sudo,
    )

    profile = read_user_profile_from_bot(bot_home=home, user_key=user_key)
    if profile is None:
        # No file yet — create an empty DNT-on profile so the inferrer
        # short-circuits the next time it runs.
        profile = UserProfile(
            frontmatter=UserProfileFrontmatter(
                user_key=user_key, bot_id=bot_id, tracking_enabled=False
            ),
        )
    else:
        if not profile.tracking_enabled:
            return _speak(_msg_dnt_already_on(), role=role)
        profile.frontmatter.tracking_enabled = False
        wipe_for_dnt(profile)

    # Audit-log the toggle. ``wipe_for_dnt`` cleared any previous Audit
    # Log content; this entry is the new history-of-one.
    profile.sections[AUDIT_LOG_SECTION] = (
        f"- {_now_iso()} (dnt) tracking_enabled set to false"
    )

    try:
        write_user_profile_via_sudo(profile, bot_home=home, bot_user=bot_user)
    except Exception as e:  # noqa: BLE001
        logger.warning("evo profile dnt on: write failed for %s: %s", user_key, e)
        return _speak(_msg_dnt_write_failed("on"), role=role)

    return _speak(_msg_dnt_on_confirmed(), role=role)


def _handle_dnt_off(
    *,
    home: Path,
    user_key: str,
    bot_id: str,
    bot_user: str,
    role: Role,
) -> DispatchResult:
    from user_profile import AUDIT_LOG_SECTION
    from ..wizard.user_profile_writer import (
        read_user_profile_from_bot,
        write_user_profile_via_sudo,
    )

    profile = read_user_profile_from_bot(bot_home=home, user_key=user_key)
    if profile is None:
        # No file yet — there's nothing to flip back on. The inferrer
        # treats absence as ``tracking_enabled=True`` already, so this
        # is a true no-op rather than "already off". Don't write a
        # phantom profile file.
        return _speak(_msg_dnt_already_off(), role=role)

    if profile.tracking_enabled:
        return _speak(_msg_dnt_already_off(), role=role)

    profile.frontmatter.tracking_enabled = True
    existing_audit = (profile.sections.get(AUDIT_LOG_SECTION) or "").strip()
    new_audit = f"- {_now_iso()} (dnt) tracking_enabled set to true"
    profile.sections[AUDIT_LOG_SECTION] = (
        f"{existing_audit}\n{new_audit}" if existing_audit else new_audit
    )

    try:
        write_user_profile_via_sudo(profile, bot_home=home, bot_user=bot_user)
    except Exception as e:  # noqa: BLE001
        logger.warning("evo profile dnt off: write failed for %s: %s", user_key, e)
        return _speak(_msg_dnt_write_failed("off"), role=role)

    return _speak(_msg_dnt_off_confirmed(), role=role)


# ─────────────────────────────────────────────────────────────────────────────
# User-facing messages
# ─────────────────────────────────────────────────────────────────────────────


def _msg_no_profile() -> str:
    return (
        "**No profile yet**\n\n"
        "I haven't recorded anything about you yet. Once we've had a few "
        "conversations, this will start filling in. You can run `evo profile dnt on` "
        "any time to stop me from learning."
    )


def _msg_empty_profile() -> str:
    return (
        "**Profile exists, but no facts yet**\n\n"
        "The file's there but nothing's been recorded yet. Run `evo profile dnt on` "
        "to opt out of profile-building entirely."
    )


def _msg_dnt_active() -> str:
    return (
        "**Profile tracking is OFF (DNT)**\n\n"
        "I'm not learning anything about you right now, and the previous "
        "profile was wiped when DNT was turned on.\n\n"
        "To resume, run `evo profile dnt off`. (No backfill — I start "
        "fresh from the next conversation.)"
    )


def _msg_dnt_on_confirmed() -> str:
    return (
        "**DNT is now ON**\n\n"
        "Your existing profile content has been wiped, and I won't learn "
        "anything new from our conversations. The bot still works normally; "
        "I just won't keep notes.\n\n"
        "Run `evo profile dnt off` to turn this back on later."
    )


def _msg_dnt_off_confirmed() -> str:
    return (
        "**DNT is now OFF**\n\n"
        "I'll resume building your profile from new conversations going "
        "forward. Past conversations under DNT aren't reconstructed — "
        "starting fresh.\n\n"
        "You can review what I've recorded any time with `evo profile`, "
        "or turn DNT back on with `evo profile dnt on`."
    )


def _msg_dnt_already_on() -> str:
    return (
        "**DNT was already on**\n\n"
        "Nothing to change. Run `evo profile dnt off` to turn it off."
    )


def _msg_dnt_already_off() -> str:
    return (
        "**DNT was already off**\n\n"
        "Profile tracking is already active. Run `evo profile` to see "
        "what's been recorded."
    )


def _msg_dnt_write_failed(direction: str) -> str:
    return (
        f"**Couldn't toggle DNT {direction}**\n\n"
        "Something went wrong writing your profile file. The state is "
        "unchanged. Ask your pod admin to check the logs."
    )


def _msg_unknown_subform(args: str) -> str:
    return (
        f"**Unknown evo profile form: `{args}`**\n\n"
        "Supported forms:\n"
        "  * `evo profile` — show what I've recorded about you\n"
        "  * `evo profile dnt on` — stop me from learning, wipe existing notes\n"
        "  * `evo profile dnt off` — resume learning"
    )


def _msg_cant_identify() -> str:
    return (
        "I can't tell who you are on this channel. `evo profile` needs to "
        "know which user is asking. Try again from a direct chat with the bot."
    )


def _msg_internal_error() -> str:
    return (
        "Something went wrong handling that command. Ask your pod admin to "
        "check the logs."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Speak wrapper
# ─────────────────────────────────────────────────────────────────────────────


def _speak(message: str, *, role: Role) -> DispatchResult:
    return DispatchResult(
        subcommand="profile",
        role=role,
        mode="speak",
        system_append=(
            "IMPORTANT: The user has typed `evo profile`. "
            "Respond ONLY with the following message, verbatim. "
            "Do not add commentary, framing, or any additional text:\n\n"
            + message
        ),
        direct_send_message=message,
    )

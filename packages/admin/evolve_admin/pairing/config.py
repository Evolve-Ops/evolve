"""Per-channel pairing UI + validation config.

One source of truth for the per-channel surface area used by the
admin-UI pairing wizard, the install-wizard Done-screen handoff, and
the Overview tile chip. Adding a new channel means adding one entry
here, not touching three call sites.

Each entry describes:

  label              Channel name for prose ("Telegram", "Slack", …)
  id_label           UI label for the ID input field
  id_format_hint     Help text shown under the input
  id_validator       Regex (compiled) that a valid bare ID matches
  discovery_method   How the operator gets their ID/code:
                       "deeplink"  — Telegram, t.me/<bot>?start=…
                       "dm_bot"    — Slack/Discord/WhatsApp, send any DM
  deeplink_template  Format string with {bot_username}/{token} placeholders
                     (None for non-deeplink channels)
  open_button_label  Text on the "open the bot" button in the modal

The "primary input" in the wizard is the *pairing code* the bot sent
back to the operator, not the raw ID — codes are unique per pending
request and let us auto-resolve identity (id + name + username) from
the credentials/<channel>-pairing.json file. The id_validator is for
the secondary "type your ID directly" path.

OC stores pairing state per-bot at
``<bot_home>/.openclaw/credentials/<channel>-pairing.json`` (pending
requests) and ``<channel>-default-allowFrom.json`` (approved IDs). See
``routes_bot_users`` for the read/write helpers we reuse.
"""

from __future__ import annotations

import re
from typing import Optional


class ChannelConfig:
    """One row in the per-channel pairing table.

    Attributes are read by both the Python backend (routes_pairing,
    install wizard) and serialized into the admin-UI bundle via
    ``to_ui_dict`` so the JS modal can render the right copy without
    forking the table. Keeping the JS side a dumb consumer of the
    Python truth source avoids drift across the three call sites.
    """

    __slots__ = (
        "channel",
        "label",
        "id_label",
        "id_format_hint",
        "_id_validator_pattern",
        "_id_validator",
        "discovery_method",
        "deeplink_template",
        "open_button_label",
    )

    def __init__(
        self,
        channel: str,
        label: str,
        id_label: str,
        id_format_hint: str,
        id_validator_pattern: str,
        discovery_method: str,
        deeplink_template: Optional[str],
        open_button_label: str,
    ) -> None:
        if discovery_method not in ("deeplink", "dm_bot"):
            raise ValueError(
                f"unknown discovery_method {discovery_method!r} for {channel}"
            )
        if discovery_method == "deeplink" and not deeplink_template:
            raise ValueError(
                f"discovery_method=deeplink requires deeplink_template ({channel})"
            )
        self.channel = channel
        self.label = label
        self.id_label = id_label
        self.id_format_hint = id_format_hint
        self._id_validator_pattern = id_validator_pattern
        self._id_validator = re.compile(id_validator_pattern)
        self.discovery_method = discovery_method
        self.deeplink_template = deeplink_template
        self.open_button_label = open_button_label

    def validate_id(self, value: str) -> bool:
        """Cheap format check for the "type your ID directly" path.

        Returns True iff ``value`` matches the channel's ID format.
        This catches typos and obvious copy/paste mistakes; it does
        NOT verify the ID actually exists on the channel side — that
        happens implicitly when the bot's gateway sees a real DM from
        that ID and writes the pairing request.
        """
        if not isinstance(value, str):
            return False
        return bool(self._id_validator.fullmatch(value.strip()))

    def deeplink_for(
        self,
        bot_username: Optional[str],
        token: Optional[str] = None,
    ) -> Optional[str]:
        """Build the per-channel "open bot DM" URL, or None.

        For Telegram, returns ``https://t.me/<bot_username>?start=<token>``
        — the deeplink opens the bot conversation in Telegram desktop
        or mobile with one tap. The ``start`` param is decorative
        (OC's gateway doesn't capture it into pairing meta — verified
        2026-06-01 in dist/bot-iSDqdz0Y.js:1380), but harmless and
        useful for telemetry.

        For Slack/Discord/WhatsApp, returns None — those platforms
        either don't support deeplinks (Slack) or the UX is just
        "search for the bot by name." The modal renders a copyable
        bot-handle string in those cases.
        """
        if not self.deeplink_template:
            return None
        if not bot_username:
            return None
        # Template uses str.format placeholders; missing token is OK
        # (renders as empty string after stripping).
        rendered = self.deeplink_template.format(
            bot_username=bot_username.lstrip("@"),
            token=token or "",
        )
        # If the token slot was empty, strip the trailing query param
        # so the URL stays clean.
        if rendered.endswith("?start="):
            rendered = rendered[: -len("?start=")]
        return rendered

    def to_ui_dict(self) -> dict:
        """Serializable shape for the admin-UI bundle.

        The JS modal reads these fields verbatim — keep the keys in
        sync with ``index.html``'s ``_PAIR_CFG`` consumer.
        """
        return {
            "channel": self.channel,
            "label": self.label,
            "id_label": self.id_label,
            "id_format_hint": self.id_format_hint,
            "id_validator_pattern": self._id_validator_pattern,
            "discovery_method": self.discovery_method,
            "has_deeplink": bool(self.deeplink_template),
            "open_button_label": self.open_button_label,
        }


# ── Channel table ───────────────────────────────────────────────────────

# Order matters for UI display where we iterate (e.g., the install
# wizard's Done screen if/when we ever offer multi-channel pairing).
# Today the operator picks one channel per bot in the install wizard,
# so the order is mostly cosmetic — Telegram first because it's the
# dominant test-pod channel.
_CHANNELS: list[ChannelConfig] = [
    ChannelConfig(
        channel="telegram",
        label="Telegram",
        id_label="Telegram user ID",
        id_format_hint=(
            "6-12 digit number. If you don't know yours, DM the bot "
            "and paste the code it replies with."
        ),
        # Telegram user IDs are 32-bit ints today but the API will
        # widen to 64-bit per Telegram's roadmap. We bound 6-12 to
        # cover both eras without admitting unrelated numeric noise.
        id_validator_pattern=r"^\d{6,12}$",
        discovery_method="deeplink",
        # The ``start`` query param IS captured by Telegram and
        # delivered as ``/start <param>`` in the bot's first message,
        # but OC's enforceTelegramDmAccess (bot-iSDqdz0Y.js:1380)
        # only reads msg.from.{id,username,first_name,last_name} into
        # pairing meta — the text body is dropped. So the token is
        # decorative for one-tap opening; identity still resolves via
        # the pairing code the operator pastes back.
        deeplink_template="https://t.me/{bot_username}?start={token}",
        open_button_label="Open in Telegram",
    ),
    ChannelConfig(
        channel="slack",
        label="Slack",
        id_label="Slack member ID",
        id_format_hint=(
            "Starts with U (e.g. U01ABCDE2FG). Find it in your "
            "Slack profile → ⋮ menu → Copy member ID."
        ),
        # Slack member IDs are U or W (workspace-bot edge case) +
        # uppercase alphanumeric; 9-11 chars in practice.
        id_validator_pattern=r"^[UW][A-Z0-9]{8,10}$",
        discovery_method="dm_bot",
        deeplink_template=None,
        open_button_label="Copy bot handle",
    ),
    ChannelConfig(
        channel="discord",
        label="Discord",
        id_label="Discord user ID",
        id_format_hint=(
            "17-19 digit snowflake. Enable Developer Mode in "
            "Settings → Advanced, then right-click yourself → Copy User ID."
        ),
        # Discord snowflake IDs are 17-19 digits (and growing — the
        # range will keep shifting upward as Discord's epoch advances).
        id_validator_pattern=r"^\d{17,19}$",
        discovery_method="dm_bot",
        deeplink_template=None,
        open_button_label="Copy bot handle",
    ),
    ChannelConfig(
        channel="whatsapp",
        label="WhatsApp",
        id_label="WhatsApp phone number",
        id_format_hint=(
            "E.164 format with country code, e.g. +14155551234. "
            "It's your own phone number — no need to look anything up."
        ),
        # E.164 caps phone numbers at 15 digits; we floor at 10 to
        # admit US-style numbers without the leading 1.
        id_validator_pattern=r"^\+\d{10,15}$",
        discovery_method="dm_bot",
        deeplink_template=None,
        open_button_label="Copy bot handle",
    ),
]

_BY_CHANNEL: dict[str, ChannelConfig] = {c.channel: c for c in _CHANNELS}


def get_channel_config(channel: str) -> Optional[ChannelConfig]:
    """Look up the config row for one channel id, or None if unknown."""
    if not isinstance(channel, str):
        return None
    return _BY_CHANNEL.get(channel.lower())


def known_channels() -> list[str]:
    """The channel ids this module supports, in display order."""
    return [c.channel for c in _CHANNELS]


def all_ui_dicts() -> list[dict]:
    """All channel rows in UI shape, for the admin-UI bundle."""
    return [c.to_ui_dict() for c in _CHANNELS]

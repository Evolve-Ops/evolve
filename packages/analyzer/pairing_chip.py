"""Compute Overview-tile chip(s) for unfinished messaging pairing.

Lights when a bot has a configured messaging channel but no operator
is paired into it yet, OR when the bot's primary user is recorded but
their channel ID hasn't been approved into the bot's allowFrom.

The chip is the "🔗 Pair messaging ID" affordance that opens the
admin-UI pairing wizard (modal). It replaces the wizard's broken
"End-to-end echo" Check 4, which promised something the wizard
couldn't deliver: a way to enter the pairing code OC sent the
operator.

"Configured channel" signal: the bot has either a ``<channel>-pairing
.json`` or a ``<channel>-default-allowFrom.json`` file in its
``.openclaw/credentials/`` directory. OC creates the first on the
operator's first DM and the second on first approval, so one of them
existing means OC has handled at least one pairing action for that
channel — which only happens when the channel is configured. Same
signal the per-bot Users page uses (``routes_bot_users._read_per_channel``).

"Paired" signal: the bot's ``<channel>-default-allowFrom.json``
contains at least one ID. Until then the bot can receive DMs but
rejects them all — exactly the cul-de-sac this chip guides operators
out of.

This module lives in ``packages/analyzer/`` rather than ``evolve_admin/``
because ``tile_metrics.py`` consumes it from the same package —
keeping the chip computation here avoids a cross-package import
that the analyzer wouldn't otherwise need.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from evolve_config import bot_home as _resolve_bot_home


log = logging.getLogger(__name__)


# Channels that support pairing in the admin-UI wizard. Mirrors the
# canonical list in ``evolve_admin.pairing.config.known_channels`` —
# kept duplicated here to avoid a cross-package import. Adding a new
# channel means adding it both places.
_PAIRING_CHANNELS: tuple[str, ...] = ("telegram", "slack", "discord", "whatsapp")

# Channel labels for chip prose. The chip says "🔗 Pair telegram ID"
# when one channel is unpaired, falling back to "🔗 Pair messaging ID"
# when we can't pin down the channel (shouldn't normally happen, but
# defensive).
_CHANNEL_LABEL = {
    "telegram": "Telegram",
    "slack": "Slack",
    "discord": "Discord",
    "whatsapp": "WhatsApp",
}


def compute_pairing_chips(
    network: Optional[dict],
    bot_id: str,
) -> list[dict]:
    """Return chip dicts for any unfinished pairing on this bot.

    Reads each channel's pairing/allowFrom files via the same
    direct-then-sudo-fallback pattern as the Users page. Empty list
    means "no pairing work needed" — either the bot has no configured
    channels, or every configured channel has at least one approved
    operator.

    The chip's ``nav`` field uses the ``pair:<bot>:<channel>`` prefix
    convention; the frontend's ``chipNav`` helper routes that to
    ``openPairingWizard(bot, channel)``.
    """
    if not isinstance(network, dict) or not bot_id:
        return []
    try:
        bot_home_path = _resolve_bot_home(bot_id, network)
    except Exception:  # noqa: BLE001
        return []
    cred_dir = bot_home_path / ".openclaw" / "credentials"
    bot = (network.get("bots") or {}).get(bot_id) or {}
    primary = bot.get("primary_user") or {}
    primary_name = primary.get("name") or "primary user"
    primary_ext = primary.get("external_ids") or {}

    # openclaw.json's channels block is the source of truth for "is this
    # channel configured?" — credential files outlive channel removal
    # (skill uninstall, dmPolicy flip), and without this gate the chip
    # fires on leftover state forever. Fall back to credential-file
    # signal when openclaw.json is unreadable so we don't accidentally
    # hide real pairing affordances on a transient read failure.
    oc_config = _read_json(bot_home_path / ".openclaw" / "openclaw.json")
    use_channel_config = isinstance(oc_config, dict) and bool(oc_config)
    configured_channels = (
        (oc_config or {}).get("channels") or {}
        if isinstance(oc_config, dict) else {}
    )

    chips: list[dict] = []
    for ch in _PAIRING_CHANNELS:
        if use_channel_config:
            ch_config = configured_channels.get(ch)
            if not isinstance(ch_config, dict):
                continue
            if ch_config.get("enabled") is False:
                continue
        pairing_path = cred_dir / f"{ch}-pairing.json"
        allowfrom_path = cred_dir / f"{ch}-default-allowFrom.json"
        pairing_raw = _read_json(pairing_path)
        allowfrom_raw = _read_json(allowfrom_path)
        # Not configured for this channel — no chip.
        if pairing_raw is None and allowfrom_raw is None:
            continue
        approved = (allowfrom_raw or {}).get("allowFrom") or []
        approved_ids = {str(x) for x in approved if x}
        label_ch = _CHANNEL_LABEL.get(ch, ch.capitalize())

        if not approved_ids:
            # Configured but nobody paired — the urgent case. The bot
            # can receive DMs but rejects them all.
            chips.append({
                "id": f"pairing_needed_{ch}",
                "severity": "warn",
                "label": f"🔗 Pair {label_ch} ID",
                "detail": (
                    f"Open the pairing wizard to connect your "
                    f"{label_ch} ID and start chatting with this bot."
                ),
                "nav": f"pair:{bot_id}:{ch}",
            })
            continue

        # Someone is paired. Check whether the bot's recorded primary
        # user has actually paired their channel ID yet — the "admin
        # paired in install wizard, primary still needs to" case
        # flagged 2026-06-01.
        primary_id = primary_ext.get(ch)
        if primary_id and str(primary_id) not in approved_ids:
            short_name = primary_name.split()[0] if primary_name else "primary user"
            chips.append({
                "id": f"pairing_primary_missing_{ch}",
                "severity": "warn",
                "label": f"🔗 Pair {short_name}",
                "detail": (
                    f"{primary_name} (the bot's primary user) hasn't "
                    f"paired their {label_ch} ID yet. Open the pairing "
                    f"wizard to send them through."
                ),
                "nav": f"pair:{bot_id}:{ch}",
            })

    return chips


# ── File-read helpers ───────────────────────────────────────────────────


def _read_json(path: Path) -> Optional[dict]:
    """Read JSON with a sudo fallback. Returns parsed dict, ``{}`` on
    parse error, or ``None`` when the file is genuinely absent.

    Mirrors ``routes_bot_users._read_json_or_none`` — kept lean so the
    analyzer doesn't take a cross-package import for this one helper.
    The evolve user has macOS ACL read on every bot's ``.openclaw/``
    via ``deploy.set_evolve_read_acl``; sudo fallback covers bots
    deployed before that ACL existed.
    """
    text: Optional[str] = None
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        text = None
    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(path)],
                check=True, capture_output=True, text=True, timeout=5,
            )
            text = r.stdout
        except subprocess.CalledProcessError:
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}

"""evolve_admin.channels — read a bot's live messaging-channel state.

One question, answered from the bot's own ``openclaw.json``: which
channels does this bot actually have enabled right now? Used by

* the forge approval path, which declares a messaging app's connected
  channel(s) in ``requirements.integrations[]`` so the C-A4 coherence
  gate verifies the real channel state instead of refusing every
  messaging app outright (U1 activation fix, 2026-06-11 design sync);
* the briefing auto-activation hook (``briefing_activation``), which
  fires when a bot gains its first messaging channel.

The channel-id vocabulary is the coherence gate's
``MESSAGING_INTEGRATION_IDS`` data set — no provider names in logic
here (see docs/principle-* on provider literals).

The reader is injectable for tests; production reads the bot's
openclaw.json via ``skills._oc_install_common.read_oc_config`` (direct
read with the ``sudo /bin/cat`` fallback per CLAUDE.md).
"""

from __future__ import annotations

from typing import Any, Callable

# (cfg_dict | None, error | None) — the read_oc_config contract.
ConfigReader = Callable[[str], "tuple[dict[str, Any] | None, str | None]"]


def _default_reader(bot_id: str) -> tuple[dict[str, Any] | None, str | None]:
    from .skills._oc_install_common import read_oc_config

    return read_oc_config(bot_id)


def enabled_channels_from_config(cfg: dict[str, Any] | None) -> set[str]:
    """Channel ids with ``channels.<id>.enabled`` truthy in ``cfg``."""
    if not isinstance(cfg, dict):
        return set()
    channels = cfg.get("channels")
    if not isinstance(channels, dict):
        return set()
    return {
        str(name).lower()
        for name, block in channels.items()
        if isinstance(block, dict) and block.get("enabled")
    }


def enabled_messaging_channels_from_config(
    cfg: dict[str, Any] | None,
) -> set[str]:
    """Like :func:`enabled_channels_from_config`, filtered to channels
    the coherence gate recognizes as messaging-capable."""
    from .applications.coherence_pass_a import MESSAGING_INTEGRATION_IDS

    return enabled_channels_from_config(cfg) & MESSAGING_INTEGRATION_IDS


def enabled_messaging_channels(
    bot_id: str,
    *,
    read: ConfigReader | None = None,
) -> set[str]:
    """The bot's currently-enabled messaging channels, from its own
    openclaw.json. Returns an empty set when the config is unreadable —
    callers treat that the same as "no channel" (the conservative
    answer for both the gate stamp and the activation hook)."""
    cfg, _err = (read or _default_reader)(bot_id)
    return enabled_messaging_channels_from_config(cfg)

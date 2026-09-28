"""Per-channel pairing UI + validation config — a PROJECTION of the registry.

The per-channel surface area used by the admin-UI pairing wizard, the
install-wizard Done-screen handoff, and the Overview tile chip.

**Where the data lives (changed 2026-07-30, M1-B1).** The ``ChannelConfig``
class and the per-channel rows moved to ``evolve_admin.channel_registry`` —
the single declarative channel table (spec-users-meta invariant 7). This
module is now the pairing-shaped *view* over it: it selects the rows whose
``pairing`` column is populated and exposes them under the same names every
existing caller already imports. Adding a channel still means one edit, it is
just one edit in the registry rather than here.

The pairing column is deliberately NARROWER than the registry — Signal,
iMessage, SMS, email and webhook have registry rows but no pairing config,
because Evolve has no ID-pairing flow for them. ``known_channels()`` must
therefore never be treated as "all channels Evolve knows about"; ask
``channel_registry`` for that.

See ``channel_registry.ChannelConfig`` for the per-field documentation
(id_label / id_format_hint / id_validator / discovery_method /
deeplink_template / open_button_label) and for the OC pairing-state file
layout.
"""

from __future__ import annotations

from typing import Optional

from ..channel_registry import ChannelConfig  # re-exported: stable import path
from .. import channel_registry as _registry

__all__ = [
    "ChannelConfig",
    "get_channel_config",
    "known_channels",
    "all_ui_dicts",
]


def _rows() -> tuple[ChannelConfig, ...]:
    """Pairing configs in registry display order.

    Not cached: the registry is an immutable module-level tuple, so this is a
    cheap comprehension, and re-deriving keeps tests that monkeypatch the
    registry honest.
    """
    return tuple(
        spec.pairing
        for spec in _registry.pairing_channels()
        if spec.pairing is not None
    )


def get_channel_config(channel: str) -> Optional[ChannelConfig]:
    """Look up the config row for one channel id, or None if unknown.

    Returns None for registry channels that have no pairing flow (Signal,
    iMessage, …) as well as for ids the registry has never heard of — from
    the caller's point of view both mean "cannot pair on this channel".
    """
    if not isinstance(channel, str):
        return None
    spec = _registry.get(channel)
    return spec.pairing if spec is not None else None


def known_channels() -> list[str]:
    """The channel ids this module supports, in display order.

    Pairing-capable channels ONLY — see the module docstring.
    """
    return [c.channel for c in _rows()]


def all_ui_dicts() -> list[dict]:
    """All pairing rows in UI shape, for the admin-UI bundle."""
    return [c.to_ui_dict() for c in _rows()]

"""Platform detector: openclaw.json validity (Signal-emitting)."""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import openclaw_config_invalid_signal_kwargs


def detect_signal(ctx: DetectorContext) -> dict | None:
    valid = ctx.metric("openclaw_config.valid")
    if valid.value >= 1.0:
        return None
    return openclaw_config_invalid_signal_kwargs(ctx.bot_id)

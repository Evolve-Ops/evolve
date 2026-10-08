"""Platform detector: Evolve plugin loaded in bot's gateway (Signal-emitting)."""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import plugin_missing_signal_kwargs


def detect_signal(ctx: DetectorContext) -> dict | None:
    loaded = ctx.metric("plugin.loaded")
    if loaded.value >= 1.0:
        return None

    # Don't fire plugin-missing if the gateway itself is down — gateway
    # detector owns that higher-priority signal.
    gateway_up = ctx.metric("gateway.up")
    if gateway_up.value < 1.0:
        return None

    return plugin_missing_signal_kwargs(ctx.bot_id)

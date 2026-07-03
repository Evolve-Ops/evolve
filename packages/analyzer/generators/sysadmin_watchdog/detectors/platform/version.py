"""Platform detector: Evolve version drift (Signal-emitting)."""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import version_behind_signal_kwargs


DEFAULT_DAYS_THRESHOLD = 14


def detect_signal(ctx: DetectorContext) -> dict | None:
    behind = ctx.metric("version.currency_days_behind")
    if behind.confidence < 0.6:
        # Source data incomplete; don't fire on low confidence
        return None

    threshold = int(
        ctx.config.get("version_days_threshold", DEFAULT_DAYS_THRESHOLD)
    )
    if behind.value < threshold:
        return None

    return version_behind_signal_kwargs(ctx.bot_id, days_behind=int(behind.value))

"""Platform detector: macOS user account exists for the bot (Signal-emitting).

Ports the legacy ``HealthCheckAdapter`` users-check signal — the only
useful entry-point UI item that adapter contributed (``Team_bot_b: users
check failed`` was the canonical example because team_bot_b's logical bot_id
differs from its macOS account name).
"""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import user_missing_signal_kwargs

from evolve_config import get_bot_user as _get_bot_user


def detect_signal(ctx: DetectorContext) -> dict | None:
    exists = ctx.metric("platform.user_exists")
    if exists.value >= 1.0:
        return None
    user = _get_bot_user(ctx.bot_id)
    return user_missing_signal_kwargs(ctx.bot_id, user=user)

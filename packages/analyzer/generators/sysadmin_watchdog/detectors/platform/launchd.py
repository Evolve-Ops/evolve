"""Platform detector: bot gateway service loaded (Signal-emitting).

Platform-aware (Linux port): the underlying metric ``launchd.service_loaded``
is platform-branched in its resolver (launchctl on macOS,
``get_scheduler().status`` on Linux), so the value is truthful on both. This
detector emits a platform-CORRECT Signal off that value — a launchd-named
signal with a ``launchctl load`` runbook on macOS, a systemd-named signal
with a ``systemctl enable --now`` runbook on Linux. A pod is one OS, never
both, so the two signatures never co-fire for the same bot; keeping them
distinct stops a Linux pod from emitting a launchd-named ``launchd_not_loaded``
Signal with a macOS-only recovery command (the false signal bite 5 kills).
"""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import (
    launchd_not_loaded_signal_kwargs,
    systemd_not_loaded_signal_kwargs,
)

from platform_profile import get_profile


def detect_signal(ctx: DetectorContext) -> dict | None:
    loaded = ctx.metric("launchd.service_loaded")
    if loaded.value >= 1.0:
        return None
    if get_profile().name == "macos":
        return launchd_not_loaded_signal_kwargs(ctx.bot_id)
    return systemd_not_loaded_signal_kwargs(ctx.bot_id)

"""Platform detector: gateway up/down (Signal-emitting).

Gateway-down used to emit two distinct Proposals (Investigation + WorkflowInstruction).
Both were alert-shaped (detect a broken substrate condition + attach a runbook),
so per the alerts/signal-store consolidation we now emit a single
``gateway_down`` Signal that escalates from warn → alert when the failure is chronic.
The signature stays stable across the escalation so the Alerts UI shows one incident.
"""

from __future__ import annotations

from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.signals import gateway_down_signal_kwargs


# Default thresholds; generator's config can override via ctx.config
DEFAULT_FAILURE_THRESHOLD = 3  # incidents in the last 24h
DEFAULT_CHRONIC_HOURS = 6  # consecutive-hours threshold treated as chronic


def detect_signal(ctx: DetectorContext) -> dict | None:
    gateway_up = ctx.metric("gateway.up")
    if gateway_up.value >= 1.0:
        return None

    failures = ctx.metric("gateway.consecutive_failures_24h")
    threshold = int(
        ctx.config.get("gateway_failure_threshold", DEFAULT_FAILURE_THRESHOLD)
    )
    if failures.value < threshold:
        return None

    chronic_hours = int(
        ctx.config.get("chronic_hours_threshold", DEFAULT_CHRONIC_HOURS)
    )
    return gateway_down_signal_kwargs(
        ctx.bot_id,
        consecutive_failures=int(failures.value),
        chronic=failures.value >= chronic_hours,
    )

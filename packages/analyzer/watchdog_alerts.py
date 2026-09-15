"""watchdog_alerts — shared helpers for emitting operational WatchdogEvents.

Detectors that observe a problem but do not have a concrete fix to propose
emit a WatchdogEvent on the operator's Alerts surface rather than filing a
non-actionable Investigation proposal. Originally introduced in heal.py
(PR #453); extracted here so test_runner.py and other detectors can reuse
the same primitives without depending on heal.

The two functions:

- ``emit_watchdog_alert(...)`` writes a new WatchdogEvent if one of the same
  ``(event_type, bot_id)`` does not already exist within the dedup window.
  Returns ``True`` if written, ``False`` if suppressed.

- ``recent_watchdog_event_exists(...)`` is the dedup primitive on its own,
  exposed for callers that want to gate side-effects (e.g. notifications)
  on whether the event was actually fresh.

The watchdog event store lives at ``{shared_dir}/watchdog/{YYYY-MM-DD}.jsonl``
and is read by ``/api/arbiter/health/watchdog-events`` for the Alerts page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_util import now_iso as _now_iso


def recent_watchdog_event_exists(
    shared_dir: Path,
    event_type: str,
    bot_id: str | None,
    window_hours: float,
    *,
    details_match: dict | None = None,
) -> bool:
    """True if a same-type same-scope WatchdogEvent already exists in
    ``[now - window_hours, now]``. Suppresses repeat alerts for an issue
    that is still firing.

    ``details_match`` narrows the dedup key beyond ``(event_type, bot_id)``.
    test_failure_pattern, for example, fires per-application within a bot —
    pass ``{"application_id": "demo_app"}`` so two different apps on the
    same bot each get their own alert.
    """
    from generators.evolve_watchdog.events import read_events_range

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=window_hours)
    try:
        for ev in read_events_range(shared_dir, start, now):
            if ev.event_type != event_type:
                continue
            if ev.bot_id != bot_id:
                continue
            if details_match and not all(
                (ev.details or {}).get(k) == v for k, v in details_match.items()
            ):
                continue
            return True
    except (OSError, ValueError):
        return False
    return False


def emit_watchdog_alert(
    *,
    shared_dir: Path,
    event_type: str,
    severity: str,
    bot_id: str | None,
    details: dict,
    dedup_window_hours: float,
    log_prefix: str = "evolve",
    dedup_details_keys: tuple[str, ...] = (),
) -> bool:
    """Emit a deduped operational WatchdogEvent.

    Returns True if a new event was written, False if a same-type same-scope
    event already exists in the dedup window. The watchdog endpoint at
    ``/api/arbiter/health/watchdog-events`` reads these for the Alerts page.

    ``log_prefix`` controls the label on the diagnostic print so callers
    show up distinguishably in the daemon log (e.g. ``"evolve/heal"`` vs
    ``"evolve/test_runner"``).

    ``dedup_details_keys`` extends the dedup key beyond ``(event_type, bot_id)``
    by also matching the listed keys from ``details``. test_failure_pattern
    passes ``("application_id",)`` so that two different apps failing on
    the same bot each get their own alert rather than masking each other.
    """
    details_match = (
        {k: details.get(k) for k in dedup_details_keys}
        if dedup_details_keys
        else None
    )
    if recent_watchdog_event_exists(
        shared_dir,
        event_type,
        bot_id,
        dedup_window_hours,
        details_match=details_match,
    ):
        return False

    from schema.watchdog import WatchdogEvent, new_watchdog_event_id
    from generators.evolve_watchdog.events import write_events

    event = WatchdogEvent(
        id=new_watchdog_event_id(),
        bot_id=bot_id,
        timestamp=_now_iso(),
        event_type=event_type,
        severity=severity,
        details=details,
    )
    write_events([event], shared_dir=shared_dir)
    scope = bot_id if bot_id else "pod-wide"
    print(f"[{log_prefix}] emitted watchdog alert {event_type} ({scope}) — {event.id}")
    return True

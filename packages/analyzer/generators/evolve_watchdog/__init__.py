"""generators.evolve_watchdog — Meta-guardian (L6 active monitoring).

Spec §7. Monitors the RSI system itself — other generators' track records,
proposal volumes, calibration drift, verification reliability — and
emits proposals to throttle/pause/rollback when patterns indicate
degradation.
"""

from generators.evolve_watchdog.events import (
    WatchdogEventStore,
    write_events,
    read_events_range,
)
from generators.evolve_watchdog.observe import (
    EvolveWatchdogContext,
    observe,
)

__all__ = [
    "EvolveWatchdogContext",
    "WatchdogEventStore",
    "observe",
    "read_events_range",
    "write_events",
]

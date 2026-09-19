"""generators.app_permission_drift.observe — Detector entry point.

Reads firing ``app_permission_drift`` Signals from the
``app_manifest_monitor`` producer and dispatches each to a per-kind
factory in ``signal_proposals.py``.

Per-bot context: one bot per invocation, signals filtered by bot_id.
Per-finding fan-out happens at the monitor (one Signal per finding),
so this module's only job is iterating Signals and calling the right
factory.

Mirrors the auth_drift_filler shape — see that module's observe.py for
the canonical pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators._signal_consumer import iter_firing_signals
from generators.app_permission_drift.signal_proposals import make_proposals
from schema.proposal import Proposal


GENERATOR_ID = "app_permission_drift"
DIMENSION = "safety"

# Producer + signal type we consume. Pinned constants so a typo in the
# monitor's PRODUCER (or a future rename) shows up as a test failure
# rather than silently emitting zero proposals.
APP_MANIFEST_MONITOR_PRODUCER = "app_manifest_monitor"
CONSUMED_SIGNAL_TYPE = "app_permission_drift"


@dataclass
class AppPermissionDriftContext:
    """Per-bot run context.

    The generator is per-bot — one bot per ``observe()`` call — even
    though the underlying monitor's findings span every bot. Filtering
    by ``bot_id`` here keeps the generator's outputs scoped correctly
    for the arbiter's per-bot ingestion path.
    """

    bot_id: str
    shared_dir: Path


def observe(ctx: AppPermissionDriftContext) -> list[Proposal]:
    """Emit one Proposal per firing Signal for this bot.

    Per-signal failures are swallowed — one bad payload can't torpedo
    the rest of the run. Same defensive pattern as auth_drift_filler.
    """
    proposals: list[Proposal] = []
    for sig in iter_firing_signals(
        ctx.shared_dir,
        ctx.bot_id,
        APP_MANIFEST_MONITOR_PRODUCER,
        CONSUMED_SIGNAL_TYPE,
    ):
        try:
            proposals.extend(make_proposals(sig))
        except Exception:  # noqa: BLE001 — never let one Signal break the loop
            continue
    return proposals

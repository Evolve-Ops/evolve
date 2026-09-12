"""generators.cost_spike.observe — Detector entry point.

Reads firing ``cost_spike`` Signals from the ``cost_watchdog`` producer
and dispatches each through ``make_cost_spike_proposal``. Mirrors the
cache_ttl_tuner / auth_drift_filler observe shape — the upstream
monitor has already computed the spike condition, so this generator
just templates a Proposal over the Signal payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generators.cost_spike.signal_proposals import (
    DISMISS_SIG_COST_SPIKE,
    make_cost_spike_proposal,
)
from schema.proposal import Proposal


GENERATOR_ID = "cost_spike"
DIMENSION = "cost"

COST_WATCHDOG_PRODUCER = "cost_watchdog"
CONSUMED_SIGNAL_TYPE = "cost_spike"


@dataclass
class CostSpikeContext:
    """Per-bot run context.

    ``bot_id`` filters signals to this bot; ``shared_dir`` locates the
    Signal store. No ledger/window readers — the cost_watchdog monitor
    has done that work and the comparison is already encoded in the
    Signal's ``details``.
    """

    bot_id: str
    shared_dir: Path
    # Phase A.5 — universal dismiss suppression. Skip emission when
    # the operator has dismissed this finding for this bot.
    consult_dismissals: bool = True


def observe(ctx: CostSpikeContext) -> list[Proposal]:
    """Emit one Proposal per firing cost_spike Signal for this bot.

    Per-signal failures are swallowed so one malformed payload doesn't
    torpedo the rest of the run.
    """
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    # Phase A.5 dismiss-suppression gate. Per-bot scoping flows through
    # arbiter.dismissals.is_suppressed; pod-wide entries (bot_id=None)
    # match any bot. Fail-open on read failure so a broken sidecar
    # can't suppress legitimate proposals.
    if ctx.consult_dismissals and _is_dismissed(ctx.shared_dir, ctx.bot_id):
        return []

    proposals: list[Proposal] = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=COST_WATCHDOG_PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        if getattr(sig, "type", None) != CONSUMED_SIGNAL_TYPE:
            continue
        try:
            proposals.append(make_cost_spike_proposal(sig))
        except Exception:
            continue
    return proposals


def _is_dismissed(shared_dir: Path, bot_id: str) -> bool:
    """Return True if the operator has dismissed the cost-spike finding
    for this bot (or pod-wide). Fail-open on any read failure."""
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(
            shared_dir,
            signature=DISMISS_SIG_COST_SPIKE,
            bot_id=bot_id,
        )
    except Exception:
        return False

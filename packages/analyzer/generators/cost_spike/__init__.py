"""generators.cost_spike — Week-over-week cost-spike responder.

Consumes ``cost_spike`` Signals from the ``cost_watchdog`` monitor and
emits one Investigation Proposal per firing signal. Each Proposal
surfaces the comparison context (current 7d vs prior 7d, ratio) and
suggests what to check; the operator decides whether the spike is
intentional or a regression.

Migration note: this generator replaces the inline cost_spike branch
in ScoreboardAdapter, which detected the same condition and emitted a
Better Engine recommendation directly without going through Signals or
Proposals. The new flow preserves the surface (operators still see a
cost-spike card on the dashboard via ProposalReaderAdapter) but
inherits the full L1/L2 lifecycle and ``motivating_signals[]``
traceability.
"""

from generators.cost_spike.observe import (
    CostSpikeContext,
    observe,
)

__all__ = ["CostSpikeContext", "observe"]

"""investigation — Shared toolkit for the smarter-generator architecture.

Spec: internal/spec-smarter-generators-2026-05-28.md §"What 'investigate' is".

Generators that follow the investigate-before-propose pattern pull in
this toolkit and call into it from their observe() step. Each function
is a small pure lookup over data the rest of the system already
produces — Signals, on-disk files, ledgers, intent annotations. No new
producers; no side effects beyond what the underlying readers already
do.

The toolkit exists so adding the *next* investigating generator is
cheap. The shape is deliberate: every tool returns dataclasses /
dicts / lists, fails open (empty result rather than raising), and is
trivially injectable in tests.
"""

from investigation.peer_baseline import (
    PeerBaselineResult,
    peer_baseline,
    role_for_bot,
)
from investigation.proposal_history import (
    ProposalHistoryEntry,
    ProposalHistorySummary,
    operator_already_declined,
    proposal_history,
    summarize_history,
)
from investigation.toolkit import (
    ConfigIntent,
    CorrelatedSignal,
    FileSize,
    ManifestMention,
    config_intent,
    correlated_signals,
    file_top_contributors,
    manifest_mentions,
    recent_config_changes,
    time_series_cost_per_call,
)


__all__ = [
    "ConfigIntent",
    "CorrelatedSignal",
    "FileSize",
    "ManifestMention",
    "PeerBaselineResult",
    "ProposalHistoryEntry",
    "ProposalHistorySummary",
    "config_intent",
    "correlated_signals",
    "file_top_contributors",
    "manifest_mentions",
    "operator_already_declined",
    "peer_baseline",
    "proposal_history",
    "recent_config_changes",
    "role_for_bot",
    "summarize_history",
    "time_series_cost_per_call",
]

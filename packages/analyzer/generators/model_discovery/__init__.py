"""generators.model_discovery — discovery-based model freshness (Phase 4).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §"Freshness check rework".

Pod-wide guardian. Enumerates each credentialed provider's live model
listing, diffs it against the pod's rungs, and surfaces unknown chat-capable
models (the class that catches a new model line like claude-fable-5) as
model_discovery Signals (signal-only — spec §Addendum 12; adoption is
operator-driven from the AI Optimization Model Freshness card, not a queued
Proposal). Never edits config; a failed listing call is reported degraded,
never silently "all current".

The diff/enumerate engine lives in :mod:`model_discovery`; this package is
the generator-runner glue.
"""

from generators.model_discovery.observe import (
    ModelDiscoveryContext,
    observe,
    observe_signals,
    GENERATOR_ID,
    PRODUCER,
    SIGNAL_TYPE,
)

__all__ = [
    "ModelDiscoveryContext",
    "observe",
    "observe_signals",
    "GENERATOR_ID",
    "PRODUCER",
    "SIGNAL_TYPE",
]

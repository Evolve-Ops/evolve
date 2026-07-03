"""breakers — circuit-breaker detection and (later) enforcement.

Spec: docs/spec-circuit-breakers-2026-05-21.md

Phase 1 (this module): activity-shape detector + backtest harness.
Observe-only — no enforcement, no state writes. The detector takes
turn data and a baseline, and decides whether a candidate window
would trip an L1 cost breaker.

Phase 2+: state store, enforcement, UI, evo integration, auto-trip.
"""

from breakers.classify import (
    TurnClassification,
    classify_turn,
    is_auto_source,
)
from breakers.baseline import Baseline, compute_baseline
from breakers.detector import (
    Decision,
    DetectorConfig,
    DEFAULT_CONFIG,
    evaluate_window,
)

__all__ = [
    "Baseline",
    "compute_baseline",
    "Decision",
    "DetectorConfig",
    "DEFAULT_CONFIG",
    "evaluate_window",
    "TurnClassification",
    "classify_turn",
    "is_auto_source",
]

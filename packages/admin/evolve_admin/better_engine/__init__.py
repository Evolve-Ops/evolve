"""
better_engine — Better Engine: Tier 0 data layer + Tier 1 engine core.

Exports:
  Recommendation   — core dataclass
  BetterEngine     — main engine class
  OnboardingTask   — onboarding task dataclass
  PendingTask      — pending admin task dataclass
  now_iso          — UTC ISO timestamp helper

Key functions from sub-modules are re-exported for convenience.
"""
from __future__ import annotations

from .model import Recommendation, now_iso
from .engine import BetterEngine
from .onboarding import OnboardingTask
from .pending_tasks import PendingTask
from .storage import (
    load_recommendations,
    save_recommendations,
    load_learning,
    save_learning,
    load_getting_started,
    save_getting_started,
    load_source_freshness,
)
from .scoring import (
    freshness_score,
    compute_base_score,
    compute_priority_score,
    urgency_label,
)
from .learning import (
    bayesian_rate,
    compute_learning_weight,
    get_effective_weight,
    record_feedback,
    apply_time_decay,
)
from .snooze import (
    SNOOZE_SCHEDULE,
    snooze_recommendation,
    wake_snoozed,
)
from .portfolio import compose_portfolio
from .hints import generate_triggers, build_hints_file, write_all_hints
# generate_suggestions retired — replaced by generators/app_suggester
# (consumed via ProposalReaderAdapter alongside every other generator).

__all__ = [
    # Core model
    "Recommendation",
    "now_iso",
    # Engine
    "BetterEngine",
    # Onboarding
    "OnboardingTask",
    # Pending tasks
    "PendingTask",
    # Storage
    "load_recommendations",
    "save_recommendations",
    "load_learning",
    "save_learning",
    "load_getting_started",
    "save_getting_started",
    "load_source_freshness",
    # Scoring
    "freshness_score",
    "compute_base_score",
    "compute_priority_score",
    "urgency_label",
    # Learning
    "bayesian_rate",
    "compute_learning_weight",
    "get_effective_weight",
    "record_feedback",
    "apply_time_decay",
    # Snooze
    "SNOOZE_SCHEDULE",
    "snooze_recommendation",
    "wake_snoozed",
    # Portfolio
    "compose_portfolio",
    # Hints (contextual discovery)
    "generate_triggers",
    "build_hints_file",
    "write_all_hints",
]

"""arbiter — Proposal lifecycle machinery (L1).

Submodules:
  - state_machine: allowed transitions + enforcement
  - routing: autonomous-vs-human decision, approval-audience routing
  - ingest: schema validation, invariant checks, dedup hook
  - dedup: fingerprint similarity (L1: pass-through)
  - snapshot: before-state capture per Action kind
  - apply: Applier dispatch over Action kinds
  - snooze_wake: transitions expired snoozed proposals back to pending

See docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §5.
"""

from arbiter.state_machine import (
    IllegalTransitionError,
    allowed_transitions,
    is_legal_transition,
    transition,
)
from arbiter.routing import (
    RoutingDecision,
    is_autonomous_eligible,
    resolve_audience,
    route,
)
from arbiter.ingest import (
    IngestError,
    IngestResult,
    InvariantViolation,
    ingest,
)
from arbiter.dedup import compute_fingerprint, find_collisions, run_dedup_hook
from arbiter.ranking import (
    AUTHORITY_MAX,
    AUTHORITY_MIN,
    URGENCY_SCORE,
    ScoreBreakdown,
    compute_authority,
    score_proposal,
)
from arbiter.referee import RankedProposal, RankingResult, rank
from arbiter.conflict import detect_conflicts
from arbiter.rate_limit import BYPASS_URGENCIES, RateLimitResult, apply_rate_limit

__all__ = [
    # state_machine
    "IllegalTransitionError",
    "allowed_transitions",
    "is_legal_transition",
    "transition",
    # routing
    "RoutingDecision",
    "is_autonomous_eligible",
    "resolve_audience",
    "route",
    # ingest
    "IngestError",
    "IngestResult",
    "InvariantViolation",
    "ingest",
    # dedup
    "compute_fingerprint",
    "find_collisions",
    "run_dedup_hook",
    # ranking (L5)
    "AUTHORITY_MAX",
    "AUTHORITY_MIN",
    "URGENCY_SCORE",
    "ScoreBreakdown",
    "compute_authority",
    "score_proposal",
    # referee (L5)
    "RankedProposal",
    "RankingResult",
    "rank",
    # conflict (L5)
    "detect_conflicts",
    # rate_limit (L5)
    "BYPASS_URGENCIES",
    "RateLimitResult",
    "apply_rate_limit",
]

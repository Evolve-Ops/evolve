"""scoring — Optimizer score calculation (L4+).

Spec: docs/archive/specs/spec-rsi-layer-4-adjacency-profile-2026-04-18.md §7.

For an optimizer generator:

    score = (adoption_rate × success_rate × avg_lift) / max(cost_per_proposal, 0.01)

Guardians return ``float('inf')`` — they run regardless of record. Meta-
guardians use a composite scoring function we don't define here (L6).

L4 implements the formula for display only. Differential resourcing
(actually routing budget based on scores) ships in L5 when a second
optimizer arrives.
"""

from __future__ import annotations

import math

from schema.generator import GeneratorRecord


# ─────────────────────────────────────────────────────────────────────────────
# Score components
# ─────────────────────────────────────────────────────────────────────────────


def adoption_rate(record: GeneratorRecord) -> float:
    """Fraction of emitted proposals that were applied (auto or human)."""
    emitted = record.track_record.proposals_emitted
    applied = record.track_record.proposals_applied
    if emitted == 0:
        return 0.0
    return applied / emitted


def success_rate(record: GeneratorRecord) -> float:
    """Fraction of applied proposals that verified as successful."""
    applied = record.track_record.proposals_applied
    succeeded = record.track_record.proposals_verified_success
    if applied == 0:
        return 0.0
    return succeeded / applied


def avg_lift(record: GeneratorRecord) -> float:
    """Average metric lift across verified successes.

    L4 ships a placeholder that returns 0 — the per_verification_outcomes
    field on TrackRecord doesn't actually store lift magnitudes until L5's
    verify daemon records them. When L5 wires this up, the real calculation
    lives here.

    For scoring purposes in L4, avg_lift=0 means the score is purely
    adoption * success / cost — still useful as a relative indicator.
    """
    # Placeholder: look in record.state for a "recent_lifts" list if present
    lifts = record.state.get("recent_lifts") if record.state else None
    if not lifts or not isinstance(lifts, list):
        return 0.0
    numeric = [float(l) for l in lifts if isinstance(l, (int, float))]
    if not numeric:
        return 0.0
    return sum(numeric) / len(numeric)


def cost_per_proposal(record: GeneratorRecord) -> float:
    """Lifetime LLM cost divided by lifetime proposals emitted."""
    emitted = record.track_record.proposals_emitted
    cost = record.track_record.lifetime_cost_usd
    if emitted == 0:
        return 0.0
    return cost / emitted


# ─────────────────────────────────────────────────────────────────────────────
# compute_score
# ─────────────────────────────────────────────────────────────────────────────


def compute_score(record: GeneratorRecord) -> float:
    """Score a generator's recent performance.

    Guardians return +inf (run regardless of record).
    Optimizers with zero track record return 0.0 (will be resourced at the
    minimum floor).
    Optimizers with a track record: ``(adoption × success × avg_lift) / cost``,
    with avg_lift defaulting to 1.0 when no lift data is available yet so
    the score doesn't collapse to zero on every generator until L5.
    """
    if record.budget_policy == "duty":
        return math.inf

    if record.track_record.proposals_emitted == 0:
        return 0.0

    adoption = adoption_rate(record)
    success = success_rate(record)
    lift = avg_lift(record)
    # L4 compromise: if no lift data, use 1.0 as a neutral placeholder so
    # active-but-new optimizers show nonzero scores. L5 replaces this
    # fallback with real lift measurements.
    if lift == 0.0:
        lift = 1.0
    cost = cost_per_proposal(record)

    return (adoption * success * lift) / max(cost, 0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────


def score_components(record: GeneratorRecord) -> dict[str, float]:
    """Return the individual components — useful for debug UIs."""
    return {
        "adoption_rate": adoption_rate(record),
        "success_rate": success_rate(record),
        "avg_lift": avg_lift(record),
        "cost_per_proposal": cost_per_proposal(record),
        "score": compute_score(record),
    }

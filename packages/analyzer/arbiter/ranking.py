"""arbiter.ranking — Pure scoring for the referee.

    score = urgency × authority_factor + savings_bonus + tiebreak

The tiebreak uses age (newer slightly preferred within the same score).
``authority_factor`` is derived from a generator's track record and
bounded to [0.5, 1.5]. ``savings_bonus`` is derived from the proposal's
generator-supplied ``estimated_savings_usd`` (when set) and capped so a
high-savings improvement can outrank other improvements without
overshadowing a security_critical or operational_urgent proposal.

Note: an earlier version multiplied by a per-bot ``dimension_weight``.
That was removed in the weights deletion pass — the weight system was
an over-engineered configuration surface that didn't earn its keep.
Authority + urgency carry the ranking signal; cross-dimension preference
is handled by pause/resume of generators when needed. Savings bonus
(PR H, 2026-05-31) is a narrow exception: a Proposal claiming concrete
weekly savings should bubble up so high-leverage work doesn't drown in
the noise floor when the operator is scrolling through 70+ alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from schema.proposal import Proposal, Urgency


# Stable, fixed urgency ladder. The ordering is load-bearing; don't
# reorder without conscious intent.
URGENCY_SCORE: dict[str, int] = {
    "security_critical": 1000,
    "operational_urgent": 700,
    "cost_alert": 500,
    "substrate_warn": 300,
    "improvement": 200,
    "hygiene": 100,
    "whimsy": 10,
}


AUTHORITY_MIN = 0.5
AUTHORITY_MAX = 1.5


# Savings-bonus parameters. ``SAVINGS_BONUS_PER_DOLLAR`` is per dollar of
# weekly savings; ``SAVINGS_BONUS_CAP`` is the absolute ceiling.
#
# Calibration: a $5/wk Proposal at urgency=improvement (200) should bubble
# above other improvement-tier work but never cross an urgency tier line
# on its own. With per-dollar=20 and cap=250:
#
#   $5/wk improvement   → 200 + 100 = 300      (above pure improvement)
#   $12.5/wk improvement → 200 + 250 = 450     (saturates the cap)
#   absurd savings improvement → 200 + 250 = 450  (still < cost_alert 500)
#   cost_alert           → 500                  (untouched)
#   operational_urgent   → 700                  (untouched)
#   security_critical    → 1000                 (untouched)
#
# Keeping the cap strictly below the gap to the next urgency tier
# (cost_alert is 300 above improvement) means a runaway estimate can
# never silently outrank an urgency promotion. Operators promote
# urgency tiers via the generator catalog, not via dollar amounts.
SAVINGS_BONUS_PER_DOLLAR: float = 20.0
SAVINGS_BONUS_CAP: float = 250.0


@dataclass
class ScoreBreakdown:
    urgency: int
    authority: float
    savings_bonus: float
    tiebreak: float
    score: float

    def to_dict(self) -> dict:
        return {
            "urgency": self.urgency,
            "authority": self.authority,
            "savings_bonus": self.savings_bonus,
            "tiebreak": self.tiebreak,
            "score": self.score,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Authority factor derivation
# ─────────────────────────────────────────────────────────────────────────────


# Discount applied to successes that required operator-driven Refine
# before reaching ``succeeded``. First-shot successes still earn full
# credit; iterated ones earn ``ITERATION_DISCOUNT`` instead. Net effect:
# generators that nail proposals on the first try score higher than
# those that need rework, but iterated successes still beat outright
# failures by a wide margin.
ITERATION_DISCOUNT: float = 0.5


def compute_authority(
    *,
    verified_success: int,
    verified_failed: int,
    succeeded_after_iteration: int = 0,
) -> float:
    """Return an authority factor in [0.5, 1.5] given success/failure counts.

    Spec §4.3: ``raw = 1.0 + 0.3 × ((wins − losses) / n)``, bounded.

    ``succeeded_after_iteration`` is a subset of ``verified_success``: the
    successes that required one or more operator-driven Refine cycles
    before landing in ``succeeded``. They contribute at ``ITERATION_DISCOUNT``
    weight (default 0.5) so first-shot wins score higher than iterated
    ones. Legacy callers that don't pass this parameter behave identically
    to the original formula (no discount).
    """
    iterated = max(0, min(succeeded_after_iteration, verified_success))
    effective_wins = verified_success - (1.0 - ITERATION_DISCOUNT) * iterated
    n = max(verified_success + verified_failed, 1)
    raw = 1.0 + 0.3 * ((effective_wins - verified_failed) / n)
    return max(AUTHORITY_MIN, min(AUTHORITY_MAX, raw))


# ─────────────────────────────────────────────────────────────────────────────
# Score a single proposal
# ─────────────────────────────────────────────────────────────────────────────


def _parse_iso(raw: str) -> datetime | None:
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tiebreak(proposal: Proposal, *, now: datetime) -> float:
    """Small negative number proportional to age — newer wins."""
    created = _parse_iso(proposal.created_at)
    if created is None:
        return 0.0
    age_minutes = max(0.0, (now - created).total_seconds() / 60.0)
    return -age_minutes / 10000.0


def _savings_bonus(proposal: Proposal) -> float:
    """Return the savings-bonus contribution for a proposal.

    None / missing / non-positive → 0. Capped at ``SAVINGS_BONUS_CAP``
    so a runaway estimate can't push an improvement-tier proposal above
    an operational_urgent one.
    """
    raw = getattr(proposal, "estimated_savings_usd", None)
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0.0:
        return 0.0
    return min(value * SAVINGS_BONUS_PER_DOLLAR, SAVINGS_BONUS_CAP)


def score_proposal(
    proposal: Proposal,
    *,
    authority: float,
    now: datetime | None = None,
) -> ScoreBreakdown:
    if now is None:
        now = datetime.now(timezone.utc)
    urgency_score = URGENCY_SCORE.get(proposal.urgency, 100)
    tiebreak = _tiebreak(proposal, now=now)
    savings_bonus = _savings_bonus(proposal)
    score = urgency_score * authority + savings_bonus + tiebreak
    return ScoreBreakdown(
        urgency=urgency_score,
        authority=authority,
        savings_bonus=savings_bonus,
        tiebreak=tiebreak,
        score=score,
    )

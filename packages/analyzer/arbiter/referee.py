"""arbiter.referee — Ranking + conflict detection + rate limiting.

Spec §4.

The referee is the "Better Engine" ranking layer: given a pending
proposal set, it:

  1. Scores each by urgency × authority_factor + tiebreak
  2. Sorts high → low
  3. Annotates cross-proposal conflicts (touches, metric direction, exclusive choice)
  4. Applies the weekly rate limit
  5. Returns a ranked list + held list + per-proposal score breakdown

The referee does NOT decide conflicts — it annotates them. The UI
surfaces the conflict to the user as a tradeoff (see §2.12 "mediator
not a judge").

Note: an earlier revision multiplied by a per-bot ``dimension_weight``
read from the user's profile. That was removed in the weights deletion
pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from arbiter.conflict import detect_conflicts
from arbiter.ranking import ScoreBreakdown, compute_authority, score_proposal
from arbiter.rate_limit import RateLimitResult, apply_rate_limit
from schema.generator import GeneratorRecord
from schema.proposal import Proposal


# Callable: (generator_id) -> GeneratorRecord | None
RecordLookup = Callable[[str], "GeneratorRecord | None"]


@dataclass
class RankedProposal:
    proposal: Proposal
    rank: int
    score: float
    components: ScoreBreakdown

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal.id,
            "rank": self.rank,
            "score": self.score,
            "components": self.components.to_dict(),
        }


@dataclass
class RankingResult:
    ranked: list[RankedProposal] = field(default_factory=list)
    held_for_rate_limit: list[str] = field(default_factory=list)
    surface_freshness: str = ""

    def top(self) -> RankedProposal | None:
        """Highest-ranked surfaceable proposal, or None if queue is empty."""
        return self.ranked[0] if self.ranked else None


def rank(
    proposals: list[Proposal],
    *,
    shared_dir: Path | None = None,
    record_lookup: RecordLookup,
    already_surfaced_this_week: int = 0,
    rate_cap: int = 7,
    now: datetime | None = None,
) -> RankingResult:
    """Produce a fully-processed ranking.

    1. Score every proposal.
    2. Sort desc by score.
    3. Annotate conflicts (mutates proposals in place).
    4. Apply the weekly rate limit (critical urgencies bypass).
    5. Build RankedProposal records with final ranks.

    ``shared_dir`` is accepted for backward compatibility; it is no
    longer consulted. Removed once all callers stop passing it.
    """
    del shared_dir  # unused
    # Step 1: score
    scored: list[tuple[Proposal, ScoreBreakdown]] = []
    for p in proposals:
        record = record_lookup(p.generator_id)
        authority = _authority_for(record)
        components = score_proposal(p, authority=authority, now=now)
        scored.append((p, components))

    # Step 2: sort
    scored.sort(key=lambda t: t[1].score, reverse=True)

    # Step 3: annotate conflicts
    detect_conflicts([p for p, _ in scored])

    # Step 4: rate limit (preserves order)
    rl = apply_rate_limit(
        [p for p, _ in scored],
        already_surfaced_this_week=already_surfaced_this_week,
        cap=rate_cap,
        now=now,
    )

    # Step 5: build ranked list
    result = RankingResult()
    scored_by_id = {p.id: comp for p, comp in scored}
    for i, proposal in enumerate(rl.surfaceable):
        comp = scored_by_id[proposal.id]
        result.ranked.append(
            RankedProposal(
                proposal=proposal, rank=i + 1, score=comp.score, components=comp
            )
        )
    result.held_for_rate_limit = [p.id for p in rl.held]
    result.surface_freshness = (now or datetime.now()).isoformat(timespec="seconds")

    return result


def _authority_for(record: "GeneratorRecord | None") -> float:
    """Derive the authority factor for a generator.

    Guardians run with authority 1.0 (they don't compete on wins).
    Optimizers derive authority from their verified success/failure counts.
    Unknown generators (no record available) default to 1.0.
    """
    if record is None:
        return 1.0
    if record.budget_policy == "duty":
        return 1.0
    return compute_authority(
        verified_success=record.track_record.proposals_verified_success,
        verified_failed=record.track_record.proposals_verified_failed,
        succeeded_after_iteration=record.track_record.proposals_succeeded_after_iteration,
    )

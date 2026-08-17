"""registry.competition — Intra-dimension competitive weight reallocation.

Spec §8. Generators sharing a ``(dimension, competitive_group)`` pair
compete head-to-head on the same observation windows. The registry
allocates ``competitive_weight`` per generator such that the sum across
a group equals 1.0.

Allocation rule (§8.1):
  - Initial weights = 1/n (equal split)
  - Weekly: recompute weights from each generator's ``authority_score``
  - Floor: no generator < 0.1
  - Grace period: new deployments get 4 weeks before reallocation kicks in

The competitive weight multiplies the per-window cost budget and the
referee's ranking score as a soft tiebreak (§8.2). Guardians are NOT
subject to this — they use duty budgets regardless.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter.ranking import compute_authority
from schema.generator import GeneratorRecord


COMPETITIVE_WEIGHT_FLOOR = 0.1
GRACE_PERIOD_DAYS = 28  # 4 weeks


# ─────────────────────────────────────────────────────────────────────────────
# Group resolution
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CompetitionGroup:
    """Records group membership + current weights + authority signals."""

    dimension: str
    group_id: str
    members: list[GeneratorRecord] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.members)

    @property
    def composite_key(self) -> str:
        return f"{self.dimension}::{self.group_id}"


def resolve_groups(records: list[GeneratorRecord]) -> list[CompetitionGroup]:
    """Group generator records by (dimension, competitive_group).

    Generators without a ``competitive_group`` set are skipped — they
    run as solo operators with competitive_weight = 1.0.

    Groups with only one member are also skipped (no competition to run).
    """
    buckets: dict[tuple, list[GeneratorRecord]] = defaultdict(list)
    for record in records:
        if not record.competitive_group:
            continue
        # We need dimension too — the caller can pass records with
        # .charter loaded, but here we rely on the record's own hints.
        # For L6 we extract dimension from the record's state if present;
        # caller is responsible for providing it via state['dimension'].
        dimension = record.state.get("dimension") if record.state else None
        if not dimension:
            continue
        buckets[(dimension, record.competitive_group)].append(record)

    return [
        CompetitionGroup(
            dimension=dim, group_id=gid, members=list(members)
        )
        for (dim, gid), members in buckets.items()
        if len(members) > 1
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Weight computation
# ─────────────────────────────────────────────────────────────────────────────


def _is_in_grace_period(record: GeneratorRecord, *, now: datetime) -> bool:
    """A generator deployed less than GRACE_PERIOD_DAYS ago keeps its
    initial weight (no reallocation yet)."""
    created = record.state.get("deployed_at") if record.state else None
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(
            created.replace("Z", "+00:00") if created.endswith("Z") else created
        )
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = now - dt
    return age < timedelta(days=GRACE_PERIOD_DAYS)


def compute_group_weights(
    group: CompetitionGroup, *, now: datetime | None = None
) -> dict[str, float]:
    """Return ``{generator_id: competitive_weight}`` for a group.

    Weights sum to 1.0 across the group. Uses authority_score derived
    from verified success/failure counts; applies the floor; honors the
    grace period.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if group.n <= 1:
        return {r.id: 1.0 for r in group.members}

    # Split: grace-period members keep equal-share initial weights;
    # established members split the remaining budget proportionally by
    # authority.
    grace_members = [
        r for r in group.members if _is_in_grace_period(r, now=now)
    ]
    established = [
        r for r in group.members if not _is_in_grace_period(r, now=now)
    ]

    # Equal initial split if all members are in grace
    if not established:
        share = 1.0 / group.n
        return {r.id: share for r in group.members}

    # If there are no grace members, all compete
    if not grace_members:
        authorities = {
            r.id: compute_authority(
                verified_success=r.track_record.proposals_verified_success,
                verified_failed=r.track_record.proposals_verified_failed,
                succeeded_after_iteration=r.track_record.proposals_succeeded_after_iteration,
            )
            for r in established
        }
        total = sum(authorities.values()) or 1.0
        weights = {gid: a / total for gid, a in authorities.items()}
    else:
        # Give grace members equal share; split remainder to established
        grace_share_each = 1.0 / group.n
        grace_total = grace_share_each * len(grace_members)
        remaining = max(0.0, 1.0 - grace_total)
        authorities = {
            r.id: compute_authority(
                verified_success=r.track_record.proposals_verified_success,
                verified_failed=r.track_record.proposals_verified_failed,
                succeeded_after_iteration=r.track_record.proposals_succeeded_after_iteration,
            )
            for r in established
        }
        total_auth = sum(authorities.values()) or 1.0
        weights = {r.id: grace_share_each for r in grace_members}
        for gid, a in authorities.items():
            weights[gid] = (a / total_auth) * remaining

    # Apply floor
    floored = {}
    for gid, w in weights.items():
        floored[gid] = max(w, COMPETITIVE_WEIGHT_FLOOR)

    # Re-normalize so sum equals 1.0 (floors may have pushed sum above 1)
    total = sum(floored.values())
    if total == 0:
        share = 1.0 / group.n
        return {r.id: share for r in group.members}
    return {gid: w / total for gid, w in floored.items()}


def apply_weights_to_records(
    records: list[GeneratorRecord], weights_by_gen: dict[str, float]
) -> None:
    """Update records' competitive_weight fields in place.

    Only touches records whose id appears in ``weights_by_gen`` — other
    records (solo operators, guardians) are unchanged.
    """
    for record in records:
        if record.id in weights_by_gen:
            record.competitive_weight = weights_by_gen[record.id]


def rebalance(
    records: list[GeneratorRecord], *, now: datetime | None = None
) -> dict[str, float]:
    """End-to-end: group records, compute weights, apply in place.

    Returns the flattened {generator_id: weight} mapping for audit /
    logging. Solo-operator records are omitted.
    """
    groups = resolve_groups(records)
    all_weights: dict[str, float] = {}
    for group in groups:
        weights = compute_group_weights(group, now=now)
        all_weights.update(weights)
    apply_weights_to_records(records, all_weights)
    return all_weights

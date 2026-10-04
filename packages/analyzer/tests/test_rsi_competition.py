"""tests/test_rsi_competition.py — Intra-dimension competition mechanics."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from registry.competition import (  # noqa: E402
    COMPETITIVE_WEIGHT_FLOOR,
    GRACE_PERIOD_DAYS,
    compute_group_weights,
    rebalance,
    resolve_groups,
)
from schema.generator import GeneratorRecord, TrackRecord  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _record(
    id_,
    *,
    dimension="utility",
    competitive_group=None,
    wins=0,
    losses=0,
    deployed_days_ago=None,
):
    state = {"dimension": dimension}
    if deployed_days_ago is not None:
        deployed_at = (_NOW - timedelta(days=deployed_days_ago)).isoformat(
            timespec="seconds"
        )
        state["deployed_at"] = deployed_at
    return GeneratorRecord(
        id=id_,
        charter_fingerprint="x",
        track_record=TrackRecord(
            proposals_verified_success=wins,
            proposals_verified_failed=losses,
        ),
        competitive_group=competitive_group,
        competitive_weight=1.0,
        state=state,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_solo_generators_not_grouped():
    records = [
        _record("a", dimension="utility", competitive_group=None),
        _record("b", dimension="utility", competitive_group=None),
    ]
    groups = resolve_groups(records)
    assert groups == []


def test_single_member_groups_skipped():
    records = [
        _record("a", dimension="utility", competitive_group="adj"),
    ]
    groups = resolve_groups(records)
    assert groups == []


def test_multi_member_group_resolved():
    records = [
        _record("a", dimension="utility", competitive_group="adj"),
        _record("b", dimension="utility", competitive_group="adj"),
    ]
    groups = resolve_groups(records)
    assert len(groups) == 1
    assert groups[0].dimension == "utility"
    assert groups[0].group_id == "adj"
    assert len(groups[0].members) == 2


def test_different_dimensions_dont_group():
    records = [
        _record("a", dimension="utility", competitive_group="adj"),
        _record("b", dimension="efficiency", competitive_group="adj"),
    ]
    groups = resolve_groups(records)
    assert groups == []


# ─────────────────────────────────────────────────────────────────────────────
# Weight computation
# ─────────────────────────────────────────────────────────────────────────────


def test_equal_split_when_all_grace_period():
    records = [
        _record("a", competitive_group="g", wins=5, losses=5, deployed_days_ago=7),
        _record("b", competitive_group="g", wins=0, losses=0, deployed_days_ago=3),
    ]
    group = resolve_groups(records)[0]
    weights = compute_group_weights(group, now=_NOW)
    assert abs(weights["a"] - 0.5) < 1e-9
    assert abs(weights["b"] - 0.5) < 1e-9


def test_authority_based_split_when_all_established():
    records = [
        _record("winner", competitive_group="g", wins=10, losses=0, deployed_days_ago=60),
        _record("loser", competitive_group="g", wins=0, losses=10, deployed_days_ago=60),
    ]
    group = resolve_groups(records)[0]
    weights = compute_group_weights(group, now=_NOW)
    # Winner should get more than loser
    assert weights["winner"] > weights["loser"]
    # Sum ≈ 1.0
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_floor_enforced():
    # Extreme loser should still get floor
    records = [
        _record("winner", competitive_group="g", wins=1000, losses=0, deployed_days_ago=60),
        _record("loser", competitive_group="g", wins=0, losses=1000, deployed_days_ago=60),
    ]
    group = resolve_groups(records)[0]
    weights = compute_group_weights(group, now=_NOW)
    assert weights["loser"] >= COMPETITIVE_WEIGHT_FLOOR
    # Sum still = 1.0 after renormalization
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_mixed_grace_and_established():
    records = [
        _record("grace", competitive_group="g", wins=0, losses=0, deployed_days_ago=7),
        _record("established", competitive_group="g", wins=10, losses=0, deployed_days_ago=90),
    ]
    group = resolve_groups(records)[0]
    weights = compute_group_weights(group, now=_NOW)
    assert "grace" in weights and "established" in weights
    assert abs(sum(weights.values()) - 1.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Rebalance (end-to-end)
# ─────────────────────────────────────────────────────────────────────────────


def test_rebalance_updates_records_in_place():
    records = [
        _record("a", competitive_group="g", wins=10, losses=0, deployed_days_ago=60),
        _record("b", competitive_group="g", wins=0, losses=5, deployed_days_ago=60),
    ]
    rebalance(records, now=_NOW)
    # Weights updated on records
    a_weight = next(r.competitive_weight for r in records if r.id == "a")
    b_weight = next(r.competitive_weight for r in records if r.id == "b")
    assert a_weight > b_weight
    assert abs(a_weight + b_weight - 1.0) < 1e-6


def test_rebalance_ignores_solo_generators():
    solo = _record("solo", competitive_group=None, wins=5, losses=0)
    solo.competitive_weight = 0.7  # pre-existing value
    rebalance([solo], now=_NOW)
    # Weight unchanged for solo operators
    assert solo.competitive_weight == 0.7


def test_rebalance_respects_grace_period_boundary():
    # A generator deployed exactly GRACE_PERIOD_DAYS + 1 ago is established
    established = _record(
        "a",
        competitive_group="g",
        wins=5,
        losses=0,
        deployed_days_ago=GRACE_PERIOD_DAYS + 1,
    )
    # A generator deployed GRACE_PERIOD_DAYS - 1 days ago is still in grace
    grace = _record(
        "b",
        competitive_group="g",
        wins=0,
        losses=0,
        deployed_days_ago=GRACE_PERIOD_DAYS - 1,
    )
    weights = compute_group_weights(
        resolve_groups([established, grace])[0], now=_NOW
    )
    # Grace member keeps equal-split share (1/n); established gets remainder
    assert abs(weights["b"] - 0.5) < 1e-9

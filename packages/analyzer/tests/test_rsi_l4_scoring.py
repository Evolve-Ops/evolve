"""tests/test_rsi_l4_scoring.py — scoring helpers."""

from __future__ import annotations

import math
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from schema.generator import (  # noqa: E402
    GeneratorRecord,
    TrackRecord,
)
from scoring import (  # noqa: E402
    adoption_rate,
    avg_lift,
    compute_score,
    cost_per_proposal,
    score_components,
    success_rate,
)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────────────────────────────────────


def _record(
    policy="competitive",
    emitted=0,
    applied=0,
    success=0,
    failed=0,
    cost=0.0,
    recent_lifts=None,
):
    tr = TrackRecord(
        proposals_emitted=emitted,
        proposals_applied=applied,
        proposals_verified_success=success,
        proposals_verified_failed=failed,
        lifetime_cost_usd=cost,
    )
    state = {"recent_lifts": recent_lifts} if recent_lifts else {}
    return GeneratorRecord(
        id="test",
        charter_fingerprint="x",
        track_record=tr,
        status="active",
        budget_policy=policy,
        state=state,
    )


def test_adoption_rate_zero_when_no_proposals():
    assert adoption_rate(_record()) == 0.0


def test_adoption_rate_fraction():
    r = _record(emitted=10, applied=4)
    assert adoption_rate(r) == 0.4


def test_success_rate_fraction():
    r = _record(emitted=10, applied=5, success=4)
    assert success_rate(r) == 0.8


def test_avg_lift_with_recent_lifts():
    r = _record(recent_lifts=[0.2, 0.3, 0.1])
    assert abs(avg_lift(r) - 0.2) < 1e-9


def test_avg_lift_zero_without_data():
    r = _record()
    assert avg_lift(r) == 0.0


def test_cost_per_proposal_division():
    r = _record(emitted=4, cost=2.0)
    assert cost_per_proposal(r) == 0.5


def test_compute_score_guardian_is_infinity():
    r = _record(policy="duty")
    assert compute_score(r) == math.inf


def test_compute_score_meta_is_not_inf():
    r = _record(policy="meta")
    # Not a guardian (duty); meta gets same treatment as competitive in L4
    assert compute_score(r) != math.inf


def test_compute_score_optimizer_zero_without_track_record():
    r = _record(policy="competitive", emitted=0)
    assert compute_score(r) == 0.0


def test_compute_score_optimizer_positive_with_track_record():
    r = _record(
        policy="competitive",
        emitted=10,
        applied=5,
        success=4,
        cost=0.50,
    )
    # adoption=0.5, success=0.8, lift=1.0 (default), cost=0.05
    # → (0.5 * 0.8 * 1.0) / 0.05 = 8.0
    assert compute_score(r) == 8.0


def test_score_components_returns_all_fields():
    r = _record(emitted=10, applied=4, success=2, cost=1.0)
    comp = score_components(r)
    for field in ("adoption_rate", "success_rate", "avg_lift", "cost_per_proposal", "score"):
        assert field in comp

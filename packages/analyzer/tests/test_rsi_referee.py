"""tests/test_rsi_referee.py — ranking, conflict detection, rate limiting."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import (  # noqa: E402
    AUTHORITY_MAX,
    AUTHORITY_MIN,
    URGENCY_SCORE,
    apply_rate_limit,
    compute_authority,
    detect_conflicts,
    rank,
    score_proposal,
)
from arbiter.rate_limit import BYPASS_URGENCIES  # noqa: E402
from schema import ConflictAnnotation  # noqa: E402
from schema.generator import GeneratorRecord, TrackRecord  # noqa: E402
from schema.proposal import Claim  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
    make_workflow_proposal,
)


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Authority
# ─────────────────────────────────────────────────────────────────────────────


def test_authority_balanced_is_one():
    assert compute_authority(verified_success=5, verified_failed=5) == 1.0


def test_authority_more_wins_than_losses_above_one():
    auth = compute_authority(verified_success=8, verified_failed=2)
    assert auth > 1.0


def test_authority_perfect_win_record_respects_formula_and_clamp():
    # Spec §4.3: raw = 1.0 + 0.3 × ((wins - losses) / n). All-wins → 1.3.
    # AUTHORITY_MAX (1.5) is the outer clamp; the formula doesn't reach it
    # on its own — clamp is a safety net, not a normal operating point.
    auth = compute_authority(verified_success=100, verified_failed=0)
    assert auth == 1.3
    assert auth <= AUTHORITY_MAX


def test_authority_perfect_loss_record_respects_formula_and_clamp():
    auth = compute_authority(verified_success=0, verified_failed=100)
    assert auth == 0.7
    assert auth >= AUTHORITY_MIN


def test_authority_zero_records_default_one():
    assert compute_authority(verified_success=0, verified_failed=0) == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────


def test_score_scales_with_urgency():
    p_critical = make_investigation_proposal(urgency="security_critical")
    p_hygiene = make_investigation_proposal(urgency="hygiene")
    s1 = score_proposal(p_critical, authority=1.0, now=_NOW)
    s2 = score_proposal(p_hygiene, authority=1.0, now=_NOW)
    assert s1.score > s2.score


def test_score_scales_with_authority():
    p = make_investigation_proposal()
    s_low = score_proposal(p, authority=0.5, now=_NOW)
    s_high = score_proposal(p, authority=1.5, now=_NOW)
    assert s_high.score > s_low.score


def test_tiebreak_prefers_newer_proposals():
    old = make_investigation_proposal()
    new = make_investigation_proposal()
    old.created_at = (_NOW - timedelta(hours=48)).isoformat(timespec="seconds")
    new.created_at = (_NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
    s_old = score_proposal(old, authority=1.0, now=_NOW)
    s_new = score_proposal(new, authority=1.0, now=_NOW)
    assert s_new.score > s_old.score


# ─────────────────────────────────────────────────────────────────────────────
# Conflict detection
# ─────────────────────────────────────────────────────────────────────────────


def test_touches_overlap_detected(tmp_path):
    a = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme", value="dark"
    )
    b = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme", value="light"
    )
    detect_conflicts([a, b])
    assert any(
        c.conflict_type == "touches_overlap" for c in a.conflicts_with
    )
    # Symmetric
    assert any(
        c.conflict_type == "touches_overlap" for c in b.conflicts_with
    )


def test_metric_direction_opposite_detected():
    a = make_workflow_proposal()
    b = make_workflow_proposal()
    a.claim = Claim(
        metric="cost.daily_usd",
        direction="up",
        magnitude=1.0,
        window_days=7,
        baseline=0.0,
    )
    b.claim = Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=1.0,
        window_days=7,
        baseline=0.0,
    )
    detect_conflicts([a, b])
    assert any(
        c.conflict_type == "metric_direction_opposite" for c in a.conflicts_with
    )


def test_exclusive_choice_detected():
    # Two proposals from different generators with identical fingerprint
    a = make_investigation_proposal(generator_id="a")
    b = make_investigation_proposal(generator_id="b")
    a.trigger_observations = ["shared"]
    b.trigger_observations = ["shared"]
    # Force same action (harness does this for Investigation by construction)
    detect_conflicts([a, b])
    # Should detect exclusive_choice since fingerprint matches
    assert any(
        c.conflict_type == "exclusive_choice" for c in a.conflicts_with
    )


def test_no_conflict_on_unrelated_proposals():
    a = make_investigation_proposal(dimension="substrate_health")
    b = make_workflow_proposal(dimension="utility")
    detect_conflicts([a, b])
    assert a.conflicts_with == []
    assert b.conflicts_with == []


def test_conflict_detection_idempotent(tmp_path):
    a = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme", value="dark"
    )
    b = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme", value="light"
    )
    detect_conflicts([a, b])
    count_before = len(a.conflicts_with)
    detect_conflicts([a, b])  # second pass
    assert len(a.conflicts_with) == count_before  # no duplicates


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────


def test_rate_limit_under_cap_surfaces_all():
    proposals = [make_investigation_proposal(urgency="improvement") for _ in range(3)]
    result = apply_rate_limit(proposals, cap=7, now=_NOW)
    assert len(result.surfaceable) == 3
    assert result.held == []


def test_rate_limit_over_cap_holds_excess():
    proposals = [make_investigation_proposal(urgency="improvement") for _ in range(10)]
    result = apply_rate_limit(proposals, cap=5, now=_NOW)
    assert len(result.surfaceable) == 5
    assert len(result.held) == 5


def test_rate_limit_critical_bypass():
    proposals = [
        make_investigation_proposal(urgency="improvement") for _ in range(5)
    ] + [
        make_investigation_proposal(urgency="security_critical") for _ in range(3)
    ]
    result = apply_rate_limit(proposals, cap=2, now=_NOW)
    # 2 non-critical + 3 critical bypasses = 5 surfaceable
    assert len(result.surfaceable) == 5
    assert len(result.held) == 3
    # All critical are in surfaceable
    critical = [p for p in result.surfaceable if p.urgency == "security_critical"]
    assert len(critical) == 3


def test_rate_limit_respects_already_surfaced():
    proposals = [make_investigation_proposal(urgency="improvement") for _ in range(5)]
    result = apply_rate_limit(
        proposals, cap=7, already_surfaced_this_week=5, now=_NOW
    )
    # 5 + 5 > 7 → 2 surfaceable, 3 held
    assert len(result.surfaceable) == 2
    assert len(result.held) == 3


def test_rate_limit_bypass_urgencies_set():
    assert "security_critical" in BYPASS_URGENCIES
    assert "operational_urgent" in BYPASS_URGENCIES


# ─────────────────────────────────────────────────────────────────────────────
# Referee (rank) — integration
# ─────────────────────────────────────────────────────────────────────────────


def _record_lookup(records: dict):
    def lookup(generator_id):
        return records.get(generator_id)

    return lookup


def test_rank_produces_sorted_output():
    low = make_investigation_proposal(urgency="hygiene", generator_id="a")
    high = make_investigation_proposal(urgency="security_critical", generator_id="a")
    records = {
        "a": GeneratorRecord(id="a", charter_fingerprint="x"),
    }
    result = rank(
        [low, high],
        record_lookup=_record_lookup(records),
        now=_NOW,
    )
    assert result.ranked[0].proposal.id == high.id
    assert result.ranked[1].proposal.id == low.id


def test_rank_empty_input_yields_empty_result():
    result = rank([], record_lookup=lambda gid: None, now=_NOW)
    assert result.ranked == []
    assert result.top() is None


def test_rank_annotates_conflicts(tmp_path):
    a = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme",
        value="dark",
        generator_id="a",
    )
    b = make_config_patch_proposal(
        target_path=f"{tmp_path}/cfg.json::ui.theme",
        value="light",
        generator_id="b",
    )
    records = {
        "a": GeneratorRecord(id="a", charter_fingerprint="x"),
        "b": GeneratorRecord(id="b", charter_fingerprint="x"),
    }
    rank(
        [a, b],
        record_lookup=_record_lookup(records),
        now=_NOW,
    )
    assert len(a.conflicts_with) >= 1
    assert len(b.conflicts_with) >= 1


def test_rank_held_proposals_not_in_ranked_list():
    proposals = [
        make_investigation_proposal(urgency="improvement", generator_id="a")
        for _ in range(10)
    ]
    records = {"a": GeneratorRecord(id="a", charter_fingerprint="x")}
    result = rank(
        proposals,
        record_lookup=_record_lookup(records),
        rate_cap=3,
        now=_NOW,
    )
    assert len(result.ranked) == 3
    assert len(result.held_for_rate_limit) == 7


def test_rank_uses_authority_from_track_record():
    # Two identical proposals from two generators; one has strong track record
    p_strong = make_investigation_proposal(urgency="improvement", generator_id="strong")
    p_weak = make_investigation_proposal(urgency="improvement", generator_id="weak")
    records = {
        "strong": GeneratorRecord(
            id="strong",
            charter_fingerprint="x",
            track_record=TrackRecord(
                proposals_verified_success=10,
                proposals_verified_failed=0,
            ),
        ),
        "weak": GeneratorRecord(
            id="weak",
            charter_fingerprint="x",
            track_record=TrackRecord(
                proposals_verified_success=0,
                proposals_verified_failed=10,
            ),
        ),
    }
    result = rank(
        [p_strong, p_weak],
        record_lookup=_record_lookup(records),
        now=_NOW,
    )
    # Strong-authority proposal ranks first
    assert result.ranked[0].proposal.id == p_strong.id

"""
Tests for Better Engine Tier 0 data layer.

Run with: python -m pytest packages/admin/tests/test_better_engine_tier0.py -v
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.better_engine.model import Recommendation, now_iso
from evolve_admin.better_engine import storage, scoring, learning, snooze, portfolio


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_rec(**kwargs) -> Recommendation:
    """Create a minimal valid Recommendation with overridable fields."""
    defaults = dict(
        id="rec_001",
        dedup_key="health::team_bot_a::gateway::test",
        type="operational",
        source="health_check",
        scope="bot",
        scope_id="team_bot_a",
        title="Team_bot_a gateway down",
        detail="Port 8723 not responding.",
        context="Gateway has been failing for 2 cycles.",
        action_label="Restart",
        action="launchctl_kickstart",
        action_args={"label": "ai.openclaw.team_bot_a-gateway"},
        tags=["bot:team_bot_a", "category:gateway", "source:health_check", "intent:fix", "urgency:critical"],
        priority_score=85,
        priority_components={"urgency": 38, "impact": 28, "actionability": 14, "freshness": 7},
        learning_weight=1.0,
        status="pending",
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


# ── model.py ──────────────────────────────────────────────────────────────────

class TestRecommendationModel:
    def test_round_trip_to_dict_from_dict(self):
        """to_dict() → from_dict() preserves all fields."""
        rec = _make_rec()
        d = rec.to_dict()
        rec2 = Recommendation.from_dict(d)

        assert rec2.id == rec.id
        assert rec2.dedup_key == rec.dedup_key
        assert rec2.type == rec.type
        assert rec2.source == rec.source
        assert rec2.scope == rec.scope
        assert rec2.scope_id == rec.scope_id
        assert rec2.title == rec.title
        assert rec2.detail == rec.detail
        assert rec2.context == rec.context
        assert rec2.action_label == rec.action_label
        assert rec2.action == rec.action
        assert rec2.action_args == rec.action_args
        assert rec2.bot_executable == rec.bot_executable
        assert rec2.bridge_strategy == rec.bridge_strategy
        assert rec2.accept_label == rec.accept_label
        assert rec2.member_bot_title == rec.member_bot_title
        assert rec2.member_bot_detail == rec.member_bot_detail
        assert rec2.priority_score == rec.priority_score
        assert rec2.priority_components == rec.priority_components
        assert rec2.learning_weight == rec.learning_weight
        assert rec2.tags == rec.tags
        assert rec2.status == rec.status
        assert rec2.snooze_count == rec.snooze_count
        assert rec2.snooze_until == rec.snooze_until
        assert rec2.created_at == rec.created_at
        assert rec2.updated_at == rec.updated_at
        assert rec2.resolved_at == rec.resolved_at
        assert rec2.expires_at == rec.expires_at
        assert rec2.feedback_signal == rec.feedback_signal
        assert rec2.feedback_reason == rec.feedback_reason
        assert rec2.source_ref == rec.source_ref

    def test_round_trip_preserves_new_fields(self):
        """New fields (bot_executable, bridge_strategy, accept_label, member_bot_*) survive round-trip."""
        rec = _make_rec(
            bot_executable=False,
            bridge_strategy="task_queue",
            accept_label="Add to queue",
            member_bot_title="Gateway's having a moment",
            member_bot_detail="The gateway is offline — want me to restart it?",
        )
        d = rec.to_dict()
        rec2 = Recommendation.from_dict(d)

        assert rec2.bot_executable is False
        assert rec2.bridge_strategy == "task_queue"
        assert rec2.accept_label == "Add to queue"
        assert rec2.member_bot_title == "Gateway's having a moment"
        assert rec2.member_bot_detail == "The gateway is offline — want me to restart it?"

    def test_from_dict_missing_optional_fields(self):
        """from_dict() handles missing optional fields gracefully."""
        minimal = {
            "id": "rec_min",
            "dedup_key": "health::admin::test",
            "type": "operational",
            "source": "health_check",
            "scope": "admin",
            "scope_id": "admin",
            "title": "Something broken",
            "detail": "It is broken.",
        }
        rec = Recommendation.from_dict(minimal)

        assert rec.context == ""
        assert rec.action_label is None
        assert rec.action is None
        assert rec.action_args == {}
        assert rec.bot_executable is True
        assert rec.bridge_strategy is None
        assert rec.accept_label == "Accept"
        assert rec.member_bot_title is None
        assert rec.member_bot_detail is None
        assert rec.priority_score == 0
        assert rec.priority_components == {}
        assert rec.learning_weight == 1.0
        assert rec.tags == []
        assert rec.status == "pending"
        assert rec.snooze_count == 0
        assert rec.snooze_until is None
        assert rec.resolved_at is None
        assert rec.expires_at is None
        assert rec.feedback_signal is None
        assert rec.feedback_reason == ""
        assert rec.source_ref == {}

    def test_update_from_candidate_preserves_immutable(self):
        """update_from_candidate preserves id, created_at, status, snooze_*, feedback_*."""
        original_created = "2026-04-01T10:00:00+00:00"
        rec = _make_rec(
            created_at=original_created,
            status="snoozed",
            snooze_count=2,
            snooze_until="2026-04-10T10:00:00+00:00",
            feedback_signal="negative",
            feedback_reason="not helpful",
        )
        original_id = rec.id

        candidate = _make_rec(
            id="rec_candidate_different",
            created_at="2026-04-15T10:00:00+00:00",
            status="pending",
            snooze_count=0,
            snooze_until=None,
            feedback_signal=None,
            feedback_reason="",
            title="Updated Title",
            detail="Updated detail.",
            context="Updated context.",
            priority_score=90,
            member_bot_title="Updated casual title",
            member_bot_detail="Updated casual detail.",
        )

        rec.update_from_candidate(candidate)

        # Immutable fields unchanged
        assert rec.id == original_id
        assert rec.created_at == original_created
        assert rec.status == "snoozed"
        assert rec.snooze_count == 2
        assert rec.snooze_until == "2026-04-10T10:00:00+00:00"
        assert rec.feedback_signal == "negative"
        assert rec.feedback_reason == "not helpful"

        # Mutable fields updated
        assert rec.title == "Updated Title"
        assert rec.detail == "Updated detail."
        assert rec.context == "Updated context."
        assert rec.priority_score == 90
        assert rec.member_bot_title == "Updated casual title"
        assert rec.member_bot_detail == "Updated casual detail."

    def test_default_status_is_pending(self):
        rec = _make_rec()
        assert rec.status == "pending"

    def test_default_snooze_count_is_zero(self):
        rec = _make_rec()
        assert rec.snooze_count == 0

    def test_now_iso_returns_string(self):
        ts = now_iso()
        assert isinstance(ts, str)
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None


# ── storage.py ────────────────────────────────────────────────────────────────

class TestStorage:
    def test_save_load_recommendations_round_trip(self, tmp_path):
        """save → load round-trip preserves recommendation data."""
        path = tmp_path / "recs.json"
        recs = [_make_rec(id="rec_1"), _make_rec(id="rec_2", title="Second rec")]

        storage.save_recommendations(recs, path)
        loaded = storage.load_recommendations(path)

        assert len(loaded) == 2
        assert loaded[0].id == "rec_1"
        assert loaded[1].title == "Second rec"

    def test_load_recommendations_missing_file(self, tmp_path):
        """Missing file returns empty list."""
        path = tmp_path / "nonexistent.json"
        result = storage.load_recommendations(path)
        assert result == []

    def test_load_recommendations_corrupt_json(self, tmp_path):
        """Corrupt JSON returns empty list."""
        path = tmp_path / "recs.json"
        path.write_text("{ this is not valid json !!!", encoding="utf-8")
        result = storage.load_recommendations(path)
        assert result == []

    def test_save_recommendations_is_atomic(self, tmp_path):
        """Save uses .tmp then rename (no .tmp file left after save)."""
        path = tmp_path / "recs.json"
        tmp_path_file = tmp_path / "recs.tmp"

        storage.save_recommendations([_make_rec()], path)

        assert path.exists()
        assert not tmp_path_file.exists()

    def test_save_load_learning_round_trip(self, tmp_path):
        """Learning save → load round-trip."""
        path = tmp_path / "learning.json"
        data = {
            "signals": {"source:health_check": {"accepted": 5, "rejected": 1, "snoozed": 0}},
            "bot_overrides": {},
            "recent_events": [],
        }
        storage.save_learning(data, path)
        loaded = storage.load_learning(path)
        assert loaded["signals"]["source:health_check"]["accepted"] == 5

    def test_load_learning_missing_file(self, tmp_path):
        """Missing learning file returns defaults."""
        path = tmp_path / "learning.json"
        result = storage.load_learning(path)
        assert result == {"signals": {}, "bot_overrides": {}, "recent_events": []}

    def test_load_learning_corrupt_json(self, tmp_path):
        """Corrupt learning JSON returns defaults."""
        path = tmp_path / "learning.json"
        path.write_text("NOT JSON", encoding="utf-8")
        result = storage.load_learning(path)
        assert result == {"signals": {}, "bot_overrides": {}, "recent_events": []}

    def test_load_getting_started_missing_file(self, tmp_path):
        """Missing getting-started file returns defaults."""
        path = tmp_path / "getting-started.json"
        result = storage.load_getting_started(path)
        assert result["schema_version"] == 1
        assert result["started_at"] is None
        assert result["completed_at"] is None
        assert result["dismissed"] is False
        assert result["tasks"] == {}

    def test_load_getting_started_corrupt_json(self, tmp_path):
        """Corrupt getting-started JSON returns defaults."""
        path = tmp_path / "getting-started.json"
        path.write_text("BROKEN", encoding="utf-8")
        result = storage.load_getting_started(path)
        assert result["schema_version"] == 1

    def test_save_load_getting_started_round_trip(self, tmp_path):
        """Getting-started save → load round-trip."""
        path = tmp_path / "gs.json"
        data = {
            "schema_version": 1,
            "started_at": "2026-04-01T10:00:00+00:00",
            "completed_at": None,
            "dismissed": False,
            "tasks": {"setup_bot": "done"},
        }
        storage.save_getting_started(data, path)
        loaded = storage.load_getting_started(path)
        assert loaded["tasks"]["setup_bot"] == "done"
        assert loaded["started_at"] == "2026-04-01T10:00:00+00:00"


# ── scoring.py ────────────────────────────────────────────────────────────────

class TestScoring:
    def test_freshness_clamps_at_3_snoozes(self):
        """Freshness is clamped to 1 when snooze_count >= 3."""
        ts = now_iso()  # brand new
        assert scoring.freshness_score(ts, snooze_count=3) == 1
        assert scoring.freshness_score(ts, snooze_count=4) == 1
        assert scoring.freshness_score(ts, snooze_count=10) == 1

    def test_freshness_new_rec_scores_10(self):
        """A brand new recommendation (just created) scores 10."""
        ts = now_iso()
        assert scoring.freshness_score(ts, snooze_count=0) == 10

    def test_freshness_lt_1_day_scores_7(self):
        """A rec created 12 hours ago scores 7."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        assert scoring.freshness_score(ts, snooze_count=0) == 7

    def test_freshness_1_to_3_days_scores_5(self):
        """A rec created 2 days ago scores 5."""
        ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert scoring.freshness_score(ts, snooze_count=0) == 5

    def test_freshness_3_to_7_days_scores_3(self):
        """A rec created 5 days ago scores 3."""
        ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        assert scoring.freshness_score(ts, snooze_count=0) == 3

    def test_freshness_gt_7_days_scores_1(self):
        """A rec older than 7 days scores 1."""
        ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert scoring.freshness_score(ts, snooze_count=0) == 1

    def test_urgency_label_critical(self):
        assert scoring.urgency_label(70) == "critical"
        assert scoring.urgency_label(100) == "critical"
        assert scoring.urgency_label(85) == "critical"

    def test_urgency_label_high(self):
        assert scoring.urgency_label(40) == "high"
        assert scoring.urgency_label(69) == "high"
        assert scoring.urgency_label(55) == "high"

    def test_urgency_label_medium(self):
        assert scoring.urgency_label(20) == "medium"
        assert scoring.urgency_label(39) == "medium"

    def test_urgency_label_low(self):
        assert scoring.urgency_label(0) == "low"
        assert scoring.urgency_label(19) == "low"

    def test_compute_base_score(self):
        """Base score is the sum of all components."""
        assert scoring.compute_base_score(38, 28, 20, 10) == 96
        assert scoring.compute_base_score(0, 0, 0, 0) == 0

    def test_compute_priority_score_clamped_to_100(self):
        """Priority score is clamped to 100 even if base * weight > 100."""
        rec = _make_rec(
            priority_components={"urgency": 38, "impact": 28, "actionability": 20, "freshness": 10},
            learning_weight=1.6,
        )
        score = scoring.compute_priority_score(rec)
        assert score <= 100
        assert score >= 0

    def test_compute_priority_score_clamped_to_0(self):
        """Priority score is clamped to 0 even with 0.4 weight on near-zero base."""
        rec = _make_rec(
            priority_components={"urgency": 0, "impact": 0, "actionability": 0, "freshness": 0},
            learning_weight=0.4,
        )
        score = scoring.compute_priority_score(rec)
        assert score == 0

    def test_compute_priority_score_applies_learning_weight(self):
        """Learning weight is applied to base score."""
        rec_neutral = _make_rec(
            priority_components={"urgency": 20, "impact": 10, "actionability": 10, "freshness": 5},
            learning_weight=1.0,
        )
        rec_boosted = _make_rec(
            priority_components={"urgency": 20, "impact": 10, "actionability": 10, "freshness": 5},
            learning_weight=1.6,
        )
        score_neutral = scoring.compute_priority_score(rec_neutral)
        score_boosted = scoring.compute_priority_score(rec_boosted)
        assert score_boosted > score_neutral


# ── learning.py ───────────────────────────────────────────────────────────────

class TestLearning:
    def test_bayesian_rate_zero_signals(self):
        """With no observations, rate regresses to the prior (0.5)."""
        rate = learning.bayesian_rate(0, 0, 0)
        assert abs(rate - 0.5) < 0.01

    def test_bayesian_rate_with_5_rejections(self):
        """5 rejections should yield meaningful dampening."""
        rate = learning.bayesian_rate(0, 5, 0)
        # effective_positive = 0 + 0*0.4 + 1.0 = 1.0
        # effective_total = 0 + 5 + 0*0.4 + 2.0 = 7.0
        # rate = 1/7 ≈ 0.1429
        # (Note: spec example says 1/8=0.125 but that's a typo; the formula gives 1/7)
        assert abs(rate - (1.0 / 7.0)) < 0.001

    def test_bayesian_rate_with_10_rejections_near_floor(self):
        """10 rejections → rate ≈ 0.077 → weight ≈ 0.49."""
        rate = learning.bayesian_rate(0, 10, 0)
        weight = 0.4 + rate * 1.2
        assert weight < 0.52
        assert weight >= 0.4

    def test_weight_stays_in_range_04_16(self):
        """Learning weight must always be between 0.4 and 1.6."""
        # All rejections
        rate_low = learning.bayesian_rate(0, 100, 0)
        weight_low = 0.4 + rate_low * 1.2
        assert weight_low >= 0.4
        assert weight_low < 1.6

        # All accepts
        rate_high = learning.bayesian_rate(100, 0, 0)
        weight_high = 0.4 + rate_high * 1.2
        assert weight_high <= 1.6
        assert weight_high > 0.4

    def test_compute_learning_weight_no_data_returns_neutral(self):
        """No signals → weight 1.0 (neutral)."""
        rec = _make_rec(tags=["bot:team_bot_a", "source:health_check"])
        w = learning.compute_learning_weight(rec, signals={})
        assert w == 1.0

    def test_compute_learning_weight_insufficient_observations_skipped(self):
        """Signals with < 5 observations are skipped."""
        rec = _make_rec(tags=["source:health_check"])
        signals = {"source:health_check": {"accepted": 2, "rejected": 1, "snoozed": 0}}
        w = learning.compute_learning_weight(rec, signals=signals)
        assert w == 1.0  # skipped, no data used

    def test_compute_learning_weight_with_sufficient_data(self):
        """Sufficient signals produce a weight in 0.4–1.6."""
        rec = _make_rec(tags=["source:health_check", "intent:fix"])
        signals = {
            "source:health_check": {"accepted": 10, "rejected": 0, "snoozed": 0},
            "intent:fix": {"accepted": 8, "rejected": 2, "snoozed": 1},
        }
        w = learning.compute_learning_weight(rec, signals=signals)
        assert 0.4 <= w <= 1.6

    def test_record_feedback_updates_signals(self):
        """record_feedback increments the correct signal counter."""
        rec = _make_rec(tags=["bot:team_bot_a", "source:health_check"])
        ld = {"signals": {}, "bot_overrides": {}, "recent_events": []}
        ld = learning.record_feedback(rec, "accepted", "", ld)

        assert ld["signals"]["bot:team_bot_a"]["accepted"] == 1
        assert ld["signals"]["source:health_check"]["accepted"] == 1

    def test_record_feedback_updates_pair_signals(self):
        """record_feedback also updates 2-tag combination signals."""
        rec = _make_rec(tags=["bot:team_bot_a", "source:health_check"])
        ld = {"signals": {}, "bot_overrides": {}, "recent_events": []}
        ld = learning.record_feedback(rec, "rejected", "", ld)

        pair_key = "bot:team_bot_a+source:health_check"
        assert pair_key in ld["signals"]
        assert ld["signals"][pair_key]["rejected"] == 1

    def test_record_feedback_appends_to_recent_events(self):
        """record_feedback appends an event to recent_events."""
        rec = _make_rec()
        ld = {"signals": {}, "bot_overrides": {}, "recent_events": []}
        ld = learning.record_feedback(rec, "accepted", "great", ld)

        assert len(ld["recent_events"]) == 1
        evt = ld["recent_events"][0]
        assert evt["signal"] == "accepted"
        assert evt["reason"] == "great"
        assert evt["rec_id"] == rec.id

    def test_record_feedback_recent_events_capped_at_200(self):
        """recent_events is capped at 200 entries."""
        rec = _make_rec()
        ld = {
            "signals": {},
            "bot_overrides": {},
            "recent_events": [{"ts": now_iso(), "signal": "accepted"} for _ in range(200)],
        }
        ld = learning.record_feedback(rec, "accepted", "", ld)
        assert len(ld["recent_events"]) == 200

    def test_bot_override_triggers_at_3_rejections_in_14_days(self):
        """Bot override is written after 3 rejections of same (scope_id, type) within 14 days."""
        rec = _make_rec(scope_id="team_bot_a", type="operational")
        # Pre-populate with 2 recent rejections (so current feedback = 3rd)
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        ld = {
            "signals": {},
            "bot_overrides": {},
            "recent_events": [
                {
                    "rec_id": "rec_x",
                    "dedup_key": "key_x",
                    "type": "operational",
                    "source": "health_check",
                    "scope_id": "team_bot_a",
                    "tags": [],
                    "signal": "rejected",
                    "reason": "",
                    "ts": recent_ts,
                },
                {
                    "rec_id": "rec_y",
                    "dedup_key": "key_y",
                    "type": "operational",
                    "source": "health_check",
                    "scope_id": "team_bot_a",
                    "tags": [],
                    "signal": "rejected",
                    "reason": "",
                    "ts": recent_ts,
                },
            ],
        }
        ld = learning.record_feedback(rec, "rejected", "", ld)

        assert "team_bot_a" in ld["bot_overrides"]
        assert "operational" in ld["bot_overrides"]["team_bot_a"]
        assert ld["bot_overrides"]["team_bot_a"]["operational"]["multiplier"] == 0.5

    def test_bot_override_not_triggered_for_old_rejections(self):
        """Bot override is NOT triggered if prior rejections are older than 14 days."""
        rec = _make_rec(scope_id="team_bot_a", type="operational")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        ld = {
            "signals": {},
            "bot_overrides": {},
            "recent_events": [
                {
                    "rec_id": "rec_x",
                    "dedup_key": "key",
                    "type": "operational",
                    "source": "health_check",
                    "scope_id": "team_bot_a",
                    "tags": [],
                    "signal": "rejected",
                    "reason": "",
                    "ts": old_ts,
                },
                {
                    "rec_id": "rec_y",
                    "dedup_key": "key2",
                    "type": "operational",
                    "source": "health_check",
                    "scope_id": "team_bot_a",
                    "tags": [],
                    "signal": "rejected",
                    "reason": "",
                    "ts": old_ts,
                },
            ],
        }
        ld = learning.record_feedback(rec, "rejected", "", ld)
        # Old events are outside 14-day window
        assert "operational" not in ld.get("bot_overrides", {}).get("team_bot_a", {})

    def test_apply_time_decay_attaches_weight(self):
        """apply_time_decay adds _weight to each event."""
        now = datetime.now(timezone.utc)
        events = [
            {"ts": now.isoformat(), "signal": "accepted"},
            {"ts": (now - timedelta(days=45)).isoformat(), "signal": "rejected"},
            {"ts": (now - timedelta(days=80)).isoformat(), "signal": "snoozed"},
        ]
        decayed = learning.apply_time_decay(events)
        assert decayed[0]["_weight"] == 1.0
        assert decayed[1]["_weight"] == 0.5
        assert decayed[2]["_weight"] == 0.2

    def test_get_effective_weight_combines_tag_and_override(self):
        """get_effective_weight = tag_weight * override_multiplier."""
        rec = _make_rec(scope_id="team_bot_a", type="operational", tags=["source:health_check"])
        # Enough data so tag weight is meaningful
        signals = {"source:health_check": {"accepted": 8, "rejected": 2, "snoozed": 0}}
        ld = {
            "signals": signals,
            "bot_overrides": {
                "team_bot_a": {
                    "operational": {"multiplier": 0.5, "reason": "test", "set_at": now_iso()}
                }
            },
            "recent_events": [],
        }
        w = learning.get_effective_weight(rec, ld)
        tag_w = learning.compute_learning_weight(rec, signals)
        assert abs(w - tag_w * 0.5) < 0.001


# ── snooze.py ─────────────────────────────────────────────────────────────────

class TestSnooze:
    def test_snooze_schedule_is_correct(self):
        """SNOOZE_SCHEDULE should be [1, 2, 4, 7]."""
        from evolve_admin.better_engine.snooze import SNOOZE_SCHEDULE
        assert SNOOZE_SCHEDULE == [1, 2, 4, 7]

    def test_first_snooze_is_1_day(self):
        """First snooze (snooze_count=0) should set snooze_until ~1 day out."""
        rec = _make_rec(snooze_count=0)
        snooze.snooze_recommendation(rec)

        assert rec.status == "snoozed"
        assert rec.snooze_count == 1
        until = datetime.fromisoformat(rec.snooze_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        diff = (until - datetime.now(timezone.utc)).total_seconds()
        assert 86300 < diff < 86500  # ~1 day in seconds

    def test_second_snooze_is_2_days(self):
        """Second snooze (snooze_count=1) should set snooze_until ~2 days out."""
        rec = _make_rec(snooze_count=1)
        snooze.snooze_recommendation(rec)
        until = datetime.fromisoformat(rec.snooze_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        diff = (until - datetime.now(timezone.utc)).total_seconds()
        assert 2 * 86300 < diff < 2 * 86500

    def test_third_snooze_is_4_days(self):
        """Third snooze (snooze_count=2) should set snooze_until ~4 days out."""
        rec = _make_rec(snooze_count=2)
        snooze.snooze_recommendation(rec)
        until = datetime.fromisoformat(rec.snooze_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        diff = (until - datetime.now(timezone.utc)).total_seconds()
        assert 4 * 86300 < diff < 4 * 86500

    def test_fourth_and_beyond_snooze_is_7_days(self):
        """4th+ snooze (snooze_count >= 3) should set snooze_until ~7 days out."""
        for count in (3, 5, 10):
            rec = _make_rec(snooze_count=count)
            snooze.snooze_recommendation(rec)
            until = datetime.fromisoformat(rec.snooze_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            diff = (until - datetime.now(timezone.utc)).total_seconds()
            assert 7 * 86300 < diff < 7 * 86500, f"Failed for snooze_count={count}"

    def test_wake_snoozed_reactivates_past_due(self):
        """wake_snoozed sets past-due snoozed recs back to pending."""
        past_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rec = _make_rec(status="snoozed", snooze_until=past_ts)
        result = snooze.wake_snoozed([rec])

        assert result[0].status == "pending"
        assert result[0].snooze_until is None

    def test_wake_snoozed_does_not_wake_future(self):
        """wake_snoozed does not touch recs still within snooze window."""
        future_ts = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        rec = _make_rec(status="snoozed", snooze_until=future_ts)
        result = snooze.wake_snoozed([rec])

        assert result[0].status == "snoozed"
        assert result[0].snooze_until == future_ts

    def test_wake_snoozed_ignores_non_snoozed(self):
        """wake_snoozed leaves non-snoozed recs untouched."""
        rec = _make_rec(status="pending")
        result = snooze.wake_snoozed([rec])
        assert result[0].status == "pending"


# ── portfolio.py ──────────────────────────────────────────────────────────────

class TestPortfolio:
    def _make_recs_by_type(self, types_and_tags: list[tuple[str, list[str], int]]) -> list[Recommendation]:
        """Helper to create recs with specific type, tags, and priority_score."""
        recs = []
        for i, (rtype, tags, score) in enumerate(types_and_tags):
            recs.append(_make_rec(
                id=f"rec_{i}",
                dedup_key=f"key_{i}",
                type=rtype,
                tags=tags,
                priority_score=score,
                status="pending",
            ))
        return recs

    def test_empty_list_returns_empty(self):
        """compose_portfolio([]) returns []."""
        assert portfolio.compose_portfolio([]) == []

    def test_all_non_pending_returns_empty(self):
        """If no pending recs, returns []."""
        rec = _make_rec(status="accepted")
        assert portfolio.compose_portfolio([rec]) == []

    def test_urgency_budget_respected(self):
        """At most 40% of slots should be urgent (critical/high)."""
        n = 10
        urgent_budget = max(1, int(n * 0.4))  # 4

        recs = []
        for i in range(8):
            recs.append(_make_rec(
                id=f"urgent_{i}",
                dedup_key=f"urgent_key_{i}",
                tags=["urgency:critical"],
                type="operational",
                priority_score=90 - i,
                status="pending",
            ))
        for i in range(5):
            recs.append(_make_rec(
                id=f"growth_{i}",
                dedup_key=f"growth_key_{i}",
                tags=["intent:improve"],
                type="app_quality",
                priority_score=50 - i,
                status="pending",
            ))

        result = portfolio.compose_portfolio(recs, n=n)
        urgent_in_result = sum(
            1 for r in result if "urgency:critical" in r.tags or "urgency:high" in r.tags
        )
        assert urgent_in_result <= urgent_budget

    def test_whimsy_suppressed_at_3_or_more_critical(self):
        """Whimsy should not appear when 3+ critical items are in the portfolio."""
        recs = []
        # 4 critical operational recs
        for i in range(4):
            recs.append(_make_rec(
                id=f"crit_{i}",
                dedup_key=f"crit_key_{i}",
                tags=["urgency:critical"],
                type="operational",
                priority_score=90 - i,
                status="pending",
            ))
        # 1 whimsy rec
        recs.append(_make_rec(
            id="whimsy_0",
            dedup_key="whimsy_key",
            type="whimsy",
            tags=[],
            priority_score=30,
            status="pending",
        ))

        result = portfolio.compose_portfolio(recs, n=10)
        whimsy_in_result = [r for r in result if r.type == "whimsy"]
        assert len(whimsy_in_result) == 0

    def test_whimsy_included_when_no_critical_items(self):
        """Whimsy should appear when there are fewer than 3 critical items."""
        recs = [
            _make_rec(
                id="op_0",
                dedup_key="op_key",
                type="operational",
                tags=["urgency:high"],
                priority_score=70,
                status="pending",
            ),
            _make_rec(
                id="whimsy_0",
                dedup_key="whimsy_key",
                type="whimsy",
                tags=[],
                priority_score=30,
                status="pending",
            ),
        ]

        result = portfolio.compose_portfolio(recs, n=10)
        whimsy_in_result = [r for r in result if r.type == "whimsy"]
        assert len(whimsy_in_result) == 1

    def test_whimsy_fallback_when_only_whimsy_pending(self):
        """When only whimsy items are pending, one whimsy is surfaced so the strip is non-empty."""
        recs = [
            _make_rec(
                id=f"whimsy_{i}",
                dedup_key=f"whimsy_key_{i}",
                type="whimsy",
                tags=[],
                priority_score=30,
                status="pending",
            )
            for i in range(3)
        ]

        result = portfolio.compose_portfolio(recs, n=10)
        assert len(result) == 1
        assert result[0].type == "whimsy"

    def test_type_diversity_cap(self):
        """The fill-remaining phase applies a type diversity cap.

        The spec applies the cap: ``type_count / max(len(selected), 1) <= 0.4``.
        With a large portfolio (n=5) that starts with 2 urgent items of the same type,
        a 3rd item of that type should be blocked (3/3=100% > 40%) while a different
        type gets through.

        Scenario:
        - 2 urgency:critical operational recs → land in urgent bucket (both selected)
        - 3 more operational recs (no urgency tag) → fill-remaining, blocked by cap
        - 3 security recs (no urgency tag) → fill-remaining, at least some get through
        Expected: final portfolio does not consist entirely of operational items.
        """
        n = 5
        recs = []
        # 2 urgent operational (will fill urgent bucket, max 2 at n=5)
        for i in range(2):
            recs.append(_make_rec(
                id=f"urgent_op_{i}",
                dedup_key=f"urgent_op_key_{i}",
                type="operational",
                tags=["urgency:critical"],
                priority_score=90 - i,
                status="pending",
            ))
        # 3 more operational (no urgency tag — go to fill-remaining)
        for i in range(3):
            recs.append(_make_rec(
                id=f"op_fill_{i}",
                dedup_key=f"op_fill_key_{i}",
                type="operational",
                tags=["source:health_check"],
                priority_score=75 - i,
                status="pending",
            ))
        # 3 security recs (no urgency tag — go to fill-remaining, different type)
        for i in range(3):
            recs.append(_make_rec(
                id=f"sec_{i}",
                dedup_key=f"sec_key_{i}",
                type="security",
                tags=["source:security_review"],
                priority_score=70 - i,
                status="pending",
            ))

        result = portfolio.compose_portfolio(recs, n=n)

        # Verify the cap blocked some operational items in fill-remaining
        # The portfolio should not be 100% operational
        types_in_result = {r.type for r in result}
        assert len(types_in_result) > 1, (
            "Expected type diversity in portfolio; got only: "
            + ", ".join(types_in_result)
        )

    def test_final_sort_by_priority_score_desc(self):
        """Portfolio is sorted by priority_score descending."""
        recs = [
            _make_rec(id="a", dedup_key="k_a", priority_score=50, status="pending"),
            _make_rec(id="b", dedup_key="k_b", priority_score=90, status="pending"),
            _make_rec(id="c", dedup_key="k_c", priority_score=70, status="pending"),
        ]
        result = portfolio.compose_portfolio(recs, n=10)
        scores = [r.priority_score for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_portfolio_max_n_items(self):
        """Portfolio never returns more than n items."""
        recs = [
            _make_rec(
                id=f"rec_{i}",
                dedup_key=f"k_{i}",
                priority_score=100 - i,
                status="pending",
                type="operational" if i % 3 != 0 else "app_quality",
                tags=["urgency:high" if i % 2 == 0 else "intent:improve"],
            )
            for i in range(20)
        ]
        result = portfolio.compose_portfolio(recs, n=5)
        assert len(result) <= 5

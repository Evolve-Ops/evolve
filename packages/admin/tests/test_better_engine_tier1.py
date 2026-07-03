"""
Tests for Better Engine Tier 1: engine core, onboarding, and pending tasks.

Run with:
    cd /Users/pod_admin/GitHub/evolve/.claude/worktrees/strange-lichterman
    python -m pytest packages/admin/tests/test_better_engine_tier1.py -v
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
from evolve_admin.better_engine.engine import BetterEngine
from evolve_admin.better_engine import storage
from evolve_admin.better_engine.onboarding import (
    build_onboarding_state,
    auto_complete_tasks,
    get_active_tasks,
    mark_task_complete,
    ONBOARDING_TASKS,
)
from evolve_admin.better_engine.pending_tasks import (
    PendingTask,
    add_task,
    resolve_task,
    get_all_pending_tasks,
    pending_tasks_path,
    load_pending_tasks,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_rec(**kwargs) -> Recommendation:
    """Create a minimal valid Recommendation with overridable fields.

    Defaults set member_bot_title/detail so member-bot surface tests that
    aren't specifically about the empty-title exclusion path get past the
    "must have user-facing copy" gate. Tests that exercise that gate
    explicitly should pass member_bot_title=None.
    """
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
        member_bot_title="My gateway is having trouble.",
        member_bot_detail="It hasn't been responding for a couple of cycles.",
        tags=["bot:team_bot_a", "category:gateway", "source:health_check",
              "intent:fix", "urgency:critical"],
        priority_score=85,
        priority_components={"urgency": 38, "impact": 28,
                             "actionability": 14, "freshness": 7},
        learning_weight=1.0,
        status="pending",
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


def _make_engine(tmp_path: Path) -> tuple[BetterEngine, dict]:
    """Create a BetterEngine backed by a tmp directory."""
    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {"members": ["team_bot_a", "admin_bot"], "sharedDir": str(shared_dir)}
    engine = BetterEngine(shared_dir=shared_dir, network=network)
    return engine, network


# ── engine.merge() ────────────────────────────────────────────────────────────

class TestEngineMerge:
    def test_new_candidates_added(self):
        existing: list[Recommendation] = []
        candidates = [
            _make_rec(id="rec_001", dedup_key="health::team_bot_a::gw"),
            _make_rec(id="rec_002", dedup_key="health::admin_bot::gw"),
        ]
        result = BetterEngine.merge(existing, candidates)
        keys = {r.dedup_key for r in result}
        assert "health::team_bot_a::gw" in keys
        assert "health::admin_bot::gw" in keys

    def test_existing_pending_updated_from_candidate(self):
        existing = [
            _make_rec(
                id="rec_001",
                dedup_key="health::team_bot_a::gw",
                title="Old title",
                status="pending",
            )
        ]
        candidates = [
            _make_rec(
                id="rec_NEW",
                dedup_key="health::team_bot_a::gw",
                title="New title",
                status="pending",
            )
        ]
        result = BetterEngine.merge(existing, candidates)
        rec = next(r for r in result if r.dedup_key == "health::team_bot_a::gw")
        # id preserved from existing
        assert rec.id == "rec_001"
        # title updated from candidate
        assert rec.title == "New title"

    def test_missing_pending_auto_resolved(self):
        existing = [
            _make_rec(dedup_key="health::team_bot_a::gw", status="pending"),
            _make_rec(id="rec_002", dedup_key="health::admin_bot::gw", status="pending"),
        ]
        candidates = [
            _make_rec(dedup_key="health::team_bot_a::gw"),  # team_bot_a still present
            # admin_bot NOT in candidates
        ]
        result = BetterEngine.merge(existing, candidates)
        admin_bot = next(r for r in result if r.dedup_key == "health::admin_bot::gw")
        assert admin_bot.status == "auto_resolved"
        assert admin_bot.resolved_at is not None

    def test_accepted_rejected_preserved(self):
        existing = [
            _make_rec(dedup_key="health::team_bot_a::gw", status="accepted"),
            _make_rec(id="rec_002", dedup_key="health::admin_bot::gw", status="rejected"),
        ]
        candidates = []  # nothing in candidates
        result = BetterEngine.merge(existing, candidates)
        team_bot_a = next(r for r in result if r.dedup_key == "health::team_bot_a::gw")
        admin_bot = next(r for r in result if r.dedup_key == "health::admin_bot::gw")
        # accepted/rejected should NOT be auto_resolved
        assert team_bot_a.status == "accepted"
        assert admin_bot.status == "rejected"

    def test_snoozed_rec_missing_from_candidates_is_auto_resolved(self):
        existing = [_make_rec(dedup_key="health::team_bot_a::gw", status="snoozed")]
        candidates = []
        result = BetterEngine.merge(existing, candidates)
        rec = result[0]
        assert rec.status == "auto_resolved"

    def test_new_candidate_gets_id_from_itself(self):
        candidates = [_make_rec(id="rec_brand_new", dedup_key="cost::team_bot_a::spend")]
        result = BetterEngine.merge([], candidates)
        assert result[0].id == "rec_brand_new"


# ── engine.filter_for_surface() ───────────────────────────────────────────────

class TestFilterForSurface:
    def _base_recs(self) -> list[Recommendation]:
        return [
            _make_rec(
                id="r1",
                dedup_key="health::team_bot_a::gw",
                type="operational",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a", "urgency:critical"],
                bot_executable=True,
                status="pending",
            ),
            _make_rec(
                id="r2",
                dedup_key="security::team_bot_a::perm",
                type="security",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a", "urgency:high"],
                bot_executable=False,
                bridge_strategy=None,
                status="pending",
            ),
            _make_rec(
                id="r3",
                dedup_key="compliance::admin_bot::stale",
                type="app_quality",
                scope="bot",
                scope_id="admin_bot",
                tags=["bot:admin_bot", "urgency:medium"],
                bot_executable=True,
                status="pending",
            ),
            _make_rec(
                id="r4",
                dedup_key="compliance::team_bot_a::stale",
                type="app_quality",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a", "urgency:low"],
                bot_executable=True,
                status="pending",
            ),
            _make_rec(
                id="r5",
                dedup_key="onboarding::team_bot_a::scan",
                type="onboarding",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a", "intent:setup"],
                bot_executable=True,
                status="pending",
            ),
        ]

    def test_admin_gets_all_pending(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "admin")
        ids = {r.id for r in result}
        assert ids == {"r1", "r2", "r3", "r4", "r5"}

    def test_admin_filtered_by_scope_id(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "admin", scope_id="team_bot_a")
        ids = {r.id for r in result}
        assert "r3" not in ids  # admin_bot
        assert "r1" in ids
        assert "r2" in ids
        assert "r4" in ids

    def test_member_bot_excludes_security(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        ids = {r.id for r in result}
        assert "r2" not in ids  # security

    def test_member_bot_excludes_other_bots_recs(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        ids = {r.id for r in result}
        assert "r3" not in ids  # admin_bot's rec

    def test_member_bot_includes_urgency_critical_operational(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        ids = {r.id for r in result}
        # r1 is operational + urgency:critical — exception allows it
        assert "r1" in ids

    def test_member_bot_excludes_non_critical_operational(self):
        recs = [
            _make_rec(
                id="r_op_med",
                dedup_key="health::team_bot_a::launchd",
                type="operational",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a", "urgency:medium"],
                bot_executable=True,
                status="pending",
            )
        ]
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        assert len(result) == 0

    def test_member_bot_respects_bot_executable_flag(self):
        recs = [
            _make_rec(
                id="r_nonexec",
                dedup_key="app_quality::team_bot_a::stale",
                type="app_quality",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a"],
                bot_executable=False,
                bridge_strategy=None,  # no bridge — exclude
                status="pending",
            ),
            _make_rec(
                id="r_bridged",
                dedup_key="app_quality::team_bot_a::mcp",
                type="app_quality",
                scope="bot",
                scope_id="team_bot_a",
                tags=["bot:team_bot_a"],
                bot_executable=False,
                bridge_strategy="task_queue",  # has bridge — include
                status="pending",
            ),
        ]
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        ids = {r.id for r in result}
        assert "r_nonexec" not in ids
        assert "r_bridged" in ids

    def test_member_bot_includes_app_quality_and_onboarding(self):
        recs = self._base_recs()
        result = BetterEngine.filter_for_surface(recs, "member_bot", scope_id="team_bot_a")
        ids = {r.id for r in result}
        assert "r4" in ids  # app_quality
        assert "r5" in ids  # onboarding


# ── engine.record_feedback() ──────────────────────────────────────────────────

class TestRecordFeedback:
    def test_accepted_signal_updates_status(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw", status="pending")
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        updated = engine.record_feedback("rec_001", "accepted")
        assert updated is not None
        assert updated.status == "accepted"
        assert updated.feedback_signal == "positive"
        assert updated.resolved_at is not None

    def test_rejected_signal_updates_status(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw", status="pending")
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        updated = engine.record_feedback("rec_001", "rejected", reason="not useful")
        assert updated is not None
        assert updated.status == "rejected"
        assert updated.feedback_signal == "negative"
        assert updated.feedback_reason == "not useful"

    def test_feedback_recorded_in_learning_file(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(
            id="rec_001",
            dedup_key="test::team_bot_a::gw",
            tags=["bot:team_bot_a", "category:gateway"],
            status="pending",
        )
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        engine.record_feedback("rec_001", "accepted")

        learning = storage.load_learning(engine.learning_path)
        # Tag signals updated
        assert "bot:team_bot_a" in learning["signals"]
        assert learning["signals"]["bot:team_bot_a"]["accepted"] == 1
        # Recent events logged
        assert len(learning["recent_events"]) == 1

    def test_returns_none_for_unknown_rec_id(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        storage.save_recommendations([], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)
        result = engine.record_feedback("no_such_id", "accepted")
        assert result is None


# ── engine.snooze() ───────────────────────────────────────────────────────────

class TestSnooze:
    def test_snooze_increments_count(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw",
                        status="pending", snooze_count=0)
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        updated = engine.snooze("rec_001")
        assert updated is not None
        assert updated.snooze_count == 1
        assert updated.status == "snoozed"

    def test_snooze_sets_snooze_until(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw",
                        status="pending", snooze_count=0)
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        updated = engine.snooze("rec_001")
        assert updated.snooze_until is not None
        # Should be ~1 day from now (first snooze)
        until = datetime.fromisoformat(updated.snooze_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        diff = (until - datetime.now(timezone.utc)).total_seconds()
        assert 86000 < diff < 86500, f"Expected ~1 day, got {diff}s"

    def test_snooze_escalates(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        # Pre-snoozed once — next snooze should be 2 days
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw",
                        status="pending", snooze_count=1)
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        updated = engine.snooze("rec_001")
        assert updated.snooze_count == 2
        until = datetime.fromisoformat(updated.snooze_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        diff = (until - datetime.now(timezone.utc)).total_seconds()
        assert 2 * 86000 < diff < 2 * 86500, f"Expected ~2 days, got {diff}s"

    def test_snooze_records_snoozed_signal(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        rec = _make_rec(id="rec_001", dedup_key="test::team_bot_a::gw",
                        status="pending", snooze_count=0,
                        tags=["bot:team_bot_a", "urgency:low"])
        storage.save_recommendations([rec], engine.recs_path)
        storage.save_learning({"signals": {}, "bot_overrides": {}, "recent_events": []},
                              engine.learning_path)

        engine.snooze("rec_001")
        learning = storage.load_learning(engine.learning_path)
        assert learning["signals"].get("bot:team_bot_a", {}).get("snoozed", 0) == 1


# ── onboarding.build_onboarding_state() ──────────────────────────────────────

class TestBuildOnboardingState:
    def test_returns_correct_fields(self, tmp_path):
        shared_dir = tmp_path / "evolve"
        shared_dir.mkdir()
        network = {
            "members": ["team_bot_a"],
            "thresholds": {"dailySpendAlertUsd": 5.0},
            "alerts": {"chatId": "12345"},
        }
        state = build_onboarding_state(shared_dir, network)
        assert "members" in state
        assert "health_cache_exists" in state
        assert "app_counts" in state
        assert "reviewed_app_counts" in state
        assert "spend_threshold_set" in state
        assert "pod_report_configured" in state
        # reviewed_proposals_count removed 2026-06-04 along with the
        # first_security_audit task — see test_pod_setup_onboarding.py
        # for the operator-review context.

    def test_health_cache_exists_false_when_missing(self, tmp_path):
        shared_dir = tmp_path / "evolve"
        shared_dir.mkdir()
        state = build_onboarding_state(shared_dir, {"members": []})
        assert state["health_cache_exists"] is False

    def test_health_cache_exists_true_when_present(self, tmp_path):
        shared_dir = tmp_path / "evolve"
        cache_dir = shared_dir / "better-engine" / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "health-latest.json").write_text("{}")
        state = build_onboarding_state(shared_dir, {"members": []})
        assert state["health_cache_exists"] is True

    def test_spend_threshold_set_from_network(self, tmp_path):
        shared_dir = tmp_path / "evolve"
        shared_dir.mkdir()
        network = {"members": [], "thresholds": {"dailySpendAlertUsd": 5.0}}
        state = build_onboarding_state(shared_dir, network)
        assert state["spend_threshold_set"] is True

    def test_pod_report_configured_from_network(self, tmp_path):
        shared_dir = tmp_path / "evolve"
        shared_dir.mkdir()
        network = {"members": [], "alerts": {"chatId": "abc"}}
        state = build_onboarding_state(shared_dir, network)
        assert state["pod_report_configured"] is True

    # test_reviewed_proposals_count removed 2026-06-04 alongside the
    # first_security_audit task. Proposals appearing is system output,
    # not operator setup. See test_pod_setup_onboarding.py for the
    # operator-review context.

    def test_missing_dirs_handled_gracefully(self, tmp_path):
        shared_dir = tmp_path / "does_not_exist"
        state = build_onboarding_state(shared_dir, {"members": ["team_bot_a"]})
        assert state["members"] == ["team_bot_a"]
        assert state["app_counts"] == {"team_bot_a": 0}


# ── onboarding.auto_complete_tasks() ─────────────────────────────────────────

class TestAutoCompleteTasks:
    def test_marks_newly_complete_task(self):
        state = {
            "members": ["team_bot_a"],
            "health_cache_exists": True,
            "app_counts": {"team_bot_a": 0},
            "reviewed_app_counts": {"team_bot_a": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {"tasks": {}, "dismissed": False}
        # add_first_bot check: state["members"] is non-empty → should auto-complete
        updated_gs, completed = auto_complete_tasks(state, getting_started)
        assert "add_first_bot" in completed
        assert updated_gs["tasks"]["add_first_bot"]["completed"] is True
        assert updated_gs["tasks"]["add_first_bot"]["how"] == "auto"

    def test_already_complete_not_re_completed(self):
        state = {
            "members": ["team_bot_a"],
            "health_cache_exists": True,
            "app_counts": {"team_bot_a": 0},
            "reviewed_app_counts": {"team_bot_a": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {
            "tasks": {
                "add_first_bot": {
                    "completed": True, "completed_at": "2026-01-01T00:00:00Z", "how": "manual"
                }
            },
            "dismissed": False,
        }
        _, completed = auto_complete_tasks(state, getting_started)
        assert "add_first_bot" not in completed

    def test_health_cache_task_completes(self):
        state = {
            "members": ["team_bot_a"],
            "health_cache_exists": True,
            "app_counts": {"team_bot_a": 0},
            "reviewed_app_counts": {"team_bot_a": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {
            "tasks": {
                "add_first_bot": {"completed": True, "completed_at": "x", "how": "auto"}
            },
            "dismissed": False,
        }
        _, completed = auto_complete_tasks(state, getting_started)
        assert "run_health_check" in completed

    def test_dismissed_guide_returns_empty(self):
        state = {"members": ["team_bot_a"], "health_cache_exists": True,
                 "app_counts": {}, "reviewed_app_counts": {},
                 "spend_threshold_set": False, "pod_report_configured": False,
                 "reviewed_proposals_count": 0}
        getting_started = {"dismissed": True, "tasks": {}}
        _, completed = auto_complete_tasks(state, getting_started)
        assert completed == []


# ── onboarding.get_active_tasks() ────────────────────────────────────────────

class TestGetActiveTasks:
    def test_returns_only_unlocked_tasks(self):
        state = {
            "members": ["team_bot_a"],
            "health_cache_exists": False,
            "app_counts": {"team_bot_a": 0},
            "reviewed_app_counts": {"team_bot_a": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {"dismissed": False, "tasks": {}}
        active = get_active_tasks(state, getting_started, members=["team_bot_a"])
        ids = [t.id for t in active]
        # Only add_first_bot has no dependencies — should be the only active task
        assert "add_first_bot" in ids
        # run_health_check depends on add_first_bot, so not active yet
        assert "run_health_check" not in ids

    def test_skips_completed_tasks(self):
        state = {
            "members": ["team_bot_a"],
            "health_cache_exists": False,
            "app_counts": {"team_bot_a": 0},
            "reviewed_app_counts": {"team_bot_a": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {
            "dismissed": False,
            "tasks": {
                "add_first_bot": {"completed": True, "completed_at": "x", "how": "auto"},
            },
        }
        active = get_active_tasks(state, getting_started, members=["team_bot_a"])
        ids = [t.id for t in active]
        assert "add_first_bot" not in ids
        # run_health_check now unlocked
        assert "run_health_check" in ids

    def test_per_bot_tasks_instantiated(self):
        state = {
            "members": ["team_bot_a", "admin_bot"],
            "health_cache_exists": True,
            "app_counts": {"team_bot_a": 0, "admin_bot": 0},
            "reviewed_app_counts": {"team_bot_a": 0, "admin_bot": 0},
            "spend_threshold_set": False,
            "pod_report_configured": False,
            "reviewed_proposals_count": 0,
        }
        getting_started = {
            "dismissed": False,
            "tasks": {
                "add_first_bot": {"completed": True, "completed_at": "x", "how": "auto"},
                "run_health_check": {"completed": True, "completed_at": "x", "how": "auto"},
            },
        }
        active = get_active_tasks(state, getting_started, members=["team_bot_a", "admin_bot"])
        ids = [t.id for t in active]
        assert "scan_applications_team_bot_a" in ids
        assert "scan_applications_admin_bot" in ids

    def test_dismissed_guide_returns_empty(self):
        state = {"members": ["team_bot_a"], "health_cache_exists": False,
                 "app_counts": {}, "reviewed_app_counts": {},
                 "spend_threshold_set": False, "pod_report_configured": False,
                 "reviewed_proposals_count": 0}
        getting_started = {"dismissed": True, "tasks": {}}
        active = get_active_tasks(state, getting_started, members=["team_bot_a"])
        assert active == []


# ── pending_tasks.add_task() ──────────────────────────────────────────────────

class TestAddTask:
    def test_creates_file_if_missing(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)
        assert not path.exists()

        task = add_task(
            bot_id="team_bot_a",
            rec_id="rec_001",
            dedup_key="test::team_bot_a::mcp",
            type="app_quality",
            title="Install mem0",
            detail="Enables persistent memory.",
            context="User requested this.",
            action="install_mcp_server",
            action_args={"server": "mem0"},
            path=path,
        )
        assert path.exists()
        assert task.id.startswith("pat_")
        assert task.bot_id == "team_bot_a"

    def test_appends_to_existing(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)

        task1 = add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T1", detail="D1", context="C1",
            action="act1", action_args={}, path=path,
        )
        task2 = add_task(
            bot_id="team_bot_a", rec_id="r2", dedup_key="k2", type="app_quality",
            title="T2", detail="D2", context="C2",
            action="act2", action_args={}, path=path,
        )
        tasks = load_pending_tasks(path)
        assert len(tasks) == 2
        ids = {t.id for t in tasks}
        assert task1.id in ids
        assert task2.id in ids

    def test_task_id_is_unique(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)

        ids = set()
        for i in range(5):
            t = add_task(
                bot_id="team_bot_a", rec_id=f"r{i}", dedup_key=f"k{i}", type="app_quality",
                title=f"T{i}", detail="D", context="C",
                action="act", action_args={}, path=path,
            )
            ids.add(t.id)
        assert len(ids) == 5  # all unique


# ── pending_tasks.resolve_task() ─────────────────────────────────────────────

class TestResolveTask:
    def test_sets_resolved_fields(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)

        task = add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        resolved = resolve_task(task.id, "admin_completed", path)
        assert resolved is not None
        assert resolved.resolved_how == "admin_completed"
        assert resolved.resolved_at is not None

    def test_returns_none_for_unknown_id(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)

        add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        result = resolve_task("no_such_id", "admin_completed", path)
        assert result is None

    def test_resolved_task_persisted(self, tmp_path):
        workspace = tmp_path / "team_bot_a_workspace"
        workspace.mkdir()
        path = pending_tasks_path(workspace)

        task = add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        resolve_task(task.id, "admin_dismissed", path)

        tasks = load_pending_tasks(path)
        resolved = next(t for t in tasks if t.id == task.id)
        assert resolved.resolved_how == "admin_dismissed"
        assert resolved.resolved_at is not None


# ── pending_tasks.get_all_pending_tasks() ────────────────────────────────────

class TestGetAllPendingTasks:
    def test_aggregates_across_bots(self, tmp_path):
        team_bot_a_ws = tmp_path / "team_bot_a"
        team_bot_a_ws.mkdir()
        admin_bot_ws = tmp_path / "admin_bot"
        admin_bot_ws.mkdir()

        add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="Team_bot_a task", detail="D", context="C",
            action="act", action_args={},
            path=pending_tasks_path(team_bot_a_ws),
        )
        add_task(
            bot_id="admin_bot", rec_id="r2", dedup_key="k2", type="app_quality",
            title="Admin_bot task", detail="D", context="C",
            action="act", action_args={},
            path=pending_tasks_path(admin_bot_ws),
        )

        all_tasks = get_all_pending_tasks({"team_bot_a": team_bot_a_ws, "admin_bot": admin_bot_ws})
        bot_ids = {t.bot_id for t in all_tasks}
        assert "team_bot_a" in bot_ids
        assert "admin_bot" in bot_ids

    def test_excludes_resolved_tasks(self, tmp_path):
        team_bot_a_ws = tmp_path / "team_bot_a"
        team_bot_a_ws.mkdir()
        path = pending_tasks_path(team_bot_a_ws)

        task = add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        resolve_task(task.id, "admin_completed", path)

        all_tasks = get_all_pending_tasks({"team_bot_a": team_bot_a_ws})
        assert len(all_tasks) == 0

    def test_sorted_by_queued_at(self, tmp_path):
        team_bot_a_ws = tmp_path / "team_bot_a"
        team_bot_a_ws.mkdir()
        path = pending_tasks_path(team_bot_a_ws)

        t1 = add_task(
            bot_id="team_bot_a", rec_id="r1", dedup_key="k1", type="app_quality",
            title="T1", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        time.sleep(0.01)  # ensure different timestamps
        t2 = add_task(
            bot_id="team_bot_a", rec_id="r2", dedup_key="k2", type="app_quality",
            title="T2", detail="D", context="C",
            action="act", action_args={}, path=path,
        )
        all_tasks = get_all_pending_tasks({"team_bot_a": team_bot_a_ws})
        assert all_tasks[0].id == t1.id
        assert all_tasks[1].id == t2.id

    def test_empty_dir_returns_empty_list(self, tmp_path):
        team_bot_a_ws = tmp_path / "team_bot_a"
        team_bot_a_ws.mkdir()
        all_tasks = get_all_pending_tasks({"team_bot_a": team_bot_a_ws})
        assert all_tasks == []

    def test_missing_workspace_returns_empty_list(self, tmp_path):
        # Workspace dir doesn't exist at all
        missing_ws = tmp_path / "nonexistent_bot"
        all_tasks = get_all_pending_tasks({"ghost": missing_ws})
        assert all_tasks == []


# ── engine.refresh() (zero adapters) ─────────────────────────────────────────

class TestEngineRefresh:
    def test_refresh_with_zero_adapters_returns_empty_list(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        pending = engine.refresh(adapters=[])
        assert pending == []

    def test_refresh_creates_recommendations_file(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        engine.refresh(adapters=[])
        assert engine.recs_path.exists()

    def test_refresh_preserves_existing_accepted_recs(self, tmp_path):
        engine, _ = _make_engine(tmp_path)
        # Pre-populate with an accepted rec
        rec = _make_rec(
            id="rec_acc",
            dedup_key="test::team_bot_a::acc",
            status="accepted",
        )
        storage.save_recommendations([rec], engine.recs_path)

        engine.refresh(adapters=[])

        recs = storage.load_recommendations(engine.recs_path)
        accepted = [r for r in recs if r.status == "accepted"]
        assert any(r.id == "rec_acc" for r in accepted)

    def test_refresh_prunes_auto_resolved_arbiter_recs(self, tmp_path):
        """Arbiter-bridge recs (dedup_key prefix `arbiter::`) drop when their
        source proposal disappears — dedup_keys are unbounded per proposal_id
        so accumulating auto_resolved entries grows the file without bound.
        Other adapters reuse a fixed set of dedup_keys, so their auto_resolved
        entries are useful history and stay (caught arbiter-bridge V1 finding
        2026-05-02-005: extra=306 stale arbiter recs)."""
        engine, _ = _make_engine(tmp_path)
        arbiter_rec = _make_rec(
            id="rec_prop_p1",
            dedup_key="arbiter::sysadmin_watchdog::p1",
            source="generator:sysadmin_watchdog",
            status="pending",
        )
        health_rec = _make_rec(
            id="rec_health",
            dedup_key="health::team_bot_a::gateway::test",
            status="pending",
        )
        storage.save_recommendations(
            [arbiter_rec, health_rec], engine.recs_path
        )

        # No adapter → merge auto_resolves both; prune step drops the arbiter
        # one but keeps the health rec as auto_resolved history.
        engine.refresh(adapters=[])

        recs = storage.load_recommendations(engine.recs_path)
        keys = {r.dedup_key for r in recs}
        assert "arbiter::sysadmin_watchdog::p1" not in keys
        assert "health::team_bot_a::gateway::test" in keys
        health_after = next(
            r for r in recs if r.dedup_key == "health::team_bot_a::gateway::test"
        )
        assert health_after.status == "auto_resolved"

    def test_refresh_keeps_pending_arbiter_recs(self, tmp_path):
        """An arbiter rec whose proposal is still in pending/snoozed (i.e. an
        adapter is still emitting it) must not be pruned."""
        from evolve_admin.better_engine.engine import BetterEngine

        engine, _ = _make_engine(tmp_path)
        arbiter_rec = _make_rec(
            id="rec_prop_p1",
            dedup_key="arbiter::sysadmin_watchdog::p1",
            source="generator:sysadmin_watchdog",
            status="pending",
        )
        storage.save_recommendations([arbiter_rec], engine.recs_path)

        class _StillEmittingAdapter:
            source_name = "proposal_reader"

            def generate(self, shared_dir, network):
                return [
                    _make_rec(
                        id="rec_prop_p1",
                        dedup_key="arbiter::sysadmin_watchdog::p1",
                        source="generator:sysadmin_watchdog",
                        status="pending",
                    )
                ]

        engine.refresh(adapters=[_StillEmittingAdapter()])

        recs = storage.load_recommendations(engine.recs_path)
        keys = {r.dedup_key for r in recs}
        assert "arbiter::sysadmin_watchdog::p1" in keys

"""
Tests for Better Engine Tier 3: OnboardingAdapter, WhimsyAdapter,
hints.py, suggestions.py, and whimsy dynamic impact.

Run with:
    cd /Users/pod_admin/GitHub/evolve/.claude/worktrees/strange-lichterman
    python -m pytest packages/admin/tests/test_better_engine_tier3.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.better_engine.model import Recommendation, now_iso
from evolve_admin.better_engine.adapters.onboarding import OnboardingAdapter
from evolve_admin.better_engine.adapters.whimsy import WhimsyAdapter
from evolve_admin.better_engine.hints import (
    generate_triggers,
    build_hints_file,
    write_hints_for_bot,
)
# The suggestions module was retired alongside its better_engine.suggestions
# adapter path — exploratory app suggestions now flow through the
# generators/app_suggester generator. Tests for that generator live at:
#   packages/analyzer/tests/test_app_suggester_generator.py
from evolve_admin.better_engine.scoring import compute_priority_score


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_shared(tmp_path: Path) -> Path:
    """Minimal shared_dir tree."""
    (tmp_path / "better-engine" / "cache").mkdir(parents=True)
    (tmp_path / "better-engine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics").mkdir(parents=True)
    (tmp_path / "alerts").mkdir(parents=True)
    (tmp_path / "proposals" / "reviewed").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def network() -> dict:
    return {"members": ["team_bot_a", "admin_bot"]}


@pytest.fixture
def single_member_network() -> dict:
    return {"members": ["team_bot_a"]}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_metrics(shared_dir: Path, bot_id: str, day: date, data: dict) -> None:
    """Write a daily metrics file."""
    day_dir = shared_dir / "metrics" / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{bot_id}.json").write_text(json.dumps(data))


def _base_metrics(**kwargs) -> dict:
    """Build a base metrics dict with sensible defaults.

    Defaults: session_count=10, maintenance_ratio=0.10 (maintenance_sessions=1).
    Override maintenance_sessions to change effective ratio.
    """
    defaults = {
        "session_count": 10,
        "productive_sessions": 8,
        "maintenance_sessions": 1,   # 1/10 = 0.10 ratio — below all thresholds
        "maintenance_ratio": 0.1,
        "correction_sessions": 1,
        "correction_rate": 0.1,
        "top_maintenance_signals": [],
        "application_corrections": {},
    }
    defaults.update(kwargs)
    return defaults


def _make_pending_rec(type_: str = "operational", scope_id: str = "team_bot_a",
                      score: int = 50) -> Recommendation:
    """Build a minimal pending Recommendation for testing."""
    return Recommendation(
        id="rec_test_001",
        dedup_key=f"test::{scope_id}::thing",
        type=type_,
        source="health_check",
        scope="bot",
        scope_id=scope_id,
        title=f"Test rec for {scope_id}",
        detail="Test detail",
        context="",
        action_label=None,
        action=None,
        priority_score=score,
        priority_components={"urgency": 20, "impact": 16, "actionability": 14, "freshness": 10},
        tags=[f"bot:{scope_id}", f"type:{type_}"],
        status="pending",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# OnboardingAdapter tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestOnboardingAdapter:

    def test_active_task_produces_rec(self, tmp_shared, single_member_network):
        """An incomplete task with dependencies met → Recommendation emitted."""
        # Mark add_first_bot as complete (team_bot_a is already a member), which unlocks
        # run_health_check
        gs_path = tmp_shared / "better-engine" / "getting-started.json"
        gs_path.write_text(json.dumps({
            "schema_version": 1,
            "dismissed": False,
            "tasks": {
                "add_first_bot": {
                    "completed": True,
                    "completed_at": now_iso(),
                    "how": "auto",
                }
            }
        }))
        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, single_member_network)
        keys = {r.dedup_key for r in recs}
        assert any("run_health_check" in k for k in keys), \
            f"Expected run_health_check to surface; got: {keys}"

    def test_completed_task_not_emitted(self, tmp_shared, single_member_network):
        """A task marked completed in getting-started.json → no rec."""
        gs_path = tmp_shared / "better-engine" / "getting-started.json"
        gs_data = {
            "schema_version": 1,
            "dismissed": False,
            "tasks": {
                "add_first_bot": {
                    "completed": True,
                    "completed_at": now_iso(),
                    "how": "auto",
                },
                "run_health_check": {
                    "completed": True,
                    "completed_at": now_iso(),
                    "how": "auto",
                }
            }
        }
        gs_path.write_text(json.dumps(gs_data))

        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, single_member_network)
        keys = {r.dedup_key for r in recs}
        assert not any("run_health_check" in k for k in keys), \
            "Completed task should not emit a rec"

    def test_dependency_not_met_suppresses_task(self, tmp_shared):
        """scan_applications depends on run_health_check; if not done, no scan rec."""
        # Network has a member but run_health_check is not complete
        network = {"members": ["team_bot_a"]}
        # getting-started.json absent → add_first_bot auto-complete (members non-empty)
        # but run_health_check not done → scan_applications_team_bot_a blocked
        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, network)
        keys = {r.dedup_key for r in recs}
        # scan_applications_team_bot_a requires run_health_check to be complete
        assert not any("scan_applications_team_bot_a" in k for k in keys), \
            f"scan_applications should be blocked; keys: {keys}"

    def test_dismissed_guide_returns_empty(self, tmp_shared, single_member_network):
        """dismissed: true in getting-started.json → no recs."""
        gs_path = tmp_shared / "better-engine" / "getting-started.json"
        gs_path.write_text(json.dumps({
            "schema_version": 1,
            "dismissed": True,
            "tasks": {}
        }))
        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, single_member_network)
        assert recs == []

    def test_rec_type_is_onboarding(self, tmp_shared, single_member_network):
        """All onboarding recs have type='onboarding' and source='onboarding'."""
        # No getting-started.json → add_first_bot (depends on nothing) should surface
        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, single_member_network)
        assert recs, "Expected at least add_first_bot task to surface with empty getting-started"
        for r in recs:
            assert r.type == "onboarding"
            assert r.source == "onboarding"

    def test_dedup_key_format(self, tmp_shared, single_member_network):
        """Dedup keys follow onboarding::{scope_id}::{task_id} format."""
        adapter = OnboardingAdapter()
        recs = adapter.generate(tmp_shared, single_member_network)
        for r in recs:
            assert r.dedup_key.startswith("onboarding::")
            parts = r.dedup_key.split("::")
            assert len(parts) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# WhimsyAdapter tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWhimsyAdapter:

    def _write_pool(self, shared_dir: Path, items: list[dict]) -> None:
        pool_path = shared_dir / "better-engine" / "whimsy-pool.json"
        pool_path.parent.mkdir(parents=True, exist_ok=True)
        pool_path.write_text(json.dumps(items))

    def test_unused_items_become_recs(self, tmp_shared):
        items = [
            {"id": "w001", "type": "fun_fact", "content": "Test fact.",
             "answer": None, "created_at": now_iso(), "used": False},
            {"id": "w002", "type": "dad_joke", "content": "Why? Because.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert len(recs) == 2
        assert all(r.type == "whimsy" for r in recs)

    def test_used_items_are_skipped(self, tmp_shared):
        items = [
            {"id": "w001", "type": "fun_fact", "content": "Seen it.",
             "answer": None, "created_at": now_iso(), "used": True},
            {"id": "w002", "type": "fun_fact", "content": "New fact.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert len(recs) == 1
        # Whimsy dedup keys include the ISO week so items can rotate weekly;
        # we just check the item-id portion.
        assert recs[0].dedup_key.startswith("whimsy::w002")

    def test_riddle_carries_answer_in_source_ref(self, tmp_shared):
        """Riddles no longer get a dedicated action — their answer is
        exposed via source_ref for the member-bot surface to render.
        All whimsy shares the same 'Got it' accept_label."""
        items = [
            {"id": "riddle001", "type": "riddle",
             "content": "What has keys but no locks?",
             "answer": "A piano.", "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert len(recs) == 1
        r = recs[0]
        assert r.action is None
        assert r.accept_label == "Got it"

    def test_non_riddle_no_action(self, tmp_shared):
        items = [
            {"id": "w003", "type": "fun_fact",
             "content": "Bees can recognize human faces.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert len(recs) == 1
        r = recs[0]
        assert r.action is None

    def test_missing_pool_falls_back_to_seed(self, tmp_shared):
        """If whimsy-pool.json is absent, falls back to whimsy_seed.json."""
        adapter = WhimsyAdapter()
        # No pool file written
        recs = adapter.generate(tmp_shared, {})
        # Should get items from seed file
        assert len(recs) > 0
        assert all(r.type == "whimsy" for r in recs)

    def test_all_recs_bot_executable(self, tmp_shared):
        items = [
            {"id": "w001", "type": "quote", "content": "Be yourself.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert all(r.bot_executable for r in recs)

    def test_scope_is_admin_pod(self, tmp_shared):
        items = [
            {"id": "w001", "type": "word_of_the_day",
             "content": "Petrichor (n): the smell of rain on dry earth.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert recs[0].scope == "admin"
        assert recs[0].scope_id == "pod"

    def test_tags_include_whimsy_type(self, tmp_shared):
        items = [
            {"id": "w001", "type": "historical_trivia",
             "content": "Napoleon was not actually short.",
             "answer": None, "created_at": now_iso(), "used": False},
        ]
        self._write_pool(tmp_shared, items)
        adapter = WhimsyAdapter()
        recs = adapter.generate(tmp_shared, {})
        assert "whimsy_type:historical_trivia" in recs[0].tags
        assert "source:whimsy" in recs[0].tags


# ═══════════════════════════════════════════════════════════════════════════════
# hints.generate_triggers tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateTriggers:

    def _make_rec(self, title: str, tags: list[str], type_: str = "app_quality",
                  source: str = "compliance_scan") -> Recommendation:
        return Recommendation(
            id="rec_test",
            dedup_key="test::team_bot_a::thing",
            type=type_,
            source=source,
            scope="bot",
            scope_id="team_bot_a",
            title=title,
            detail="",
            context="",
            action_label=None,
            action=None,
            tags=tags,
            status="pending",
        )

    def test_app_tag_produces_both_forms(self):
        """app:health-tracker → both 'health-tracker' and 'health tracker'."""
        rec = self._make_rec(
            "Check this app",
            ["app:health-tracker", "source:compliance_scan"]
        )
        triggers = generate_triggers(rec)
        assert "health-tracker" in triggers
        assert "health tracker" in triggers

    def test_domain_tag_produces_keyword(self):
        """domain:health → 'health' in triggers."""
        rec = self._make_rec(
            "Something about wellness",
            ["domain:health", "source:compliance_scan"]
        )
        triggers = generate_triggers(rec)
        assert "health" in triggers

    def test_title_bigrams_generated(self):
        """Adjacent word pairs from title appear as triggers."""
        rec = self._make_rec(
            "health tracker review needed",
            []
        )
        triggers = generate_triggers(rec)
        assert "tracker review" in triggers

    def test_short_words_excluded(self):
        """Words <= 5 chars are not added as standalone triggers (only in bigrams)."""
        rec = self._make_rec(
            "app is bad",
            []
        )
        triggers = generate_triggers(rec)
        # "app", "is", "bad" are all <= 5 chars — should not appear as standalone
        assert "app" not in triggers
        assert "is" not in triggers
        assert "bad" not in triggers

    def test_max_15_triggers(self):
        """Result is capped at 15 triggers."""
        # Create lots of tags to generate many triggers
        tags = [f"app:capability-{i}" for i in range(20)]
        rec = self._make_rec("some long title with many words here today", tags)
        triggers = generate_triggers(rec)
        assert len(triggers) <= 15

    # The test_explore_llm_source_stub_returns_no_extra test was retired
    # alongside the ``explore`` type / ``llm_suggestion`` source pair.
    # Exploratory recs from generators/app_suggester surface as
    # ``app_quality`` via the proposal_reader bridge and are exercised by
    # the existing app_quality / app_name / domain tests above.

    def test_sorted_deduplicated(self):
        """Triggers are sorted and deduplicated."""
        rec = self._make_rec(
            "health tracker status",
            ["app:health-tracker", "domain:health"]
        )
        triggers = generate_triggers(rec)
        assert triggers == sorted(set(triggers))


# ═══════════════════════════════════════════════════════════════════════════════
# hints.build_hints_file tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildHintsFile:

    def test_only_eligible_types_included(self):
        """Only explore, app_quality, onboarding types appear in hints."""
        recs = [
            _make_pending_rec("operational"),
            _make_pending_rec("security"),
            _make_pending_rec("cost"),
            _make_pending_rec("whimsy"),
        ]
        # Give them tags that generate triggers
        for r in recs:
            r.tags.append("domain:health")

        result = build_hints_file(recs, "team_bot_a")
        # All operational/security/cost/whimsy recs should be excluded
        assert result["hints"] == []

    def test_app_quality_included(self):
        """app_quality recs appear in hints."""
        rec = _make_pending_rec("app_quality")
        rec.tags = ["bot:team_bot_a", "app:health-tracker", "domain:health"]
        result = build_hints_file([rec], "team_bot_a")
        assert len(result["hints"]) == 1
        assert result["hints"][0]["type"] == "app_quality"

    def test_only_pending_status(self):
        """Non-pending recs are excluded."""
        rec = _make_pending_rec("app_quality")
        rec.tags = ["app:health-tracker"]
        rec.status = "accepted"
        result = build_hints_file([rec], "team_bot_a")
        assert result["hints"] == []

    def test_not_executable_no_bridge_excluded(self):
        """Recs with bot_executable=False and bridge_strategy=None excluded."""
        rec = _make_pending_rec("app_quality")
        rec.tags = ["app:health-tracker", "domain:health"]
        rec.bot_executable = False
        rec.bridge_strategy = None
        result = build_hints_file([rec], "team_bot_a")
        assert result["hints"] == []

    def test_bot_executable_included(self):
        """Recs with bot_executable=True included."""
        rec = _make_pending_rec("app_quality")
        rec.tags = ["app:health-tracker", "domain:health"]
        rec.bot_executable = True
        result = build_hints_file([rec], "team_bot_a")
        assert len(result["hints"]) == 1

    def test_hints_file_schema(self):
        """Result has required fields."""
        rec = _make_pending_rec("onboarding")
        rec.tags = ["app:some-app", "domain:health"]
        rec.title = "Scan applications for health tracking"
        result = build_hints_file([rec], "team_bot_a")
        assert "generated_at" in result
        assert "bot_id" in result
        assert result["bot_id"] == "team_bot_a"
        assert "hints" in result

    def test_hint_has_required_fields(self):
        """Each hint entry has rec_id, type, priority_score, triggers, hint."""
        rec = _make_pending_rec("app_quality")
        rec.tags = ["app:health-tracker"]
        result = build_hints_file([rec], "team_bot_a")
        if result["hints"]:
            h = result["hints"][0]
            assert "rec_id" in h
            assert "type" in h
            assert "priority_score" in h
            assert "triggers" in h
            assert "hint" in h


# ═══════════════════════════════════════════════════════════════════════════════
# hints.write_hints_for_bot tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWriteHintsForBot:

    def test_creates_file_in_correct_location(self, tmp_path):
        """rec-hints.json is written to {workspace_dir}/evolve/rec-hints.json."""
        workspace_dir = tmp_path / "workspace"
        rec = _make_pending_rec("app_quality")
        rec.tags = ["app:health-tracker"]
        write_hints_for_bot([rec], "team_bot_a", workspace_dir)
        hints_file = workspace_dir / "evolve" / "rec-hints.json"
        assert hints_file.exists()

    def test_creates_parent_dir_if_missing(self, tmp_path):
        """Creates workspace/evolve/ directory if it doesn't exist."""
        workspace_dir = tmp_path / "new-workspace"
        assert not workspace_dir.exists()
        write_hints_for_bot([], "team_bot_a", workspace_dir)
        assert (workspace_dir / "evolve").exists()

    def test_file_is_valid_json(self, tmp_path):
        """Written file is valid JSON."""
        workspace_dir = tmp_path / "workspace"
        rec = _make_pending_rec("onboarding")
        rec.tags = ["domain:health"]
        rec.title = "Scan Team_bot_a for applications"
        write_hints_for_bot([rec], "team_bot_a", workspace_dir)
        text = (workspace_dir / "evolve" / "rec-hints.json").read_text()
        data = json.loads(text)
        assert "generated_at" in data
        assert data["bot_id"] == "team_bot_a"

    def test_atomic_write(self, tmp_path):
        """No .tmp file left behind after write."""
        workspace_dir = tmp_path / "workspace"
        write_hints_for_bot([], "team_bot_a", workspace_dir)
        tmp_files = list((workspace_dir / "evolve").glob("*.tmp"))
        assert tmp_files == []


# ═══════════════════════════════════════════════════════════════════════════════
# Whimsy dynamic impact tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWhimsyDynamicImpact:

    def _make_whimsy_rec(self, score: int = 0) -> Recommendation:
        r = Recommendation(
            id="rec_whimsy",
            dedup_key="whimsy::w001",
            type="whimsy",
            source="whimsy",
            scope="admin",
            scope_id="pod",
            title="Fun fact",
            detail="Interesting.",
            context="",
            action_label="Show me",
            action=None,
            priority_score=score,
            priority_components={
                "urgency": 6,
                "impact": 6,
                "actionability": 4,
                "freshness": 10,
            },
            tags=["source:whimsy"],
            status="pending",
        )
        return r

    def _simulate_engine_whimsy_step(self, all_recs: list[Recommendation]) -> None:
        """Simulate the whimsy dynamic impact recalculation in engine.refresh()."""
        pending_non_whimsy = sum(
            1 for r in all_recs if r.status == "pending" and r.type != "whimsy"
        )
        for rec in all_recs:
            if rec.type == "whimsy" and rec.status == "pending":
                impact = max(0, 12 - (pending_non_whimsy * 2))
                rec.priority_components["impact"] = impact
                rec.priority_score = compute_priority_score(rec)

    def test_high_pending_count_lowers_whimsy_impact(self):
        """With many pending non-whimsy recs, whimsy impact approaches 0."""
        whimsy = self._make_whimsy_rec()
        other_recs = [_make_pending_rec("operational", score=80) for _ in range(7)]
        all_recs = [whimsy] + other_recs

        self._simulate_engine_whimsy_step(all_recs)

        # 7 non-whimsy pending → impact = max(0, 12 - 14) = 0
        assert whimsy.priority_components["impact"] == 0

    def test_empty_queue_gives_high_whimsy_impact(self):
        """With no pending non-whimsy recs, whimsy gets max impact (12)."""
        whimsy = self._make_whimsy_rec()
        all_recs = [whimsy]

        self._simulate_engine_whimsy_step(all_recs)

        # 0 non-whimsy → impact = max(0, 12 - 0) = 12
        assert whimsy.priority_components["impact"] == 12

    def test_moderate_queue_intermediate_impact(self):
        """With 3 non-whimsy recs, impact = 12 - 6 = 6."""
        whimsy = self._make_whimsy_rec()
        other_recs = [_make_pending_rec("operational", score=50) for _ in range(3)]
        all_recs = [whimsy] + other_recs

        self._simulate_engine_whimsy_step(all_recs)

        assert whimsy.priority_components["impact"] == 6


# The TestShouldRegenerate and TestSuggestionsCacheRoundTrip classes
# that used to live here were retired alongside the suggestions module.
# Exploratory app suggestions now flow through generators/app_suggester
# (tests at packages/analyzer/tests/test_app_suggester_generator.py).

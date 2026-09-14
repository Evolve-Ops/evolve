"""
Tests for Better Engine Tier 4: surface_rewrite(), filter_for_surface() with
rewriting, and the /api/better/recommendations?surface=member_bot endpoint.

The legacy MEMBER_BOT_REWRITES template system was removed — adapters
that target the bot-user audience must set member_bot_title/detail at
emit time, and filter_for_surface excludes recs without member_bot_title
from the member_bot surface. These tests pin that contract.

Run with:
    cd /Users/pod_admin/GitHub/evolve/.claude/worktrees/strange-lichterman
    python -m pytest packages/admin/tests/test_better_engine_tier4.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.better_engine.model import Recommendation, now_iso
from evolve_admin.better_engine.engine import (
    BetterEngine,
    surface_rewrite,
)
from evolve_admin.better_engine.storage import save_recommendations


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rec(**kwargs) -> Recommendation:
    """Create a minimal valid pending Recommendation with overridable fields.

    Defaults set member_bot_title/detail so tests that aren't specifically
    about the no-bot-copy exclusion path get past the contract gate. Pass
    member_bot_title=None to test that exclusion path explicitly.
    """
    defaults = dict(
        id="rec_001",
        dedup_key="compliance::team_bot_a::health-tracker::stale",
        type="app_quality",
        source="compliance_scan",
        scope="bot",
        scope_id="team_bot_a",
        title="health-tracker hasn't been reviewed in 112 days",
        detail="The manifest is stale and may not reflect current behavior.",
        context="Last reviewed 112 days ago.",
        action_label="Start review",
        action="start_review_flow",
        action_args={"app_id": "health-tracker", "bot_id": "team_bot_a"},
        bot_executable=True,
        bridge_strategy=None,
        accept_label="Start review",
        member_bot_title="Your health-tracker app could use a review.",
        member_bot_detail="It's been a while since we checked it.",
        tags=["bot:team_bot_a", "app:health-tracker", "source:compliance_scan",
              "intent:improve"],
        priority_score=55,
        priority_components={"urgency": 14, "impact": 12,
                             "actionability": 20, "freshness": 7},
        learning_weight=1.0,
        status="pending",
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


def _make_engine(tmp_path: Path) -> BetterEngine:
    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {"members": ["team_bot_a"], "sharedDir": str(shared_dir)}
    return BetterEngine(shared_dir=shared_dir, network=network)


# ── surface_rewrite() unit tests ──────────────────────────────────────────────

class TestSurfaceRewrite:
    def test_admin_surface_returns_rec_unchanged(self):
        rec = _make_rec()
        result = surface_rewrite(rec, "admin")
        assert result is rec  # same object, not a copy

    def test_unknown_surface_returns_rec_unchanged(self):
        rec = _make_rec()
        result = surface_rewrite(rec, "api")
        assert result is rec

    def test_member_bot_with_explicit_title_uses_it(self):
        rec = _make_rec(
            member_bot_title="Your health tracker could be better",
            member_bot_detail="A quick check would help it match how you use it.",
        )
        result = surface_rewrite(rec, "member_bot")
        # Should return a copy with overridden title/detail
        assert result is not rec
        assert result.title == "Your health tracker could be better"
        assert result.detail == "A quick check would help it match how you use it."

    def test_member_bot_explicit_title_does_not_mutate_original(self):
        original_title = "health-tracker hasn't been reviewed in 112 days"
        rec = _make_rec(
            member_bot_title="Your health tracker could be better",
            member_bot_detail="Some casual detail.",
        )
        _ = surface_rewrite(rec, "member_bot")
        # Original rec is untouched
        assert rec.title == original_title

    def test_member_bot_without_explicit_title_returns_unchanged(self):
        """Adapters that don't set member_bot_title get the rec back unchanged.

        filter_for_surface excludes such recs from the member_bot surface
        in the first place, so this only fires defensively if a caller
        invokes surface_rewrite directly. The contract: no rewrite.
        """
        rec = _make_rec(member_bot_title=None, member_bot_detail=None)
        result = surface_rewrite(rec, "member_bot")
        assert result is rec  # unchanged, same object

    def test_member_bot_explicit_title_only_no_member_detail(self):
        """If member_bot_title is set but member_bot_detail is None, only title is overridden."""
        original_detail = "Original admin-narrative detail."
        rec = _make_rec(
            detail=original_detail,
            member_bot_title="Custom casual title",
            member_bot_detail=None,
        )
        result = surface_rewrite(rec, "member_bot")
        assert result.title == "Custom casual title"
        # detail should remain the original (not overridden)
        assert result.detail == original_detail


# ── filter_for_surface() with rewriting ──────────────────────────────────────

class TestFilterForSurfaceWithRewrite:
    def test_member_bot_excludes_recs_without_member_bot_title(self, tmp_path):
        """Recs whose adapter didn't set member_bot_title are admin-only;
        the member_bot surface filters them out. Whimsy is exempt because
        the WhimsyAdapter always populates member_bot_title."""
        engine = _make_engine(tmp_path)
        rec = _make_rec(
            type="app_quality",
            source="compliance_scan",
            tags=["bot:team_bot_a", "app:health-tracker"],
            member_bot_title=None,
            member_bot_detail=None,
        )
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert result == []

    def test_member_bot_includes_recs_with_member_bot_title(self, tmp_path):
        engine = _make_engine(tmp_path)
        rec = _make_rec(
            type="app_quality",
            source="compliance_scan",
            tags=["bot:team_bot_a", "app:health-tracker"],
            action_args={"app_id": "health-tracker"},
            member_bot_title="Your health-tracker could use a review",
            member_bot_detail="It's been a while since we checked it.",
        )
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert len(result) == 1
        assert result[0].title == "Your health-tracker could use a review"
        assert result[0].detail == "It's been a while since we checked it."

    def test_admin_surface_recs_are_not_rewritten(self, tmp_path):
        """Admin surface always shows the original admin-narrative title."""
        engine = _make_engine(tmp_path)
        original_title = "health-tracker hasn't been reviewed in 112 days"
        rec = _make_rec(
            type="app_quality",
            source="compliance_scan",
            tags=["bot:team_bot_a", "app:health-tracker"],
            title=original_title,
            member_bot_title="Casual bot-user title",
        )
        result = engine.filter_for_surface([rec], "admin", scope_id="team_bot_a")
        assert len(result) == 1
        assert result[0].title == original_title

    def test_member_bot_filter_excludes_security_recs(self, tmp_path):
        engine = _make_engine(tmp_path)
        rec = _make_rec(type="security", source="security_review",
                        tags=["bot:team_bot_a"])
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert result == []

    def test_member_bot_filter_excludes_wrong_scope_id(self, tmp_path):
        engine = _make_engine(tmp_path)
        rec = _make_rec(scope_id="admin_bot")
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert result == []

    def test_member_bot_filter_includes_critical_operational(self, tmp_path):
        engine = _make_engine(tmp_path)
        rec = _make_rec(
            type="operational",
            source="health_check",
            tags=["bot:team_bot_a", "urgency:critical"],
        )
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert len(result) == 1

    def test_member_bot_filter_excludes_non_critical_operational(self, tmp_path):
        engine = _make_engine(tmp_path)
        rec = _make_rec(
            type="operational",
            source="health_check",
            tags=["bot:team_bot_a", "urgency:medium"],
        )
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        assert result == []

    def test_original_recs_not_mutated_after_filter(self, tmp_path):
        """filter_for_surface returns copies; original rec objects are unchanged."""
        engine = _make_engine(tmp_path)
        original_title = "health-tracker hasn't been reviewed in 112 days"
        casual_title = "Your health-tracker app could use a review."
        rec = _make_rec(
            type="app_quality",
            source="compliance_scan",
            tags=["bot:team_bot_a", "app:health-tracker"],
            action_args={"app_id": "health-tracker"},
            title=original_title,
            member_bot_title=casual_title,
        )
        result = engine.filter_for_surface([rec], "member_bot", scope_id="team_bot_a")
        # Returned rec has the casual title (substituted from member_bot_title)
        assert result[0].title == casual_title
        # Original rec is unchanged — surface_rewrite returns a copy
        assert rec.title == original_title


# ── API endpoint tests ─────────────────────────────────────────────────────────

@pytest.fixture
def app_with_recs(tmp_path):
    """Flask test app with a seeded recommendations.json."""
    import sys
    sys.path.insert(0, str(_ADMIN_DIR))

    from evolve_admin.web.server import create_app

    # Create network.json in tmp dir
    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(shared_dir),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Seed recommendations.json with a rec that has bot-user-facing copy.
    from evolve_admin.better_engine.storage import save_recommendations
    rec = _make_rec(
        type="app_quality",
        source="compliance_scan",
        tags=["bot:team_bot_a", "app:fitness-goals"],
        action_args={"app_id": "fitness-goals"},
        member_bot_title="Your fitness-goals app could use a review.",
        member_bot_detail="It's been a while.",
    )
    recs_path = shared_dir / "better-engine" / "recommendations.json"
    save_recommendations([rec], recs_path)

    flask_app = create_app(network_path)
    flask_app.config["TESTING"] = True
    return flask_app


def _unwrap_recs(raw):
    """The /api/better/recommendations endpoint wraps the list in a dict
    with {recommendations: [...], never_run: bool}. Older tests assumed
    a bare list; centralize the unwrap so updates are localized."""
    if isinstance(raw, dict):
        return raw.get("recommendations", [])
    return raw


class TestApiEndpointMemberBot:
    def test_member_bot_surface_returns_rewritten_rec(self, app_with_recs):
        with app_with_recs.test_client() as client:
            resp = client.get(
                "/api/better/recommendations?surface=member_bot&scope_id=team_bot_a"
            )
            assert resp.status_code == 200
            data = _unwrap_recs(json.loads(resp.data))
            assert isinstance(data, list)
            assert len(data) == 1
            # Title comes from member_bot_title set on the seeded rec.
            assert data[0]["title"] == "Your fitness-goals app could use a review."

    def test_admin_surface_returns_original_title(self, app_with_recs):
        with app_with_recs.test_client() as client:
            resp = client.get(
                "/api/better/recommendations?surface=admin&scope_id=team_bot_a"
            )
            assert resp.status_code == 200
            data = _unwrap_recs(json.loads(resp.data))
            assert isinstance(data, list)
            assert len(data) == 1
            # Original title should be unchanged
            assert data[0]["title"] == "health-tracker hasn't been reviewed in 112 days"

    def test_member_bot_surface_with_explicit_member_title(self, tmp_path):
        """Recs with explicit member_bot_title use it instead of template."""
        import sys
        sys.path.insert(0, str(_ADMIN_DIR))
        from evolve_admin.web.server import create_app

        shared_dir = tmp_path / "evolve"
        (shared_dir / "better-engine").mkdir(parents=True)
        network = {"members": ["team_bot_a"], "sharedDir": str(shared_dir)}
        network_path = tmp_path / "network.json"
        network_path.write_text(json.dumps(network))

        rec = _make_rec(
            type="app_quality",
            source="compliance_scan",
            tags=["bot:team_bot_a", "app:sleep-tracker"],
            action_args={"app_id": "sleep-tracker"},
            member_bot_title="Your sleep tracking needs a check-in",
            member_bot_detail="We haven't verified your sleep tracker recently.",
        )
        recs_path = shared_dir / "better-engine" / "recommendations.json"
        from evolve_admin.better_engine.storage import save_recommendations as _save
        _save([rec], recs_path)

        flask_app = create_app(network_path)
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as client:
            resp = client.get(
                "/api/better/recommendations?surface=member_bot&scope_id=team_bot_a"
            )
            assert resp.status_code == 200
            data = _unwrap_recs(json.loads(resp.data))
            assert data[0]["title"] == "Your sleep tracking needs a check-in"
            assert data[0]["detail"] == "We haven't verified your sleep tracker recently."

    def test_member_bot_surface_excludes_security_recs(self, tmp_path):
        import sys
        sys.path.insert(0, str(_ADMIN_DIR))
        from evolve_admin.web.server import create_app

        shared_dir = tmp_path / "evolve"
        (shared_dir / "better-engine").mkdir(parents=True)
        network = {"members": ["team_bot_a"], "sharedDir": str(shared_dir)}
        network_path = tmp_path / "network.json"
        network_path.write_text(json.dumps(network))

        rec = _make_rec(
            type="security",
            source="security_review",
            tags=["bot:team_bot_a"],
        )
        recs_path = shared_dir / "better-engine" / "recommendations.json"
        from evolve_admin.better_engine.storage import save_recommendations as _save
        _save([rec], recs_path)

        flask_app = create_app(network_path)
        flask_app.config["TESTING"] = True
        with flask_app.test_client() as client:
            resp = client.get(
                "/api/better/recommendations?surface=member_bot&scope_id=team_bot_a"
            )
            assert resp.status_code == 200
            data = _unwrap_recs(json.loads(resp.data))
            assert data == []

    def test_default_surface_is_admin(self, app_with_recs):
        """If no surface param is given, defaults to admin (no member-bot rewrite)."""
        with app_with_recs.test_client() as client:
            resp = client.get(
                "/api/better/recommendations?scope_id=team_bot_a"
            )
            assert resp.status_code == 200
            data = _unwrap_recs(json.loads(resp.data))
            assert isinstance(data, list)
            assert len(data) == 1
            # Admin surface: original title
            assert data[0]["title"] == "health-tracker hasn't been reviewed in 112 days"

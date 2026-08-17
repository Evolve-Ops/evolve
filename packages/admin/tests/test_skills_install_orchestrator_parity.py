"""
test_skills_install_orchestrator_parity.py — V2.1-4 Skills page parity with Gallery install UX.

Verifies that ``POST /api/skills/install/<skill_id>`` now routes through the V2.1-1
orchestrator for OAuth-requiring skills, returning the same ``awaiting_oauth`` response
shape as the Gallery install flow.

Two core scenarios per spec:

1. Already-satisfied: bot has GOG configured → response is ``{status: "already_active"}``.
   No awaiting_oauth state. UI short-circuits to success.

2. Not-satisfied: bot lacks GOG → response is ``{status: "awaiting_oauth", missing: [...]}``.
   HTTP 202. ``missing[0].action_url`` points at the skill's install route.
   No forge job created (option-b design: skills are lighter than gallery apps).

Additional checks:
- Obsidian (non-OAuth skill, no registered provider) is unaffected: continues to
  return the old step-plan shape (``{ok, status: {...}, steps: [...], skill: {...}}``).
- The 37 existing GOG install tests and the 21 inventory tests must still pass
  (ensured by not touching gog_install.py / obsidian_install.py internals).
- Unknown skills still return 404.
- Missing bot_id still returns 400.

Design note (option b over option a):
    Skills page installs do NOT use forge jobs. Skills are simpler than gallery apps
    (no build phase, no cron, no forge session). The orchestrator gives us provider
    lookup; the awaiting_oauth shape is returned directly in the HTTP 202 response
    so the UI can render action links and poll ``/api/skills/install/<id>/status``
    until active. The forge-job + sweeper machinery remains gallery-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def skills_app(monkeypatch, tmp_path):
    """Flask app with the GOG orchestrator wired to injectable stubs.

    The fixture exposes a ``dial`` dict with two keys:
      - ``satisfied``: bool — what the mocked orchestrator returns
      - ``gog_status``:  the status string the mocked gog resolve_status returns
        (used by the status route, which is not orchestrator-gated)

    The orchestrator's ``evaluate_install_prerequisites`` is patched at the
    module level used by ``server.py`` so we can control its output without
    needing real bot filesystem access.
    """
    from flask import Flask

    network_path = tmp_path / "network.json"
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()

    network_path.write_text(json.dumps({
        "bots": {"admin_bot": {"user": "admin_bot"}},
        "primary": "admin_bot",
        "sharedDir": str(shared_dir),
        "googleOAuthClient": {
            "mode": "self_hosted",
            "client_id": "stub.apps.googleusercontent.com",
            "client_secret_ref": {"bot": "admin_bot", "profile": "_evolve_google_oauth_client"},
        },
    }))

    # Control knob shared between the fixture and tests
    dial = {
        "satisfied": False,      # orchestrator says satisfied?
        "gog_status": "oauth_pending",  # what resolve_status returns for the status route
    }

    # Build the fake orchestrator result based on dial["satisfied"]
    def _fake_eval_prereqs(bot_id, requirements, *, shared_dir, **kwargs):
        if dial["satisfied"]:
            return {"satisfied": True, "missing": []}
        return {
            "satisfied": False,
            "missing": [
                {
                    "integration_id": "gog",
                    "skill_id": "gog",
                    "display_name": "Google (GOG)",
                    "reason": "Gmail & Calendar access required",
                    "status": "oauth_pending",
                    "action_url": "/api/skills/install/gog",
                    "action_label": "Set up Gmail & Calendar",
                    "install_plan_steps": [
                        {"id": "enable_plugin", "label": "Turn on Google plugin"},
                        {"id": "oauth",         "label": "Sign in with Google"},
                        {"id": "confirm",       "label": "Confirm connection"},
                    ],
                }
            ],
        }

    # Patch the orchestrator at the path server.py imports it from
    monkeypatch.setattr(
        "evolve_admin.oauth.orchestrator.evaluate_install_prerequisites",
        _fake_eval_prereqs,
    )

    # Also patch gog_install.resolve_status for the status route (unchanged)
    from evolve_admin.skills import gog_install as _gog

    def _fake_resolve(bot_id, *, read_plugin_enabled, read_oauth_profile,
                      read_oauth_client_configured):
        return _gog.InstallStatus(
            bot_id=bot_id,
            status=dial["gog_status"],
            plugin_enabled=dial["gog_status"] in ("active", "oauth_pending"),
            has_oauth_profile=dial["gog_status"] == "active",
        )

    monkeypatch.setattr(_gog, "resolve_status", _fake_resolve)

    from evolve_admin.web import server as _srv
    app = Flask(__name__)
    _srv._register_admin_routes(app, network_path)
    # 4.1b Inc 2d: the generic /api/skills/install/<skill_id> dispatcher moved
    # to routes_skills_workspace; register it so these parity tests resolve.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network_path)

    yield app, dial


# ── Scenario 1: already satisfied ────────────────────────────────────────────


class TestAlreadySatisfied:
    """Bot has GOG fully configured → orchestrator says satisfied → 200 already_active."""

    def test_returns_200_already_active(self, skills_app):
        app, dial = skills_app
        dial["satisfied"] = True
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "already_active"

    def test_already_active_has_friendly_message(self, skills_app):
        app, dial = skills_app
        dial["satisfied"] = True
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        body = r.get_json()
        msg = body.get("message", "")
        # Message must reference the skill by name, not raw id or code jargon.
        assert "Google" in msg or "connected" in msg.lower()

    def test_no_steps_returned_when_already_active(self, skills_app):
        """Satisfied path must not return steps — those are for the old wizard flow."""
        app, dial = skills_app
        dial["satisfied"] = True
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        assert "steps" not in body


# ── Scenario 2: OAuth needed ──────────────────────────────────────────────────


class TestAwaitingOauth:
    """Bot lacks GOG → orchestrator says not satisfied → 202 awaiting_oauth."""

    def test_returns_202_awaiting_oauth(self, skills_app):
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        assert r.status_code == 202
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "awaiting_oauth"

    def test_missing_list_is_populated(self, skills_app):
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        missing = body.get("missing", [])
        assert len(missing) == 1
        item = missing[0]
        assert item["integration_id"] == "gog"

    def test_missing_action_url_points_to_skill_install_route(self, skills_app):
        """The action_url in missing[0] must point at the skill's install endpoint.

        This is the load-bearing routing anchor for the UI helper: it renders the
        action_url as a link the operator clicks to start OAuth.
        """
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        item = body["missing"][0]
        assert item["action_url"] == "/api/skills/install/gog"

    def test_missing_has_action_label(self, skills_app):
        """action_label must be present and Plex-test friendly (no jargon)."""
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        item = body["missing"][0]
        label = item.get("action_label", "")
        assert label
        # No raw OAuth jargon in the label
        assert "oauth" not in label.lower()
        assert "scope" not in label.lower()

    def test_missing_has_install_plan_steps(self, skills_app):
        """install_plan_steps in missing[0] gives the UI enough info to show
        what will happen (informational list, not a step-driver contract)."""
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        item = body["missing"][0]
        steps = item.get("install_plan_steps", [])
        assert len(steps) >= 1
        # Each step has at least an id and a label
        for s in steps:
            assert "id" in s
            assert "label" in s

    def test_next_message_present(self, skills_app):
        """A 'next' key with a plain-language message must be in the response."""
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        body = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"}).get_json()
        assert "next" in body
        assert body["next"]

    def test_no_forge_job_created(self, skills_app, tmp_path):
        """Skills installs do NOT create forge jobs (option b). The forge jobs
        directory must remain empty after a skills install, unlike gallery installs."""
        app, dial = skills_app
        dial["satisfied"] = False
        client = app.test_client()
        client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        # Forge jobs directory must not exist or be empty
        forge_dir = tmp_path / "evolve" / "forge_jobs"
        if forge_dir.exists():
            assert list(forge_dir.iterdir()) == [], "No forge jobs should be created for skills installs"


# ── Withdrawn skills return 404 (2026-05-30) ─────────────────────────────────


class TestWithdrawnSkills:
    """Two paste-token dead-ends remain withdrawn: home_assistant, runway.
    Obsidian / Dropbox / Notion / Linear — the others — were rewired as
    MCP-server installs and have their own positive-coverage classes
    below (or in test_skills_<skill>_install.py).

    See docs/design/paste-token-skills-future-2026-05-30.md for the design
    of the MCP-server-install pattern the remaining two will come back as.

    Regression guard: assert that none of those two ids gets a 200 from
    the install plan or status endpoints — re-adding either of them
    without also wiring an MCP install destination would resurrect the
    dead-end class.
    """

    @pytest.mark.parametrize("skill_id", [
        # Home Assistant — original paste-token withdrawal pending the
        # tools-allowlist scope-toggle design (see deep-skills-audit F6).
        "home_assistant",
        # Phase 1c of the deep skills audit (PR #1839 — merged) — see
        # docs/skills-deep-audit-2026-05-30.md for per-skill rationale.
        # Each promised capabilities the bot couldn't deliver; several
        # silently reported status="active" on bots that couldn't use
        # the feature.
        "gdrive", "unity", "apple_local",
        # `linear` and `runway` are NOT here — they ship in #1834
        # (this PR) and #1833 (already merged) as real MCP and
        # bundled-plugin installs respectively.
    ])
    def test_withdrawn_install_post_returns_404(self, skills_app, skill_id):
        """POST /api/skills/install/<withdrawn>/ — must 404, not 200, so the
        UI can't silently believe a paste-token install succeeded."""
        app, _dial = skills_app
        client = app.test_client()
        r = client.post(
            f"/api/skills/install/{skill_id}", json={"bot_id": "admin_bot"},
        )
        assert r.status_code == 404, (
            f"{skill_id} install endpoint should 404 after withdrawal — "
            f"got {r.status_code}: {r.get_json()}"
        )
        body = r.get_json()
        assert body["ok"] is False
        assert "unknown skill" in (body.get("error") or "").lower()

    @pytest.mark.parametrize("skill_id", [
        # Home Assistant — original paste-token withdrawal pending the
        # tools-allowlist scope-toggle design (see deep-skills-audit F6).
        "home_assistant",
        # Phase 1c of the deep skills audit (PR #1839 — merged) — see
        # docs/skills-deep-audit-2026-05-30.md for per-skill rationale.
        # Each promised capabilities the bot couldn't deliver; several
        # silently reported status="active" on bots that couldn't use
        # the feature.
        "gdrive", "unity", "apple_local",
        # `linear` and `runway` are NOT here — they ship in #1834
        # (this PR) and #1833 (already merged) as real MCP and
        # bundled-plugin installs respectively.
    ])
    def test_withdrawn_status_get_returns_404(self, skills_app, skill_id):
        """GET /api/skills/install/<withdrawn>/status — must 404. The status
        endpoint is what the UI polls after install; if it returns 200, the
        UI's auto-poll keeps treating the skill as live."""
        app, _dial = skills_app
        client = app.test_client()
        r = client.get(
            f"/api/skills/install/{skill_id}/status?bot_id=admin_bot",
        )
        assert r.status_code == 404, (
            f"{skill_id} status endpoint should 404 after withdrawal — "
            f"got {r.status_code}: {r.get_json()}"
        )

    @pytest.mark.parametrize("skill_id", [
        # Home Assistant — original paste-token withdrawal pending the
        # tools-allowlist scope-toggle design (see deep-skills-audit F6).
        "home_assistant",
        # Phase 1c of the deep skills audit (PR #1839 — merged) — see
        # docs/skills-deep-audit-2026-05-30.md for per-skill rationale.
        # Each promised capabilities the bot couldn't deliver; several
        # silently reported status="active" on bots that couldn't use
        # the feature.
        "gdrive", "unity", "apple_local",
        # `linear` and `runway` are NOT here — they ship in #1834
        # (this PR) and #1833 (already merged) as real MCP and
        # bundled-plugin installs respectively.
    ])
    def test_withdrawn_skills_not_in_catalog_list(self, skills_app, skill_id):
        """GET /api/skills/catalog — the withdrawn skills must not appear in
        the listed catalog. If they did, the Skills tab would render their
        tiles, and clicking install would hit the 404. obsidian_vault /
        dropbox / notion / linear are intentionally absent from this list —
        they're back in the catalog as MCP-backed installs (see their
        test_skills_<skill>_install.py)."""
        app, _dial = skills_app
        client = app.test_client()
        r = client.get("/api/skills/catalog")
        assert r.status_code == 200
        catalog = r.get_json().get("skills") or []
        ids = {entry.get("id") for entry in catalog}
        assert skill_id not in ids, (
            f"{skill_id} should not appear in /api/skills/catalog after "
            f"withdrawal — found in {sorted(ids)}"
        )


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_bot_id_returns_400(self, skills_app):
        app, dial = skills_app
        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={})
        assert r.status_code == 400
        body = r.get_json()
        assert "bot_id" in body.get("error", "")

    def test_unknown_skill_returns_404(self, skills_app):
        app, dial = skills_app
        client = app.test_client()
        r = client.post("/api/skills/install/no-such-skill", json={"bot_id": "admin_bot"})
        assert r.status_code == 404
        body = r.get_json()
        assert body["ok"] is False

    def test_orchestrator_failure_falls_through_gracefully(self, skills_app, monkeypatch):
        """If the orchestrator import or call raises, the route falls through to
        the per-skill dispatch (graceful degradation, not 500)."""
        app, dial = skills_app
        dial["satisfied"] = False  # would be awaiting_oauth if orchestrator worked

        # Make find_provider raise — simulates an import or registry error
        def _raising_find(*a, **kw):
            raise RuntimeError("simulated provider lookup failure")

        monkeypatch.setattr(
            "evolve_admin.oauth.providers.find_provider",
            _raising_find,
        )

        client = app.test_client()
        r = client.post("/api/skills/install/gog", json={"bot_id": "admin_bot"})
        # Should fall through to the per-skill GOG flow (returns 200 with steps)
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        # Falls through to old step-plan shape
        assert "steps" in body

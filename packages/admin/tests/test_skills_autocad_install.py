"""Tests for evolve_admin.skills.autocad_install.

v1 ships as a deliberate catalog stub — the access panel and the
``needs_app`` → manual_setup install plan are pinned so the catalog
discoverability is real, while the full OAuth installer is deferred to a
follow-up. These tests pin the contract today's UI relies on:

  - Skill is in the catalog with a meaningful access panel.
  - resolve_status always returns ``needs_app`` (until OAuth lands).
  - build_install_plan emits ``manual_setup`` + ``confirm`` whose label
    steers the user to register an APS app — the canonical first step.
  - Flask routes wire everything correctly and return the new id from
    the hint string in the unknown-skill branch (for skill discovery
    via the error path too).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import autocad_install as autocad


# ── Module-level shape ────────────────────────────────────────────────────────

class TestRegistry:
    def test_skill_id_matches_constants(self):
        assert autocad.AUTOCAD_SKILL_ID == "autocad"
        assert autocad.SKILL_REGISTRY_ENTRY["id"] == "autocad"

    def test_access_panel_will_wont_lists_present(self):
        """The access panel is what the modal renders to the user; without
        non-empty will/won't lists, the install screen is bare."""
        ap = autocad.SKILL_REGISTRY_ENTRY.get("access_panel") or {}
        assert ap.get("will") and len(ap["will"]) >= 2
        assert ap.get("wont") and len(ap["wont"]) >= 2
        # Plex-test: mention the platform name in the summary so users
        # who search "autodesk" find the right card.
        assert "Autodesk" in autocad.SKILL_REGISTRY_ENTRY["summary"] \
            or "Autodesk" in autocad.SKILL_REGISTRY_ENTRY["display_name"]


# ── resolve_status (v1) ──────────────────────────────────────────────────────

class TestResolveStatus:
    def test_always_needs_app_in_v1(self):
        """Until the OAuth installer lands, every bot is in the same state:
        the operator must register an APS app before progress is possible."""
        st = autocad.resolve_status("admin_bot")
        assert st.status == "needs_app"
        assert st.bot_id == "admin_bot"


# ── build_install_plan ───────────────────────────────────────────────────────

class TestInstallPlan:
    def test_needs_app_emits_manual_setup_and_confirm(self):
        st = autocad.InstallStatus(bot_id="admin_bot", status="needs_app")
        steps = autocad.build_install_plan(st)
        assert [s.id for s in steps] == ["manual_setup", "confirm"]
        # The label IS the instruction the modal renders verbatim. It
        # must name the registration page so the user can act on it.
        label = steps[0].label
        assert "aps.autodesk.com" in label
        assert "Client ID" in label or "Client Secret" in label

    def test_unknown_status_yields_no_steps(self):
        st = autocad.InstallStatus(bot_id="admin_bot", status="unknown",
                                    error="probe_failed")
        assert autocad.build_install_plan(st) == []

    def test_manual_setup_carries_access_panel(self):
        """Modal's manual_setup renderer reads label + access_panel together —
        without the panel we'd lose the will/won't context on this screen."""
        st = autocad.InstallStatus(bot_id="admin_bot", status="needs_app")
        steps = autocad.build_install_plan(st)
        assert steps[0].access_panel is not None
        assert steps[0].access_panel.get("will")


# ── Flask route smoke tests ───────────────────────────────────────────────────

@pytest.fixture
def app_client(tmp_path):
    from flask import Flask
    from evolve_admin.web import server as _srv

    network = tmp_path / "network.json"
    network.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"admin_bot": {"user": "admin_bot"}},
    }))
    app = Flask(__name__)
    _srv._register_admin_routes(app, network)
    # 4.1b Inc 2d: the generic skills-install dispatchers moved to
    # routes_skills_workspace; register it so autocad status/install resolve.
    from evolve_admin.web.routes_skills_workspace import register_skills_workspace_routes
    register_skills_workspace_routes(app, network)
    return app.test_client()


class TestRoutes:
    def test_catalog_lists_autocad(self, app_client):
        r = app_client.get("/api/skills/catalog")
        ids = [s["id"] for s in r.get_json()["skills"]]
        assert "autocad" in ids

    def test_catalog_detail_returns_access_panel(self, app_client):
        r = app_client.get("/api/skills/catalog/autocad")
        assert r.status_code == 200
        body = r.get_json()
        ap = body.get("access_panel") or {}
        assert ap.get("will") and ap.get("wont")

    def test_status_returns_needs_app(self, app_client):
        r = app_client.get("/api/skills/install/autocad/status?bot_id=admin_bot")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "needs_app"

    def test_install_post_returns_manual_setup_plan(self, app_client):
        r = app_client.post("/api/skills/install/autocad",
                            json={"bot_id": "admin_bot"})
        body = r.get_json()
        assert body["ok"] is True
        ids = [s["id"] for s in body["steps"]]
        assert ids == ["manual_setup", "confirm"]
        # The label IS the user-facing instruction (modal renders verbatim).
        assert "aps.autodesk.com" in body["steps"][0]["label"]

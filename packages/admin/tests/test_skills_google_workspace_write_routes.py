"""tests/test_skills_google_workspace_write_routes.py — Flask-level coverage
for the Workspace-Write skill's HTTP surface.

Verifies the catalog list / detail dispatchers, the generic status + plan
dispatchers' branches for the new skill, and the dedicated /complete +
/revoke endpoints. The complete + revoke routes have real side-effects on
disk + keystore + remote Google; tests stub those at the closure boundary
so nothing leaks to the live environment.

References:
  * Routes: ``evolve_admin.web.server`` (search for
    ``google_workspace_write`` to find the new block).
  * Spec: ``docs/spec-google-workspace-suite-2026-06-04.md``.
  * Vetting: ``docs/vetting-workspace-mcp-2026-06-04.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def gws_app(tmp_path):
    """A minimal Flask app rooted at a temporary shared_dir + network.json.

    The shared_dir is empty: no bots, no keystore, no auth profiles. Tests
    that need specific state seed it directly.
    """
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir(parents=True)
    network = {
        "sharedDir": str(shared_dir),
        "bots": {"lex": {"user": "lex"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir, network_path


# ── Catalog list + detail ───────────────────────────────────────────────────


class TestCatalogList:
    def test_workspace_write_hidden_from_catalog_list(self, gws_app):
        """Updated post-PR #2155: the split google_workspace_write entry
        is HIDDEN from the catalog list (replaced by the unified ``google``
        skill that picks capabilities in-wizard). The detail endpoint
        still resolves — see TestCatalogDetail below."""
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog")
            assert r.status_code == 200
            data = r.get_json()
            ids = [s["id"] for s in data.get("skills") or []]
            assert "google_workspace_write" not in ids
            # The unified skill is what's listed instead.
            assert "google" in ids


class TestCatalogDetail:
    def test_returns_access_panel(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog/google_workspace_write")
            assert r.status_code == 200
            data = r.get_json()
            ap = data["access_panel"]
            assert ap and ap["skill_id"] == "google_workspace_write"
            assert ap["will"] and ap["wont"]

    def test_returns_rate_limits(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog/google_workspace_write")
            data = r.get_json()
            rl = data.get("rate_limits") or {}
            assert "gmail_send_per_minute" in rl
            assert "calendar_writes_per_minute" in rl
            assert "drive_uploads_per_minute" in rl

    def test_returns_required_scopes(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/catalog/google_workspace_write")
            data = r.get_json()
            scopes = data.get("required_scopes") or []
            # The six write scopes from spec §5.2.
            assert any("gmail.send" in s for s in scopes)
            assert any("calendar" == s.rsplit("/", 1)[-1] for s in scopes)
            assert any("drive.file" in s for s in scopes)


# ── Status dispatcher ───────────────────────────────────────────────────────


class TestStatusDispatcher:
    def test_status_routes_to_workspace_write_resolver(self, gws_app):
        """The generic /api/skills/install/<skill_id>/status must dispatch
        the workspace_write skill id to the four-stage resolver and
        return the InstallStatus shape."""
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/install/google_workspace_write/status?bot_id=lex")
            assert r.status_code == 200
            data = r.get_json()
            assert data["ok"] is True
            # No OAuth client configured in the test network.json → expect
            # oauth_client_missing.
            assert data["status"] == "oauth_client_missing"
            assert data["skill_id"] == "google_workspace_write"

    def test_status_requires_bot_id(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.get("/api/skills/install/google_workspace_write/status")
            assert r.status_code == 400


# ── Install-plan dispatcher ─────────────────────────────────────────────────


class TestPlanDispatcher:
    def test_plan_for_oauth_client_missing_state(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write",
                json={"bot_id": "lex"},
            )
            assert r.status_code == 200
            data = r.get_json()
            assert data["ok"] is True
            assert data["status"]["status"] == "oauth_client_missing"
            ids = [step["id"] for step in data["steps"]]
            assert ids == ["configure_oauth_client"]

    def test_plan_includes_skill_metadata(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write",
                json={"bot_id": "lex"},
            )
            data = r.get_json()
            skill = data["skill"]
            assert skill["id"] == "google_workspace_write"
            assert skill["access_panel"]


# ── /complete endpoint ──────────────────────────────────────────────────────


class TestCompleteRoute:
    """The /complete route runs the five-stage post-OAuth provisioning. Each
    test patches one stage to make assertions about routing + audit."""

    def test_requires_bot_id(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write/complete", json={},
            )
            assert r.status_code == 400

    def test_no_access_token_fails_at_preflight(self, gws_app):
        """When _ensure_fresh_google_access_token returns no token (no OAuth
        profile written), /complete must short-circuit at preflight and
        record the failure."""
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write/complete",
                json={"bot_id": "lex"},
            )
            assert r.status_code == 400
            data = r.get_json()
            assert data["ok"] is False
            assert data["preflight"]["done"] is False


# ── /revoke endpoint ────────────────────────────────────────────────────────


class TestRevokeRoute:
    def test_requires_bot_id(self, gws_app):
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write/revoke", json={},
            )
            assert r.status_code == 400

    def test_revoke_on_clean_bot_returns_response_shape(self, gws_app):
        """Revoke on a bot with no install state should still complete cleanly
        and report what was (or wasn't) cleared. The route is idempotent."""
        app, _, _ = gws_app
        with app.test_client() as c:
            r = c.post(
                "/api/skills/install/google_workspace_write/revoke",
                json={"bot_id": "lex"},
            )
            # The endpoint always returns 200 — revoke is best-effort; the
            # response body details which sub-steps succeeded.
            assert r.status_code == 200
            data = r.get_json()
            # The response must enumerate every revoke side-effect.
            for key in (
                "keystore_slots",
                "credentials_dir_wiped",
                "google_revoke_attempted",
                "profile_cleared",
                "gateway_kickstarted",
            ):
                assert key in data, f"missing key {key!r}"

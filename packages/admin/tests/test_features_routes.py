"""Tests for the /api/features endpoints (PR 4c).

The endpoints surface the install.json power-feature gating layer to the
admin UI so an operator can flip a watcher on/off without dropping to
the CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin import deploy
    from evolve_admin.web.evo_routes import register_evo_routes

    # Stub launchd handlers so the test never shells out to launchctl.
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: result.log("install stubbed"))
    monkeypatch.setattr(deploy, "uninstall_inbound_issues_watcher_now",
                        lambda result: result.log("uninstall stubbed"))

    network = {"sharedDir": str(tmp_path), "primary": "evo"}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_SHARED_DIR"] = tmp_path
    return app


# ── GET /api/features ─────────────────────────────────────────────────────


def test_list_features_returns_inbound_watcher(app):
    r = app.test_client().get("/api/features")
    assert r.status_code == 200
    body = r.get_json()
    names = [f["name"] for f in body["features"]]
    assert "inbound_issues_watcher" in names


def test_list_features_default_profile_is_standard(app):
    body = app.test_client().get("/api/features").get_json()
    assert body["feature_profile"] == "standard"


def test_list_features_default_inbound_off(app):
    """Fresh install with no install.json — inbound watcher must be off."""
    body = app.test_client().get("/api/features").get_json()
    inbound = next(f for f in body["features"] if f["name"] == "inbound_issues_watcher")
    assert inbound["enabled"] is False


# ── GET /api/features/<name> ──────────────────────────────────────────────


def test_get_feature_returns_known_feature(app):
    r = app.test_client().get("/api/features/inbound_issues_watcher")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"]["name"] == "inbound_issues_watcher"


def test_get_feature_404_for_unknown(app):
    r = app.test_client().get("/api/features/nonexistent_feature")
    assert r.status_code == 404


# ── POST /api/features/<name> ─────────────────────────────────────────────


def test_post_feature_enable_persists_to_install_json(app):
    client = app.test_client()
    r = client.post(
        "/api/features/inbound_issues_watcher",
        json={"enabled": True},
    )
    assert r.status_code == 200
    assert r.get_json()["status"]["enabled"] is True
    # Verify durably persisted.
    install_json = json.loads(
        (app.config["_SHARED_DIR"] / "install.json").read_text()
    )
    assert install_json["features"]["inbound_issues_watcher"]["enabled"] is True


def test_post_feature_disable_reflected_in_subsequent_get(app):
    """Round-trip: enable, then disable, then GET must show disabled."""
    client = app.test_client()
    client.post("/api/features/inbound_issues_watcher", json={"enabled": True})
    client.post("/api/features/inbound_issues_watcher", json={"enabled": False})
    body = client.get("/api/features/inbound_issues_watcher").get_json()
    assert body["status"]["enabled"] is False


def test_post_feature_requires_enabled_field(app):
    r = app.test_client().post(
        "/api/features/inbound_issues_watcher",
        json={},
    )
    assert r.status_code == 400


def test_post_feature_unknown_name_404(app):
    r = app.test_client().post(
        "/api/features/bogus",
        json={"enabled": True},
    )
    assert r.status_code == 404


def test_post_feature_returns_log_lines(app):
    """The response includes the install/uninstall path's log so the UI
    can render "installed" / "skipped" / etc. for transparency."""
    client = app.test_client()
    body = client.post(
        "/api/features/inbound_issues_watcher",
        json={"enabled": True},
    ).get_json()
    assert "log" in body
    assert any("install stubbed" in line for line in body["log"])
    # install.json change is the first log line — verify it's there.
    assert any("install.json" in line for line in body["log"])


def test_post_feature_invokes_install_handler_on_enable(app, monkeypatch):
    from evolve_admin import deploy
    calls = []
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: calls.append("install"))
    app.test_client().post(
        "/api/features/inbound_issues_watcher",
        json={"enabled": True},
    )
    assert calls == ["install"]


def test_post_feature_invokes_uninstall_handler_on_disable(app, monkeypatch):
    from evolve_admin import deploy
    calls = []
    monkeypatch.setattr(deploy, "uninstall_inbound_issues_watcher_now",
                        lambda result: calls.append("uninstall"))
    app.test_client().post(
        "/api/features/inbound_issues_watcher",
        json={"enabled": False},
    )
    assert calls == ["uninstall"]

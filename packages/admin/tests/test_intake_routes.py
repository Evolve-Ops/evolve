"""tests/test_intake_routes.py — Flask routes for /api/evo/intake/*."""

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
def intake_app(tmp_path):
    from flask import Flask
    from evolve_admin.web.evo_routes import register_evo_routes

    network = {
        "members": ["team_bot_a", "evo"],
        "sharedDir": str(tmp_path),
        "primary": "evo",
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_evo_routes(app, network_path)
    app.config["_NETWORK_PATH"] = network_path
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _write_network(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body))


def test_post_intake_creates(intake_app):
    client = intake_app.test_client()
    r = client.post(
        "/api/evo/intake",
        json={
            "kind": "bug",
            "body": "apps page is empty",
            "submitter_user_key": "ext:telegram:99",
            "context": {"active_bot": "team_bot_a", "primary_bot": "evo"},
        },
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()
    ix = data["intake"]
    assert ix["kind"] == "bug"
    assert ix["body"] == "apps page is empty"
    assert ix["state"] == "open"
    assert ix["context"]["active_bot"] == "team_bot_a"
    assert ix["id"].startswith("intake-")
    assert ix["submitter"]["user_key"] == "ext:telegram:99"


def test_post_intake_rejects_bad_kind(intake_app):
    client = intake_app.test_client()
    r = client.post("/api/evo/intake", json={"kind": "rant", "body": "x"})
    assert r.status_code == 400


def test_post_intake_rejects_empty_body(intake_app):
    client = intake_app.test_client()
    r = client.post("/api/evo/intake", json={"kind": "bug", "body": "   "})
    assert r.status_code == 400


def test_list_and_get_intake(intake_app):
    client = intake_app.test_client()
    # Create two
    r1 = client.post("/api/evo/intake", json={"kind": "bug", "body": "a"}).get_json()
    r2 = client.post("/api/evo/intake", json={"kind": "feature", "body": "b"}).get_json()

    r = client.get("/api/evo/intake")
    items = r.get_json()["intakes"]
    ids = {ix["id"] for ix in items}
    assert ids == {r1["intake"]["id"], r2["intake"]["id"]}

    # Filter by kind
    r = client.get("/api/evo/intake?kind=feature")
    items = r.get_json()["intakes"]
    assert [ix["id"] for ix in items] == [r2["intake"]["id"]]

    # Get single
    r = client.get(f"/api/evo/intake/{r1['intake']['id']}")
    assert r.status_code == 200
    assert r.get_json()["intake"]["body"] == "a"

    r = client.get("/api/evo/intake/intake-missing")
    assert r.status_code == 404


def test_promote_without_config_returns_412(intake_app):
    client = intake_app.test_client()
    created = client.post(
        "/api/evo/intake", json={"kind": "bug", "body": "x"}
    ).get_json()
    r = client.post(f"/api/evo/intake/{created['intake']['id']}/promote", json={})
    assert r.status_code == 412
    assert "intake.github not configured" in r.get_json()["error"]


def test_promote_without_token_returns_412(intake_app):
    """Config set but no keystore value — still 412 with a clear message."""
    client = intake_app.test_client()
    # Configure intake.github
    network_path: Path = intake_app.config["_NETWORK_PATH"]
    n = json.loads(network_path.read_text())
    n["intake"] = {
        "github": {"owner": "evolve-ops", "repo": "evolve", "token_slot": "github_intake"}
    }
    network_path.write_text(json.dumps(n))

    created = client.post(
        "/api/evo/intake", json={"kind": "bug", "body": "x"}
    ).get_json()
    r = client.post(f"/api/evo/intake/{created['intake']['id']}/promote", json={})
    assert r.status_code == 412
    assert "no token" in r.get_json()["error"]


def test_promote_404_when_missing(intake_app):
    client = intake_app.test_client()
    r = client.post("/api/evo/intake/intake-missing/promote", json={})
    assert r.status_code == 404


def test_dismiss_moves_to_closed(intake_app):
    client = intake_app.test_client()
    created = client.post(
        "/api/evo/intake", json={"kind": "bug", "body": "x"}
    ).get_json()
    iid = created["intake"]["id"]

    r = client.post(
        f"/api/evo/intake/{iid}/dismiss",
        json={"actor": "admin", "note": "not a bug"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["intake"]["state"] == "closed"

    # File lives in closed subdir; list excludes by state
    r = client.get("/api/evo/intake?state=closed")
    items = r.get_json()["intakes"]
    assert [ix["id"] for ix in items] == [iid]

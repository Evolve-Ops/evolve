"""GET/PUT /api/bot/<id>/purpose — the Effectiveness-Layer purpose anchor (Phase B)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from evolve_admin.web import server as srv

    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps({
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(tmp_path / "shared"),
    }))
    # Isolate from the deployment-side sudo + pod_config sync in the real
    # save_network — the endpoint logic is what's under test.
    monkeypatch.setattr(
        srv, "save_network",
        lambda data, path: Path(path).write_text(json.dumps(data, indent=2)),
    )
    app = srv.create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_get_returns_archetypes_and_null_when_unset(client):
    j = client.get("/api/bot/team_bot_a/purpose").get_json()
    assert j["purpose"] is None
    assert "personal-assistant" in j["archetypes"]
    assert "custom" in j["archetypes"]


def test_put_sets_persists_and_get_reflects(client):
    r = client.put("/api/bot/team_bot_a/purpose",
                   json={"archetype": "Project-Manager", "mission": "Run the project."})
    assert r.status_code == 200
    p = r.get_json()["purpose"]
    assert p["archetype"] == "project-manager"   # normalized
    assert p["mission"] == "Run the project."
    assert p["captured"] == "declared"
    assert "reviewed_at" in p
    # persisted → a fresh GET reflects it
    assert client.get("/api/bot/team_bot_a/purpose").get_json()["purpose"]["archetype"] == "project-manager"


def test_put_unknown_archetype_becomes_custom(client):
    r = client.put("/api/bot/team_bot_a/purpose", json={"archetype": "spellcaster", "mission": "x"})
    assert r.get_json()["purpose"]["archetype"] == "custom"


def test_put_to_unknown_bot_is_404(client):
    assert client.put("/api/bot/ghost/purpose", json={"archetype": "custom", "mission": "x"}).status_code == 404


def test_put_empty_clears_the_purpose(client):
    client.put("/api/bot/team_bot_a/purpose", json={"archetype": "custom", "mission": "x"})
    r = client.put("/api/bot/team_bot_a/purpose", json={"archetype": "", "mission": "  "})
    assert r.get_json()["purpose"] is None
    assert client.get("/api/bot/team_bot_a/purpose").get_json()["purpose"] is None

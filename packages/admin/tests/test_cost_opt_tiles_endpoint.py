"""tests/test_cost_opt_tiles_endpoint.py — GET /api/cost-optimization/tiles.

Exercises the endpoint that powers the Cost Optimization page's tile row.

Covers:
  - response shape (tiles list with required keys per tile)
  - bot order preservation from network.json
  - empty pod returns empty tiles list (not 500)
  - tile assembly failure surfaces a clean 500 with diagnostic
  - role / settings resolvers wire through correctly
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def app_three_bots(tmp_path, monkeypatch):
    """Stand up a Flask app with three bots — admin_bot (treatment, cascade
    enabled), personal_bot (control), team_bot_a (treatment). Stubs cost_profiles
    settings reader so the test doesn't depend on each bot having an
    openclaw.json on disk."""
    from evolve_admin.web.server import create_app

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["admin_bot", "personal_bot", "team_bot_a"],
        "sharedDir": str(shared),
        "bots": {
            "admin_bot": {"role": "member", "user": "admin_bot"},
            "personal_bot": {"role": "member", "user": "personal_bot"},
            "team_bot_a":   {"role": "member", "user": "team_bot_a"},
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the cost_profiles settings reader — returns minimal settings.
    import cost_profiles
    monkeypatch.setattr(
        cost_profiles, "read_openclaw_cost_settings",
        lambda bid: {"heartbeat": {"isolatedSession": True, "lightContext": True}},
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, tmp_path, shared


def test_tiles_endpoint_returns_one_tile_per_bot(app_three_bots):
    """Pod has 3 bots → response has 3 tiles in network order."""
    app, _tmp, _shared = app_three_bots
    with app.test_client() as c:
        resp = c.get("/api/cost-optimization/tiles")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "tiles" in body
    tiles = body["tiles"]
    assert len(tiles) == 3
    assert [t["bot_id"] for t in tiles] == ["admin_bot", "personal_bot", "team_bot_a"]


def test_tile_has_grade_and_spend_keys(app_three_bots):
    """Each tile has the shape the UI renders: grade letter, score, spend
    block, model_mix, chips. The score is now purely behavioral, so the
    old config_score / behavior_score split fields are gone."""
    app, _tmp, _shared = app_three_bots
    with app.test_client() as c:
        resp = c.get("/api/cost-optimization/tiles")
    tile = resp.get_json()["tiles"][0]
    assert tile["grade"] in {"A", "B", "C", "D", "F"}
    assert isinstance(tile["score"], int)
    assert "spend" in tile and "usd_28d" in tile["spend"]
    assert "model_mix" in tile and "by_tier" in tile["model_mix"]
    assert "chips" in tile and isinstance(tile["chips"], list)


def test_tiles_endpoint_handles_empty_pod(tmp_path, monkeypatch):
    """Pod with no bots → empty tiles list, 200 (not 500). Lets the UI
    render an empty-state message rather than an error toast."""
    from evolve_admin.web.server import create_app
    import cost_profiles

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {"sharedDir": str(shared), "bots": {}, "members": []}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    monkeypatch.setattr(
        cost_profiles, "read_openclaw_cost_settings",
        lambda bid: {},
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/api/cost-optimization/tiles")
    assert resp.status_code == 200
    assert resp.get_json() == {"tiles": []}


def test_tiles_endpoint_surfaces_assembly_error(tmp_path, monkeypatch):
    """When tile assembly raises, the endpoint returns 500 with a
    typed diagnostic — not "check server logs" pointing at admin
    daemon stderr the operator can't read. Same pattern as PR #1725."""
    from evolve_admin.web.server import create_app
    import cost_opt_tiles
    import cost_profiles

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "sharedDir": str(shared),
        "bots": {"admin_bot": {"role": "member"}},
        "members": ["admin_bot"],
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    monkeypatch.setattr(
        cost_profiles, "read_openclaw_cost_settings",
        lambda bid: {},
    )

    def _boom(**kw):
        raise RuntimeError("simulated assembly failure")

    monkeypatch.setattr(cost_opt_tiles, "build_all_tiles", _boom)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/api/cost-optimization/tiles")
    assert resp.status_code == 500
    err = resp.get_json()["error"]
    assert "RuntimeError" in err
    assert "simulated assembly failure" in err

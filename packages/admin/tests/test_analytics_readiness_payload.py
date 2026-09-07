"""The Apps grid payload (/api/analytics/applications) carries promotion
readiness for a discovered app (AL-1.6b; roadmap acceptance "readiness visible
on Apps page").

The route's per-manifest body sits inside a broad ``except: pass``, so a
scorer that raised would not error — it would silently drop the whole app from
the grid. ``test_a_manifest_that_would_break_the_scorer_still_appears`` is the
one that matters most here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import routes_analytics as _ra  # noqa: E402


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    bot = "team-bot-a"
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"members": [bot], "sharedDir": str(shared)}))

    mdir = tmp_path / "manifests"
    mdir.mkdir()
    written: dict[str, Path] = {}

    def _write(stem: str, data: dict) -> None:
        p = mdir / f"{stem}.json"
        p.write_text(json.dumps(data))
        written[stem] = p

    monkeypatch.setattr(
        _ra, "_list_manifests_as_bot", lambda bot_id, user=None: [str(p) for p in written.values()]
    )

    app = Flask(__name__)
    _ra.register_analytics_routes(app, network)
    app.testing = True
    return {"bot": bot, "client": app.test_client(), "write": _write}


def _payload(client) -> list:
    res = client["client"].get(f"/api/analytics/applications?bot={client['bot']}")
    assert res.status_code == 200, res.data
    return res.get_json()[client["bot"]]


def test_a_discovered_app_carries_a_readiness_block(client):
    client["write"]("task-manager", {
        "id": "task-manager", "name": "Task Manager",
        "definition_status": "discovered",
        "files": [{"path": "scripts/tasks.py", "layer": "code"}],
    })
    r = _payload(client)[0]["readiness"]
    assert r is not None
    assert r["score"] == 100
    assert r["band"] == "ready"
    assert r["dimensions_measured"] == 1
    assert r["dimensions_total"] == 3
    # 100 on one signal is not grounds to ask a user to promote an app.
    assert r["eligible_to_offer"] is False


def test_a_defined_app_carries_no_readiness(client):
    """It is already vouched, so there is nothing to rank — and a stale number
    the tile would have to learn to ignore is worse than no number."""
    client["write"]("rep", {
        "id": "rep", "name": "Reporter", "definition_status": "defined",
        "files": [{"path": "scripts/x.py", "layer": "code"}],
    })
    assert _payload(client)[0]["readiness"] is None


def test_an_absent_definition_status_is_scored_like_a_discovered_app(client):
    """``(absent)`` is the v27 inert default and reads as discovered — 7 of the
    81 manifests on the live pod are in that state."""
    client["write"]("legacy", {"id": "legacy", "name": "Legacy"})
    a = _payload(client)[0]
    assert a["definition_status"] == "discovered"
    assert a["readiness"] is not None


def test_the_unmeasured_dimensions_are_named_in_the_payload(client):
    """The tile's "scored on N of 3" note is rendered off these; without them
    a 100 would read as a three-part judgement it is not."""
    client["write"]("t", {"id": "t", "name": "T", "files": [{"path": "a.py", "layer": "code"}]})
    dims = _payload(client)[0]["readiness"]["dimensions"]
    unmeasured = {d["key"] for d in dims if not d["measured"]}
    assert unmeasured == {"recurrence", "stability"}
    assert all(d["score"] is None for d in dims if not d["measured"])


def test_a_manifest_that_would_break_the_scorer_still_appears(client, monkeypatch):
    """The route body is wrapped in ``except: pass``. A raising readiness
    computation must degrade to ``readiness: None``, not vanish the app."""
    import evolve_admin.applications.app_readiness as ar

    def _boom(_manifest):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(ar, "readiness_payload", _boom)
    client["write"]("t", {"id": "t", "name": "T", "files": [{"path": "a.py", "layer": "code"}]})
    apps = _payload(client)
    assert len(apps) == 1
    assert apps[0]["name"] == "T"
    assert apps[0]["readiness"] is None

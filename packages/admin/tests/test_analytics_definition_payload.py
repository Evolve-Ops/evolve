"""The Apps grid payload (/api/analytics/applications) must now carry the
Defined/Discovered fields the Bite-4 tile renders off of (spec §9): the
provenance status, the filename STEM the definition endpoints write back to
(§9.7 finding #5), and the unreviewed-drift count + slim drift list.
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

    def _write_manifest(stem: str, data: dict) -> Path:
        p = mdir / f"{stem}.json"
        p.write_text(json.dumps(data))
        return p

    written: dict[str, Path] = {}

    def _fake_list(bot_id, user=None):
        return [str(p) for p in written.values()]

    monkeypatch.setattr(_ra, "_list_manifests_as_bot", _fake_list)

    app = Flask(__name__)
    _ra.register_analytics_routes(app, network)
    app.testing = True
    return {
        "bot": bot,
        "client": app.test_client(),
        "write": lambda stem, data: written.__setitem__(stem, _write_manifest(stem, data)),
    }


def _payload(client) -> list:
    res = client["client"].get(f"/api/analytics/applications?bot={client['bot']}")
    assert res.status_code == 200, res.data
    return res.get_json()[client["bot"]]


def test_discovered_app_payload_shape(client):
    client["write"]("task-manager", {
        "id": "task-manager", "name": "Task Manager",
        "definition_status": "discovered",
    })
    apps = _payload(client)
    assert len(apps) == 1
    a = apps[0]
    assert a["definition_status"] == "discovered"
    assert a["manifest_stem"] == "task-manager"
    assert a["unreviewed_drift_count"] == 0
    assert a["drift_log"] == []


def test_absent_definition_status_defaults_discovered(client):
    client["write"]("legacy", {"id": "legacy", "name": "Legacy"})
    a = _payload(client)[0]
    assert a["definition_status"] == "discovered"


def test_manifest_stem_carries_when_id_differs(client):
    # File at the display slug, internal id differs (gallery v7-arc-pre shape).
    client["write"]("atlas-article-capture", {
        "id": "app_atlas_article_capture", "name": "Atlas",
        "definition_status": "defined",
    })
    a = _payload(client)[0]
    assert a["id"] == "app_atlas_article_capture"        # internal id
    assert a["manifest_stem"] == "atlas-article-capture"  # the write-back key


def test_defined_app_carries_unreviewed_drift(client):
    client["write"]("rep", {
        "id": "rep", "name": "Reporter", "definition_status": "defined",
        "drift_log": [
            {"ts": "2026-06-20T00:00:00Z", "kind": "add", "target_type": "cron",
             "target": "scripts/x.py", "significance": "major", "summary": "added cron",
             "reviewed": False, "source": "scanner_drift", "classifier": "deterministic"},
            {"ts": "2026-06-19T00:00:00Z", "kind": "remove", "target_type": "file",
             "target": "scripts/y.py", "significance": "major", "summary": "removed script",
             "reviewed": True, "source": "scanner_drift", "classifier": "deterministic"},
        ],
    })
    a = _payload(client)[0]
    assert a["definition_status"] == "defined"
    assert a["unreviewed_drift_count"] == 1            # one unreviewed
    assert len(a["drift_log"]) == 2                    # slim list, newest first
    assert a["drift_log"][0]["ts"] == "2026-06-20T00:00:00Z"
    # Slim shape — no audit fields leak into the grid payload.
    assert "source" not in a["drift_log"][0]
    assert "summary" in a["drift_log"][0]

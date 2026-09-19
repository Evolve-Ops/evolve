"""The Apps grid payload must be HARNESS-AWARE (Bite 2a).

/api/analytics/applications attaches c.freelance for at-risk-shaped apps. Bite 2a
adds ``integrity_covered``: true only when the execution-integrity middleware
PROVABLY covers the app's script (its id is listed in the plugin's per-bot
coverage file). apps.js clears the "May misreport failures" badge iff that flag
is true. The conservative contract is the load-bearing property: the flag is
false — badge KEPT — on any absence, staleness, or non-match.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import routes_analytics as _ra  # noqa: E402
from evolve_admin.applications.app_integrity_coverage import (  # noqa: E402
    COVERAGE_FILENAME,
    COVERAGE_FRESH_S,
)


# An at-risk-shaped, agent_invokes manifest with no structured triggers → the
# validator returns severity "warning", so the endpoint attaches c.freelance.
_AT_RISK_MANIFEST = {
    "id": "tasks",
    "pkg_id": "app_tasks",
    "name": "Task Manager",
    "invocation_mode": "agent_invokes",
    "realized_files": [{"path": "scripts/tasks.py"}],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    bot = "team-bot-a"
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"members": [bot], "sharedDir": str(shared)}))

    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "tasks.json").write_text(json.dumps(_AT_RISK_MANIFEST))

    monkeypatch.setattr(_ra, "_list_manifests_as_bot", lambda b, user=None: [str(mdir / "tasks.json")])

    app = Flask(__name__)
    _ra.register_analytics_routes(app, network)
    app.testing = True
    return {"bot": bot, "shared": shared, "client": app.test_client()}


def _app_payload(client) -> dict:
    res = client["client"].get(f"/api/analytics/applications?bot={client['bot']}")
    assert res.status_code == 200, res.data
    apps = res.get_json()[client["bot"]]
    assert len(apps) == 1
    return apps[0]


def _write_coverage(shared: Path, bot: str, ids: list, *, version=1) -> Path:
    d = shared / bot
    d.mkdir(parents=True, exist_ok=True)
    p = d / COVERAGE_FILENAME
    p.write_text(json.dumps({
        "version": version,
        "bot_id": bot,
        "covered_apps": [{"appId": i, "scripts": ["/x/tasks.py"]} for i in ids],
    }))
    return p


def test_freelance_attached_and_uncovered_by_default(client):
    """No coverage file → warning fires, integrity_covered False (badge kept)."""
    a = _app_payload(client)
    assert a["freelance"] is not None
    assert a["freelance"]["severity"] == "warning"
    assert a["freelance"]["integrity_covered"] is False


def test_covered_app_clears(client):
    """Coverage file lists the app's pkg_id → integrity_covered True (badge clears)."""
    _write_coverage(client["shared"], client["bot"], ["app_tasks"])
    a = _app_payload(client)
    assert a["freelance"] is not None  # payload still present…
    assert a["freelance"]["integrity_covered"] is True  # …but the badge clears


def test_other_app_covered_does_not_clear_this_one(client):
    """A coverage file that covers a DIFFERENT app must not clear this one."""
    _write_coverage(client["shared"], client["bot"], ["some_other_app"])
    a = _app_payload(client)
    assert a["freelance"]["integrity_covered"] is False


def test_stale_coverage_does_not_clear(client):
    """A frozen file past the freshness window must not certify coverage."""
    p = _write_coverage(client["shared"], client["bot"], ["app_tasks"])
    old = time.time() - COVERAGE_FRESH_S - 60
    os.utime(p, (old, old))
    a = _app_payload(client)
    assert a["freelance"]["integrity_covered"] is False


def test_unknown_sentinel_coverage_does_not_clear(client):
    """An 'unknown'-keyed coverage entry never proves a specific app covered."""
    _write_coverage(client["shared"], client["bot"], ["unknown"])
    a = _app_payload(client)
    assert a["freelance"]["integrity_covered"] is False

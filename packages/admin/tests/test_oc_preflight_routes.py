"""/api/oc/preflight/* — the HTTP surface for the per-bot OC preflight.

hold-fix-4277-preflight-fails-closed-and-is-wired-to-the-upgrade item 2:
the OC card's Update control reads these to gate on "has a passing preflight
run for the exact from → to pair", cached rather than re-run on every load.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_preflight_store as pfs  # noqa: E402
from evolve_admin import upstream_version as uv  # noqa: E402
from evolve_admin.web import routes_maintenance as rm  # noqa: E402

INSTALLED = "2026.9.2"
TARGET = "2026.9.4"


@pytest.fixture
def client(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared), "bots": {}, "members": [],
    }))
    monkeypatch.setattr(uv, "installed_package_version", lambda *a, **k: INSTALLED)
    app = Flask(__name__)
    rm._register_maintenance_routes(app, network_path)
    return {"client": app.test_client(), "shared": shared}


def test_check_with_no_target_is_400(client):
    resp = client["client"].post("/api/oc/preflight/check", json={})
    assert resp.status_code == 400


def test_check_spawns_a_background_run_and_the_pair_route_serves_it(client, monkeypatch):
    from evolve_admin.oc_preflight import BotPreflightRow, PreflightReport

    monkeypatch.setattr(
        pfs, "preflight",
        lambda network, target_version, **kw: PreflightReport(
            target_version=target_version, rows=[BotPreflightRow(bot_id="x")],
        ),
    )
    resp = client["client"].post("/api/oc/preflight/check", json={"target": TARGET})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "running"
    assert payload["target"] == TARGET
    assert payload["installed"] == INSTALLED
    rid = payload["report_id"]

    for _ in range(50):
        if pfs.inflight_report_id() is None:
            break
        time.sleep(0.02)

    by_id = client["client"].get(f"/api/oc/preflight/report/{rid}")
    assert by_id.status_code == 200
    assert by_id.get_json()["blocking"] is False

    by_pair = client["client"].get(f"/api/oc/preflight/report/pair?target={TARGET}")
    assert by_pair.status_code == 200
    assert by_pair.get_json()["report_id"] == rid


def test_pair_route_404s_when_never_checked(client):
    resp = client["client"].get(f"/api/oc/preflight/report/pair?target={TARGET}")
    assert resp.status_code == 404


def test_pair_route_with_no_target_is_400(client):
    resp = client["client"].get("/api/oc/preflight/report/pair")
    assert resp.status_code == 400


def test_report_route_404s_for_an_unknown_id(client):
    resp = client["client"].get("/api/oc/preflight/report/does-not-exist")
    assert resp.status_code == 404


def test_report_route_reports_running_for_the_inflight_id(client, monkeypatch):
    import threading

    release = threading.Event()

    def _slow(network, target_version, **kw):
        release.wait(timeout=5)
        from evolve_admin.oc_preflight import PreflightReport
        return PreflightReport(target_version=target_version)

    monkeypatch.setattr(pfs, "preflight", _slow)
    resp = client["client"].post("/api/oc/preflight/check", json={"target": TARGET})
    rid = resp.get_json()["report_id"]
    try:
        poll = client["client"].get(f"/api/oc/preflight/report/{rid}")
        assert poll.status_code == 200
        assert poll.get_json()["status"] == "running"
    finally:
        release.set()


def test_a_second_check_while_running_returns_the_same_report_id(client, monkeypatch):
    import threading

    release = threading.Event()

    def _slow(network, target_version, **kw):
        release.wait(timeout=5)
        from evolve_admin.oc_preflight import PreflightReport
        return PreflightReport(target_version=target_version)

    monkeypatch.setattr(pfs, "preflight", _slow)
    r1 = client["client"].post("/api/oc/preflight/check", json={"target": TARGET})
    r2 = client["client"].post("/api/oc/preflight/check", json={"target": TARGET})
    release.set()
    assert r1.get_json()["report_id"] == r2.get_json()["report_id"]
    assert r2.get_json()["status"] == "running"

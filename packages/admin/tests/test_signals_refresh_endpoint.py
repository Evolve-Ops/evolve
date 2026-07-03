"""tests/test_signals_refresh_endpoint.py — POST /api/signals/refresh.

The Refresh buttons on the Alerts page wire here. The endpoint fans out
to the five live signal-emitting monitors (integration_probe,
host_health, error_reporter, pod_health, cron_exit_monitor) and returns
per-monitor outcomes. These tests pin the contract:

  - the five expected monitors all show up in the response
  - per-monitor success/failure is reported independently
  - a slow / hung monitor doesn't block the others past the wall-clock
    budget; it just shows up as ok=False with error="timeout"

The probes themselves are heavy (real disk I/O, real psutil), so we
monkeypatch the module-level entrypoints to no-ops or controlled
failures. The integration_probe arm goes through closure-scoped helpers
that aren't patchable from here — for that arm we accept whatever the
real (empty) network produces and only check that it ran.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "signals").mkdir(parents=True)
    network = {"members": [], "bots": {}, "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the module-level probe entrypoints so the test doesn't do real
    # psutil / log-tail / pod-health / launchctl work. The
    # integration_probe arm is closure-scoped and runs through with an
    # empty bot list (network has no members), which is harmless.
    from evolve_admin import cron_exit_monitor as _cron_exit
    from evolve_admin import host_health as _host_health
    from evolve_admin import reporter as _reporter
    from evolve_admin import health as _health

    monkeypatch.setattr(
        _host_health, "collect_host_health",
        lambda: {"ok": True, "available": False, "hostname": "test"},
    )
    monkeypatch.setattr(
        _host_health, "emit_signals_from_snapshot",
        lambda snap, shared: 0,
    )
    monkeypatch.setattr(_reporter, "emit_error_signals", lambda shared, **kw: 0)

    class _Report:
        checks = []
        def as_dict(self): return {"checks": []}
    monkeypatch.setattr(_health, "run_health_check", lambda **kw: _Report())
    monkeypatch.setattr(
        _cron_exit, "emit_cron_exit_signals",
        lambda shared, network=None, **kw: {"checked": 0, "failing": 0,
                                            "cannot_escalate": False},
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def test_refresh_returns_all_five_monitors(app):
    with app.test_client() as c:
        resp = c.post("/api/signals/refresh")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()

    assert data["ok"] is True
    assert "ran_at" in data
    assert data["budget_seconds"] == 10.0

    names = [m["name"] for m in data["monitors"]]
    assert names == [
        "integration_probe",
        "host_health",
        "error_reporter",
        "pod_health",
        "cron_exit_monitor",
    ]
    for m in data["monitors"]:
        assert m["ok"] is True, m
        assert isinstance(m["ms"], int)
        assert m["ms"] >= 0


def test_refresh_surfaces_per_monitor_failures(app, monkeypatch):
    """A failing monitor reports ok=False with an error string; the others still report ok."""
    from evolve_admin import host_health as _host_health

    def _boom():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(_host_health, "collect_host_health", _boom)

    with app.test_client() as c:
        resp = c.post("/api/signals/refresh")
        assert resp.status_code == 200
        data = resp.get_json()

    by_name = {m["name"]: m for m in data["monitors"]}
    assert by_name["host_health"]["ok"] is False
    assert "psutil exploded" in by_name["host_health"]["error"]
    # The other monitors should still have run.
    assert by_name["error_reporter"]["ok"] is True
    assert by_name["pod_health"]["ok"] is True
    assert data["ok"] is False  # overall ok = all monitors ok


@pytest.mark.real_sleep  # stubs a real hanging monitor to prove the wall-clock budget cutoff
def test_refresh_completes_within_budget_when_one_monitor_hangs(app, monkeypatch):
    """A monitor that exceeds the 10s wall-clock budget must not block the response.

    We can't actually wait 10s in a unit test, so we shrink the budget by
    monkeypatching the endpoint's behavior indirectly: stub a hanging
    monitor and assert the request returns within a generous ceiling
    (well above the 10s budget). The hung monitor surfaces as
    ok=False with error="timeout".
    """
    from evolve_admin import reporter as _reporter

    def _hang(shared, **kw):
        time.sleep(30)
        return 0

    monkeypatch.setattr(_reporter, "emit_error_signals", _hang)

    t0 = time.monotonic()
    with app.test_client() as c:
        resp = c.post("/api/signals/refresh")
    elapsed = time.monotonic() - t0

    # 10s budget + a few seconds of overhead for the request layer.
    assert elapsed < 14.0, f"refresh did not honor budget: took {elapsed:.1f}s"
    assert resp.status_code == 200

    data = resp.get_json()
    by_name = {m["name"]: m for m in data["monitors"]}
    assert by_name["error_reporter"]["ok"] is False
    assert by_name["error_reporter"]["error"] == "timeout"
    # Fast monitors should still have completed successfully.
    assert by_name["host_health"]["ok"] is True

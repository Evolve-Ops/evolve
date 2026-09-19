"""tests/test_local_backup_endpoint.py — Phase 2 endpoint smoke tests."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent  # packages/admin
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture()
def client(tmp_path):
    from evolve_admin.web.server import create_app

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, net_file


def _patch_local_backup(monkeypatch, status_dict):
    """Replace _import_analyzer('local_backup') with a stub."""
    stub_status = types.SimpleNamespace(to_dict=lambda: status_dict)
    stub_module = types.SimpleNamespace(
        get_local_backup_status=lambda pod_paths=None, **kw: stub_status,
    )
    real_import = __import__("evolve_admin.web.server", fromlist=["_import_analyzer"])

    def _fake_import(mod):
        if mod == "local_backup":
            return stub_module
        return real_import._import_analyzer(mod)

    monkeypatch.setattr(
        "evolve_admin.web.server._import_analyzer", _fake_import,
    )


def test_local_status_returns_full_payload(client, monkeypatch):
    c, _ = client
    _patch_local_backup(monkeypatch, {
        "available": True,
        "configured": True,
        "destinations": [{"name": "Backup SSD", "kind": "Local"}],
        "last_backup_at": "2026-05-28T06:00:00Z",
        "last_backup_hours_ago": 6.0,
        "in_progress": False,
        "excluded_pod_paths": [],
        "settings_deeplink": "x-apple.systempreferences:com.apple.preference.timemachine",
        "error": None,
    })
    resp = c.get("/api/backup/local/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["available"] is True
    assert data["configured"] is True
    assert data["last_backup_hours_ago"] == 6.0
    assert data["destinations"][0]["name"] == "Backup SSD"
    assert data["settings_deeplink"].startswith("x-apple.systempreferences:")


def test_local_status_not_configured(client, monkeypatch):
    c, _ = client
    _patch_local_backup(monkeypatch, {
        "available": True,
        "configured": False,
        "destinations": [],
        "last_backup_at": None,
        "last_backup_hours_ago": None,
        "in_progress": False,
        "excluded_pod_paths": [],
        "settings_deeplink": "x-apple.systempreferences:com.apple.preference.timemachine",
        "error": None,
    })
    resp = c.get("/api/backup/local/status")
    assert resp.status_code == 200
    assert resp.get_json()["configured"] is False


def test_local_status_non_macos_returns_unavailable(client, monkeypatch):
    c, _ = client
    _patch_local_backup(monkeypatch, {
        "available": False,
        "configured": False,
        "destinations": [],
    })
    resp = c.get("/api/backup/local/status")
    assert resp.status_code == 200
    assert resp.get_json()["available"] is False


def test_local_status_wrapper_crash_surfaces_error(client, monkeypatch):
    c, _ = client
    stub_module = types.SimpleNamespace(
        get_local_backup_status=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    real_import = __import__("evolve_admin.web.server", fromlist=["_import_analyzer"])
    monkeypatch.setattr(
        "evolve_admin.web.server._import_analyzer",
        lambda m: stub_module if m == "local_backup" else real_import._import_analyzer(m),
    )
    resp = c.get("/api/backup/local/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "boom" in (data.get("error") or "")


def test_local_status_passes_shared_dir_to_wrapper(client, monkeypatch):
    """Verify the route passes sharedDir + deploy checkout for exclusion checks."""
    c, net_file = client
    captured = {"pod_paths": None}

    def _capture(pod_paths=None, **kw):
        captured["pod_paths"] = pod_paths
        return types.SimpleNamespace(to_dict=lambda: {"available": True, "configured": False})

    stub = types.SimpleNamespace(get_local_backup_status=_capture)
    real_import = __import__("evolve_admin.web.server", fromlist=["_import_analyzer"])
    monkeypatch.setattr(
        "evolve_admin.web.server._import_analyzer",
        lambda m: stub if m == "local_backup" else real_import._import_analyzer(m),
    )
    c.get("/api/backup/local/status")
    paths = [str(p) for p in captured["pod_paths"]]
    # Shared dir from the test fixture
    assert any("shared" in p for p in paths)
    # Deploy checkout is a constant
    assert "/Users/Shared/evolve-repo" in paths

"""tests/test_backup_visibility_endpoints.py — Phase 1 endpoint guards.

Covers the PATCH /api/backup/cloud/config and POST
/api/backup/cloud/init endpoints (Phase 4f canonical paths; the legacy
``/api/security/backup-*`` URLs still work as aliases):

- PATCH rejects on confirmed-public; allows with warning on unknown/no-PAT.
- PATCH allows empty-URL clears unconditionally.
- backup-init refuses on confirmed-public; passes through otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent  # packages/admin
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture()
def client(tmp_path):
    """Flask test client with a minimal network.json."""
    from evolve_admin.web.server import create_app

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "github": {"pat": "ghp_test"},
        # Keep the 2.8 startup migration's keystore writes inside tmp.
        "sharedDir": str(tmp_path / "shared"),
    }
    (tmp_path / "shared").mkdir(exist_ok=True)
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, net_file


# ── PATCH /api/backup/cloud/config ─────────────────────────────────────────

def _patch_visibility(monkeypatch, *, visibility=None, pat="ghp_test"):
    """Replace backup_visibility module imports with stubs.

    Patches what ``_import_analyzer("backup_visibility")`` resolves to so
    the endpoint doesn't actually hit GitHub.
    """
    import types
    stub = types.SimpleNamespace(
        load_pat=lambda config: pat,
        check_repo_visibility=lambda url, pat=None: visibility,
        parse_github_repo=lambda url: ("cjalden", "test-repo"),
    )
    real_import = __import__("evolve_admin.web.server", fromlist=["_import_analyzer"])

    def _fake_import(mod):
        if mod == "backup_visibility":
            return stub
        return real_import._import_analyzer(mod)

    monkeypatch.setattr(
        "evolve_admin.web.server._import_analyzer", _fake_import,
    )


def test_patch_rejects_when_repo_is_public(client, monkeypatch):
    c, _ = client
    _patch_visibility(monkeypatch, visibility="public")
    resp = c.patch("/api/backup/cloud/config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "git@github.com:cjalden/test-repo.git",
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["code"] == "repo_public"
    assert "settings" in data["settings_url"]
    assert "cjalden/test-repo" in data["settings_url"]


def test_patch_allows_with_warning_when_pat_missing(client, monkeypatch):
    c, _ = client
    _patch_visibility(monkeypatch, visibility=None, pat=None)
    resp = c.patch("/api/backup/cloud/config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "git@github.com:cjalden/test-repo.git",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "warning" in data
    assert "PAT" in data["warning"]


def test_patch_allows_with_warning_when_lookup_unknown(client, monkeypatch):
    c, _ = client
    _patch_visibility(monkeypatch, visibility="unknown")
    resp = c.patch("/api/backup/cloud/config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "git@github.com:cjalden/test-repo.git",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "warning" in data
    assert "unknown" in data["warning"].lower()


def test_patch_succeeds_clean_when_repo_is_private(client, monkeypatch):
    c, _ = client
    _patch_visibility(monkeypatch, visibility="private")
    resp = c.patch("/api/backup/cloud/config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "git@github.com:cjalden/test-repo.git",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "warning" not in data


def test_patch_clearing_url_bypasses_guard(client, monkeypatch):
    # Operator is unsetting the URL — visibility shouldn't even be checked.
    # We don't patch the visibility stub; if the endpoint reaches it, the
    # import would happen and we'd notice via flaky behavior.
    c, _ = client
    resp = c.patch("/api/backup/cloud/config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


# ── POST /api/backup/cloud/init ────────────────────────────────────────────

def test_init_refuses_when_configured_repo_is_public(client, monkeypatch):
    # First save a URL — patched to private so the PATCH succeeds — then
    # flip the stub to public for the init check.
    c, net_file = client

    # Directly write the URL into network.json so we don't go through PATCH.
    net = json.loads(net_file.read_text())
    net["bots"]["team_bot_a"]["backupRepoUrl"] = "git@github.com:cjalden/test-repo.git"
    net_file.write_text(json.dumps(net))

    _patch_visibility(monkeypatch, visibility="public")
    resp = c.post("/api/backup/cloud/init", json={"botId": "team_bot_a"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["status"] == "refused:repo_public"
    assert data["code"] == "repo_public"


def test_init_proceeds_when_no_backup_url_configured(client, monkeypatch):
    # No URL set → no visibility check → init runs (and may itself fail
    # for unrelated reasons like workspace not present; we just verify the
    # guard didn't refuse).
    c, _ = client
    with patch(
        "evolve_admin.web.server.ensure_workspace_git_init",
        return_value=(True, "initialized"),
    ):
        resp = c.post("/api/backup/cloud/init", json={"botId": "team_bot_a"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["status"] == "initialized"


def test_init_proceeds_when_repo_is_private(client, monkeypatch):
    c, net_file = client
    net = json.loads(net_file.read_text())
    net["bots"]["team_bot_a"]["backupRepoUrl"] = "git@github.com:cjalden/test-repo.git"
    net_file.write_text(json.dumps(net))

    _patch_visibility(monkeypatch, visibility="private")
    with patch(
        "evolve_admin.web.server.ensure_workspace_git_init",
        return_value=(True, "initialized"),
    ):
        resp = c.post("/api/backup/cloud/init", json={"botId": "team_bot_a"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["status"] == "initialized"


def test_init_proceeds_when_pat_missing(client, monkeypatch):
    # No PAT → can't verify → guard passes through to push-time guard.
    c, net_file = client
    net = json.loads(net_file.read_text())
    net["bots"]["team_bot_a"]["backupRepoUrl"] = "git@github.com:cjalden/test-repo.git"
    net_file.write_text(json.dumps(net))

    _patch_visibility(monkeypatch, visibility=None, pat=None)
    with patch(
        "evolve_admin.web.server.ensure_workspace_git_init",
        return_value=(True, "initialized"),
    ):
        resp = c.post("/api/backup/cloud/init", json={"botId": "team_bot_a"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_init_botid_required(client):
    c, _ = client
    resp = c.post("/api/backup/cloud/init", json={})
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# ── Phase 4f: legacy aliases still resolve ─────────────────────────────────

def test_legacy_security_backup_config_patch_alias_still_works(client, monkeypatch):
    """Old /api/security/backup-config PATCH URL stays dual-registered.

    External scripts or evo's older tool callers may still pin the legacy
    path. The canonical endpoint moved to /api/backup/cloud/config in Phase
    4f; the old alias must keep routing to the same handler.
    """
    c, _ = client
    _patch_visibility(monkeypatch, visibility="private")
    resp = c.patch("/api/security/backup-config", json={
        "botId": "team_bot_a",
        "backupRepoUrl": "git@github.com:cjalden/test-repo.git",
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_legacy_security_backup_init_alias_still_works(client, monkeypatch):
    """Old /api/security/backup-init POST URL stays dual-registered."""
    c, _ = client
    _patch_visibility(monkeypatch, visibility="private")
    with patch(
        "evolve_admin.web.server.ensure_workspace_git_init",
        return_value=(True, "initialized"),
    ):
        resp = c.post("/api/security/backup-init", json={"botId": "team_bot_a"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

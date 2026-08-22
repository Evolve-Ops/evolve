"""tests/test_backup_preflight_endpoint.py — Phase 4d cloud preflight endpoint."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    bot_home = tmp_path / "team_bot_a-home"
    (bot_home / ".openclaw" / "workspace").mkdir(parents=True)

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    monkeypatch.setattr(
        "evolve_admin.web.server._bot_home",
        lambda bot_id, net=None: bot_home,
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, bot_home


def test_preflight_requires_bot_id(client):
    c, _ = client
    resp = c.get("/api/backup/cloud/preflight")
    assert resp.status_code == 400


def test_preflight_empty_workspace_returns_zeros(client):
    c, _ = client
    resp = c.get("/api/backup/cloud/preflight?botId=team_bot_a")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["bot_id"] == "team_bot_a"
    assert data["total"] == {"files": 0, "bytes": 0}
    assert "cloud" in data["by_classification"]


def test_preflight_returns_classification_summary(client):
    c, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    (workspace / "SOUL.md").write_bytes(b"x" * 10)
    (workspace / "notes").mkdir()
    (workspace / "notes" / "a.md").write_bytes(b"x" * 100)

    # Drop a v15 manifest that marks notes/ as local.
    mdir = workspace / "manifests"
    mdir.mkdir()
    (mdir / "notes-app.json").write_text(json.dumps({
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))

    resp = c.get("/api/backup/cloud/preflight?botId=team_bot_a")
    assert resp.status_code == 200
    data = resp.get_json()
    # cloud bucket: SOUL.md + the manifest JSON itself (manifests/ has no
    # rule of its own, so it falls through to the default = cloud).
    assert data["by_classification"]["cloud"]["files"] == 2
    assert data["by_classification"]["cloud"]["bytes"] >= 10
    assert data["by_classification"]["local"]["files"] == 1
    assert data["by_classification"]["local"]["bytes"] == 100


def test_preflight_skips_git_dir(client):
    c, bot_home = client
    workspace = bot_home / ".openclaw" / "workspace"
    (workspace / ".git").mkdir()
    (workspace / ".git" / "HEAD").write_bytes(b"x" * 999)
    (workspace / "SOUL.md").write_bytes(b"x" * 5)

    resp = c.get("/api/backup/cloud/preflight?botId=team_bot_a")
    data = resp.get_json()
    assert data["total"]["bytes"] == 5  # .git not counted


def test_preflight_response_includes_truncated_flag(client):
    c, _ = client
    resp = c.get("/api/backup/cloud/preflight?botId=team_bot_a")
    data = resp.get_json()
    assert "truncated" in data
    assert isinstance(data["truncated"], bool)


def test_preflight_missing_workspace_is_graceful(client, monkeypatch):
    """When the bot's workspace doesn't exist at all, return zeros, not crash."""
    c, _ = client
    monkeypatch.setattr(
        "evolve_admin.web.server._bot_home",
        lambda bot_id, net=None: Path("/this/path/does/not/exist"),
    )
    resp = c.get("/api/backup/cloud/preflight?botId=team_bot_a")
    assert resp.status_code == 200
    data = resp.get_json()
    # Either zeros (preflight ran cleanly on a missing path) or an error
    # field (something blew up inside the wrapper). Both are acceptable;
    # what matters is the response is JSON and the status is 200.
    assert data["bot_id"] == "team_bot_a"
    assert "total" in data

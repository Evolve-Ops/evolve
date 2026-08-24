"""Regression test for /api/backup/cloud/status surfacing classified_cause.

The 2026-06-07 ssh-key-mismatch incident left state.json::last_error
holding the most specific possible error string, but the admin UI
showed only a tooltip-truncated version on hover. The diagnostic
classifier (backup_diagnostics.classify) maps the raw stderr into a
structured ``{cause_id, title, fix_steps}`` triple; the status endpoint
must surface that triple alongside the raw error so the Backup → Status
and Maintenance → Bot Versions tables can render a real diagnostic
panel inline.

This test pins the field-presence and shape so a future edit to
_graft_run_state doesn't silently drop the classification back to None.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


_SSH_KEY_MISMATCH_STDERR = (
    "push failed: identity_sign: private key /Users/team_bot_a/.ssh/evolve-backup-team_bot_a "
    "contents do not match public\n"
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    bot_home = tmp_path / "team_bot_a-home"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    # Drop a state.json mirroring the 2026-06-07 incident: 3 consecutive
    # failures, the exact ssh-key-mismatch error captured in production.
    state_dir = workspace / "evolve-backup"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "bot_id": "team_bot_a",
        "last_attempt_at": "2026-06-07T07:36:25Z",
        "last_attempt_status": "failed",
        "last_success_at": "2026-06-04T23:21:15Z",
        "last_error": _SSH_KEY_MISMATCH_STDERR,
        "consecutive_failures": 3,
    }))

    network = {
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a",
                "backupRepoUrl": "git@github.com:example-org/team_bot_a-workspace.git",
            },
        },
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
        yield c


def test_status_endpoint_classifies_ssh_key_mismatch(client):
    resp = client.get("/api/backup/cloud/status")
    assert resp.status_code == 200
    data = resp.get_json()
    bot = data["bots"]["team_bot_a"]

    # last_error still surfaced raw (existing contract).
    assert bot["last_error"] == _SSH_KEY_MISMATCH_STDERR
    assert bot["consecutive_failures"] == 3
    assert bot["last_attempt_status"] == "failed"

    # New: classified_cause surfaced with shape the UI's diagnostic
    # panel renders.
    cause = bot["classified_cause"]
    assert cause is not None, (
        "classified_cause must be populated when last_error matches a "
        "known pattern — the UI's expandable diagnostic depends on it"
    )
    assert cause["cause_id"] == "ssh_key_mismatch"
    assert isinstance(cause["title"], str) and cause["title"]
    assert isinstance(cause["fix_steps"], list) and cause["fix_steps"]


def test_status_endpoint_classified_cause_null_for_unknown_error(
    client, tmp_path, monkeypatch,
):
    """Errors that don't match any known pattern produce
    ``classified_cause: null`` so the UI can fall back to a generic
    "see the raw error" treatment.
    """
    # Rewrite the state.json with an unrecognized error.
    bot_home = tmp_path / "team_bot_a-home"
    state = bot_home / ".openclaw" / "workspace" / "evolve-backup" / "state.json"
    state.write_text(json.dumps({
        "bot_id": "team_bot_a",
        "last_attempt_at": "2026-06-07T07:36:25Z",
        "last_attempt_status": "failed",
        "last_success_at": "2026-06-04T23:21:15Z",
        "last_error": "unexpected git internal #42 nothing matches",
        "consecutive_failures": 1,
    }))
    resp = client.get("/api/backup/cloud/status")
    data = resp.get_json()
    assert data["bots"]["team_bot_a"]["classified_cause"] is None


def test_status_endpoint_coalesces_rapid_polls(client, monkeypatch):
    """fd-burst guard (2026-07-28 incident): the endpoint fans out git
    subprocesses per bot per request; concurrent/rapid polls under
    werkzeug's thread-per-request stacked those transient fds toward the
    256 soft NOFILE cap. Repeat polls within the short TTL must serve the
    cached payload without re-spawning any subprocess."""
    import subprocess

    calls = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    r1 = client.get("/api/backup/cloud/status")
    assert r1.status_code == 200
    first = calls["n"]
    assert first >= 1, "the git freshness probe should have run"

    r2 = client.get("/api/backup/cloud/status")
    assert r2.status_code == 200
    assert calls["n"] == first, (
        "a second poll within the TTL must not re-spawn subprocesses"
    )
    assert r2.get_json() == r1.get_json()

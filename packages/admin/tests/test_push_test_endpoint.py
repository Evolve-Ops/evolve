"""Regression test for the post-Distribute Key push-test probe.

POST /api/backup/cloud/keys/push-test runs git ls-remote against each
bot's backupRepoUrl using the canonical shared SSH key. It catches the
post-distribute failure mode user-key registration silently allows:
when the SSH identity authenticates fine but can't push to a
particular repo (e.g. team-bot whose backup repo lives under an org
where the PAT user is a read-only collaborator).

The probe is deliberately lightweight — one git ls-remote per bot.
Successful rows confirm the SSH identity reaches the repo; they do
NOT prove write access. The next nightly backup is the real verifier.

These tests stub subprocess.run so they never hit GitHub:

  - happy path: ls-remote returns 0 → status "ok"
  - failure path: ls-remote returns non-zero with stderr → status
    "failed" + stderr captured (truncated to 600 chars)
  - missing-source: canonical key file gone → 400 with operator-
    actionable error message
  - skipped: bot has no backupRepoUrl → status "skipped:no_url"
  - botId filter: body {botId: <id>} probes only that bot
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Bring up the Flask app with the canonical SSH source path
    redirected at a tmp file, so the endpoint's existence check
    succeeds without touching real /Users/evolve/.ssh/.
    """
    from evolve_admin import backup_keys
    from evolve_admin.web.server import create_app

    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    priv = ssh_root / "evolve-backup-shared"
    pub = ssh_root / "evolve-backup-shared.pub"
    priv.write_bytes(b"FAKE PRIVATE KEY BYTES")
    pub.write_text("ssh-ed25519 FAKEBLOB evolve-backup-shared\n")
    monkeypatch.setenv(backup_keys._ENV_PRIV, str(priv))
    monkeypatch.setenv(backup_keys._ENV_PUB, str(pub))

    network = {
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a",
                "backupRepoUrl": "git@github.com:example-org/team_bot_a-workspace.git",
            },
            "team_bot_b": {
                "user": "team_bot_b",
                "backupRepoUrl": "git@github.com:example-org/team_bot_b-workspace.git",
            },
            "team_bot_c": {
                "user": "team_bot_c",
                # no backupRepoUrl — should be reported as skipped
            },
        },
        "sharedDir": str(tmp_path / "shared"),
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    from evolve_admin.web import server as server_mod
    monkeypatch.setattr(server_mod, "load_network", lambda _p=None: dict(network))

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, priv


def _fake_subprocess(behaviour):
    """Return a subprocess.run stand-in that dispatches on the URL.

    behaviour: dict {url: (returncode, stderr)} — git ls-remote calls
    look up the URL (cmd[-1]) and return a CompletedProcess with that
    code + stderr. Unknown URLs default to (0, "").
    """
    def fake_run(cmd, *args, **kwargs):
        url = cmd[-1] if cmd and cmd[0] == "git" else ""
        code, stderr = behaviour.get(url, (0, ""))
        return subprocess.CompletedProcess(
            args=cmd, returncode=code, stdout="", stderr=stderr,
        )
    return fake_run


def test_push_test_happy_path(client, monkeypatch):
    """Both configured bots should succeed; the bot with no URL gets
    skipped:no_url. Canonical key existence is reflected in
    source_path so the operator can verify which key was probed.
    """
    c, priv = client
    monkeypatch.setattr(subprocess, "run", _fake_subprocess({}))
    resp = c.post("/api/backup/cloud/keys/push-test", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["source_path"] == str(priv)
    by_bot = {r["bot_id"]: r for r in data["results"]}
    assert by_bot["team_bot_a"]["status"] == "ok"
    assert by_bot["team_bot_b"]["status"] == "ok"
    assert by_bot["team_bot_c"]["status"] == "skipped:no_url"


def test_push_test_captures_stderr_on_failure(client, monkeypatch):
    """Failure rows must surface stderr verbatim — that's the whole
    diagnostic value of the probe. The operator reads the real
    GitHub error (e.g. "ERROR: Permission to org/repo denied to user.")
    instead of a generic chip label.
    """
    c, _ = client
    monkeypatch.setattr(subprocess, "run", _fake_subprocess({
        "git@github.com:example-org/team_bot_a-workspace.git": (
            128,
            "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
        ),
    }))
    resp = c.post("/api/backup/cloud/keys/push-test", json={})
    assert resp.status_code == 200
    by_bot = {r["bot_id"]: r for r in resp.get_json()["results"]}
    assert by_bot["team_bot_a"]["status"] == "failed"
    assert "Repository not found" in by_bot["team_bot_a"]["stderr"]
    assert by_bot["team_bot_b"]["status"] == "ok"


def test_push_test_bot_filter(client, monkeypatch):
    """Body {botId: <id>} must narrow the probe to one bot. Without
    this the operator would have no way to re-test a single failing
    bot without re-probing the whole pod.
    """
    c, _ = client
    monkeypatch.setattr(subprocess, "run", _fake_subprocess({}))
    resp = c.post("/api/backup/cloud/keys/push-test", json={"botId": "team_bot_b"})
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert len(results) == 1
    assert results[0]["bot_id"] == "team_bot_b"
    assert results[0]["status"] == "ok"


def test_push_test_missing_canonical_key_returns_400(client, monkeypatch, tmp_path):
    """If the canonical shared key doesn't exist, the endpoint must
    return 400 with an actionable error message — running the probe
    before Distribute Key has been clicked at least once.
    """
    from evolve_admin import backup_keys

    c, _ = client
    # Repoint the env override at a path that does not exist.
    monkeypatch.setenv(backup_keys._ENV_PRIV, str(tmp_path / "no-such-key"))
    resp = c.post("/api/backup/cloud/keys/push-test", json={})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "Distribute Key" in data["error"]
    assert data["results"] == []


def test_push_test_truncates_long_stderr(client, monkeypatch):
    """Long stderr (a verbose SSH dump, perhaps with -v) must be
    truncated so the JSON response stays bounded. 600 chars is a
    reasonable cap — long enough to capture the GitHub error block,
    short enough to render in a single <pre> in the UI.
    """
    c, _ = client
    long_stderr = "x" * 2000
    monkeypatch.setattr(subprocess, "run", _fake_subprocess({
        "git@github.com:example-org/team_bot_a-workspace.git": (1, long_stderr),
    }))
    resp = c.post("/api/backup/cloud/keys/push-test", json={})
    by_bot = {r["bot_id"]: r for r in resp.get_json()["results"]}
    assert by_bot["team_bot_a"]["status"] == "failed"
    assert len(by_bot["team_bot_a"]["stderr"]) <= 600


def test_push_test_handles_timeout(client, monkeypatch):
    """If git ls-remote hangs past the timeout, the endpoint must
    catch TimeoutExpired and return a clear status rather than
    propagating the exception (which would 500 the whole probe and
    drop every other bot's result).
    """
    c, _ = client

    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else ["git"], timeout=20)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    resp = c.post("/api/backup/cloud/keys/push-test", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    # Both configured bots failed with timeout messages.
    by_bot = {r["bot_id"]: r for r in data["results"]}
    assert by_bot["team_bot_a"]["status"] == "failed"
    assert "timed out" in by_bot["team_bot_a"]["stderr"]

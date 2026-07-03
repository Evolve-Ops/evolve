"""Regression test for Distribute Key auto-registering the canonical
shared pubkey on every backup repo via the GitHub API.

The 2026-06-07 incident exposed two bugs that left five team bots
silently failing backups for days:

  - Distribute Key wrote the new shared key to every bot's ~/.ssh/
    but never told GitHub about it. The operator was supposed to
    manually copy the pubkey to each repo's Settings → Deploy keys.
    There was no UI indication this step was required.

  - The "Set up GitHub backup" wizard's "✓ reused — already has
    evolve deploy key" check read /Users/evolve/.ssh/evolve-backup-
    <bot>.pub (stale per-bot legacy files), found those OLD pubkeys
    registered on GitHub, and reported success while the actually-
    deployed shared pubkey wasn't on GitHub at all.

PR #2357 fixed both incrementally with inline GitHub-registration
code in the distribute endpoint. The unification spec at
docs/spec-backup-key-distribution-unification-2026-06-08.md moves
that logic into ``evolve_admin.backup_keys.ensure_deploy_key_registered``
called from ``reconcile_pod``; this test pins the user-facing
contract (the JSON response shape) against the new implementation.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


_FAKE_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYBLOB123 evolve-backup-shared"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Bring up the Flask app with the canonical source redirected at a
    tmpdir, fake bot homes under another tmpdir, sudo bypassed, and
    pwd.getpwnam patched so the synthetic bot users count as existing.
    """
    from evolve_admin import backup_keys
    from evolve_admin.web.server import create_app

    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    priv = ssh_root / "evolve-backup-shared"
    pub = ssh_root / "evolve-backup-shared.pub"
    priv.write_bytes(b"FAKE PRIVATE KEY BYTES")
    pub.write_text(_FAKE_PUBKEY + "\n")

    users_root = tmp_path / "Users"
    users_root.mkdir()
    for bot_user in ("team_bot_a", "team_bot_b"):
        (users_root / bot_user).mkdir()

    monkeypatch.setenv(backup_keys._ENV_PRIV, str(priv))
    monkeypatch.setenv(backup_keys._ENV_PUB, str(pub))
    monkeypatch.setenv(backup_keys._ENV_USERS_ROOT, str(users_root))
    monkeypatch.setenv(backup_keys._ENV_NO_SUDO, "1")

    # Synthetic bots don't exist in /etc/passwd; tell pwd they do.
    import pwd as real_pwd
    real_getpwnam = real_pwd.getpwnam

    def fake_getpwnam(name):
        if name in ("team_bot_a", "team_bot_b"):
            return real_pwd.struct_passwd((name, "x", 1000, 1000, "", str(users_root / name), "/bin/sh"))
        return real_getpwnam(name)

    monkeypatch.setattr(backup_keys.pwd, "getpwnam", fake_getpwnam)

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
                # No URL — should be skipped from distribution.
            },
        },
        "sharedDir": str(tmp_path / "shared"),
        "github": {"pat": "fake-pat-token"},
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))

    from evolve_admin.web import server as server_mod
    monkeypatch.setattr(
        server_mod, "load_network",
        lambda _p=None: dict(network),
    )
    monkeypatch.setattr(
        server_mod, "save_network",
        lambda updated, _p=None: net_file.write_text(json.dumps(updated)),
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, net_file


def test_distribute_key_registers_new_pubkey_on_github(client, monkeypatch):
    c, net_file = client
    api_calls: list[dict] = []

    class FakeResp:
        def __init__(self, status, body_text):
            self.status = status
            self._body_text = body_text
            self.headers = {}

        def read(self):
            return self._body_text.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request as urlreq
    real_urlopen = urlreq.urlopen

    def fake_urlopen(req, *a, **kw):
        url = req.full_url
        if "api.github.com" not in url:
            return real_urlopen(req, *a, **kw)
        method = req.get_method()
        body_json = None
        if req.data:
            try:
                body_json = json.loads(req.data.decode("utf-8"))
            except Exception:
                pass
        path = url.split("api.github.com", 1)[1]
        api_calls.append({"method": method, "path": path, "body": body_json})
        if method == "GET" and "/keys" in path:
            return FakeResp(200, "[]")
        if method == "POST" and "/keys" in path:
            return FakeResp(201, json.dumps({"id": 1}))
        return FakeResp(404, "{}")

    monkeypatch.setattr(urlreq, "urlopen", fake_urlopen)

    resp = c.post("/api/backup/cloud/keys/distribute", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["pat_available"] is True

    by_bot = {r["bot_id"]: r for r in data["results"]}
    # team_bot_c has no backupRepoUrl so it doesn't appear in results.
    assert set(by_bot.keys()) == {"team_bot_a", "team_bot_b"}
    for bot_id, r in by_bot.items():
        assert "github_status" in r, f"{bot_id} missing github_status"
        assert r["github_status"] == "registered", (
            f"{bot_id} expected registered, got {r['github_status']}"
        )

    # Post-2026-06-08: registration is ONE POST to /user/keys (the
    # user-account SSH key path), not per-repo deploy keys. Per-repo
    # would 422 with "key is already in use" after the first POST
    # because GitHub allows a given SSH key to be a deploy key on at
    # most one repo at a time.
    post_calls = [c for c in api_calls if c["method"] == "POST" and "/keys" in c["path"]]
    assert len(post_calls) == 1, (
        f"expected 1 POST /user/keys call (pod-wide), got {len(post_calls)}: {post_calls}"
    )
    assert post_calls[0]["path"] == "/user/keys"

    # Sentinel: backup.key_mode = "shared" lands in network.json after
    # full alignment.
    updated_net = json.loads(net_file.read_text())
    assert updated_net.get("backup", {}).get("key_mode") == "shared"


def test_distribute_key_skips_registration_without_pat(client, monkeypatch):
    """When network.json has no github.pat every bot's github_status
    must be 'skipped:no_pat' and pat_available=False so the UI shows
    the manual-fallback prompt. Disk distribution still succeeds.
    """
    c, net_file = client
    from evolve_admin.web import server as server_mod

    network = {
        "bots": {
            "team_bot_a": {
                "user": "team_bot_a",
                "backupRepoUrl": "git@github.com:example-org/team_bot_a-workspace.git",
            },
        },
        "sharedDir": str(net_file.parent / "shared"),
        # No github block — no PAT.
    }
    monkeypatch.setattr(server_mod, "load_network", lambda _p=None: dict(network))

    resp = c.post("/api/backup/cloud/keys/distribute", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["pat_available"] is False
    for r in data["results"]:
        assert r["github_status"] == "skipped:no_pat"

    # Sentinel must NOT be written when registration was skipped on every
    # bot — the pod isn't fully migrated until clause 4 (GitHub) holds.
    updated_net = json.loads(net_file.read_text())
    assert "backup" not in updated_net or updated_net["backup"].get("key_mode") != "shared"

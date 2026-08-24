"""Stale-lock recovery for POST /api/applications/scan.

Before this change, the endpoint gated on plain `Path.exists()` for
`{tempdir}/.evolve-scan-{bot}.lock`. If the admin daemon crashed or
restarted mid-scan, the lockfile leaked and every subsequent scan
returned `{"status": "already_running"}` until an operator removed
the file by hand.

The fix swaps existence-check for fcntl.flock: a stale file on disk
still exists, but the kernel released the advisory lock when the
holding process died, so the next request acquires cleanly.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    # Redirect tempfile.gettempdir() to a per-test scratch dir so the
    # lockfile lives somewhere we control and can pre-populate.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    bot_home = tmp_path / "team_bot_a-home"
    (bot_home / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)

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
    monkeypatch.setattr(
        "evolve_admin.web.server._resolve_bot_user",
        lambda bot_id, network_path=None: "team_bot_a",
    )

    # Don't actually shell out to sudo/python during the test — the
    # endpoint returns "started" immediately after spawning the monitor
    # thread, so a no-op Popen is enough to let the thread exit cleanly.
    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"", b"")

        def kill(self):
            pass

    monkeypatch.setattr(
        "evolve_admin.web.server.subprocess.Popen",
        lambda *a, **kw: _FakeProc(),
    )

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, tmp_path


def test_stale_lockfile_does_not_block_new_scan(client):
    """File exists from a prior crashed run but no process holds the
    flock → next scan acquires cleanly."""
    c, tmp_path = client
    lockfile = tmp_path / ".evolve-scan-team_bot_a.lock"
    lockfile.write_text("12345\n2026-06-09T01:13:00+00:00\n")  # dead PID
    assert lockfile.exists()

    resp = c.post("/api/applications/scan?bot=team_bot_a")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "started", data


def test_live_flock_holder_blocks_new_scan(client, tmp_path):
    """Another process actively holds the flock → endpoint returns
    already_running. Proves the flock check still serializes correctly."""
    c, tmp_path = client
    lockfile = tmp_path / ".evolve-scan-team_bot_a.lock"

    holder_fh = open(lockfile, "a+")
    try:
        fcntl.flock(holder_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

        resp = c.post("/api/applications/scan?bot=team_bot_a")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "already_running", data
    finally:
        holder_fh.close()


def test_flock_releases_when_holding_process_exits(tmp_path):
    """Backstop: the kernel really does release flock on process death.

    Spawn a child that grabs the lock and exits without releasing.
    The parent should then be able to acquire it immediately.
    """
    lockfile = tmp_path / "kernel-release.lock"
    code = (
        "import fcntl, sys\n"
        f"fh = open(r'{lockfile}', 'a+')\n"
        "fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "fh.write('held')\n"
        "fh.flush()\n"
        "# exit without releasing; kernel reclaims fd + lock\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    assert lockfile.exists()

    fh = open(lockfile, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fh.close()

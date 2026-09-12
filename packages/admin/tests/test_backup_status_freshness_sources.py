"""Freshness sources for /api/backup/cloud/status — 2026-07-28 incident.

Two regressions pinned here:

1. **No path filter on the [backup] git log.** The endpoint used to ask
   for the last ``[backup]`` commit *that touched* ``evolve-backup/``.
   When any other committer in the workspace swept up the state.json
   diff first (a bot-authored nightly job, committing seconds after
   ours), our subsequent ``[backup]`` commits stopped touching that dir
   and a bot with healthy nightly pushes rendered ⚠ stale for 10 days.
   The ``^\\[backup\\]`` grep alone identifies backup commits.

2. **state.json::last_success_at is the primary freshness source.** It
   advances on both "ok" and "no-changes" attempts, so a healthy daemon
   with a quiet workspace never reads as stale; the git commit
   timestamp is the fallback when run state is missing.

Uses a real tmp git repo (same create_app + _bot_home monkeypatch
harness as test_backup_status_classifies_cause.py). Commit dates are
set relative to now — no epoch pinning (see the overnight-red
clock-coupling note in memory).
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


def _git(args: list[str], cwd: Path, *, date: "str | None" = None) -> None:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        check=True, capture_output=True, text=True,
    )


def _iso(hours_ago: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Workspace git repo + network.json + app client, parameterizable
    per test via the returned dict of paths."""
    bot_home = tmp_path / "team_bot_a-home"
    workspace = bot_home / ".openclaw" / "workspace"
    (workspace / "evolve-backup").mkdir(parents=True)

    _git(["init", "-q", "-b", "main"], workspace)
    _git(["config", "user.name", "Evolve Backup"], workspace)
    _git(["config", "user.email", "evolve@localhost"], workspace)

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

    from evolve_admin.web.server import create_app
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    client = app.test_client()
    return {"workspace": workspace, "client": client}


def _commit(workspace: Path, message: str, rel_file: str, *, hours_ago: float) -> None:
    f = workspace / rel_file
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{message} @ {hours_ago}h ago\n")
    _git(["add", "-A"], workspace)
    _git(["commit", "-q", "-m", message], workspace, date=_iso(hours_ago))


def _write_state(workspace: Path, *, success_hours_ago: "float | None") -> None:
    state = {
        "bot_id": "team_bot_a",
        "last_attempt_status": "ok",
        "last_error": None,
        "consecutive_failures": 0,
    }
    if success_hours_ago is not None:
        ts = _iso(success_hours_ago)
        state["last_attempt_at"] = ts
        state["last_success_at"] = ts
    (workspace / "evolve-backup" / "state.json").write_text(json.dumps(state))


def _get(client) -> dict:
    resp = client.get("/api/backup/cloud/status")
    assert resp.status_code == 200
    return resp.get_json()["bots"]["team_bot_a"]


def test_backup_commit_outside_evolve_backup_dir_counts(harness):
    """The 2026-07-28 shape: last [backup] commit touching evolve-backup/ is
    11 days old (a rogue committer steals the state.json diff every
    night), but [backup] commits land daily elsewhere in the tree.
    Must render fresh."""
    ws = harness["workspace"]
    # Old backup commit that touched evolve-backup/ (pre-rogue era).
    _commit(ws, "[backup] team_bot_a old", "evolve-backup/snapshot.json", hours_ago=264)
    # Rogue commit sweeps up the evolve-backup/ churn...
    _commit(ws, "Automated cron backup", "evolve-backup/state-echo.json", hours_ago=30)
    # ...so the fresh [backup] commit touches only files outside it.
    _commit(ws, "[backup] team_bot_a fresh", "evolve/audits/trail.jsonl", hours_ago=2)
    _write_state(ws, success_hours_ago=None)  # git is the only source here

    bot = _get(harness["client"])
    assert bot["stale"] is False, (
        "a [backup] commit 2h ago must count even when it doesn't touch "
        "evolve-backup/ — the path filter regression rendered the bot stale "
        "for 10 days while nightly pushes were succeeding"
    )
    assert bot["hours_ago"] < 26


def test_recent_no_changes_success_beats_old_commit(harness):
    """Daemon ran and recorded success ('no-changes') recently; the last
    actual commit is old because the workspace was quiet. Not stale."""
    ws = harness["workspace"]
    _commit(ws, "[backup] team_bot_a quiet-era", "evolve-backup/snapshot.json", hours_ago=100)
    _write_state(ws, success_hours_ago=3)

    bot = _get(harness["client"])
    assert bot["stale"] is False
    assert bot["hours_ago"] < 26
    # last_backup keeps reporting the commit timestamp (display contract);
    # staleness is what the run state upgrades.
    assert bot["last_backup"] is not None


def test_stale_when_both_sources_old(harness):
    ws = harness["workspace"]
    _commit(ws, "[backup] team_bot_a old", "evolve-backup/snapshot.json", hours_ago=80)
    _write_state(ws, success_hours_ago=80)

    bot = _get(harness["client"])
    assert bot["stale"] is True
    assert bot["hours_ago"] > 26


def test_no_commits_but_recorded_success_uses_run_state(harness):
    """Grep finds no [backup] commit (e.g. history rewritten or
    unreadable) but state.json records a recent success — trust the
    run state rather than rendering 'stale'."""
    ws = harness["workspace"]
    # A repo with a non-backup commit only, so git log --grep matches nothing.
    _commit(ws, "initial", "README.md", hours_ago=50)
    _write_state(ws, success_hours_ago=4)

    bot = _get(harness["client"])
    assert bot["stale"] is False
    assert bot["hours_ago"] is not None and bot["hours_ago"] < 26

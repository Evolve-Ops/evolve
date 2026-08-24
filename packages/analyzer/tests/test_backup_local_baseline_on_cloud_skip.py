"""tests/test_backup_local_baseline_on_cloud_skip.py — local drift baseline survives cloud-skip.

``heal.detect_backup_drift_keys`` compares each bot's live openclaw.json
against the committed ``evolve-backup/openclaw.json`` baseline. On a
cloud-enabled pod that baseline follows live on every successful backup
commit, so config_drift is only an intra-interval tripwire.

The push-visibility guard in ``_backup_bot_attempt`` returns *before* the
staging/commit step when there's no GitHub PAT or the repo isn't confirmed
private. Pre-fix, that froze the drift baseline on cloud-backup-less pods,
so an operator-authorized deploy/apply drift re-fired "Unexplained config
drift" on every heal tick forever once the 7d explained-window expired
(footprint disk-output audit 2026-06-28).

The fix refreshes the local baseline on both cloud-skip return paths via
``commit_baseline_local`` (no push). These tests pin that contract.

Coverage:
- no-PAT skip path still refreshes the local baseline (status unchanged)
- non-private "failed" path still refreshes the local baseline
- confirmed-private path does NOT take the cloud-skip refresh (the normal
  staging/commit owns the baseline there)
- a refresh failure never changes the returned cloud status (best-effort)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


def _drive(tmp_path, monkeypatch, *, pat, visibility, refresh_raises=False):
    """Run _backup_bot_attempt with a stubbed env; record baseline-refresh calls.

    Returns (status, error_msg, refresh_calls) where refresh_calls is the
    list of bot_ids passed to commit_baseline_local.
    """
    import backup
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    def git_stub(args, cwd, env=None):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    refresh_calls: list[str] = []

    def fake_commit_baseline_local(bot_id, shared_dir):
        refresh_calls.append(bot_id)
        if refresh_raises:
            raise RuntimeError("simulated baseline refresh failure")
        return True, "baseline committed locally"

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._load_github_pat", lambda network: pat)
    monkeypatch.setattr("backup.check_repo_visibility", lambda url, pat=None: visibility)
    monkeypatch.setattr("backup.commit_baseline_local", fake_commit_baseline_local)
    # Keep the private-repo happy-path cheap if it runs.
    monkeypatch.setattr("backup._copy_with_sudo", lambda s, d, b: (True, ""))
    monkeypatch.setattr("backup._redact_snapshot_file", lambda p: (False, "no-secrets"))
    monkeypatch.setattr("backup.sha256_file", lambda p: "h" * 16)
    monkeypatch.setattr("backup.get_applied_since_last_backup", lambda b, s, w: [])
    monkeypatch.setattr("backup.now_iso", lambda: "2026-06-28T12:00:00Z")

    status, err = _backup_bot_attempt(
        bot_id="team_bot_a",
        shared_dir=tmp_path / "shared",
        backup_url="git@github.com:evolve-ops/test-repo.git",
        dry_run=False,
        network={"github": {"pat": pat or ""}},
    )
    return status, err, refresh_calls


def test_missing_pat_skip_still_refreshes_local_baseline(tmp_path, monkeypatch):
    status, err, refresh_calls = _drive(tmp_path, monkeypatch, pat=None, visibility="public")
    assert status == "skipped"
    assert refresh_calls == ["team_bot_a"], (
        "no-PAT cloud-skip must still refresh the local drift baseline so "
        "config_drift doesn't re-fire forever on cloud-backup-less pods"
    )


def test_non_private_failed_still_refreshes_local_baseline(tmp_path, monkeypatch):
    status, err, refresh_calls = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="public")
    assert status == "failed"
    assert "not 'private'" in (err or "")
    assert refresh_calls == ["team_bot_a"], (
        "visibility-guard failure must still refresh the local drift baseline"
    )


def test_private_repo_does_not_take_cloud_skip_refresh(tmp_path, monkeypatch):
    # The confirmed-private path proceeds to the normal staging/commit, which
    # owns the baseline. It must NOT invoke the cloud-skip refresh helper.
    status, err, refresh_calls = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="private")
    assert refresh_calls == [], (
        "the cloud-skip baseline refresh is only for the no-PAT / non-private "
        "early returns; the private path's normal commit handles the baseline"
    )


def test_refresh_failure_does_not_change_returned_status(tmp_path, monkeypatch):
    # Best-effort: a baseline-refresh exception must not flip the cloud status.
    status, err, refresh_calls = _drive(
        tmp_path, monkeypatch, pat=None, visibility="public", refresh_raises=True,
    )
    assert status == "skipped"
    assert refresh_calls == ["team_bot_a"]

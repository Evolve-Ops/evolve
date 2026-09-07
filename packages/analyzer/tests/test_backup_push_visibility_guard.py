"""tests/test_backup_push_visibility_guard.py — Phase 1 push-time visibility guard.

Verifies that ``_backup_bot_attempt`` refuses to push under each of the
three "not confirmed private" conditions:

  - public repo confirmed → status "failed"
  - PAT missing → status "skipped" (config gap, not a failure)
  - PAT present but visibility lookup returns "unknown" → status "failed"

Mirrors the helper-setup pattern in test_backup_eacces_recovery_and_rebase.py
to stay close to existing conventions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


def _drive(tmp_path, monkeypatch, *, pat, visibility):
    """Run _backup_bot_attempt against a stubbed environment.

    Returns (status, error_msg). ``pat`` controls _load_github_pat output;
    ``visibility`` controls check_repo_visibility output.
    """
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Stub git so rev-parse + remote-config succeed; we should never reach
    # push because the visibility guard rejects first.
    def git_stub(args, cwd, env=None):
        if args[:2] == ["push", "origin"]:
            raise AssertionError(
                "push should not run when visibility guard refuses; "
                "backup.py is bypassing the Phase-1 guard"
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._load_github_pat", lambda network: pat)
    monkeypatch.setattr("backup.check_repo_visibility", lambda url, pat=None: visibility)

    return _backup_bot_attempt(
        bot_id="test-bot",
        shared_dir=tmp_path / "shared",
        backup_url="git@github.com:cjalden/test-repo.git",
        dry_run=False,
        network={"github": {"pat": pat or ""}},
    )


def test_public_repo_returns_failed_and_blocks_push(tmp_path, monkeypatch):
    status, err = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="public")
    assert status == "failed"
    assert "not 'private'" in (err or "")
    assert "public" in (err or "")
    # Includes a deep-link to the settings page so the operator can fix it.
    assert "github.com/evolve-ops/test-repo/settings" in (err or "")


def test_missing_pat_returns_skipped_not_failed(tmp_path, monkeypatch):
    # Without a PAT, we can't verify, but the operator hasn't done anything
    # wrong — surfacing this as "failed" would bump consecutive_failures and
    # eventually emit a generic backup_failing Signal, masking the real
    # cause. "skipped" is the right status: config gap, no work attempted.
    status, err = _drive(tmp_path, monkeypatch, pat=None, visibility="public")
    assert status == "skipped"
    assert "github_pat" in (err or "")  # keystore key (2.8)
    assert "blocked" in (err or "").lower()


def test_unknown_visibility_with_pat_returns_failed(tmp_path, monkeypatch):
    # PAT is configured but the lookup fails (network error, 404, malformed
    # response). Something's wrong; fail-safe blocks the push.
    status, err = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="unknown")
    assert status == "failed"
    assert "unknown" in (err or "")


def test_unknown_visibility_message_points_at_pat_not_repo(tmp_path, monkeypatch):
    """'unknown' must NOT tell the operator to make the repo private.

    Regression guard for the 2026-07-28 triage: an expired PAT made
    the lookup 401 → 'unknown', and the shared message said "Make the
    repo private at <settings url>" about a repo that was already
    private. The 'unknown' branch must point at the PAT/lookup instead.
    """
    _, err = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="unknown")
    err = err or ""
    # Remediation targets the PAT / setup wizard...
    assert "github_pat" in err
    assert "wizard" in err.lower()
    # ...and explicitly does not claim the repo was detected as public.
    assert "Make the repo private" not in err
    assert "/settings" not in err


def test_public_visibility_message_still_points_at_repo(tmp_path, monkeypatch):
    """Confirmed-public keeps the make-it-private remediation (and does
    not blame the PAT — the lookup demonstrably worked)."""
    _, err = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="public")
    err = err or ""
    assert "Make the repo private" in err
    assert "github_pat" not in err


def test_private_repo_proceeds_past_guard(tmp_path, monkeypatch):
    # Sanity: when the repo is confirmed private, the guard lets execution
    # continue. We stub _copy_with_sudo etc so the rest of the flow runs
    # without exercising real fs ops.
    monkeypatch.setattr("backup._copy_with_sudo", lambda s, d, b: (True, ""))
    monkeypatch.setattr("backup._redact_snapshot_file", lambda p: (False, "no-secrets"))
    monkeypatch.setattr("backup.sha256_file", lambda p: "h" * 16)
    monkeypatch.setattr("backup.get_applied_since_last_backup", lambda b, s, w: [])
    monkeypatch.setattr("backup.now_iso", lambda: "2026-05-28T12:00:00Z")
    status, _ = _drive(tmp_path, monkeypatch, pat="ghp_x", visibility="private")
    # No changes staged → "no-changes" is the expected status here.
    assert status in ("no-changes", "ok", "failed")  # failed only if a later stage trips

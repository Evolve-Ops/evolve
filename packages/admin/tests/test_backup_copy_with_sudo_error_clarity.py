"""test_backup_copy_with_sudo_error_clarity.py — `_copy_with_sudo` and
`commit_baseline_local` surface read-vs-write failures distinctly.

Regression guard for the 2026-05-21 accept_drift confabulation. The old
code swallowed PermissionError from the *write* step inside an
``except OSError`` block intended for the *read* fallback, then returned
False with the message "cannot read … skipping". The caller propagated
"could not read live openclaw.json" out to the API, and evo then
confabulated an ACL/admin-user misconfiguration.

These tests assert:
* When the write step fails (read OK), the reason token is "cannot
  write" and the propagated API string says "could not write
  evolve-backup/openclaw.json" — NOT "could not read live openclaw.json".
* When the read step genuinely fails, the existing "could not read" path
  still works.

See internal/diagnosis-accept-drift-regression-2026-05-21.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Pre-bind analyzer to the worktree mirror so we exercise this worktree's
# backup.py rather than the editable-installed copy at the main repo.
_ANALYZER_DIR = Path(__file__).resolve().parent.parent.parent / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup  # noqa: E402


class _Result:
    """Minimal subprocess.CompletedProcess stand-in."""
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ─────────────────────────────────────────────────────────────────────────────
# _copy_with_sudo: read-vs-write error attribution
# ─────────────────────────────────────────────────────────────────────────────


def test_copy_with_sudo_write_failure_returns_cannot_write(
    monkeypatch, tmp_path, capsys,
):
    """The 2026-05-21 failure mode: shutil.copy2 hits PermissionError on
    the dst (evolve-backup/ owned by root), the sudo cat read succeeds,
    then dst.write_bytes also hits PermissionError. The function must
    return (False, "cannot write") and the message must say "cannot
    write" (NOT "cannot read")."""

    src = tmp_path / "openclaw.json"
    src.write_bytes(b'{"k":"v"}')
    # dst lives under a tmp_path subdir; we don't actually create the file
    # there — the monkeypatched write_bytes raises before any FS write.
    dst = tmp_path / "evolve-backup" / "openclaw.json"

    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(d))

    def _sudo_cat_succeeds(args, **kwargs):
        # Confirm the call is the expected ``sudo /bin/cat <src>`` shape.
        assert args[:2] == ["sudo", "/bin/cat"]
        return _Result(0, stdout=b'{"k":"v"}')

    def _write_bytes_fails(self, data):
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_succeeds)
    monkeypatch.setattr(Path, "write_bytes", _write_bytes_fails)

    ok, reason = backup._copy_with_sudo(src, dst, "team_bot_a")

    assert ok is False
    assert reason == "cannot write", (
        f"Expected reason 'cannot write' (read OK but dst write failed), "
        f"got {reason!r}. This is exactly the 2026-05-21 misattribution."
    )

    captured = capsys.readouterr()
    log = captured.err
    assert "cannot write" in log, (
        f"stderr log should say 'cannot write' — got: {log!r}. The "
        f"diagnostic message is what an operator/evo sees when reading "
        f"daemon logs; misattributing the failure here led to "
        f"confabulation on 2026-05-21."
    )
    assert "cannot read" not in log, (
        f"stderr log must NOT say 'cannot read' on a write failure — "
        f"that was the historical bug. Got: {log!r}"
    )


def test_copy_with_sudo_read_failure_returns_cannot_read(
    monkeypatch, tmp_path, capsys,
):
    """When the read genuinely fails (shutil.copy2 raises *and* sudo cat
    returns non-zero), the function must return (False, "cannot read")
    and the log must say "cannot read"."""

    src = tmp_path / "openclaw.json"
    # Source file doesn't even exist — copy2 will raise.
    dst = tmp_path / "evolve-backup" / "openclaw.json"

    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(s))

    def _sudo_cat_fails(args, **kwargs):
        assert args[:2] == ["sudo", "/bin/cat"]
        return _Result(1, stdout=b"", stderr=b"cat: no such file")

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_fails)

    ok, reason = backup._copy_with_sudo(src, dst, "team_bot_a")

    assert ok is False
    assert reason == "cannot read"

    captured = capsys.readouterr()
    log = captured.err
    assert "cannot read" in log
    assert "cannot write" not in log, (
        f"On a genuine read failure the log must not mention write. "
        f"Got: {log!r}"
    )


def test_copy_with_sudo_read_failure_via_oserror_returns_cannot_read(
    monkeypatch, tmp_path, capsys,
):
    """When sudo cat itself raises (timeout or OSError), classify as
    'cannot read' rather than letting it mislabel as anything else."""

    src = tmp_path / "openclaw.json"
    dst = tmp_path / "evolve-backup" / "openclaw.json"

    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(s))

    def _sudo_cat_raises(args, **kwargs):
        raise OSError("sudo missing")

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_raises)

    ok, reason = backup._copy_with_sudo(src, dst, "team_bot_a")

    assert ok is False
    assert reason == "cannot read"
    log = capsys.readouterr().err
    assert "cannot read" in log


def test_copy_with_sudo_happy_path_returns_ok(monkeypatch, tmp_path):
    """Direct shutil.copy2 success short-circuits — must return
    (True, "")."""
    src = tmp_path / "openclaw.json"
    src.write_bytes(b'{"k":"v"}')
    dst = tmp_path / "evolve-backup" / "openclaw.json"

    ok, reason = backup._copy_with_sudo(src, dst, "team_bot_a")
    assert ok is True
    assert reason == ""
    assert dst.read_bytes() == b'{"k":"v"}'


def test_copy_with_sudo_fallback_path_succeeds(monkeypatch, tmp_path):
    """When direct copy2 fails but sudo cat + write_bytes both succeed,
    we get (True, "")."""
    src = tmp_path / "openclaw.json"
    src.write_bytes(b"shouldn't be read directly")
    dst = tmp_path / "evolve-backup" / "openclaw.json"
    dst.parent.mkdir(parents=True, exist_ok=True)

    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(d))

    def _sudo_cat_succeeds(args, **kwargs):
        return _Result(0, stdout=b'{"via":"sudo cat"}')

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_succeeds)

    ok, reason = backup._copy_with_sudo(src, dst, "team_bot_a")
    assert ok is True
    assert reason == ""
    assert dst.read_bytes() == b'{"via":"sudo cat"}'


# ─────────────────────────────────────────────────────────────────────────────
# commit_baseline_local: propagated error strings
# ─────────────────────────────────────────────────────────────────────────────


def _stub_workspace(tmp_path: Path, bot_id: str) -> Path:
    """Stand up a fake /Users/<bot>/.openclaw/workspace under tmp_path
    and return the bot_home root."""
    bot_home = tmp_path / "Users" / bot_id
    oc = bot_home / ".openclaw"
    workspace = oc / "workspace"
    workspace.mkdir(parents=True)
    (oc / "openclaw.json").write_text('{"k":"v"}')
    # Initialize the workspace as a git repo so _git rev-parse passes.
    subprocess.run(
        ["/usr/bin/git", "init", "-b", "main", str(workspace)],
        check=True, capture_output=True,
    )
    return bot_home


def test_commit_baseline_local_says_cannot_write_when_write_fails(
    monkeypatch, tmp_path,
):
    """The full 2026-05-21 failure mode end-to-end: when the
    evolve-backup write fails (read OK), commit_baseline_local must
    return the explicit "could not write evolve-backup/openclaw.json"
    string — NOT the historical "could not read live openclaw.json".
    """
    bot_home = _stub_workspace(tmp_path, "team_bot_a")
    monkeypatch.setattr(backup, "_bot_home", lambda bot_id: bot_home)

    # Capture the real subprocess.run *before* monkeypatching so the
    # interceptor can delegate to it for git calls without recursing.
    real_run = subprocess.run

    # Force the write-failure branch in _copy_with_sudo.
    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(d))

    def _sudo_cat_succeeds(args, **kwargs):
        # _git uses subprocess.run too — only intercept sudo cat calls.
        if list(args[:2]) == ["sudo", "/bin/cat"]:
            return _Result(0, stdout=b'{"k":"v"}')
        # Fall back to real subprocess.run for git commands.
        return real_run(args, **kwargs)

    real_write_bytes = Path.write_bytes

    def _write_bytes_fails(self, data):
        # Only fail the evolve-backup destination write.
        if "evolve-backup" in str(self):
            raise PermissionError(13, "denied", str(self))
        # Otherwise, do real bytes write so any other code path works.
        return real_write_bytes(self, data)

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_succeeds)
    monkeypatch.setattr(Path, "write_bytes", _write_bytes_fails)

    ok, msg = backup.commit_baseline_local(
        "team_bot_a", tmp_path / "shared",
    )

    assert ok is False
    assert "could not write" in msg, (
        f"commit_baseline_local should report a write failure with "
        f"'could not write' so evo's tool result names the right path. "
        f"Got: {msg!r}. The 2026-05-21 confabulation would recur."
    )
    assert "evolve-backup" in msg, (
        f"Error message should name evolve-backup/openclaw.json so the "
        f"operator/evo gets the right path. Got: {msg!r}"
    )
    assert "could not read" not in msg, (
        f"On a write failure the message must NOT say 'could not read' "
        f"— that's the bug this PR fixes. Got: {msg!r}"
    )


def test_commit_baseline_local_says_cannot_read_when_read_fails(
    monkeypatch, tmp_path,
):
    """If the source openclaw.json is genuinely unreadable (read fails),
    commit_baseline_local should keep the existing "could not read live
    openclaw.json" string."""
    bot_home = _stub_workspace(tmp_path, "team_bot_a")
    monkeypatch.setattr(backup, "_bot_home", lambda bot_id: bot_home)

    real_run = subprocess.run

    def _copy2_fails(s, d):
        raise PermissionError(13, "denied", str(s))

    def _sudo_cat_fails(args, **kwargs):
        if list(args[:2]) == ["sudo", "/bin/cat"]:
            return _Result(1, stderr=b"cat: permission denied")
        return real_run(args, **kwargs)

    monkeypatch.setattr(backup.shutil, "copy2", _copy2_fails)
    monkeypatch.setattr(backup.subprocess, "run", _sudo_cat_fails)

    ok, msg = backup.commit_baseline_local("team_bot_a", tmp_path / "shared")

    assert ok is False
    assert "could not read" in msg, (
        f"On a genuine read failure, message should remain the 'could "
        f"not read live openclaw.json' string. Got: {msg!r}"
    )
    assert "could not write" not in msg

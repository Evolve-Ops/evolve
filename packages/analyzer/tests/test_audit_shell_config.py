"""Tests for audit.audit_shell_config and audit._hash_bot_zshrc.

Pins three fixes from 2026-05-07:

  1. The previous version called `sudo /bin/cat /Users/<bot>/.zshrc`, but
     /etc/sudoers.d/evolve never granted /bin/cat for that path. Every
     read failed with "sudo: a password is required" and the audit
     surfaced a WARN per bot per 15-min run. Fix: try a direct
     (non-sudo) read first; fall back to `sudo /bin/cat` for bots
     whose .zshrc is locked-down.

  2. The state-comparison logic conflated "still absent" with
     "unreadable" — a bot whose baseline was "absent" and whose current
     read returned None hit the `current_hash is None` branch and
     produced a WARN, even when the file was correctly still absent.
     Fix: distinguish "absent" (file doesn't exist) from None
     (file exists but evolve can't read it) in _hash_bot_zshrc.

  3. The "still absent" baseline match wasn't expressed as the same
     idiom as "still same hash" — the new logic uses `state == stored`
     for both, since "absent" is a sentinel string that compares cleanly.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _patch_bot_home(home: Path):
    """audit_shell_config calls _bot_home(bot_id) to find each home; redirect to a tmpdir."""
    return patch.object(audit, "_bot_home", lambda *_a, **_k: home)


# ── _hash_bot_zshrc: three-state read ──


def test_hash_returns_hex_for_world_readable_zshrc(tmp_path: Path):
    home = tmp_path / "team_bot_a"
    home.mkdir()
    (home / ".zshrc").write_text("export FOO=bar\n")
    expected = hashlib.sha256(b"export FOO=bar\n").hexdigest()
    with _patch_bot_home(home):
        assert audit._hash_bot_zshrc("team_bot_a") == expected


def test_hash_returns_absent_when_file_missing(tmp_path: Path):
    home = tmp_path / "team_bot_b"
    home.mkdir()  # no .zshrc inside
    with _patch_bot_home(home):
        assert audit._hash_bot_zshrc("team_bot_b") == "absent"


def test_hash_returns_none_when_file_unreadable(tmp_path: Path, monkeypatch):
    """Permission error on direct read AND sudo fallback fails → None."""
    home = tmp_path / "admin_bot"
    home.mkdir()
    zshrc = home / ".zshrc"
    zshrc.write_text("locked")

    # Force the direct read to raise PermissionError (we can't reliably
    # produce 0600 in tmp under pytest). Then make the sudo fallback also fail.
    real_read = Path.read_bytes

    def fake_read_bytes(self):
        if self == zshrc:
            raise PermissionError("simulated 0600")
        return real_read(self)

    def fake_run(args, **_kw):
        # Sudo fallback returns non-zero — mimics no-grant
        return type("R", (), {"returncode": 1, "stdout": b""})()

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    with _patch_bot_home(home):
        assert audit._hash_bot_zshrc("admin_bot") is None


def test_hash_uses_sudo_fallback_when_direct_read_perm_denied(tmp_path: Path, monkeypatch):
    """If sudoers grants /bin/cat for .zshrc, a 0600 file still gets hashed."""
    home = tmp_path / "admin_bot"
    home.mkdir()
    zshrc = home / ".zshrc"
    zshrc.write_text("locked content")

    real_read = Path.read_bytes

    def fake_read_bytes(self):
        if self == zshrc:
            raise PermissionError("simulated 0600")
        return real_read(self)

    def fake_run(args, **_kw):
        return type("R", (), {
            "returncode": 0,
            "stdout": b"locked content",
        })()

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    with _patch_bot_home(home):
        h = audit._hash_bot_zshrc("admin_bot")
    assert h == hashlib.sha256(b"locked content").hexdigest()


# ── audit_shell_config: state-comparison logic ──


def _baseline_path(shared_dir: Path) -> Path:
    return shared_dir / "security" / "baselines" / "shell-hashes.json"


def _setup(tmp_path: Path):
    home = tmp_path / "bot"
    home.mkdir()
    return home, tmp_path  # (bot home, shared_dir)


def test_first_run_baseline_present(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    (home / ".zshrc").write_text("hi")
    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    assert any(f.level == "ok" and "baseline created" in f.message for f in findings)


def test_first_run_baseline_absent(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    assert any(f.level == "ok" and "absent — baseline" in f.message for f in findings)


def test_still_absent_does_not_warn(tmp_path: Path):
    """The headline regression: baseline=absent + current=absent must be OK,
    not the spurious 'unreadable' WARN the old code produced."""
    home, shared_dir = _setup(tmp_path)
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text('{"bot": "absent"}')
    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    levels = {f.level for f in findings}
    assert "warn" not in levels, [f.message for f in findings]
    assert any(f.level == "ok" and "OK" in f.message for f in findings)


def test_unreadable_warns_when_baseline_present(tmp_path: Path, monkeypatch):
    home, shared_dir = _setup(tmp_path)
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text('{"bot": "abc123def456"}')

    monkeypatch.setattr(audit, "_hash_bot_zshrc", lambda _bot: None)
    findings = audit.audit_shell_config(["bot"], shared_dir)
    assert any(f.level == "warn" and "unreadable" in f.message for f in findings)


def test_appeared_warns_and_updates_baseline(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text('{"bot": "absent"}')
    (home / ".zshrc").write_text("brand new content")

    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)

    appeared = [f for f in findings if "appeared" in f.message]
    assert appeared and appeared[0].level == "warn"

    import json
    new_baseline = json.loads(_baseline_path(shared_dir).read_text())
    assert new_baseline["bot"] != "absent"
    assert len(new_baseline["bot"]) == 64  # sha256 hex


def test_deleted_warns(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text('{"bot": "abc123def456"}')

    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    assert any(f.level == "warn" and "deleted" in f.message for f in findings)


def test_changed_critical(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    (home / ".zshrc").write_text("changed content")
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text('{"bot": "old_hash_abcdef"}')

    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    crits = [f for f in findings if f.level == "critical"]
    assert crits and "hash changed" in crits[0].message


def test_unchanged_ok(tmp_path: Path):
    home, shared_dir = _setup(tmp_path)
    (home / ".zshrc").write_text("steady state")
    h = hashlib.sha256(b"steady state").hexdigest()
    _baseline_path(shared_dir).parent.mkdir(parents=True)
    _baseline_path(shared_dir).write_text(f'{{"bot": "{h}"}}')

    with _patch_bot_home(home):
        findings = audit.audit_shell_config(["bot"], shared_dir)
    assert all(f.level == "ok" for f in findings)

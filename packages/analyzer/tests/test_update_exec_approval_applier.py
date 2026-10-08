"""Tests for the UpdateExecApproval applier."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbiter.appliers import permissions as _perms_app  # noqa: F401 — registers
from arbiter.appliers.base import get_applier
from schema.proposal import UpdateExecApproval


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    return home


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _applier(bot_home: Path, shared: Path | None = None):
    a = get_applier("UpdateExecApproval")
    a.home_override = bot_home  # type: ignore[attr-defined]
    a.shared_override = shared  # type: ignore[attr-defined]
    return a


def _write_approvals(home: Path, obj: dict) -> None:
    (home / ".openclaw" / "exec-approvals.json").write_text(json.dumps(obj))


# ── add ─────────────────────────────────────────────────────────────────────

def test_add_creates_file_when_missing(bot_home: Path, shared_dir: Path):
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="add", pattern="git status")

    result = a.apply(action, "bot")

    assert result.ok, result.message
    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    assert "git status" in (saved.get("agents", {}).get("main", {}).get("approvals", {}))


def test_add_appends_to_existing(bot_home: Path, shared_dir: Path):
    _write_approvals(bot_home, {"agents": {"main": {"approvals": {"git status": {}}}}})
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="add", pattern="git log")

    result = a.apply(action, "bot")

    assert result.ok
    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    approvals = saved["agents"]["main"]["approvals"]
    assert "git status" in approvals
    assert "git log" in approvals


def test_add_rejects_denylist_match(bot_home: Path, shared_dir: Path):
    """rm -rf <path> should be refused per default denylist."""
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="add", pattern="rm -rf /etc/foo")

    result = a.apply(action, "bot")

    assert not result.ok
    assert "denylist" in result.message.lower()
    # File should not have been written
    assert not (bot_home / ".openclaw" / "exec-approvals.json").exists()


def test_add_to_defaults_scope(bot_home: Path, shared_dir: Path):
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(
        bot_id="bot", operation="add", pattern="ls /Users",
        scope="defaults",
    )

    result = a.apply(action, "bot")

    assert result.ok
    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    assert "ls /Users" in saved.get("defaults", {})


# ── revoke ──────────────────────────────────────────────────────────────────

def test_revoke_removes_exact_match(bot_home: Path, shared_dir: Path):
    _write_approvals(bot_home, {
        "agents": {"main": {"approvals": {"git status": {}, "git log": {}}}}
    })
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="revoke", pattern="git status")

    result = a.apply(action, "bot")

    assert result.ok
    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    approvals = saved["agents"]["main"]["approvals"]
    assert "git status" not in approvals
    assert "git log" in approvals


def test_revoke_by_canonical_removes_all_raw_matches(bot_home: Path, shared_dir: Path):
    """Revoking 'python3 <path>' should remove every raw entry that canonicalizes to it."""
    _write_approvals(bot_home, {
        "agents": {
            "main": {
                "approvals": {
                    "python3 /Users/x/script.py": {},
                    "python3 ~/other.py": {},
                    "git status": {},
                }
            }
        }
    })
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="revoke", pattern="python3 <path>")

    result = a.apply(action, "bot")

    assert result.ok
    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    approvals = saved["agents"]["main"]["approvals"]
    assert "python3 /Users/x/script.py" not in approvals
    assert "python3 ~/other.py" not in approvals
    assert "git status" in approvals


def test_revoke_missing_is_noop(bot_home: Path, shared_dir: Path):
    _write_approvals(bot_home, {"agents": {"main": {"approvals": {"git status": {}}}}})
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="revoke", pattern="rm -rf /")

    result = a.apply(action, "bot")

    assert result.ok
    assert result.details.get("no_op") is True


# ── snapshot + revert ───────────────────────────────────────────────────────

def test_revert_restores_prior_file(bot_home: Path, shared_dir: Path):
    initial = {"agents": {"main": {"approvals": {"git status": {}}}}}
    _write_approvals(bot_home, initial)
    a = _applier(bot_home, shared_dir)
    action = UpdateExecApproval(bot_id="bot", operation="add", pattern="git log")

    snap = a.capture_snapshot(action, "bot")
    a.apply(action, "bot")
    a.revert(snap, "bot")

    saved = json.loads((bot_home / ".openclaw" / "exec-approvals.json").read_text())
    assert saved == initial

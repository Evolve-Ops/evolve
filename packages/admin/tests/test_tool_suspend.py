"""Tests for evolve_admin.tool_suspend.

The suspend flag is the load-bearing mechanism that stops self-cancelling
tools (heal.py was the canonical case). These tests cover:
  - Read path: missing file, present file, corrupted file, expired entries
  - Write path: atomic semantics, idempotency, cleanup
  - Round-trip: suspend → is_suspended → unsuspend → is_suspended
  - guard(): the one-line caller convenience
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evolve_admin import tool_suspend


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A temp dir that looks like a repo root (has docs/verification/)."""
    (tmp_path / "docs" / "verification").mkdir(parents=True)
    return tmp_path


# ── Read path ─────────────────────────────────────────────────────────────


def test_is_suspended_returns_false_when_file_missing(tmp_repo: Path):
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_is_suspended_returns_false_for_unknown_tool(tmp_repo: Path):
    flag = tmp_repo / "docs/verification/.tool-suspended.json"
    flag.write_text(json.dumps({"apply": {"reason": "test", "finding_id": "x", "suspended_at": "..."}}))
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_is_suspended_returns_true_for_active_entry(tmp_repo: Path):
    tool_suspend.suspend("heal", "test reason", "2026-04-26-001", repo_root=tmp_repo)
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is True


def test_is_suspended_handles_corrupt_file(tmp_repo: Path):
    flag = tmp_repo / "docs/verification/.tool-suspended.json"
    flag.write_text("{ not valid json")
    # Defensive: corrupted file shouldn't block tool from running
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_is_suspended_handles_non_dict_root(tmp_repo: Path):
    flag = tmp_repo / "docs/verification/.tool-suspended.json"
    flag.write_text(json.dumps(["not", "a", "dict"]))
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_is_suspended_respects_suspended_until(tmp_repo: Path):
    # Entry with suspended_until in the past — should be treated as expired
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    tool_suspend.suspend("heal", "old", "x", suspended_until=past, repo_root=tmp_repo)
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_is_suspended_active_when_until_in_future(tmp_repo: Path):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    tool_suspend.suspend("heal", "active", "x", suspended_until=future, repo_root=tmp_repo)
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is True


# ── Write path ────────────────────────────────────────────────────────────


def test_suspend_writes_full_record(tmp_repo: Path):
    tool_suspend.suspend("heal", "M1 failed", "2026-04-26-014", repo_root=tmp_repo)
    record = tool_suspend.get_suspension("heal", repo_root=tmp_repo)
    assert record is not None
    assert record["reason"] == "M1 failed"
    assert record["finding_id"] == "2026-04-26-014"
    assert record["suspended_until"] is None
    # suspended_at is an ISO timestamp; validate it parses
    assert datetime.fromisoformat(record["suspended_at"].replace("Z", "+00:00"))


def test_suspend_is_idempotent_on_same_tool(tmp_repo: Path):
    tool_suspend.suspend("heal", "first", "f1", repo_root=tmp_repo)
    tool_suspend.suspend("heal", "second", "f2", repo_root=tmp_repo)
    record = tool_suspend.get_suspension("heal", repo_root=tmp_repo)
    # Newer wins
    assert record["reason"] == "second"
    assert record["finding_id"] == "f2"


def test_unsuspend_removes_entry(tmp_repo: Path):
    tool_suspend.suspend("heal", "test", "x", repo_root=tmp_repo)
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is True
    removed = tool_suspend.unsuspend("heal", repo_root=tmp_repo)
    assert removed is True
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False


def test_unsuspend_returns_false_when_not_suspended(tmp_repo: Path):
    assert tool_suspend.unsuspend("heal", repo_root=tmp_repo) is False


def test_unsuspend_only_affects_named_tool(tmp_repo: Path):
    tool_suspend.suspend("heal", "h", "f1", repo_root=tmp_repo)
    tool_suspend.suspend("apply", "a", "f2", repo_root=tmp_repo)
    tool_suspend.unsuspend("heal", repo_root=tmp_repo)
    assert tool_suspend.is_suspended("heal", repo_root=tmp_repo) is False
    assert tool_suspend.is_suspended("apply", repo_root=tmp_repo) is True


def test_atomic_write_no_partial_files_left_behind(tmp_repo: Path):
    tool_suspend.suspend("heal", "test", "x", repo_root=tmp_repo)
    flag_dir = tmp_repo / "docs/verification"
    leftover = list(flag_dir.glob(".tool-suspended-*.json"))
    assert leftover == [], f"tmp files leaked: {leftover}"


# ── List + guard ──────────────────────────────────────────────────────────


def test_list_suspensions_returns_active_only(tmp_repo: Path):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    tool_suspend.suspend("heal", "expired", "x", suspended_until=past, repo_root=tmp_repo)
    tool_suspend.suspend("apply", "active", "y", suspended_until=future, repo_root=tmp_repo)
    tool_suspend.suspend("verify", "indefinite", "z", repo_root=tmp_repo)
    suspensions = tool_suspend.list_suspensions(repo_root=tmp_repo)
    assert "heal" not in suspensions   # expired
    assert "apply" in suspensions
    assert "verify" in suspensions


def test_guard_returns_false_when_not_suspended(tmp_repo: Path):
    messages = []
    assert tool_suspend.guard("heal", log_fn=messages.append, repo_root=tmp_repo) is False
    assert messages == []   # nothing logged


def test_guard_returns_true_and_logs_when_suspended(tmp_repo: Path):
    tool_suspend.suspend("heal", "M1 meta-test failed", "2026-04-26-014", repo_root=tmp_repo)
    messages = []
    assert tool_suspend.guard("heal", log_fn=messages.append, repo_root=tmp_repo) is True
    assert len(messages) == 1
    msg = messages[0]
    assert "heal is SUSPENDED" in msg
    assert "M1 meta-test failed" in msg
    assert "2026-04-26-014" in msg


def test_guard_handles_log_fn_failures(tmp_repo: Path):
    """A broken log_fn shouldn't propagate — guard's job is to gate execution."""
    tool_suspend.suspend("heal", "test", "x", repo_root=tmp_repo)

    def bad_log(_msg):
        raise RuntimeError("logging is on fire")

    # Must still return True even if logging blew up
    assert tool_suspend.guard("heal", log_fn=bad_log, repo_root=tmp_repo) is True


def test_guard_fails_open_when_is_suspended_raises(tmp_repo: Path, monkeypatch):
    """If is_suspended() blows up unexpectedly, guard returns False (proceed
    normally) rather than crashing the calling daemon. Verified by monkey-
    patching is_suspended to raise. Also confirms log_fn gets the failure
    message so an operator notices the suspend mechanism is broken."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("flag file disk corrupted")
    monkeypatch.setattr(tool_suspend, "is_suspended", boom)

    messages = []
    assert tool_suspend.guard("heal", log_fn=messages.append, repo_root=tmp_repo) is False
    assert len(messages) == 1
    assert "fail-open" in messages[0]
    assert "RuntimeError" in messages[0]


def test_guard_fails_open_when_log_fn_also_fails(tmp_repo: Path, monkeypatch):
    """Belt-and-suspenders: even if both is_suspended AND the log_fn break,
    guard never raises. Worst case the operator never sees the failure
    message, but the daemon doesn't crash."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("everything is broken")
    monkeypatch.setattr(tool_suspend, "is_suspended", boom)

    def bad_log(_msg):
        raise IOError("logging also broken")

    # Must not raise; must return False (fail-open).
    assert tool_suspend.guard("heal", log_fn=bad_log, repo_root=tmp_repo) is False


def test_guard_fails_open_with_no_log_fn(tmp_repo: Path, monkeypatch):
    """No log_fn supplied + is_suspended raises → still fail-open."""
    def boom(*_args, **_kwargs):
        raise OSError("fs gone")
    monkeypatch.setattr(tool_suspend, "is_suspended", boom)

    assert tool_suspend.guard("heal", repo_root=tmp_repo) is False

"""
tests/test_defer_queue.py — Continuity Engine v2 queue tests.

Covers:
  1. new_row validation (mode/message/action coupling, ISO timestamp parsing)
  2. append_row + read_queue round-trip
  3. is_due() time semantics
  4. rewrite_queue atomicity (file present throughout)
  5. archive append-only behavior
  6. iter_bot_queues skips bots with no queue file

Run with:
  cd packages/analyzer && python -m pytest tests/test_defer_queue.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def home_override(monkeypatch):
    """Redirect bot_evolve_dir() to a tempdir so tests don't touch /Users."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", tmp)
        yield Path(tmp)


def _future_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ── new_row validation ───────────────────────────────────────────────────────


class TestNewRow:
    def test_message_mode(self):
        from defer_queue import new_row, MODE_MESSAGE
        r = new_row("admin_bot", _future_iso(), MODE_MESSAGE, message="hi")
        assert r.mode == MODE_MESSAGE
        assert r.message == "hi"
        assert r.action is None
        assert r.status == "pending"
        assert r.bot_id == "admin_bot"
        assert r.defer_id  # populated

    def test_action_mode(self):
        from defer_queue import new_row, MODE_ACTION
        r = new_row("admin_bot", _future_iso(), MODE_ACTION, action="check builds")
        assert r.mode == MODE_ACTION
        assert r.action == "check builds"
        assert r.message is None

    def test_message_requires_message(self):
        from defer_queue import new_row, MODE_MESSAGE
        with pytest.raises(ValueError, match="non-empty message"):
            new_row("admin_bot", _future_iso(), MODE_MESSAGE)

    def test_action_requires_action(self):
        from defer_queue import new_row, MODE_ACTION
        with pytest.raises(ValueError, match="non-empty action"):
            new_row("admin_bot", _future_iso(), MODE_ACTION)

    def test_message_rejects_action(self):
        from defer_queue import new_row, MODE_MESSAGE
        with pytest.raises(ValueError, match="must not set action"):
            new_row("admin_bot", _future_iso(), MODE_MESSAGE, message="x", action="y")

    def test_unknown_mode_rejected(self):
        from defer_queue import new_row
        with pytest.raises(ValueError, match="mode must be"):
            new_row("admin_bot", _future_iso(), "schedule", message="x")

    def test_unparseable_fires_at_rejected(self):
        from defer_queue import new_row, MODE_MESSAGE
        with pytest.raises(ValueError, match="ISO 8601"):
            new_row("admin_bot", "tomorrow at noon", MODE_MESSAGE, message="x")


# ── round-trip ───────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_append_and_read(self, home_override):
        from defer_queue import new_row, append_row, read_queue, MODE_MESSAGE
        r1 = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="one")
        r2 = new_row("admin_bot", _future_iso(120), MODE_MESSAGE, message="two")
        append_row(r1)
        append_row(r2)
        rows = read_queue("admin_bot")
        assert len(rows) == 2
        ids = {r.defer_id for r in rows}
        assert {r1.defer_id, r2.defer_id} == ids

    def test_read_empty_when_no_file(self, home_override):
        from defer_queue import read_queue
        assert read_queue("admin_bot") == []

    def test_read_skips_malformed_lines(self, home_override):
        from defer_queue import new_row, append_row, read_queue, queue_path, MODE_MESSAGE
        good = new_row("admin_bot", _future_iso(), MODE_MESSAGE, message="ok")
        append_row(good)
        # Manually append a malformed line
        with queue_path("admin_bot").open("a") as f:
            f.write("not json\n")
            f.write('{"defer_id":"x"}\n')  # missing required fields
        rows = read_queue("admin_bot")
        # Only the well-formed row survives (malformed + missing-fields skipped)
        assert len(rows) == 1
        assert rows[0].defer_id == good.defer_id


# ── is_due semantics ─────────────────────────────────────────────────────────


class TestIsDue:
    def test_future_not_due(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        r = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="x")
        assert not r.is_due()

    def test_past_is_due(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        assert r.is_due()

    def test_non_pending_not_due(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE, STATUS_FIRED
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        r.status = STATUS_FIRED
        assert not r.is_due()


# ── rewrite + archive ────────────────────────────────────────────────────────


class TestRewriteAndArchive:
    def test_rewrite_drops_rows(self, home_override):
        from defer_queue import new_row, append_row, read_queue, rewrite_queue, MODE_MESSAGE
        r1 = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="keep")
        r2 = new_row("admin_bot", _future_iso(120), MODE_MESSAGE, message="drop")
        append_row(r1)
        append_row(r2)
        rewrite_queue("admin_bot", [r1])
        rows = read_queue("admin_bot")
        assert len(rows) == 1
        assert rows[0].defer_id == r1.defer_id

    def test_archive_append(self, home_override):
        from defer_queue import (
            new_row, append_archive, archive_path, MODE_MESSAGE, STATUS_FIRED,
        )
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="done")
        r.status = STATUS_FIRED
        r.fired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        append_archive("admin_bot", r)
        # Archive a second one to confirm append, not rewrite.
        r2 = new_row("admin_bot", _past_iso(30), MODE_MESSAGE, message="done2")
        r2.status = STATUS_FIRED
        append_archive("admin_bot", r2)
        lines = archive_path("admin_bot").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_rewrite_chowns_to_bot_when_runnable(self, home_override, monkeypatch):
        """When rewrite_queue runs as root, it should chown the resulting file
        to the bot user. We can't actually run as root in tests, but we can
        confirm the chown helper is called against the right user.

        Fix landed 2026-05-05 after first integration test: root-created
        queue files prevented the next bot-side defer call (EACCES)."""
        from defer_queue import new_row, append_row, rewrite_queue, MODE_MESSAGE
        import defer_queue as dq

        chown_calls: list[tuple[str, str]] = []
        original_chown_to_bot = dq._chown_to_bot

        def spy(path, bot_id):
            chown_calls.append((str(path), bot_id))
            return original_chown_to_bot(path, bot_id)

        monkeypatch.setattr(dq, "_chown_to_bot", spy)

        r = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="x")
        append_row(r)
        # rewrite (no rows kept) — should trigger chown on the new file
        rewrite_queue("admin_bot", [])

        chowned_paths = [p for p, _ in chown_calls]
        assert any("defer-queue" in p for p in chowned_paths), \
            f"Expected rewrite to chown defer-queue.jsonl; got: {chown_calls}"
        # Bot id correctly threaded
        assert all(bot == "admin_bot" for _, bot in chown_calls)

    def test_archive_chowns_first_creation(self, home_override, monkeypatch):
        """First-creation of defer-archive.jsonl by the runner (root) should
        chown to the bot. Subsequent appends should not re-chown."""
        from defer_queue import new_row, append_archive, MODE_MESSAGE, STATUS_FIRED
        import defer_queue as dq

        chown_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            dq, "_chown_to_bot",
            lambda path, bot_id: chown_calls.append((str(path), bot_id)),
        )

        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="done")
        r.status = STATUS_FIRED
        append_archive("admin_bot", r)

        archive_chowns = [c for c in chown_calls if "defer-archive" in c[0]]
        assert len(archive_chowns) == 1, \
            f"Expected exactly 1 archive chown on first creation; got: {chown_calls}"

    def test_rewrite_preserves_file_during_failure(self, home_override, monkeypatch):
        """If rewrite_queue fails partway, the original file should still exist
        with the original contents — we use temp + os.replace for atomicity."""
        from defer_queue import new_row, append_row, queue_path, rewrite_queue, MODE_MESSAGE
        r = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="original")
        append_row(r)
        original = queue_path("admin_bot").read_text()

        # Simulate os.replace raising (e.g., a permission issue) — the temp file
        # cleanup runs in finally, original file untouched.
        import defer_queue as dq
        orig_replace = os.replace
        def boom(*args, **kw):
            raise OSError("simulated")
        monkeypatch.setattr(dq.os, "replace", boom)

        with pytest.raises(OSError, match="simulated"):
            rewrite_queue("admin_bot", [])
        assert queue_path("admin_bot").read_text() == original


# ── multi-bot iteration ──────────────────────────────────────────────────────


class TestIterBotQueues:
    def test_skips_bots_without_files(self, home_override):
        from defer_queue import new_row, append_row, iter_bot_queues, MODE_MESSAGE
        r = new_row("admin_bot", _future_iso(60), MODE_MESSAGE, message="x")
        append_row(r)
        # Only admin_bot has a queue; team_bot_a and personal_bot don't.
        result = dict(iter_bot_queues(["admin_bot", "team_bot_a", "personal_bot"]))
        assert set(result.keys()) == {"admin_bot"}
        assert len(result["admin_bot"]) == 1

    def test_list_due_filters(self, home_override):
        from defer_queue import new_row, append_row, list_due, MODE_MESSAGE
        # One due, one not due — across two bots
        due = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="due")
        not_due = new_row("team_bot_a", _future_iso(3600), MODE_MESSAGE, message="future")
        append_row(due)
        append_row(not_due)
        rows = list_due(["admin_bot", "team_bot_a"])
        assert len(rows) == 1
        assert rows[0].defer_id == due.defer_id

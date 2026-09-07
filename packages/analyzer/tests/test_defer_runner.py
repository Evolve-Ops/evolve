"""
tests/test_defer_runner.py — Continuity Engine v2 runner tests.

Covers:
  1. process_bot — due rows fire, non-due rows are kept, archive grows
  2. Failed dispatch leaves row in archive with status=failed (not in queue)
  3. dispatch_row builds correct subprocess command for both modes
  4. _bot_ids handles dict vs list shapes in network.json
  5. dry_run does not invoke subprocess and does not rewrite files

Run with:
  cd packages/analyzer && python -m pytest tests/test_defer_runner.py -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def home_override(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOLVE_DEFER_HOME_OVERRIDE", tmp)
        yield Path(tmp)


def _future_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _ok_subproc(stdout: str = "{}") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


# ── _bot_ids ─────────────────────────────────────────────────────────────────


class TestBotIdParsing:
    def test_dict_shape(self):
        from defer_runner import _bot_ids
        cfg = {"bots": {"admin_bot": {}, "team_bot_a": {}, "personal_bot": {}}}
        assert _bot_ids(cfg) == ["admin_bot", "personal_bot", "team_bot_a"]

    def test_list_shape(self):
        from defer_runner import _bot_ids
        cfg = {"bots": [{"id": "admin_bot"}, {"id": "team_bot_a"}]}
        assert _bot_ids(cfg) == ["admin_bot", "team_bot_a"]

    def test_missing_bots(self):
        from defer_runner import _bot_ids
        assert _bot_ids({}) == []


# ── dispatch_row ─────────────────────────────────────────────────────────────


class TestDispatchRow:
    def test_message_mode_dispatch(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="My favorite color is blue")
        r.session_id = "53da557b-6a72-4db3-bb6c-c496cb8542a4"
        r.session_key = "agent:main:telegram:direct:1234"

        with patch("defer_runner.subprocess.run", return_value=_ok_subproc()) as mock_run:
            ok, info = dispatch_row(r)

        assert ok, info
        cmd = mock_run.call_args.args[0]
        # Dispatch runs as the bot user via `sudo -H -u <bot> openclaw agent ...`
        # because the runner runs as root and the bot user owns the gateway
        # auth token. The -H is required so openclaw resolves config from the
        # bot user's HOME, not root's.
        assert cmd[:4] == ["sudo", "-H", "-u", "admin_bot"]
        assert cmd[4:6] == ["openclaw", "agent"]
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        # --session-id must be the UUID (session_id), NOT the routing key
        # (session_key) — the CLI rejects the routing-key format.
        assert cmd[idx + 1] == r.session_id
        assert cmd[idx + 1] != r.session_key
        # --deliver is mandatory — without it the agent's reply doesn't reach
        # the user's channel, defeating the whole point of a defer firing.
        assert "--deliver" in cmd
        # subprocess.run cwd kwarg must be /tmp so the bot user can chdir
        # there from the python path importer cache.
        assert mock_run.call_args.kwargs.get("cwd") == "/tmp"
        # Message wrapper carries the literal message content inside <deliver>
        # tags and instructs the agent to output verbatim with no preamble.
        msg_idx = cmd.index("--message")
        body = cmd[msg_idx + 1]
        assert "SYSTEM_DEFER_FIRE" in body
        assert "<deliver>" in body
        assert "My favorite color is blue" in body
        assert "verbatim" in body.lower() or "no greeting" in body.lower() or "output only" in body.lower()

    def test_action_mode_dispatch(self, home_override):
        from defer_queue import new_row, MODE_ACTION
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_ACTION, action="Check build status")
        r.session_id = "uuid-action-1234"
        r.session_key = "agent:main:telegram:direct:1234"

        with patch("defer_runner.subprocess.run", return_value=_ok_subproc()) as mock_run:
            ok, _info = dispatch_row(r)

        assert ok
        cmd = mock_run.call_args.args[0]
        msg_idx = cmd.index("--message")
        body = cmd[msg_idx + 1]
        assert "SYSTEM_DEFER_FIRE" in body
        assert "<action>" in body
        assert "Check build status" in body
        # Action-mode framing tells the agent to act + reply (not just deliver
        # text). Either phrasing satisfies the rule.
        assert "complete the action" in body.lower() or "follow-up message" in body.lower()

    def test_dispatch_fails_without_session_id(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        # No session_id set → cannot dispatch (the UUID is required)
        ok, info = dispatch_row(r)
        assert not ok
        assert "session_id" in info

    def test_dispatch_handles_subprocess_failure(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        bad = MagicMock(returncode=1, stdout="", stderr="oc agent missing")
        with patch("defer_runner.subprocess.run", return_value=bad):
            ok, info = dispatch_row(r)
        assert not ok
        assert "oc agent missing" in info

    def test_dispatch_handles_timeout(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        with patch("defer_runner.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30)):
            ok, info = dispatch_row(r)
        assert not ok
        assert "timed out" in info

    def test_dry_run_skips_subprocess(self, home_override):
        from defer_queue import new_row, MODE_MESSAGE
        from defer_runner import dispatch_row
        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        with patch("defer_runner.subprocess.run") as mock_run:
            ok, info = dispatch_row(r, dry_run=True)
        assert ok
        assert "[dry-run]" in info
        mock_run.assert_not_called()


# ── process_bot ──────────────────────────────────────────────────────────────


class TestProcessBot:
    def test_due_rows_fire_kept_rows_remain(self, home_override):
        from defer_queue import new_row, append_row, read_queue, archive_path, MODE_MESSAGE
        from defer_runner import process_bot

        due = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="due")
        due.session_id = "uuid-due"
        due.session_key = "agent:admin_bot"
        future = new_row("admin_bot", _future_iso(3600), MODE_MESSAGE, message="future")
        future.session_id = "uuid-future"
        future.session_key = "agent:admin_bot"
        append_row(due)
        append_row(future)

        rows = read_queue("admin_bot")
        with patch("defer_runner.subprocess.run", return_value=_ok_subproc()):
            summary = process_bot("admin_bot", rows)

        assert summary == {"checked": 2, "fired": 1, "failed": 0, "kept": 1}

        # Active queue now contains only the future row
        remaining = read_queue("admin_bot")
        assert len(remaining) == 1
        assert remaining[0].defer_id == future.defer_id

        # Archive contains the fired row
        archived = archive_path("admin_bot").read_text().strip().splitlines()
        assert len(archived) == 1
        assert due.defer_id in archived[0]
        assert '"status":"fired"' in archived[0]

    def test_failed_dispatch_archives_as_failed(self, home_override):
        from defer_queue import new_row, append_row, read_queue, archive_path, MODE_MESSAGE
        from defer_runner import process_bot

        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="bad")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        append_row(r)

        bad = MagicMock(returncode=2, stdout="", stderr="failed")
        with patch("defer_runner.subprocess.run", return_value=bad):
            summary = process_bot("admin_bot", read_queue("admin_bot"))

        assert summary["fired"] == 0
        assert summary["failed"] == 1
        # No active rows remain
        assert read_queue("admin_bot") == []
        # Archive has the failed row
        archived = archive_path("admin_bot").read_text()
        assert '"status":"failed"' in archived

    def test_no_due_rows_no_rewrite(self, home_override):
        """If no rows fire, the queue file shouldn't be rewritten (avoids
        unnecessary I/O each cycle)."""
        from defer_queue import new_row, append_row, queue_path, read_queue, MODE_MESSAGE
        from defer_runner import process_bot

        r = new_row("admin_bot", _future_iso(3600), MODE_MESSAGE, message="future")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        append_row(r)

        before_mtime = queue_path("admin_bot").stat().st_mtime_ns
        with patch("defer_runner.subprocess.run") as mock_run:
            summary = process_bot("admin_bot", read_queue("admin_bot"))
        mock_run.assert_not_called()
        after_mtime = queue_path("admin_bot").stat().st_mtime_ns
        assert summary == {"checked": 1, "fired": 0, "failed": 0, "kept": 1}
        assert before_mtime == after_mtime

    def test_dry_run_does_not_modify_files(self, home_override):
        from defer_queue import new_row, append_row, archive_path, read_queue, MODE_MESSAGE
        from defer_runner import process_bot

        r = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="x")
        r.session_id = "uuid-admin_bot"; r.session_key = "agent:admin_bot"
        append_row(r)

        summary = process_bot("admin_bot", read_queue("admin_bot"), dry_run=True)
        # Counted as fired but archive empty + queue unchanged
        assert summary["fired"] == 1
        assert not archive_path("admin_bot").exists()
        assert len(read_queue("admin_bot")) == 1


# ── run_once ─────────────────────────────────────────────────────────────────


class TestRunOnce:
    def test_aggregates_across_bots(self, home_override):
        from defer_queue import new_row, append_row, MODE_MESSAGE
        from defer_runner import run_once

        admin_bot_due = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="s")
        admin_bot_due.session_id = "uuid-admin_bot"
        admin_bot_due.session_key = "agent:admin_bot"
        team_bot_a_due = new_row("team_bot_a", _past_iso(60), MODE_MESSAGE, message="k")
        team_bot_a_due.session_id = "uuid-team_bot_a"
        team_bot_a_due.session_key = "agent:team_bot_a"
        append_row(admin_bot_due)
        append_row(team_bot_a_due)

        cfg = {"bots": {"admin_bot": {}, "team_bot_a": {}, "personal_bot": {}}}
        with patch("defer_runner.subprocess.run", return_value=_ok_subproc()):
            totals = run_once(cfg)

        assert totals["fired"] == 2
        assert totals["bots"] == 2  # personal_bot has no queue file

    def test_per_bot_disabled_skips_dispatch(self, home_override):
        """When a bot has continuity_engine disabled, run_once must not
        dispatch its rows — they stay in the queue for when it's re-enabled."""
        from defer_queue import new_row, append_row, read_queue, MODE_MESSAGE
        from defer_runner import run_once

        admin_bot_due = new_row("admin_bot", _past_iso(60), MODE_MESSAGE, message="s")
        admin_bot_due.session_id = "uuid-admin_bot"
        admin_bot_due.session_key = "agent:admin_bot"
        team_bot_a_due = new_row("team_bot_a", _past_iso(60), MODE_MESSAGE, message="k")
        team_bot_a_due.session_id = "uuid-team_bot_a"
        team_bot_a_due.session_key = "agent:team_bot_a"
        append_row(admin_bot_due)
        append_row(team_bot_a_due)

        # admin_bot opts out; team_bot_a stays default-on
        cfg = {"bots": {
            "admin_bot": {"continuity_engine": {"enabled": False}},
            "team_bot_a": {},
        }}
        with patch("defer_runner.subprocess.run", return_value=_ok_subproc()) as mock_run:
            totals = run_once(cfg)

        # Only team_bot_a fired; admin_bot's row stayed queued
        assert totals["fired"] == 1
        assert totals["skipped_disabled"] == 1
        assert mock_run.call_count == 1
        remaining_admin_bot = read_queue("admin_bot")
        assert len(remaining_admin_bot) == 1
        assert remaining_admin_bot[0].defer_id == admin_bot_due.defer_id

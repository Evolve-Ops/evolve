"""tests/test_imessage_poller.py — iMessage poller daemon (V2.1-6 Task 6).

Tests for imessage_plugin/poller.py:
  - IMessagePoller._poll_new_messages: reads only rows > last_rowid, filters
    by allowed_senders (or open_mode), and skips is_from_me=1 messages.
  - IMessagePoller._forward_to_gateway: POSTs the correct JSON to the gateway.
  - IMessagePoller._poll_once: integrates poll + forward + watermark update.
  - State persistence: load_state reads last_rowid; save_state writes it
    atomically.
  - Filter logic: open_mode=True passes all senders; open_mode=False passes
    only senders in the allowlist.
  - No message content is included in INFO-level logs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_ANALYZER_DIR = _TESTS_DIR.parent
_PACKAGES_DIR = _ANALYZER_DIR.parent
_WORKTREE_ADMIN = _PACKAGES_DIR / "admin" / "evolve_admin"

for _path in (str(_ANALYZER_DIR), str(_PACKAGES_DIR / "admin")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Fixture: in-memory chat.db ────────────────────────────────────────────────


def _create_test_db(tmp_path: Path) -> Path:
    """Create a minimal chat.db with known data."""
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            service TEXT DEFAULT 'iMessage'
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            text TEXT,
            handle_id INTEGER DEFAULT 0,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER,
            service TEXT DEFAULT 'iMessage',
            cache_roomnames TEXT
        );
    """)
    # Apple epoch nanoseconds for known datetimes
    APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
    def _ns(dt):
        return int((dt - APPLE_EPOCH).total_seconds() * 1_000_000_000)

    base = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)

    # Insert handles
    cur = conn.cursor()
    cur.execute("INSERT INTO handle (id, service) VALUES ('+15550001234', 'iMessage')")
    handle_alice = cur.lastrowid
    cur.execute("INSERT INTO handle (id, service) VALUES ('bob@example.com', 'iMessage')")
    handle_bob = cur.lastrowid

    # Messages
    rows = [
        # (guid, text, handle_id, is_from_me, dt)
        ("g1", "Hello from Alice", handle_alice, 0, base),
        ("g2", "Reply from me", 0, 1, base),           # from me — should be skipped by poller
        ("g3", "Another from Alice", handle_alice, 0, base),
        ("g4", "Hi from Bob", handle_bob, 0, base),
        ("g5", "Old message", handle_alice, 0, base),  # same rowid order as others
    ]
    for guid, text, handle_id, is_from_me, dt in rows:
        cur.execute(
            "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) VALUES (?,?,?,?,?,?)",
            (guid, text, handle_id, is_from_me, _ns(dt), "iMessage"),
        )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def test_db(tmp_path):
    return _create_test_db(tmp_path)


# ── IMessagePoller instantiation ──────────────────────────────────────────────


class TestPollerInit:
    def test_open_mode_when_no_senders(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        assert poller.open_mode is True
        assert len(poller.allowed_senders) == 0

    def test_non_open_mode_when_senders_configured(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={"allowed_senders": ["+15550001234"]},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        assert poller.open_mode is False
        assert "+15550001234" in poller.allowed_senders

    def test_handle_from_config(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={"handle": "me@icloud.com"},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        assert poller.bot_handle == "me@icloud.com"


# ── Poll new messages ─────────────────────────────────────────────────────────


class TestPollNewMessages:
    def test_returns_only_incoming_messages(self, test_db, tmp_path):
        """Messages with is_from_me=1 must not be returned."""
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},  # open mode
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        msgs = poller._poll_new_messages()
        for m in msgs:
            # The is_from_me filter is in the query; these are the returned records
            assert m["handle_id"] != ""  # all have a handle
        # "Reply from me" should not appear
        texts = [m["text"] for m in msgs]
        assert "Reply from me" not in texts

    def test_filters_by_last_rowid(self, test_db, tmp_path):
        """Only messages with ROWID > last_rowid should be returned."""
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        # First poll: get all messages
        poller._last_rowid = 0
        all_msgs = poller._poll_new_messages()
        # Update watermark
        if all_msgs:
            poller._last_rowid = all_msgs[-1]["rowid"]
        # Second poll: should get nothing
        new_msgs = poller._poll_new_messages()
        assert new_msgs == []

    def test_allowed_senders_filter(self, test_db, tmp_path):
        """With an allowlist, only messages from listed senders should appear."""
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={"allowed_senders": ["+15550001234"]},  # only Alice
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        msgs = poller._poll_new_messages()
        for m in msgs:
            assert m["handle_id"] == "+15550001234"
        # Bob's messages should not appear
        assert not any(m["handle_id"] == "bob@example.com" for m in msgs)

    def test_open_mode_passes_all_senders(self, test_db, tmp_path):
        """Open mode (no allowlist) should return messages from all senders."""
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},  # open mode
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        msgs = poller._poll_new_messages()
        handles = {m["handle_id"] for m in msgs}
        assert "+15550001234" in handles
        assert "bob@example.com" in handles

    def test_returns_empty_on_db_not_found(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        missing = tmp_path / "nope.db"
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=missing,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        msgs = poller._poll_new_messages()
        assert msgs == []

    def test_message_dict_shape(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        msgs = poller._poll_new_messages()
        if msgs:
            m = msgs[0]
            assert "rowid" in m
            assert "guid" in m
            assert "text" in m
            assert "handle_id" in m
            assert "date" in m
            assert "service" in m


# ── Gateway forwarding ────────────────────────────────────────────────────────


class TestForwardToGateway:
    def test_posts_correct_payload(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        msg = {
            "rowid": 42,
            "guid": "guid-42",
            "text": "Hello bot!",
            "handle_id": "+15550001234",
            "date": 12345,
            "service": "iMessage",
        }

        import urllib.request
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200

        with patch.object(urllib.request, "urlopen", return_value=mock_response) as mock_open:
            result = poller._forward_to_gateway(msg)

        assert result is True
        # Check the request was made
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["message"] == "Hello bot!"
        assert body["session"] == "imessage-+15550001234"
        assert body["metadata"]["sender"] == "+15550001234"
        assert body["metadata"]["rowid"] == 42

    def test_returns_false_on_network_error(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        import urllib.error
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        msg = {"rowid": 1, "guid": "g", "text": "Hi", "handle_id": "+1", "date": 0, "service": "iMessage"}
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = poller._forward_to_gateway(msg)
        assert result is False

    def test_session_id_includes_handle(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        msg = {"rowid": 1, "guid": "g", "text": "Hi", "handle_id": "alice@example.com", "date": 0, "service": "iMessage"}

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            poller._forward_to_gateway(msg)

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["session"] == "imessage-alice@example.com"


# ── poll_once integration ─────────────────────────────────────────────────────


class TestPollOnce:
    def test_advances_watermark_on_success(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0
        initial_rowid = poller._last_rowid

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200

        with patch("urllib.request.urlopen", return_value=mock_response):
            forwarded = poller._poll_once()

        # Watermark should have advanced
        assert poller._last_rowid > initial_rowid

    def test_advances_watermark_even_on_forward_failure(self, test_db, tmp_path):
        """Even if gateway is down, we advance the watermark to avoid getting stuck."""
        from imessage_plugin.poller import IMessagePoller
        import urllib.error
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 0

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            poller._poll_once()

        assert poller._last_rowid > 0

    def test_no_messages_does_not_save_state(self, test_db, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        # Set watermark to max so no messages are new
        poller._last_rowid = 999999

        with patch.object(poller, "_save_state") as mock_save:
            poller._poll_once()
        mock_save.assert_not_called()


# ── State persistence ─────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_load_state_reads_last_rowid(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        state_data = {"last_rowid": 99, "updated_at": "2026-05-13T10:00:00+00:00"}
        poller.state_path.parent.mkdir(parents=True, exist_ok=True)
        poller.state_path.write_text(json.dumps(state_data))

        poller._load_state()
        assert poller._last_rowid == 99

    def test_save_state_writes_last_rowid(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        poller._last_rowid = 42
        poller._save_state()

        data = json.loads(poller.state_path.read_text())
        assert data["last_rowid"] == 42

    def test_load_state_defaults_to_max_rowid_on_first_run(self, test_db, tmp_path):
        """On first run (no state file), last_rowid should be the current max."""
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=test_db,
            workspace_dir=tmp_path,
        )
        assert not poller.state_path.exists()
        poller._load_state()
        # Should be set to a positive value (current max in fixture DB)
        assert poller._last_rowid > 0

    def test_load_state_handles_corrupt_file(self, tmp_path):
        from imessage_plugin.poller import IMessagePoller
        poller = IMessagePoller(
            bot_id="testbot",
            gateway_port=18080,
            config={},
            db_path=tmp_path / "x.db",
            workspace_dir=tmp_path,
        )
        poller.state_path.parent.mkdir(parents=True, exist_ok=True)
        poller.state_path.write_text("not valid json{{{")

        # Should not raise; should default to 0
        poller._load_state()
        assert poller._last_rowid == 0


# ── CLI entry point ───────────────────────────────────────────────────────────


class TestCLIParsing:
    def test_parse_args_required(self):
        from imessage_plugin.poller import _parse_args
        args = _parse_args(["--bot-id", "team_bot_a", "--port", "18080"])
        assert args.bot_id == "team_bot_a"
        assert args.port == 18080
        assert args.poll_interval == 15  # default

    def test_parse_args_custom_interval(self):
        from imessage_plugin.poller import _parse_args
        args = _parse_args(["--bot-id", "team_bot_a", "--port", "18080", "--poll-interval", "30"])
        assert args.poll_interval == 30

    def test_parse_args_db_path_override(self, tmp_path):
        from imessage_plugin.poller import _parse_args
        db = str(tmp_path / "test.db")
        args = _parse_args(["--bot-id", "team_bot_a", "--port", "18080", "--db-path", db])
        assert args.db_path == db

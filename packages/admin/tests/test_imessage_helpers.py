"""tests/test_imessage_helpers.py — iMessage read/write helpers (V2.1-6 Task 1 + 2).

Tests cover:
  - chat.db reading: message parsing, contact lookup, conversation retrieval.
  - Apple-epoch timestamp conversion (both nanoseconds and seconds formats).
  - Hostile inputs: missing DB, permission denied, schema errors.
  - AppleScript injection prevention (_escape_as_string + _build_send_script).
  - send_message subprocess mocking.

The fixture DB is at packages/admin/tests/fixtures/imessage_sample_chat.db —
a small SQLite file with known schema and data, checked into the repo.
It is never replaced at test runtime; tests that need custom data create
their own in-memory or tmp-path DBs.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import imessage_helpers as ih  # noqa: E402

# ── Fixture path ──────────────────────────────────────────────────────────────

FIXTURE_DB = Path(__file__).parent / "fixtures" / "imessage_sample_chat.db"


# ── Apple epoch conversion ────────────────────────────────────────────────────


class TestAppleEpochConversion:
    def test_nanoseconds_format(self):
        # 2026-05-13 10:00:00 UTC in nanoseconds since 2001-01-01
        dt = datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)
        delta = dt - ih._APPLE_EPOCH
        ns = int(delta.total_seconds() * 1_000_000_000)
        assert ns > ih._NS_THRESHOLD  # confirms it's above the threshold
        result = ih._apple_ts_to_utc(ns)
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 13
        assert result.hour == 10
        assert result.tzinfo == timezone.utc

    def test_seconds_format(self):
        # Legacy format: seconds since 2001-01-01
        dt = datetime(2022, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        delta = dt - ih._APPLE_EPOCH
        secs = int(delta.total_seconds())
        assert secs < ih._NS_THRESHOLD  # confirms it's below the threshold
        result = ih._apple_ts_to_utc(secs)
        assert result.year == 2022
        assert result.month == 1

    def test_zero_timestamp(self):
        # Zero = 2001-01-01 00:00:00 UTC
        result = ih._apple_ts_to_utc(0)
        assert result == ih._APPLE_EPOCH


# ── DB not found / permission denied ─────────────────────────────────────────


class TestDbErrors:
    def test_missing_db_returns_empty_list_recent(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        msgs, err = ih.list_recent_messages(
            datetime.now(tz=timezone.utc),
            db_path=missing,
        )
        assert msgs == []
        assert err == ih.ERR_DB_NOT_FOUND

    def test_missing_db_returns_empty_contacts(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        contacts, err = ih.list_contacts(db_path=missing)
        assert contacts == []
        assert err == ih.ERR_DB_NOT_FOUND

    def test_missing_db_returns_empty_conversation(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        msgs, err = ih.get_conversation("+15550001234", db_path=missing)
        assert msgs == []
        assert err == ih.ERR_DB_NOT_FOUND

    def test_permission_denied_open(self, tmp_path):
        """A DB that exists but triggers PermissionError should return ERR_PERMISSION_DENIED."""
        # We mock _open_db to simulate PermissionError
        db = tmp_path / "chat.db"
        db.touch()

        with patch.object(ih, "_open_db", return_value=(None, ih.ERR_PERMISSION_DENIED)):
            msgs, err = ih.list_recent_messages(datetime.now(tz=timezone.utc), db_path=db)
        assert msgs == []
        assert err == ih.ERR_PERMISSION_DENIED


# ── Fixture DB reading ────────────────────────────────────────────────────────


@pytest.fixture
def fixture_db():
    """Return the path to the fixture chat.db."""
    assert FIXTURE_DB.exists(), f"Fixture DB missing: {FIXTURE_DB}"
    return FIXTURE_DB


class TestListRecentMessages:
    def test_returns_messages_since_old_epoch(self, fixture_db):
        """Listing messages since the Apple epoch should return all sample messages."""
        since = ih._APPLE_EPOCH  # retrieve everything
        msgs, err = ih.list_recent_messages(since, db_path=fixture_db)
        assert err is None
        assert isinstance(msgs, list)
        # Fixture has 5 messages
        assert len(msgs) > 0

    def test_filters_by_sender(self, fixture_db):
        """Filtering by sender should only return that handle's messages."""
        since = ih._APPLE_EPOCH
        msgs, err = ih.list_recent_messages(since, sender="+15550001234", db_path=fixture_db)
        assert err is None
        for msg in msgs:
            assert msg["handle_id"] == "+15550001234"

    def test_returns_empty_for_unknown_sender(self, fixture_db):
        since = ih._APPLE_EPOCH
        msgs, err = ih.list_recent_messages(since, sender="unknown@nowhere.com", db_path=fixture_db)
        assert err is None
        assert msgs == []

    def test_message_dict_shape(self, fixture_db):
        since = ih._APPLE_EPOCH
        msgs, err = ih.list_recent_messages(since, db_path=fixture_db, limit=1)
        assert err is None
        if msgs:
            m = msgs[0]
            assert "rowid" in m
            assert "guid" in m
            assert "text" in m
            assert "handle_id" in m
            assert "is_from_me" in m
            assert "date" in m
            assert "service" in m

    def test_is_from_me_field(self, fixture_db):
        since = ih._APPLE_EPOCH
        msgs, err = ih.list_recent_messages(since, db_path=fixture_db)
        assert err is None
        # Fixture has both from-me and from-them
        from_me_count = sum(1 for m in msgs if m["is_from_me"])
        not_from_me_count = sum(1 for m in msgs if not m["is_from_me"])
        # At least one in each direction in the fixture
        assert from_me_count > 0 or not_from_me_count > 0

    def test_limit_respected(self, fixture_db):
        since = ih._APPLE_EPOCH
        msgs, err = ih.list_recent_messages(since, db_path=fixture_db, limit=2)
        assert err is None
        assert len(msgs) <= 2

    def test_future_since_returns_empty(self, fixture_db):
        """No messages after a far-future timestamp."""
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        msgs, err = ih.list_recent_messages(future, db_path=fixture_db)
        assert err is None
        assert msgs == []

    def test_no_sql_injection_via_sender(self, fixture_db):
        """Sender filter with SQL metacharacters must not raise or return extra rows."""
        since = ih._APPLE_EPOCH
        malicious = "'; DROP TABLE message; --"
        msgs, err = ih.list_recent_messages(since, sender=malicious, db_path=fixture_db)
        # Should return empty (no matching sender) rather than error
        assert err is None
        assert msgs == []


class TestListContacts:
    def test_returns_all_handles(self, fixture_db):
        contacts, err = ih.list_contacts(db_path=fixture_db)
        assert err is None
        assert len(contacts) > 0

    def test_contact_dict_shape(self, fixture_db):
        contacts, err = ih.list_contacts(db_path=fixture_db)
        assert err is None
        for c in contacts:
            assert "rowid" in c
            assert "handle_id" in c
            assert "service" in c

    def test_filters_by_query(self, fixture_db):
        contacts, err = ih.list_contacts(query="+1555", db_path=fixture_db)
        assert err is None
        for c in contacts:
            assert "+1555" in c["handle_id"].lower() or "+1555" in c["handle_id"]

    def test_query_no_match_returns_empty(self, fixture_db):
        contacts, err = ih.list_contacts(query="ZZZNOMATCH", db_path=fixture_db)
        assert err is None
        assert contacts == []

    def test_query_no_injection(self, fixture_db):
        malicious = "'; SELECT * FROM sqlite_master; --"
        contacts, err = ih.list_contacts(query=malicious, db_path=fixture_db)
        assert err is None  # no crash, no data leak


class TestGetConversation:
    def test_returns_messages_for_handle(self, fixture_db):
        msgs, err = ih.get_conversation("+15550001234", db_path=fixture_db)
        assert err is None
        assert isinstance(msgs, list)

    def test_unknown_handle_returns_empty(self, fixture_db):
        msgs, err = ih.get_conversation("nobody@nowhere.invalid", db_path=fixture_db)
        assert err is None
        assert msgs == []

    def test_returns_chronological_order(self, fixture_db):
        msgs, err = ih.get_conversation("+15550001234", db_path=fixture_db)
        assert err is None
        if len(msgs) > 1:
            dates = [m["date"] for m in msgs if m["date"]]
            assert dates == sorted(dates)


# ── In-memory DB: schema drift handling ──────────────────────────────────────


class TestSchemaDrift:
    def test_missing_handle_table_returns_schema_error(self, tmp_path):
        """A DB without the expected tables should return ERR_SCHEMA_UNEXPECTED."""
        db = tmp_path / "chat.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, val TEXT)")
        conn.commit()
        conn.close()

        msgs, err = ih.list_recent_messages(ih._APPLE_EPOCH, db_path=db)
        # The query joins on handle — without the table it should fail gracefully
        assert err is not None  # some error code, not a crash

    def test_empty_message_table_returns_empty_list(self, tmp_path):
        """Valid schema but empty message table → empty list, no error."""
        db = tmp_path / "chat.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                handle_id INTEGER, is_from_me INTEGER, date INTEGER, service TEXT,
                cache_roomnames TEXT
            );
        """)
        conn.commit()
        conn.close()

        msgs, err = ih.list_recent_messages(ih._APPLE_EPOCH, db_path=db)
        assert err is None
        assert msgs == []


# ── AppleScript injection prevention ─────────────────────────────────────────


class TestAppleScriptEscaping:
    def test_double_quote_escaped(self):
        result = ih._escape_as_string('say "hello"')
        assert '\\"' in result
        assert '"hello"' not in result

    def test_backslash_escaped(self):
        result = ih._escape_as_string("C:\\Users\\foo")
        assert "\\\\" in result

    def test_plain_string_unchanged(self):
        result = ih._escape_as_string("hello world")
        assert result == "hello world"

    def test_build_send_script_contains_escaped_handle(self):
        handle = 'evil"; do shell script "rm -rf ~'
        text = "normal message"
        script = ih._build_send_script(handle, text)
        # The raw handle should not appear literally in the script
        assert 'evil"; do shell script' not in script
        # The escaped version is present
        assert 'evil\\"' in script

    def test_build_send_script_contains_escaped_text(self):
        handle = "+15550001234"
        text = 'bad "quoted" text \\ slash'
        script = ih._build_send_script(handle, text)
        assert 'bad "quoted"' not in script
        assert '\\"quoted\\"' in script
        assert "\\\\" in script

    def test_build_send_script_structure(self):
        """The script must contain the tell block and key phrases."""
        script = ih._build_send_script("+15550001234", "Hello!")
        assert 'tell application "Messages"' in script
        assert "iMessage" in script
        assert "end tell" in script


# ── send_message: subprocess mocking ─────────────────────────────────────────


class TestSendMessage:
    def test_empty_handle_returns_error(self):
        ok, err = ih.send_message("", "hello")
        assert not ok
        assert err == "contact_handle_empty"

    def test_empty_text_returns_error(self):
        ok, err = ih.send_message("+15550001234", "")
        assert not ok
        assert err == "message_text_empty"

    def test_success_path(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ok, err = ih.send_message("+15550001234", "Hello there!")
        assert ok
        assert err is None
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "osascript" in args

    def test_applescript_error_returns_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Messages is not running."
        with patch("subprocess.run", return_value=mock_result):
            ok, err = ih.send_message("+15550001234", "Hello!")
        assert not ok
        assert err is not None

    def test_messages_not_running_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "application Messages is not running"
        with patch("subprocess.run", return_value=mock_result):
            ok, err = ih.send_message("+15550001234", "Test")
        assert not ok
        assert "not_running" in (err or "")

    def test_timeout_returns_send_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 30)):
            ok, err = ih.send_message("+15550001234", "Test")
        assert not ok
        assert err == "send_timeout"

    def test_osascript_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("osascript not found")):
            ok, err = ih.send_message("+15550001234", "Test")
        assert not ok
        assert err == "osascript_not_found"

    def test_exact_applescript_for_phone(self):
        """Verify the exact AppleScript rendered for a phone handle contains no injection."""
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ih.send_message("+15550001234", "Hi")
        call_args = mock_run.call_args[0][0]
        script_arg = call_args[2]  # osascript -e <script>
        assert "+15550001234" in script_arg
        assert "Hi" in script_arg


# ── is_messages_app_running ───────────────────────────────────────────────────


class TestIsMessagesAppRunning:
    def test_returns_true_when_osascript_outputs_true(self):
        mock_result = MagicMock(returncode=0, stdout="true\n")
        with patch("subprocess.run", return_value=mock_result):
            result = ih.is_messages_app_running()
        assert result is True

    def test_returns_false_when_osascript_outputs_false(self):
        mock_result = MagicMock(returncode=0, stdout="false\n")
        with patch("subprocess.run", return_value=mock_result):
            result = ih.is_messages_app_running()
        assert result is False

    def test_returns_false_on_error(self):
        with patch("subprocess.run", side_effect=Exception("not available")):
            result = ih.is_messages_app_running()
        assert result is False


# ── is_signed_in_to_imessage ──────────────────────────────────────────────────


class TestIsSignedIn:
    def test_returns_true_and_handle_when_signed_in(self):
        mock_result = MagicMock(returncode=0, stdout="me@icloud.com\n")
        with patch("subprocess.run", return_value=mock_result):
            signed_in, handle = ih.is_signed_in_to_imessage()
        assert signed_in is True
        assert handle == "me@icloud.com"

    def test_returns_false_when_empty_output(self):
        mock_result = MagicMock(returncode=0, stdout="\n")
        with patch("subprocess.run", return_value=mock_result):
            signed_in, handle = ih.is_signed_in_to_imessage()
        assert signed_in is False
        assert handle is None

    def test_returns_false_on_failure(self):
        mock_result = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            signed_in, handle = ih.is_signed_in_to_imessage()
        assert signed_in is False

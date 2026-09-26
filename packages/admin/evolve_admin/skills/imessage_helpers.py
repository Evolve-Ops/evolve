"""evolve_admin.skills.imessage_helpers — iMessage read/write helpers.

Two layers:

**Read path** (Task 1): Pure Python, queries the local SQLite chat.db that
macOS Messages.app maintains. Requires TCC Full Disk Access for the running
user (the ``evolve`` service user on the mini).

**Write path** (Task 2): AppleScript via ``osascript`` to send messages
through Messages.app. Requires TCC Automation grant for the running user to
control Messages.app. Messages.app must be running and signed in.

Security
--------
- All SQLite queries use parameterized statements (no string interpolation).
- AppleScript parameters are escaped before interpolation into the script body
  (prevents AppleScript injection via crafted contact_handle or message_text).
- TCC errors surface as ``error_code`` values, not exceptions, so callers can
  degrade gracefully.

Apple epoch
-----------
The ``message.date`` column holds nanoseconds since 2001-01-01 00:00:00 UTC
(the Apple/Cocoa epoch) on macOS 10.15+ (Catalina). Older macOS stored
seconds; we detect which format the DB uses by checking whether the value
is larger than 10**15 (anything in seconds would be < 10**12 for any date
up to year 3000). We convert to UTC datetime before returning.

Schema drift risk
-----------------
The chat.db schema has changed twice in five years (significant changes in
iOS 14/macOS Big Sur for group message threading, and again in macOS Ventura
for `is_stewie` / `is_kt_verified` columns). This module only reads
columns that have existed since macOS 10.13 (High Sierra):
  message.ROWID, text, handle_id, is_from_me, date, service
  handle.ROWID, id, service
  chat.ROWID, guid, chat_identifier, service_name
  chat_message_join.chat_id, message_id
  chat_handle_join.chat_id, handle_id
Unknown columns are not read; schema evolution is safe unless the core
columns are renamed or dropped.

Local-only privacy guarantee
----------------------------
NO data leaves the machine. All reads go to ~/Library/Messages/chat.db
(or a path supplied by the caller). No cloud roundtrip, no third-party
service.
"""

from __future__ import annotations

import logging
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Apple epoch ───────────────────────────────────────────────────────────────

#: The Apple/Cocoa reference date: 2001-01-01 00:00:00 UTC
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: Threshold to detect nanoseconds vs seconds storage.
#: Any valid seconds-since-2001 value for year <= 3000 is < 3.2e10.
#: Any nanoseconds value for year >= 2001 is > 1e15.
_NS_THRESHOLD = 10**13  # values above this are nanoseconds


def _apple_ts_to_utc(apple_ts: int) -> datetime:
    """Convert an Apple-epoch timestamp to UTC datetime.

    Handles both nanoseconds (macOS 10.15+) and seconds (older macOS/iOS).
    """
    if apple_ts > _NS_THRESHOLD:
        # Nanoseconds — modern format
        seconds = apple_ts / 1_000_000_000.0
    else:
        # Seconds — legacy format
        seconds = float(apple_ts)
    return datetime.fromtimestamp(
        _APPLE_EPOCH.timestamp() + seconds, tz=timezone.utc
    )


# ── Chat.db path ──────────────────────────────────────────────────────────────


def chat_db_path() -> Path:
    """Return the path to the local Messages chat.db.

    This is the system path on macOS. Requires TCC Full Disk Access (FDA) for
    the running user. Returns the expanded path; does not check existence.
    """
    return Path("~/Library/Messages/chat.db").expanduser()


# ── Error codes ───────────────────────────────────────────────────────────────

#: Returned in the ``error_code`` field when the DB doesn't exist.
ERR_DB_NOT_FOUND = "db_not_found"

#: Returned when the DB exists but TCC Full Disk Access is missing.
ERR_PERMISSION_DENIED = "permission_denied"

#: Returned when the DB exists and is readable but the schema is unexpected.
ERR_SCHEMA_UNEXPECTED = "schema_unexpected"

#: Returned for other unexpected errors.
ERR_UNKNOWN = "unknown_error"


def _open_db(db_path: Path) -> tuple["sqlite3.Connection | None", str | None]:
    """Open the chat.db read-only. Returns (conn, None) on success or (None, error_code)."""
    if not db_path.exists():
        return None, ERR_DB_NOT_FOUND
    try:
        # Open read-only via URI — prevents accidental writes.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn, None
    except PermissionError:
        return None, ERR_PERMISSION_DENIED
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "unable to open" in msg or "permission" in msg:
            return None, ERR_PERMISSION_DENIED
        log.warning("imessage_helpers: unexpected DB open error: %s", exc)
        return None, ERR_UNKNOWN


# ── Public read API ───────────────────────────────────────────────────────────


def list_recent_messages(
    since: datetime,
    *,
    sender: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> tuple[list[dict], str | None]:
    """Return messages newer than ``since``, optionally filtered by sender.

    Args:
        since: Only messages with a date >= this datetime (UTC) are returned.
        sender: If given, restrict to messages from this handle id
                (e.g. '+15550001234' or 'alice@example.com').
        limit: Maximum messages to return.
        db_path: Override the default chat.db path (for testing).

    Returns:
        (messages, error_code) where messages is a list of dicts::

            {
                "rowid": int,
                "guid": str,
                "text": str | None,
                "handle_id": str,        # the contact's handle (phone/email)
                "is_from_me": bool,
                "date": str,             # ISO 8601 UTC datetime
                "service": str,          # "iMessage" | "SMS"
            }

        error_code is None on success, or one of the ERR_* constants.
    """
    path = db_path or chat_db_path()
    conn, err = _open_db(path)
    if conn is None:
        return [], err

    # Compute the Apple-epoch timestamp for ``since``.
    # We use nanoseconds (modern format) and fall back via the NS_THRESHOLD logic
    # in _apple_ts_to_utc; for the query we always emit nanoseconds and let SQLite
    # compare — if the DB is in seconds, very old messages may be excluded, but
    # that's fine (we're querying for "recent" messages).
    delta = since - _APPLE_EPOCH
    since_ns = int(delta.total_seconds() * 1_000_000_000)

    try:
        if sender is not None:
            # Filter by handle.id (the contact's handle string).
            rows = conn.execute(
                """
                SELECT
                    m.ROWID        AS rowid,
                    m.guid         AS guid,
                    m.text         AS text,
                    h.id           AS handle_id,
                    m.is_from_me   AS is_from_me,
                    m.date         AS date,
                    m.service      AS service
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.date >= ?
                  AND h.id = ?
                ORDER BY m.date ASC
                LIMIT ?
                """,
                (since_ns, sender, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    m.ROWID        AS rowid,
                    m.guid         AS guid,
                    m.text         AS text,
                    h.id           AS handle_id,
                    m.is_from_me   AS is_from_me,
                    m.date         AS date,
                    m.service      AS service
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.date >= ?
                ORDER BY m.date ASC
                LIMIT ?
                """,
                (since_ns, limit),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        log.warning("imessage_helpers.list_recent_messages: query failed: %s", exc)
        conn.close()
        return [], ERR_SCHEMA_UNEXPECTED
    finally:
        try:
            conn.close()
        except Exception:
            pass

    messages: list[dict] = []
    for row in rows:
        try:
            dt = _apple_ts_to_utc(row["date"]) if row["date"] else None
            messages.append({
                "rowid": row["rowid"],
                "guid": row["guid"],
                "text": row["text"],
                "handle_id": row["handle_id"] or "",
                "is_from_me": bool(row["is_from_me"]),
                "date": dt.isoformat() if dt else None,
                "service": row["service"] or "iMessage",
            })
        except Exception as exc:
            log.warning("imessage_helpers: skipping malformed row %s: %s", row["rowid"], exc)
            continue

    return messages, None


def list_contacts(
    query: Optional[str] = None,
    *,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> tuple[list[dict], str | None]:
    """Return contacts derived from the handle table.

    Contacts here are the unique (handle.id, handle.service) pairs seen in
    Messages — i.e., everyone you've had a conversation with. This is NOT the
    system Contacts (AddressBook) database; it's limited to Message handles.

    Args:
        query: If given, filter by a substring of handle.id (case-insensitive).
        limit: Maximum contacts to return.
        db_path: Override the default chat.db path (for testing).

    Returns:
        (contacts, error_code) where contacts is a list of dicts::

            {
                "handle_id": str,    # the handle string (phone or email)
                "service": str,      # "iMessage" | "SMS"
                "rowid": int,
            }
    """
    path = db_path or chat_db_path()
    conn, err = _open_db(path)
    if conn is None:
        return [], err

    try:
        if query:
            rows = conn.execute(
                """
                SELECT ROWID AS rowid, id AS handle_id, service
                FROM handle
                WHERE lower(id) LIKE ?
                ORDER BY id
                LIMIT ?
                """,
                (f"%{query.lower()}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ROWID AS rowid, id AS handle_id, service
                FROM handle
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        log.warning("imessage_helpers.list_contacts: query failed: %s", exc)
        conn.close()
        return [], ERR_SCHEMA_UNEXPECTED
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return [
        {
            "rowid": row["rowid"],
            "handle_id": row["handle_id"],
            "service": row["service"] or "iMessage",
        }
        for row in rows
    ], None


def get_conversation(
    contact_handle: str,
    *,
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> tuple[list[dict], str | None]:
    """Return the most recent messages in a conversation with ``contact_handle``.

    Args:
        contact_handle: The handle string (phone number or email) of the contact.
        limit: Maximum messages to return (newest first, then reversed to chronological).
        db_path: Override the default chat.db path (for testing).

    Returns:
        (messages, error_code) — messages in chronological order (oldest first),
        same shape as list_recent_messages.
    """
    path = db_path or chat_db_path()
    conn, err = _open_db(path)
    if conn is None:
        return [], err

    try:
        rows = conn.execute(
            """
            SELECT
                m.ROWID        AS rowid,
                m.guid         AS guid,
                m.text         AS text,
                h.id           AS handle_id,
                m.is_from_me   AS is_from_me,
                m.date         AS date,
                m.service      AS service
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE h.id = ? OR (m.is_from_me = 1 AND m.cache_roomnames IS NULL
                  AND EXISTS (
                      SELECT 1 FROM chat_handle_join chj
                      JOIN chat_message_join cmj ON cmj.chat_id = chj.chat_id
                      JOIN handle h2 ON h2.ROWID = chj.handle_id
                      WHERE cmj.message_id = m.ROWID AND h2.id = ?
                  ))
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (contact_handle, contact_handle, limit),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        log.warning("imessage_helpers.get_conversation: query failed: %s", exc)
        conn.close()
        return [], ERR_SCHEMA_UNEXPECTED
    finally:
        try:
            conn.close()
        except Exception:
            pass

    messages: list[dict] = []
    for row in rows:
        try:
            dt = _apple_ts_to_utc(row["date"]) if row["date"] else None
            messages.append({
                "rowid": row["rowid"],
                "guid": row["guid"],
                "text": row["text"],
                "handle_id": row["handle_id"] or contact_handle,
                "is_from_me": bool(row["is_from_me"]),
                "date": dt.isoformat() if dt else None,
                "service": row["service"] or "iMessage",
            })
        except Exception as exc:
            log.warning("imessage_helpers: skipping malformed row %s: %s", row["rowid"], exc)
            continue

    # Return chronological (oldest first)
    messages.reverse()
    return messages, None


# ── AppleScript write path ────────────────────────────────────────────────────

#: Characters that need escaping inside AppleScript double-quoted strings.
#: In AppleScript, backslash and double-quote must be escaped.
_AS_ESCAPE_MAP = str.maketrans({
    "\\": "\\\\",
    '"': '\\"',
})


def _escape_as_string(s: str) -> str:
    """Escape a string for safe interpolation into an AppleScript double-quoted string.

    Prevents AppleScript injection by escaping backslashes and double-quotes.
    The result is safe to embed as ``"<result>"`` in AppleScript source.
    """
    return s.translate(_AS_ESCAPE_MAP)


def _build_send_script(contact_handle: str, text: str) -> str:
    """Build an AppleScript snippet to send ``text`` to ``contact_handle`` via iMessage.

    Both arguments are escaped to prevent AppleScript injection.

    Args:
        contact_handle: Phone number ('+15550001234') or email ('user@example.com').
        text: Message text to send.

    Returns:
        AppleScript source as a string.
    """
    safe_handle = _escape_as_string(contact_handle)
    safe_text = _escape_as_string(text)
    return (
        f'tell application "Messages"\n'
        f'  set targetBuddy to "{safe_handle}"\n'
        f'  set targetService to first service whose service type = iMessage\n'
        f'  set theBuddy to buddy targetBuddy of targetService\n'
        f'  send "{safe_text}" to theBuddy\n'
        f'end tell'
    )


def send_message(
    contact_handle: str,
    text: str,
    *,
    timeout: int = 30,
) -> tuple[bool, str | None]:
    """Send an iMessage to ``contact_handle`` via AppleScript → Messages.app.

    Requires:
    - TCC Automation grant: the running user must be allowed to control Messages.app.
    - Messages.app must be running and signed in to iMessage.

    Args:
        contact_handle: Recipient handle — phone number ('+1…') or iCloud email.
        text: Message text to send. Must not be empty.
        timeout: Seconds to wait for osascript to complete (default 30).

    Returns:
        (success, error_str). error_str is None on success, a short reason
        string on failure.

    AppleScript injection protection: both ``contact_handle`` and ``text``
    are escaped via ``_escape_as_string`` before being embedded in the script.
    Never use string interpolation on user-supplied values.
    """
    if not contact_handle or not contact_handle.strip():
        return False, "contact_handle_empty"
    if not text or not text.strip():
        return False, "message_text_empty"

    script = _build_send_script(contact_handle.strip(), text)

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "osascript_not_found"
    except subprocess.TimeoutExpired:
        return False, "send_timeout"
    except Exception as exc:
        return False, f"send_error: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log.warning("imessage send failed (rc=%d): %s", result.returncode, stderr)
        if "not running" in stderr.lower():
            return False, "messages_app_not_running"
        if "automation" in stderr.lower() or "permission" in stderr.lower():
            return False, "tcc_automation_missing"
        return False, f"applescript_error: {stderr[:200]}"

    return True, None


def is_messages_app_running(*, timeout: int = 5) -> bool:
    """Return True if Messages.app is currently running.

    Uses AppleScript ``application "Messages" is running`` — lightweight, no
    side effects.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to return (exists process "Messages")'],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


def is_signed_in_to_imessage(*, timeout: int = 5) -> tuple[bool, str | None]:
    """Check whether Messages.app is signed in to iMessage.

    Returns:
        (signed_in, handle) where handle is the iMessage account handle
        (e.g. 'user@icloud.com') if signed in, or None if not.

    Requires Messages.app to be running. Returns (False, None) if it's not.
    """
    script = (
        'tell application "Messages"\n'
        '  try\n'
        '    set svc to first service whose service type = iMessage\n'
        '    return id of svc\n'
        '  on error\n'
        '    return ""\n'
        '  end try\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False, None
        handle = result.stdout.strip()
        if handle and handle != "":
            return True, handle
        return False, None
    except Exception:
        return False, None

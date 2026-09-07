"""Unit tests for mcp_bridge.google_tools.

Mocks the Google API services and the credential loader so tests run
without the SDK installed (where possible) and without real GCP access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from evolve_admin.mcp_bridge import google_tools


# ── Test fixtures ─────────────────────────────────────────────────────────────

def _network(bot_id: str = "lex", with_persona: bool = True) -> dict:
    net = {
        "bots": {
            bot_id: {
                "role": "member",
                "primary_user": {"name": "Sam Riley"},
                "google_integration": {
                    "mode": "service_account_dwd",
                    "workspace_domain": "example-corp.com",
                    "subject": f"{bot_id}@example-corp.com",
                    "service_account_secret_ref": "google-sa-example-corp",
                    # Default personal-assistant-bot scope set per
                    # spec-google-integration-paths-2026-05-30 §6.
                    # The MCP tools (Phase A.3+) check this set with
                    # assert_scopes_available before calling Google.
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.send",
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/calendar",
                        "https://www.googleapis.com/auth/drive.file",
                    ],
                },
            }
        }
    }
    if with_persona:
        net["bots"][bot_id]["correspondence"] = {
            "name": "Jane",
            "email_address": "jane@example-corp.com",
            "disclosure": "soft",
        }
    return net


def _registry(bot_id: str = "lex"):
    """Minimal registry mock — only the methods _resolve_bot uses."""
    reg = MagicMock()
    info = MagicMock()
    info.bot_id = bot_id
    reg.get = MagicMock(side_effect=lambda b: info if b == bot_id else None)
    reg.primary_id = bot_id
    return reg


def _run(coro):
    # A fresh loop per call — robust to cross-file ordering where a prior
    # test closed the process's event loop (asyncio.get_event_loop() raises
    # "no current event loop" on 3.10+ once that happens).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── _build_from_header / _build_signature unit tests ──────────────────────────

def test_build_from_header_uses_persona_name_and_alias():
    net = _network("lex")
    header = google_tools._build_from_header(
        net["bots"]["lex"], default_sender="lex@example-corp.com", bot_id="lex"
    )
    assert "Jane" in header
    assert "jane@example-corp.com" in header


def test_build_from_header_falls_back_to_default_sender_when_no_email():
    net = _network("lex")
    del net["bots"]["lex"]["correspondence"]["email_address"]
    header = google_tools._build_from_header(
        net["bots"]["lex"], default_sender="lex@example-corp.com", bot_id="lex"
    )
    assert "lex@example-corp.com" in header


def test_build_from_header_falls_back_to_display_name_when_no_alias():
    """No correspondence block → use bot's display_name in From header."""
    net = _network("lex", with_persona=False)
    net["bots"]["lex"]["display_name"] = "Lex"
    header = google_tools._build_from_header(
        net["bots"]["lex"], default_sender="lex@example-corp.com", bot_id="lex"
    )
    assert "Lex" in header
    assert "lex@example-corp.com" in header


def test_build_from_header_falls_back_to_bot_id_when_no_display_name():
    """No correspondence AND no display_name → use bare bot_id."""
    net = _network("lex", with_persona=False)
    # explicitly ensure no display_name on the bot
    net["bots"]["lex"].pop("display_name", None)
    header = google_tools._build_from_header(
        net["bots"]["lex"], default_sender="lex@example-corp.com", bot_id="lex"
    )
    assert "lex" in header
    assert "lex@example-corp.com" in header


def test_build_from_header_correspondence_overrides_display_name():
    """Explicit alias wins over display_name (operator chose the override)."""
    net = _network("lex")
    net["bots"]["lex"]["display_name"] = "Lex"  # would be the fallback
    header = google_tools._build_from_header(
        net["bots"]["lex"], default_sender="lex@example-corp.com", bot_id="lex"
    )
    assert "Jane" in header  # alias wins
    assert "Lex" not in header


def test_build_signature_soft_default():
    net = _network("lex")
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "Jane\nAssistant to Sam Riley"


def test_build_signature_explicit_marks_AI():
    net = _network("lex")
    net["bots"]["lex"]["correspondence"]["disclosure"] = "explicit"
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "Jane\nAI assistant to Sam Riley"


def test_build_signature_none_is_bare_name():
    net = _network("lex")
    net["bots"]["lex"]["correspondence"]["disclosure"] = "none"
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "Jane"


def test_build_signature_custom_overrides_default():
    net = _network("lex")
    net["bots"]["lex"]["correspondence"]["signature"] = "Jane\nTravel Concierge"
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "Jane\nTravel Concierge"


def test_build_signature_no_correspondence_is_bare_name():
    """No correspondence block → bare name (no role line) using display_name."""
    net = _network("lex", with_persona=False)
    net["bots"]["lex"]["display_name"] = "Lex"
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "Lex"  # no "Assistant to Sam Riley" without an explicit disclosure


def test_build_signature_no_correspondence_falls_back_to_bot_id():
    """No correspondence AND no display_name → bare bot_id."""
    net = _network("lex", with_persona=False)
    net["bots"]["lex"].pop("display_name", None)
    sig = google_tools._build_signature(net["bots"]["lex"], "Sam Riley", "lex")
    assert sig == "lex"


def test_build_signature_soft_without_primary_user_drops_to_suffix():
    """If primary_user_name is unknown/empty, soft disclosure drops 'to <user>'."""
    net = _network("lex")
    sig = google_tools._build_signature(net["bots"]["lex"], "(unknown)", "lex")
    assert sig == "Jane\nAssistant"  # no 'to <user>' suffix
    sig2 = google_tools._build_signature(net["bots"]["lex"], "", "lex")
    assert sig2 == "Jane\nAssistant"


def test_build_signature_explicit_without_primary_user_drops_to_suffix():
    net = _network("lex")
    net["bots"]["lex"]["correspondence"]["disclosure"] = "explicit"
    sig = google_tools._build_signature(net["bots"]["lex"], "(unknown)", "lex")
    assert sig == "Jane\nAI assistant"  # no 'to <user>' suffix


def test_resolve_sender_name_priority():
    """Direct unit test of the resolution-order helper."""
    # 1. correspondence.name wins
    cfg = {"correspondence": {"name": "Jane"}, "display_name": "Lex"}
    assert google_tools._resolve_sender_name(cfg, "lex") == "Jane"
    # 2. display_name when no correspondence
    cfg = {"display_name": "Lex"}
    assert google_tools._resolve_sender_name(cfg, "lex") == "Lex"
    # 3. bot_id as last resort
    cfg = {}
    assert google_tools._resolve_sender_name(cfg, "lex") == "lex"
    # Empty string in correspondence.name → falls through to display_name
    cfg = {"correspondence": {"name": "   "}, "display_name": "Lex"}
    assert google_tools._resolve_sender_name(cfg, "lex") == "Lex"


# ── gmail_send tool ───────────────────────────────────────────────────────────

def test_gmail_send_requires_to():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="to is required"):
            _run(google_tools.tool_gmail_send({}, reg))


def test_gmail_send_requires_subject():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="subject is required"):
            _run(google_tools.tool_gmail_send({"to": "x@y.com"}, reg))


def test_gmail_send_requires_body():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="body is required"):
            _run(google_tools.tool_gmail_send(
                {"to": "x@y.com", "subject": "Hi"}, reg
            ))


def test_gmail_send_succeeds_with_no_persona_falls_back_to_bot_name():
    """Without a correspondence block, gmail_send still works — From header
    uses the bot's display_name (or bot_id fallback). The alias block is
    OPTIONAL since the 2026-06-03 fix; previously this raised ValueError."""
    reg = _registry()
    net = _network("lex", with_persona=False)
    net["bots"]["lex"]["display_name"] = "Lex"

    fake_send = MagicMock(return_value=MagicMock(execute=MagicMock(
        return_value={"id": "no-persona-123", "threadId": "t-np"}
    )))
    fake_messages = MagicMock(send=MagicMock(return_value=fake_send.return_value))
    fake_users = MagicMock(messages=MagicMock(return_value=fake_messages))
    fake_service = MagicMock(users=MagicMock(return_value=fake_users))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=net), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.assert_scopes_available",
               return_value=None), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_send(
            {"to": "x@y.com", "subject": "Hi", "body": "Hello"}, reg
        ))

    assert result["ok"] is True
    # From header should carry the bot's display_name, not raise
    assert "Lex" in result["from"]


def test_gmail_send_happy_path_with_persona():
    """Mocks load_credentials and the gmail service. Verifies the From
    header carries the persona, and the call result is shaped correctly."""
    reg = _registry()

    fake_send = MagicMock(return_value=MagicMock(execute=MagicMock(
        return_value={"id": "abc123", "threadId": "thread-xyz"}
    )))
    fake_messages = MagicMock(send=MagicMock(return_value=fake_send.return_value))
    fake_users = MagicMock(messages=MagicMock(return_value=fake_messages))
    fake_service = MagicMock(users=MagicMock(return_value=fake_users))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_send(
            {"to": "hotel@example.com", "subject": "Inquiry", "body": "Hello"},
            reg,
        ))

    assert result["ok"] is True
    assert result["id"] == "abc123"
    assert result["from"].startswith("Jane")
    assert "jane@example-corp.com" in result["from"]
    assert result["to"] == ["hotel@example.com"]
    assert result["subject"] == "Inquiry"


# ── drive_write_file tool ─────────────────────────────────────────────────────

def test_drive_write_file_requires_name():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="name is required"):
            _run(google_tools.tool_drive_write_file(
                {"content": "hi"}, reg
            ))


def test_drive_write_file_requires_content():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="content is required"):
            _run(google_tools.tool_drive_write_file(
                {"name": "notes.txt"}, reg
            ))


# ── calendar_create_event tool ────────────────────────────────────────────────

def test_calendar_create_event_requires_summary():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="summary is required"):
            _run(google_tools.tool_calendar_create_event(
                {"start": "2026-06-15T18:00:00-07:00", "end": "2026-06-15T19:00:00-07:00"},
                reg,
            ))


def test_calendar_create_event_requires_start():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="start is required"):
            _run(google_tools.tool_calendar_create_event(
                {"summary": "Hotel check-in", "end": "2026-06-15T19:00:00-07:00"},
                reg,
            ))


def test_calendar_create_event_requires_end():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="end is required"):
            _run(google_tools.tool_calendar_create_event(
                {"summary": "Hotel check-in", "start": "2026-06-15T18:00:00-07:00"},
                reg,
            ))


def test_calendar_create_event_happy_path():
    reg = _registry()

    fake_insert = MagicMock(return_value=MagicMock(execute=MagicMock(
        return_value={"id": "evt-1", "summary": "Hotel check-in", "htmlLink": "https://x/y"}
    )))
    fake_events = MagicMock(insert=MagicMock(return_value=fake_insert.return_value))
    fake_service = MagicMock(events=MagicMock(return_value=fake_events))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_calendar_create_event(
            {
                "summary": "Hotel check-in",
                "start": "2026-06-15T18:00:00-07:00",
                "end": "2026-06-15T19:00:00-07:00",
                "location": "Sonoma",
                "attendees": ["sam@example-corp.com"],
            },
            reg,
        ))

    assert result["ok"] is True
    assert result["id"] == "evt-1"
    assert result["summary"] == "Hotel check-in"
    assert result["calendar_id"] == "primary"


# ── _decode_gmail_body helper unit tests ──────────────────────────────────────

def test_decode_gmail_body_single_part_text():
    import base64
    raw = "Hello there"
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()
    payload = {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }
    assert google_tools._decode_gmail_body(payload) == raw


def test_decode_gmail_body_multipart_prefers_plain():
    import base64
    plain = base64.urlsafe_b64encode(b"plain text version").decode()
    html = base64.urlsafe_b64encode(b"<p>html version</p>").decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": plain}},
            {"mimeType": "text/html", "body": {"data": html}},
        ],
    }
    assert google_tools._decode_gmail_body(payload) == "plain text version"


def test_decode_gmail_body_falls_back_to_html():
    import base64
    html = base64.urlsafe_b64encode(b"<p>html only</p>").decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": html}},
        ],
    }
    assert google_tools._decode_gmail_body(payload) == "<p>html only</p>"


def test_decode_gmail_body_empty_when_no_text_parts():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "body": {"attachmentId": "att1"}},
        ],
    }
    assert google_tools._decode_gmail_body(payload) == ""


# ── gmail_list_messages tool ──────────────────────────────────────────────────

def test_gmail_list_messages_happy_path():
    """Mocks list() + get(format='metadata') for two messages."""
    reg = _registry()

    fake_list_exec = MagicMock(return_value={
        "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
        "nextPageToken": "tok",
    })
    fake_list = MagicMock(execute=fake_list_exec)

    def fake_get(userId, id, format, metadataHeaders):
        return MagicMock(execute=MagicMock(return_value={
            "id": id,
            "threadId": f"t-{id}",
            "snippet": f"snippet-{id}",
            "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": f"sender-{id}@example.com"},
                {"name": "Subject", "value": f"subject-{id}"},
                {"name": "Date", "value": "Mon, 1 Jun 2026 09:00:00 -0700"},
                {"name": "To", "value": "lex@example-corp.com"},
            ]},
        }))

    fake_messages = MagicMock()
    fake_messages.list = MagicMock(return_value=fake_list)
    fake_messages.get = MagicMock(side_effect=fake_get)
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_list_messages(
            {"q": "from:alice", "max_results": 2}, reg
        ))

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["next_page_token"] == "tok"
    assert [m["id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][0]["from"] == "sender-m1@example.com"
    assert result["messages"][0]["subject"] == "subject-m1"
    # q was forwarded
    fake_messages.list.assert_called_once()
    assert fake_messages.list.call_args.kwargs["q"] == "from:alice"
    assert fake_messages.list.call_args.kwargs["maxResults"] == 2


def test_gmail_list_messages_clamps_max_results():
    reg = _registry()
    fake_messages = MagicMock()
    fake_messages.list = MagicMock(return_value=MagicMock(
        execute=MagicMock(return_value={"messages": []})
    ))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        _run(google_tools.tool_gmail_list_messages({"max_results": 99999}, reg))

    assert fake_messages.list.call_args.kwargs["maxResults"] == 500


# ── gmail_get_message tool ────────────────────────────────────────────────────

def test_gmail_get_message_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_get_message({}, reg))


def test_gmail_get_message_decodes_body():
    import base64
    reg = _registry()
    body_text = "Hello, world!\nThis is a test."
    fake_get_exec = MagicMock(return_value={
        "id": "m1",
        "threadId": "t1",
        "snippet": "Hello, world!",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "To", "value": "lex@example-corp.com"},
                {"name": "Subject", "value": "Test"},
                {"name": "Date", "value": "Mon, 1 Jun 2026 09:00:00 -0700"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body_text.encode()).decode()},
        },
    })
    fake_messages = MagicMock()
    fake_messages.get = MagicMock(return_value=MagicMock(execute=fake_get_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_get_message({"id": "m1"}, reg))

    assert result["ok"] is True
    assert result["id"] == "m1"
    assert result["body"] == body_text
    assert result["from"] == "alice@example.com"
    assert result["subject"] == "Test"


# ── calendar_list_events tool ─────────────────────────────────────────────────

def test_calendar_list_events_happy_path():
    reg = _registry()
    fake_events_list = MagicMock(execute=MagicMock(return_value={
        "items": [
            {
                "id": "evt-1",
                "summary": "Hotel check-in",
                "location": "Sonoma",
                "start": {"dateTime": "2026-06-15T18:00:00-07:00"},
                "end": {"dateTime": "2026-06-15T19:00:00-07:00"},
                "attendees": [{"email": "sam@example-corp.com"}],
                "htmlLink": "https://example/evt-1",
                "status": "confirmed",
            },
            {  # all-day event uses 'date' not 'dateTime'
                "id": "evt-2",
                "summary": "Holiday",
                "start": {"date": "2026-07-04"},
                "end": {"date": "2026-07-05"},
                "status": "confirmed",
            },
        ],
    }))
    fake_events = MagicMock(list=MagicMock(return_value=fake_events_list))
    fake_service = MagicMock(events=MagicMock(return_value=fake_events))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_calendar_list_events(
            {"max_results": 10, "q": "hotel"}, reg
        ))

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["events"][0]["start"] == "2026-06-15T18:00:00-07:00"
    assert result["events"][0]["attendees"] == ["sam@example-corp.com"]
    assert result["events"][1]["start"] == "2026-07-04"  # all-day fallback
    # default time_min was set to "now" (ISO string starting with year)
    call_kwargs = fake_events.list.call_args.kwargs
    assert call_kwargs["timeMin"].startswith("20")
    assert call_kwargs["q"] == "hotel"
    assert call_kwargs["singleEvents"] is True


# ── drive_list_files tool ─────────────────────────────────────────────────────

def test_drive_list_files_happy_path():
    reg = _registry()
    fake_files_list = MagicMock(execute=MagicMock(return_value={
        "files": [
            {
                "id": "f1",
                "name": "notes.txt",
                "mimeType": "text/plain",
                "modifiedTime": "2026-06-01T09:00:00.000Z",
                "webViewLink": "https://drive/f1",
                "owners": [{"emailAddress": "lex@example-corp.com", "displayName": "Lex"}],
                "size": "1024",
                "parents": ["root"],
            },
        ],
        "nextPageToken": "tok",
    }))
    fake_files = MagicMock(list=MagicMock(return_value=fake_files_list))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_list_files(
            {"q": "mimeType='text/plain'", "page_size": 10}, reg
        ))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["files"][0]["id"] == "f1"
    assert result["files"][0]["name"] == "notes.txt"
    assert result["files"][0]["size_bytes"] == 1024
    assert result["files"][0]["owners"][0]["email"] == "lex@example-corp.com"
    assert result["next_page_token"] == "tok"
    call_kwargs = fake_files.list.call_args.kwargs
    assert call_kwargs["q"] == "mimeType='text/plain'"
    assert call_kwargs["pageSize"] == 10


# ── drive_read_file tool ──────────────────────────────────────────────────────

def test_drive_read_file_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_drive_read_file({}, reg))


def test_drive_read_file_decodes_text_mime():
    reg = _registry()
    file_content = b"line one\nline two\n"
    fake_files = MagicMock()
    fake_files.get = MagicMock(return_value=MagicMock(execute=MagicMock(return_value={
        "id": "f1",
        "name": "notes.txt",
        "mimeType": "text/plain",
        "size": str(len(file_content)),
        "modifiedTime": "2026-06-01T09:00:00.000Z",
        "webViewLink": "https://drive/f1",
    })))
    fake_files.get_media = MagicMock(return_value=MagicMock(
        execute=MagicMock(return_value=file_content)
    ))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_read_file({"id": "f1"}, reg))

    assert result["ok"] is True
    assert result["content"] == "line one\nline two\n"
    assert result["encoding"] == "utf-8"
    assert result["truncated"] is False


def test_drive_read_file_base64s_binary_mime():
    import base64 as b64
    reg = _registry()
    file_content = bytes([0xFF, 0xD8, 0xFF, 0xE0])  # JPEG magic
    fake_files = MagicMock()
    fake_files.get = MagicMock(return_value=MagicMock(execute=MagicMock(return_value={
        "id": "f2",
        "name": "photo.jpg",
        "mimeType": "image/jpeg",
        "size": str(len(file_content)),
        "modifiedTime": "2026-06-01T09:00:00.000Z",
        "webViewLink": "https://drive/f2",
    })))
    fake_files.get_media = MagicMock(return_value=MagicMock(
        execute=MagicMock(return_value=file_content)
    ))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_read_file({"id": "f2"}, reg))

    assert result["ok"] is True
    assert result["encoding"] == "base64"
    assert b64.b64decode(result["content"]) == file_content


def test_drive_read_file_rejects_google_native():
    """Google Docs/Sheets/Slides can't be downloaded; return structured error."""
    reg = _registry()
    fake_files = MagicMock()
    fake_files.get = MagicMock(return_value=MagicMock(execute=MagicMock(return_value={
        "id": "doc1",
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "webViewLink": "https://docs.google.com/document/d/doc1",
    })))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_read_file({"id": "doc1"}, reg))

    assert result["ok"] is False
    assert "Google-native" in result["error"]
    assert "documents/spreadsheets/presentations" in result["error"]
    # get_media should NOT have been called for native types
    fake_files.get_media.assert_not_called()


def test_drive_read_file_truncates_oversized():
    """Files larger than max_bytes return metadata + truncated=True."""
    reg = _registry()
    fake_files = MagicMock()
    fake_files.get = MagicMock(return_value=MagicMock(execute=MagicMock(return_value={
        "id": "big1",
        "name": "huge.bin",
        "mimeType": "application/octet-stream",
        "size": str(20 * 1024 * 1024),  # 20 MiB
        "modifiedTime": "2026-06-01T09:00:00.000Z",
        "webViewLink": "https://drive/big1",
    })))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_read_file(
            {"id": "big1", "max_bytes": 1024}, reg
        ))

    assert result["ok"] is True
    assert result["content"] is None
    assert result["truncated"] is True
    assert "20971520" in result["note"]
    fake_files.get_media.assert_not_called()


# ── MCP server registration ───────────────────────────────────────────────────

def test_mcp_server_advertises_all_google_tools():
    """The list_tools endpoint must include every Google tool, otherwise
    bots can't discover them via MCP introspection (previously the 3 write
    tools were registered as handlers but missing from the Tool list)."""
    # mcp SDK is a runtime dep but may be absent in minimal test envs.
    pytest.importorskip("mcp")
    from evolve_admin.mcp_bridge.server import _TOOLS
    tool_names = {t.name for t in _TOOLS}
    expected = {
        # write
        "gmail_send", "drive_write_file", "calendar_create_event",
        # read/list
        "gmail_list_messages", "gmail_get_message",
        "calendar_list_events", "drive_list_files", "drive_read_file",
        # modify + optional-scope (new in this PR)
        "gmail_list_labels", "gmail_label_message", "gmail_archive_message",
        "gmail_delete_message", "gmail_trash_message",
        "gmail_mark_read", "gmail_mark_unread",
        "drive_search",
    }
    missing = expected - tool_names
    assert not missing, f"MCP server doesn't advertise: {sorted(missing)}"


def test_tool_handlers_marks_reads_as_non_write():
    """Read tools must have is_write_op=False so they don't bloat the
    write-audit log."""
    from evolve_admin.mcp_bridge.tools import TOOL_HANDLERS
    read_tools = [
        "gmail_list_messages", "gmail_get_message", "gmail_list_labels",
        "calendar_list_events", "drive_list_files", "drive_read_file",
        "drive_search",
    ]
    for name in read_tools:
        handler_entry = TOOL_HANDLERS.get(name)
        assert handler_entry is not None, f"{name} not in TOOL_HANDLERS"
        _, is_write = handler_entry
        assert is_write is False, f"{name} should not be marked is_write_op"


def test_tool_handlers_marks_writes_as_write():
    """Write tools must keep is_write_op=True (regression guard)."""
    from evolve_admin.mcp_bridge.tools import TOOL_HANDLERS
    write_tools = [
        "gmail_send", "drive_write_file", "calendar_create_event",
        # gmail.modify tools mutate mailbox state → audit-logged
        "gmail_label_message", "gmail_archive_message",
        "gmail_delete_message", "gmail_trash_message",
        "gmail_mark_read", "gmail_mark_unread",
    ]
    for name in write_tools:
        _, is_write = TOOL_HANDLERS[name]
        assert is_write is True, f"{name} must be marked is_write_op"


# ── gmail_list_labels tool ────────────────────────────────────────────────────

def test_gmail_list_labels_happy_path():
    reg = _registry()
    fake_labels = MagicMock()
    fake_labels.list = MagicMock(return_value=MagicMock(execute=MagicMock(return_value={
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "Label_123", "name": "Travel", "type": "user"},
        ],
    })))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(labels=MagicMock(return_value=fake_labels)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_list_labels({}, reg))

    assert result["ok"] is True
    assert result["count"] == 3
    assert {lbl["id"] for lbl in result["labels"]} == {"INBOX", "UNREAD", "Label_123"}
    user_label = [lbl for lbl in result["labels"] if lbl["id"] == "Label_123"][0]
    assert user_label["type"] == "user"
    assert user_label["name"] == "Travel"


# ── gmail_label_message tool ──────────────────────────────────────────────────

def test_gmail_label_message_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_label_message({"add_label_ids": ["X"]}, reg))


def test_gmail_label_message_requires_at_least_one_change():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="at least one"):
            _run(google_tools.tool_gmail_label_message({"id": "m1"}, reg))


def test_gmail_label_message_happy_path():
    reg = _registry()
    fake_modify_exec = MagicMock(return_value={
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "STARRED"],
    })
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(execute=fake_modify_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_label_message(
            {"id": "m1", "add_label_ids": "STARRED", "remove_label_ids": ["UNREAD"]},
            reg,
        ))

    assert result["ok"] is True
    assert result["id"] == "m1"
    assert result["label_ids"] == ["INBOX", "STARRED"]
    assert result["added"] == ["STARRED"]
    assert result["removed"] == ["UNREAD"]
    # API was called with both add and remove
    call_kwargs = fake_messages.modify.call_args.kwargs
    assert call_kwargs["body"] == {
        "addLabelIds": ["STARRED"],
        "removeLabelIds": ["UNREAD"],
    }


def test_gmail_label_message_refuses_trash_label():
    """Applying the TRASH label is a delete (autonomy ladder §1.4). The
    labeling tool must refuse it and route to gmail_trash_message — and must
    NOT call the Gmail API (no partial trash). Case-insensitive, and matches
    whether TRASH is the sole add or buried among benign labels."""
    reg = _registry()
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock()
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        for add in (["TRASH"], "TRASH", ["STARRED", "TRASH"], [" trash "]):
            with pytest.raises(ValueError, match="gmail_trash_message"):
                _run(google_tools.tool_gmail_label_message(
                    {"id": "m1", "add_label_ids": add}, reg
                ))
    # The guard short-circuits before any API call.
    fake_messages.modify.assert_not_called()


def test_gmail_label_message_allows_removing_trash():
    """Removing the TRASH label (untrash / restore) is benign and must work —
    only ADDING TRASH is a delete."""
    reg = _registry()
    fake_modify_exec = MagicMock(return_value={"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]})
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(execute=fake_modify_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_label_message(
            {"id": "m1", "remove_label_ids": ["TRASH"]}, reg
        ))
    assert result["ok"] is True
    assert fake_messages.modify.call_args.kwargs["body"] == {"removeLabelIds": ["TRASH"]}


# ── gmail_trash_message tool ──────────────────────────────────────────────────

def test_gmail_trash_message_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_trash_message({"confirm": True}, reg))


def test_gmail_trash_message_requires_confirm_true():
    """Trash is recoverable but carries the same confirm guard as delete, so
    it stays a deliberate act before a bot's posture render lands."""
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        for args in ({"id": "m1"}, {"id": "m1", "confirm": False}, {"id": "m1", "confirm": "true"}):
            with pytest.raises(ValueError, match="confirm=true is required"):
                _run(google_tools.tool_gmail_trash_message(args, reg))


def test_gmail_trash_message_happy_path():
    reg = _registry()
    fake_messages = MagicMock()
    fake_messages.trash = MagicMock(return_value=MagicMock(
        execute=MagicMock(return_value={"id": "m1", "threadId": "t1", "labelIds": ["TRASH"]})
    ))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_trash_message(
            {"id": "m1", "confirm": True}, reg
        ))

    assert result["ok"] is True
    assert result["trashed"] is True
    assert result["id"] == "m1"
    # messages.trash (recoverable), NEVER messages.delete (permanent).
    fake_messages.trash.assert_called_once()
    fake_messages.delete.assert_not_called()


# ── gmail_archive_message tool ────────────────────────────────────────────────

def test_gmail_archive_message_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_archive_message({}, reg))


def test_gmail_archive_message_removes_inbox_label():
    reg = _registry()
    fake_modify_exec = MagicMock(return_value={
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["UNREAD"],  # INBOX gone
    })
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(execute=fake_modify_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_archive_message({"id": "m1"}, reg))

    assert result["ok"] is True
    assert result["archived"] is True
    assert "INBOX" not in result["label_ids"]
    call_kwargs = fake_messages.modify.call_args.kwargs
    assert call_kwargs["body"] == {"removeLabelIds": ["INBOX"]}


# ── gmail_delete_message tool ─────────────────────────────────────────────────

def test_gmail_delete_message_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_delete_message({"confirm": True}, reg))


def test_gmail_delete_message_requires_confirm_true():
    """Guard rail: messages.delete is unrecoverable, so confirm=true is
    mandatory. Missing confirm OR confirm=false both reject."""
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="confirm=true is required"):
            _run(google_tools.tool_gmail_delete_message({"id": "m1"}, reg))
        with pytest.raises(ValueError, match="confirm=true is required"):
            _run(google_tools.tool_gmail_delete_message(
                {"id": "m1", "confirm": False}, reg
            ))
        # Truthy non-bool (e.g. "true") must also reject — guard the
        # operator/bot against a string-vs-bool typo.
        with pytest.raises(ValueError, match="confirm=true is required"):
            _run(google_tools.tool_gmail_delete_message(
                {"id": "m1", "confirm": "true"}, reg
            ))


def test_gmail_delete_message_happy_path():
    reg = _registry()
    fake_messages = MagicMock()
    fake_messages.delete = MagicMock(return_value=MagicMock(execute=MagicMock(return_value="")))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_delete_message(
            {"id": "m1", "confirm": True}, reg
        ))

    assert result["ok"] is True
    assert result["deleted"] is True
    assert result["id"] == "m1"
    fake_messages.delete.assert_called_once()


# ── gmail_mark_read / gmail_mark_unread tools ─────────────────────────────────

def test_gmail_mark_read_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_mark_read({}, reg))


def test_gmail_mark_unread_requires_id():
    reg = _registry()
    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network", return_value=_network()):
        with pytest.raises(ValueError, match="id is required"):
            _run(google_tools.tool_gmail_mark_unread({}, reg))


def test_gmail_mark_read_removes_unread_label():
    reg = _registry()
    fake_modify_exec = MagicMock(return_value={
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
    })
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(execute=fake_modify_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_mark_read({"id": "m1"}, reg))

    assert result["ok"] is True
    assert result["unread"] is False
    call_kwargs = fake_messages.modify.call_args.kwargs
    assert call_kwargs["body"] == {"removeLabelIds": ["UNREAD"]}


def test_gmail_mark_unread_adds_unread_label():
    reg = _registry()
    fake_modify_exec = MagicMock(return_value={
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
    })
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(execute=fake_modify_exec))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_gmail_mark_unread({"id": "m1"}, reg))

    assert result["ok"] is True
    assert result["unread"] is True
    call_kwargs = fake_messages.modify.call_args.kwargs
    assert call_kwargs["body"] == {"addLabelIds": ["UNREAD"]}


# ── drive_search tool ─────────────────────────────────────────────────────────

def test_drive_search_loads_readonly_scope():
    """drive_search MUST load creds with drive.readonly (not drive.file)
    — that's the whole point of the tool vs. drive_list_files. Regression
    guard: if a future refactor accidentally swaps scopes, this test
    catches it before the bot starts silently seeing only its own slice."""
    reg = _registry()
    fake_files_list = MagicMock(execute=MagicMock(return_value={"files": []}))
    fake_files = MagicMock(list=MagicMock(return_value=fake_files_list))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()) as load_creds, \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        _run(google_tools.tool_drive_search({"q": "name contains 'invoice'"}, reg))

    scopes = load_creds.call_args.kwargs["scopes"]
    assert scopes == ["https://www.googleapis.com/auth/drive.readonly"], (
        f"drive_search must load drive.readonly, got {scopes}"
    )


def test_drive_search_happy_path():
    reg = _registry()
    fake_files_list = MagicMock(execute=MagicMock(return_value={
        "files": [
            {
                "id": "f1",
                "name": "Invoice-2026-05.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-06-01T09:00:00.000Z",
                "webViewLink": "https://drive/f1",
                "owners": [{"emailAddress": "boss@example-corp.com", "displayName": "Boss"}],
                "size": "204800",
                "parents": ["folder1"],
            },
        ],
        "nextPageToken": "tok",
    }))
    fake_files = MagicMock(list=MagicMock(return_value=fake_files_list))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = _run(google_tools.tool_drive_search(
            {"q": "name contains 'invoice'", "page_size": 10,
             "order_by": "modifiedTime desc"},
            reg,
        ))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["files"][0]["id"] == "f1"
    assert result["files"][0]["owners"][0]["email"] == "boss@example-corp.com"
    assert result["next_page_token"] == "tok"
    call_kwargs = fake_files.list.call_args.kwargs
    assert call_kwargs["q"] == "name contains 'invoice'"
    assert call_kwargs["pageSize"] == 10
    assert call_kwargs["orderBy"] == "modifiedTime desc"


def test_drive_search_clamps_page_size():
    reg = _registry()
    fake_files_list = MagicMock(execute=MagicMock(return_value={"files": []}))
    fake_files = MagicMock(list=MagicMock(return_value=fake_files_list))
    fake_service = MagicMock(files=MagicMock(return_value=fake_files))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        _run(google_tools.tool_drive_search({"page_size": 99999}, reg))

    assert fake_files.list.call_args.kwargs["pageSize"] == 1000


# ── Scope-loading regression guards for modify tools ──────────────────────────

def _capture_modify_scopes(tool_callable, arguments: dict) -> list[str]:
    """Run a modify tool with mocked Gmail API; return the scopes passed
    to load_credentials. Used to lock in the gmail.modify scope for each
    mutation tool — protects against an accidental swap to gmail.readonly
    which would let label/archive/delete calls fail at the API boundary
    with a 403 instead of failing early at creds-load time."""
    reg = _registry()
    fake_messages = MagicMock()
    fake_messages.modify = MagicMock(return_value=MagicMock(
        execute=MagicMock(return_value={"id": "m1", "threadId": "t1", "labelIds": []})
    ))
    fake_messages.delete = MagicMock(return_value=MagicMock(execute=MagicMock(return_value="")))
    fake_service = MagicMock()
    fake_service.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()) as load_creds, \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        _run(tool_callable(arguments, reg))
    return load_creds.call_args.kwargs["scopes"]


def test_gmail_modify_tools_load_gmail_modify_scope():
    modify_scope = "https://www.googleapis.com/auth/gmail.modify"
    assert _capture_modify_scopes(
        google_tools.tool_gmail_label_message,
        {"id": "m1", "add_label_ids": ["STARRED"]},
    ) == [modify_scope]
    assert _capture_modify_scopes(
        google_tools.tool_gmail_archive_message, {"id": "m1"}
    ) == [modify_scope]
    assert _capture_modify_scopes(
        google_tools.tool_gmail_delete_message, {"id": "m1", "confirm": True}
    ) == [modify_scope]
    assert _capture_modify_scopes(
        google_tools.tool_gmail_mark_read, {"id": "m1"}
    ) == [modify_scope]
    assert _capture_modify_scopes(
        google_tools.tool_gmail_mark_unread, {"id": "m1"}
    ) == [modify_scope]


# ── Phase A.3: scope gating + reauth signal ──────────────────────────────────

def test_gmail_send_blocks_when_scope_not_consented():
    """Path-A bot without gmail.send in google_integration.scopes is rejected
    BEFORE Google is called — clean operator error, not a Google 403.
    """
    reg = _registry()
    net = _network()
    # Narrow the configured scopes — gmail.send is no longer available
    net["bots"]["lex"]["google_integration"]["scopes"] = [
        "https://www.googleapis.com/auth/calendar.readonly"
    ]
    with patch(
        "evolve_admin.mcp_bridge.google_tools.config.load_network",
        return_value=net,
    ):
        with pytest.raises(
            google_tools.google_auth.InsufficientGrantedScopes,
            match="gmail.send",
        ):
            _run(google_tools.tool_gmail_send(
                {"to": "x@y.com", "subject": "Hi", "body": "Hello"}, reg
            ))


def test_gmail_send_emits_reauth_signal_on_refresh_token_expired():
    """When Path-A load_credentials raises RefreshTokenExpired, the wrapper
    fires the dedup'd Signal AND re-raises the exception for the bot.
    """
    reg = _registry()
    emitted: list[tuple] = []

    def _fake_emit(bot_id, exc):
        emitted.append((bot_id, exc))

    expired = google_tools.google_auth.RefreshTokenExpired(
        bot_id="lex", status_code=400, body='{"error":"invalid_grant"}',
    )

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.assert_scopes_available"), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               side_effect=expired), \
         patch("evolve_admin.google_service._emit_reauth_signal",
               new=_fake_emit):
        with pytest.raises(google_tools.google_auth.RefreshTokenExpired):
            _run(google_tools.tool_gmail_send(
                {"to": "x@y.com", "subject": "Hi", "body": "Hello"}, reg
            ))

    assert emitted == [("lex", expired)]


def test_drive_write_file_emits_reauth_signal_on_refresh_token_expired():
    reg = _registry()
    emitted: list[tuple] = []

    def _fake_emit(bot_id, exc):
        emitted.append((bot_id, exc))

    expired = google_tools.google_auth.RefreshTokenExpired(
        bot_id="lex", status_code=400, body='{"error":"invalid_grant"}',
    )

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.assert_scopes_available"), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               side_effect=expired), \
         patch("evolve_admin.google_service._emit_reauth_signal",
               new=_fake_emit):
        with pytest.raises(google_tools.google_auth.RefreshTokenExpired):
            _run(google_tools.tool_drive_write_file(
                {"name": "notes.txt", "content": "hi"}, reg
            ))

    assert emitted == [("lex", expired)]


def test_calendar_create_event_emits_reauth_signal_on_refresh_token_expired():
    reg = _registry()
    emitted: list[tuple] = []

    def _fake_emit(bot_id, exc):
        emitted.append((bot_id, exc))

    expired = google_tools.google_auth.RefreshTokenExpired(
        bot_id="lex", status_code=400, body='{"error":"invalid_grant"}',
    )

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               return_value=_network()), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.assert_scopes_available"), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               side_effect=expired), \
         patch("evolve_admin.google_service._emit_reauth_signal",
               new=_fake_emit):
        with pytest.raises(google_tools.google_auth.RefreshTokenExpired):
            _run(google_tools.tool_calendar_create_event(
                {
                    "summary": "Hotel check-in",
                    "start": "2026-06-15T18:00:00-07:00",
                    "end": "2026-06-15T19:00:00-07:00",
                }, reg
            ))

    assert emitted == [("lex", expired)]

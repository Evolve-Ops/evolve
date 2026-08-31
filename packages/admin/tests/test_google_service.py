"""Unit tests for evolve_admin.google_service — the shared curated Google
tool layer (the single source of the Gmail/Calendar/Drive tool logic).

Mocks the Google API service build + credential loader so tests run without
the SDK installed and without real GCP access. Mirrors the mocking seams the
bridge tests (test_mcp_google_tools.py) already use.

The exhaustive per-tool behavior is covered by test_mcp_google_tools.py
(which now drives the same logic via the bridge wrappers). This file covers
the NEW shared-module surface: run_google_tool dispatch, the eligibility
gate (is_google_configured / google_state), and that a configured Path-C
bot's read tools succeed when called directly through the shared entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import google_service


def _network(bot_id: str = "lex", mode: str = "service_account_dwd") -> dict:
    return {
        "bots": {
            bot_id: {
                "role": "member",
                "primary_user": {"name": "Sam Riley"},
                "google_integration": {
                    "mode": mode,
                    "workspace_domain": "example-corp.com",
                    "subject": f"{bot_id}@example-corp.com",
                    "service_account_secret_ref": "google-sa-example-corp",
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.send",
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/calendar",
                        "https://www.googleapis.com/auth/calendar.readonly",
                        "https://www.googleapis.com/auth/drive.file",
                    ],
                },
            }
        }
    }


# ── is_google_configured ──────────────────────────────────────────────────────


def test_is_google_configured_path_c():
    net = _network("lex", "service_account_dwd")
    assert google_service.is_google_configured("lex", net) is True


def test_is_google_configured_path_a():
    net = _network("lex", "free_gmail_oauth")
    assert google_service.is_google_configured("lex", net) is True


def test_is_google_configured_false_when_no_block():
    net = {"bots": {"plain": {"role": "member"}}}
    assert google_service.is_google_configured("plain", net) is False


def test_is_google_configured_false_for_unknown_bot():
    net = _network("lex")
    assert google_service.is_google_configured("ghost", net) is False


def test_is_google_configured_false_for_unsupported_mode():
    """workspace_user_oauth (Path B) is not runnable by this layer yet —
    a bot configured for it must NOT be treated as Google-enabled."""
    net = _network("lex", "workspace_user_oauth")
    assert google_service.is_google_configured("lex", net) is False


# ── google_state ──────────────────────────────────────────────────────────────


def test_google_state_configured_lists_all_tools():
    net = _network("lex")
    state = google_service.google_state("lex", net)
    assert state["configured"] is True
    assert state["bot"] == "lex"
    assert state["mode"] == "service_account_dwd"
    names = {t["name"] for t in state["tools"]}
    assert names == set(google_service.TOOL_SPECS)
    assert len(state["tools"]) == 16
    # write flags are carried through for the plugin's tool annotations
    by_name = {t["name"]: t for t in state["tools"]}
    assert by_name["gmail_send"]["write"] is True
    assert by_name["gmail_list_messages"]["write"] is False


def test_google_state_unconfigured_has_no_tools():
    net = {"bots": {"plain": {"role": "member"}}}
    state = google_service.google_state("plain", net)
    assert state["configured"] is False
    assert state["mode"] is None
    assert state["tools"] == []
    assert state["scopes"] == []


# ── run_google_tool dispatch ──────────────────────────────────────────────────


def test_run_google_tool_unknown_raises():
    with pytest.raises(google_service.UnknownGoogleToolError, match="frobnicate"):
        google_service.run_google_tool("lex", "frobnicate", {}, network=_network())


def test_run_google_tool_dispatches_to_named_tool():
    """run_google_tool routes to the right impl and passes bot_id + args."""
    net = _network("lex")
    captured = {}

    def _fake_list(bot_id, args, network=None):
        captured["bot_id"] = bot_id
        captured["args"] = args
        return {"ok": True, "bot": bot_id, "messages": [], "count": 0}

    # Patch the registered spec's func via the dispatch table.
    spec = google_service.TOOL_SPECS["gmail_list_messages"]
    with patch.dict(google_service.TOOL_SPECS,
                    {"gmail_list_messages": google_service.GoogleToolSpec(
                        spec.name, _fake_list, spec.write, spec.summary)}):
        result = google_service.run_google_tool(
            "lex", "gmail_list_messages", {"q": "from:alice"}, network=net,
        )
    assert result["ok"] is True
    assert captured["bot_id"] == "lex"
    assert captured["args"] == {"q": "from:alice"}


# ── Configured Path-C bot: read tools succeed through the shared entry ─────────


def _gmail_service_mock():
    fake_list = MagicMock(execute=MagicMock(return_value={
        "messages": [{"id": "m1", "threadId": "t1"}],
    }))
    def fake_get(userId, id, format, metadataHeaders):
        return MagicMock(execute=MagicMock(return_value={
            "id": id, "threadId": f"t-{id}", "snippet": "hi",
            "labelIds": ["INBOX"],
            "payload": {"headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Hello"},
            ]},
        }))
    fake_messages = MagicMock()
    fake_messages.list = MagicMock(return_value=fake_list)
    fake_messages.get = MagicMock(side_effect=fake_get)
    svc = MagicMock()
    svc.users = MagicMock(return_value=MagicMock(messages=MagicMock(return_value=fake_messages)))
    return svc


def test_path_c_gmail_list_messages_succeeds_via_run_google_tool():
    net = _network("lex")
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=_gmail_service_mock()):
        result = google_service.run_google_tool(
            "lex", "gmail_list_messages", {"max_results": 5}, network=net,
        )
    assert result["ok"] is True
    assert result["bot"] == "lex"
    assert result["count"] == 1
    assert result["messages"][0]["from"] == "alice@example.com"


def test_path_c_drive_list_files_succeeds_via_run_google_tool():
    net = _network("lex")
    fake_files_list = MagicMock(execute=MagicMock(return_value={
        "files": [{
            "id": "f1", "name": "notes.txt", "mimeType": "text/plain",
            "modifiedTime": "2026-06-01T09:00:00Z", "webViewLink": "https://d/f1",
            "owners": [{"emailAddress": "lex@example-corp.com", "displayName": "Lex"}],
            "size": "10", "parents": ["root"],
        }],
    }))
    fake_service = MagicMock(files=MagicMock(return_value=MagicMock(
        list=MagicMock(return_value=fake_files_list))))
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = google_service.run_google_tool(
            "lex", "drive_list_files", {}, network=net,
        )
    assert result["ok"] is True
    assert result["bot"] == "lex"
    assert result["files"][0]["id"] == "f1"


def test_path_c_calendar_list_events_succeeds_via_run_google_tool():
    net = _network("lex")
    fake_events_list = MagicMock(execute=MagicMock(return_value={
        "items": [{
            "id": "evt-1", "summary": "Standup",
            "start": {"dateTime": "2026-06-15T09:00:00-07:00"},
            "end": {"dateTime": "2026-06-15T09:30:00-07:00"},
            "status": "confirmed",
        }],
    }))
    fake_service = MagicMock(events=MagicMock(return_value=MagicMock(
        list=MagicMock(return_value=fake_events_list))))
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = google_service.run_google_tool(
            "lex", "calendar_list_events", {}, network=net,
        )
    assert result["ok"] is True
    assert result["bot"] == "lex"
    assert result["events"][0]["summary"] == "Standup"


def test_run_google_tool_never_reads_bot_from_args():
    """Defense-in-depth: even if a `bot`/`subject` key sneaks into args, the
    acting bot is the bot_id passed to run_google_tool — args are tool params
    only. The result is shaped for the bot_id argument, not the args field."""
    net = _network("lex")
    with patch("evolve_admin.google_service.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=_gmail_service_mock()):
        result = google_service.run_google_tool(
            "lex", "gmail_list_messages",
            {"bot": "rex", "subject": "evil@example.com"}, network=net,
        )
    assert result["bot"] == "lex"

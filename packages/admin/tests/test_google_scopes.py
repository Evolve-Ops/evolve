"""Least scope for the Calendar event tools (brief
google-calendar-delete-and-update-events item 7).

The event tools REQUEST ``calendar.events``; every pod enrolled today holds
the broader ``calendar`` (verified live 2026-09-29: no bot's grant lists
``calendar.events``). google_auth resolves the request onto that grant on
every path, so nothing is widened and nobody is asked to re-consent.
"""

from __future__ import annotations

import json

import pytest

from evolve_admin import google_auth, google_service
from evolve_admin.web import google_bot_routes

from ._calendar_fakes import FakeCalendar, calendar_env, event, network

EVENTS = "https://www.googleapis.com/auth/calendar.events"
FULL = "https://www.googleapis.com/auth/calendar"
EVENT_WRITE_TOOLS = (
    "calendar_create_event", "calendar_update_event", "calendar_delete_event")


def test_event_tools_request_the_least_scope(tmp_path):
    calls = {
        "calendar_create_event": {"summary": "s",
                                  "start": "2026-09-25T10:30:00-07:00",
                                  "end": "2026-09-25T11:00:00-07:00"},
        "calendar_update_event": {"event_id": "evt-1", "summary": "s"},
        "calendar_delete_event": {"event_id": "evt-1", "confirm": True},
    }
    requested: dict[str, list] = {}
    for tool, args in calls.items():
        with calendar_env(FakeCalendar({"evt-1": event("evt-1")})):
            google_service.run_google_tool(
                "bot-a", tool, args, network=network(tmp_path))
            load = google_service._load_credentials_or_signal
            requested[tool] = list(load.call_args.args[1])  # type: ignore[attr-defined]
    assert requested == {t: [EVENTS] for t in EVENT_WRITE_TOOLS}
    # The route's per-tool scope map says the same.
    for tool in EVENT_WRITE_TOOLS:
        assert google_bot_routes.spec_scopes_hint(tool) == [EVENTS]
    assert google_bot_routes.spec_scopes_hint("calendar_list_events") == [
        "https://www.googleapis.com/auth/calendar.readonly"]


def test_calendar_grant_covers_the_events_scope_on_the_precheck(tmp_path):
    # A DwD bot configured with only ``calendar`` passes the pre-call check.
    google_auth.assert_scopes_available(
        "bot-a", [EVENTS], network=network(tmp_path, scopes=[FULL]))


def test_a_grant_without_calendar_still_refuses(tmp_path):
    with pytest.raises(google_auth.InsufficientGrantedScopes):
        google_auth.assert_scopes_available(
            "bot-a", [EVENTS], network=network(
                tmp_path, scopes=["https://www.googleapis.com/auth/calendar.readonly"]))


def test_dwd_clamp_mints_the_enrolled_calendar_scope():
    assert google_auth._clamp_dwd_scopes("bot-a", [EVENTS], [FULL]) == [FULL]
    assert google_auth._clamp_dwd_scopes("bot-a", [EVENTS], [EVENTS, FULL]) == [EVENTS]


def test_oauth_consent_to_calendar_covers_the_events_scope(tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "bot-a.json").write_text(json.dumps({
        "access_token": "a", "refresh_token": "r",
        "expires_at": "2999-01-01T00:00:00+00:00",
        "scopes_granted": [FULL],
    }))
    monkeypatch.setattr(google_auth, "_resolve_oauth_client",
                        lambda *_a, **_k: ("cid", "csecret"))
    # No network: the token is treated as fresh.
    monkeypatch.setattr(google_auth, "_ensure_fresh_access_token",
                        lambda _bot, record, *_a, **_k: record)
    creds = google_auth._load_free_gmail_oauth_credentials(
        "bot-a", {}, {}, [EVENTS], tokens)
    # Resolved onto the consented scope — no new consent requested.
    assert list(creds.scopes or []) == [FULL]


def test_no_scope_is_widened_for_other_tools(tmp_path):
    # The coverage is calendar.events only: a Gmail read still needs its own.
    with pytest.raises(google_auth.InsufficientGrantedScopes):
        google_auth.assert_scopes_available(
            "bot-a", ["https://www.googleapis.com/auth/gmail.readonly"],
            network=network(tmp_path, scopes=[
                FULL, "https://www.googleapis.com/auth/gmail.modify"]))

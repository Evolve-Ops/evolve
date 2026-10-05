"""calendar_delete_event — a bot retracts what it created (brief
google-calendar-delete-and-update-events item 1).

Ownership is read from the credentials (the impersonated account), never
from args; an unreadable organizer fails closed as "not own"; a forced delete
of someone else's event is audited HIGH and notifies its guests.
"""

from __future__ import annotations

import pytest

from evolve_admin import google_service

from ._calendar_fakes import OTHER, OWN, FakeCalendar, calendar_env, event, network


def _delete(tmp_path, fake, *, subject: str | None = OWN, **args):
    with calendar_env(fake, subject=subject) as audits:
        result = google_service.calendar_delete_event(
            "bot-a", {"event_id": "evt-1", "confirm": True, **args},
            network(tmp_path))
    return result, audits


def test_delete_refuses_without_confirm(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    for confirm in (None, False, "true"):
        args: dict = {"event_id": "evt-1"}
        if confirm is not None:
            args["confirm"] = confirm
        with calendar_env(fake):
            with pytest.raises(ValueError, match="confirm must be true"):
                google_service.calendar_delete_event(
                    "bot-a", args, network(tmp_path))
    assert fake.calls == []


def test_delete_refuses_foreign_event_without_force(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1", organizer=OTHER,
                                        creator={"email": OTHER})})
    with pytest.raises(ValueError) as exc:
        _delete(tmp_path, fake)
    msg = str(exc.value)
    assert "organized by operator@example-corp.com" in msg
    assert "force: true" in msg
    assert fake.called("delete") == []


def test_delete_own_event_calls_api_once(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    result, audits = _delete(tmp_path, fake)
    (call,) = fake.called("delete")
    assert call == {"calendarId": "primary", "eventId": "evt-1",
                    "sendUpdates": "none"}
    assert result == {
        "ok": True, "id": "evt-1", "summary": "Sync evt-1",
        "start": "2026-09-25T10:30:00-07:00", "calendar_id": "primary",
        "was_own": True, "notified": "none", "bot": "bot-a",
    }
    (entry,) = audits
    assert entry["action"] == "google.calendar.calendar_delete_event"
    assert entry["event_id"] == "evt-1" and entry["summary"] == "Sync evt-1"
    assert entry["calendar_id"] == "primary"
    assert entry["was_own"] is True and entry["forced"] is False
    assert entry["severity"] == "info"


def test_created_by_the_bot_counts_as_own_even_when_organizer_differs(tmp_path):
    # An event the bot created on a shared calendar: organizer is the
    # calendar owner, creator is the bot.
    fake = FakeCalendar({"evt-1": event("evt-1", organizer=OTHER,
                                        creator={"email": OWN})})
    result, _ = _delete(tmp_path, fake, calendar_id="shared@example-corp.com")
    assert result["was_own"] is True


def test_force_delete_logs_high_with_organizer(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1", organizer=OTHER)})
    result, audits = _delete(tmp_path, fake, force=True)
    assert result["was_own"] is False
    # Guests of someone else's event get the cancellation.
    assert fake.called("delete")[0]["sendUpdates"] == "all"
    assert result["notified"] == "all"
    (entry,) = audits
    assert entry["severity"] == "high"
    assert entry["organizer"] == OTHER
    assert entry["forced"] is True


def test_missing_organizer_counts_as_foreign(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1", organizer=None)})
    with pytest.raises(ValueError, match="organized by \\(unknown\\)"):
        _delete(tmp_path, fake)
    assert fake.called("delete") == []


def test_unknown_own_account_counts_as_foreign(tmp_path):
    # No DwD subject on the creds and Calendar cannot say who we are.
    fake = FakeCalendar({"evt-1": event("evt-1")})
    fake.calendarList = lambda: (_ for _ in ()).throw(RuntimeError("403"))
    with pytest.raises(ValueError, match="refused"):
        _delete(tmp_path, fake, subject=None)
    assert fake.called("delete") == []


def test_oauth_identity_comes_from_the_primary_calendar(tmp_path):
    # Path A creds carry no subject; the primary calendar's id is the account.
    fake = FakeCalendar({"evt-1": event("evt-1")})
    result, _ = _delete(tmp_path, fake, subject=None)
    assert result["was_own"] is True


def test_series_scope_deletes_the_master(tmp_path):
    fake = FakeCalendar({
        "evt-1_20260930": event("evt-1_20260930", recurringEventId="evt-1"),
        "evt-1": event("evt-1", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"]),
    })
    with calendar_env(fake):
        result = google_service.calendar_delete_event(
            "bot-a", {"event_id": "evt-1_20260930", "confirm": True,
                      "scope": "series"}, network(tmp_path))
    assert fake.called("delete")[0]["eventId"] == "evt-1"
    assert result["id"] == "evt-1"

"""calendar_update_event + recurrence (brief
google-calendar-delete-and-update-events items 2, 5, 6).

PATCH semantics (only supplied fields change), the same ownership rule as
delete, notifications computed from WHAT changed, and instance-vs-series
targeting for recurring events.
"""

from __future__ import annotations

import pytest

from evolve_admin import google_service

from ._calendar_fakes import OTHER, FakeCalendar, calendar_env, event, network


def _update(tmp_path, fake, **args):
    with calendar_env(fake) as audits:
        result = google_service.calendar_update_event(
            "bot-a", {"event_id": "evt-1", **args}, network(tmp_path))
    return result, audits


def test_update_patches_only_supplied_fields(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    result, audits = _update(tmp_path, fake, summary="Renamed sync")
    (call,) = fake.called("patch")
    assert call["body"] == {"summary": "Renamed sync"}
    assert call["eventId"] == "evt-1" and call["calendarId"] == "primary"
    assert fake.called("insert") == []   # never a create
    assert result["summary"] == "Renamed sync"
    assert result["was_own"] is True
    (entry,) = audits
    assert entry["fields"] == ["summary"] and entry["severity"] == "info"


def test_update_with_nothing_to_change_is_refused(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    with pytest.raises(ValueError, match="nothing to update"):
        _update(tmp_path, fake)
    assert fake.calls == []


def test_update_refuses_foreign_without_force(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1", organizer=OTHER)})
    with pytest.raises(ValueError, match="calendar_update_event refused"):
        _update(tmp_path, fake, summary="x")
    assert fake.called("patch") == []

    result, audits = _update(tmp_path, fake, summary="x", force=True)
    assert result["was_own"] is False
    assert audits[0]["severity"] == "high" and audits[0]["organizer"] == OTHER


def test_update_sends_updates_only_when_time_or_attendees_change(tmp_path):
    cases = [
        ({"summary": "t"}, "none"),
        ({"description": "d"}, "none"),
        ({"start": "2026-09-25T11:00:00-07:00"}, "all"),
        ({"end": "2026-09-25T12:00:00-07:00"}, "all"),
        ({"location": "Room 2"}, "all"),
        ({"attendees": [OTHER, "guest@example-corp.com"]}, "all"),
        ({"start": "2026-09-25T11:00:00-07:00", "send_updates": "none"}, "none"),
    ]
    for args, expected in cases:
        fake = FakeCalendar({"evt-1": event("evt-1")})
        result, _ = _update(tmp_path, fake, **args)
        assert fake.called("patch")[0]["sendUpdates"] == expected, args
        assert result["notified"] == expected


def test_move_reports_the_new_weekday_and_checks_the_asserted_one(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    result, _ = _update(tmp_path, fake, start="2026-09-28T10:30:00-07:00",
                        end="2026-09-28T11:00:00-07:00", start_weekday="Monday")
    assert result["start_weekday"] == "Monday"
    with pytest.raises(ValueError, match="is a Monday, not the Tuesday"):
        _update(tmp_path, FakeCalendar({"evt-1": event("evt-1")}),
                start="2026-09-28T10:30:00-07:00", start_weekday="Tuesday")


def test_create_passes_recurrence_through(tmp_path):
    fake = FakeCalendar()
    rule = ["RRULE:FREQ=WEEKLY;BYDAY=WE"]
    with calendar_env(fake):
        result = google_service.calendar_create_event("bot-a", {
            "summary": "Weekly", "start": "2026-09-30T09:00:00-07:00",
            "end": "2026-09-30T09:30:00-07:00", "recurrence": rule,
        }, network(tmp_path))
    assert fake.called("insert")[0]["body"]["recurrence"] == rule
    assert result["recurrence"] == rule


def _recurring_fake():
    return FakeCalendar({
        "evt-1_20260930": event("evt-1_20260930", recurringEventId="evt-1"),
        "evt-1": event("evt-1", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE"]),
    })


def test_series_scope_resolves_the_master(tmp_path):
    fake = _recurring_fake()
    with calendar_env(fake):
        google_service.calendar_update_event("bot-a", {
            "event_id": "evt-1_20260930", "summary": "Renamed series",
            "scope": "series"}, network(tmp_path))
    assert [kw["eventId"] for kw in fake.called("get")] == [
        "evt-1_20260930", "evt-1"]
    assert fake.called("patch")[0]["eventId"] == "evt-1"


def test_instance_scope_targets_the_occurrence(tmp_path):
    fake = _recurring_fake()
    with calendar_env(fake):
        google_service.calendar_update_event("bot-a", {
            "event_id": "evt-1_20260930", "summary": "Just this one"},
            network(tmp_path))
    assert fake.called("patch")[0]["eventId"] == "evt-1_20260930"


def test_bad_scope_value_is_refused(tmp_path):
    fake = FakeCalendar({"evt-1": event("evt-1")})
    with pytest.raises(ValueError, match="scope must be"):
        _update(tmp_path, fake, summary="x", scope="following")


THREE = ["a@example-corp.com", "b@example-corp.com", "c@example-corp.com"]


def _three_guest_fake():
    return FakeCalendar({"evt-1": event(
        "evt-1", attendees=[{"email": e, "responseStatus": "accepted"}
                            for e in THREE])})


def test_one_name_attendees_call_adds_and_keeps_the_other_guests(tmp_path):
    fake = _three_guest_fake()
    result, _ = _update(tmp_path, fake, attendees=["new@example-corp.com"])
    (call,) = fake.called("patch")
    emails = [a["email"] for a in call["body"]["attendees"]]
    assert emails == THREE + ["new@example-corp.com"]
    assert call["sendUpdates"] == "all"
    # existing guests are carried whole (RSVP state survives the PATCH)
    assert call["body"]["attendees"][0]["responseStatus"] == "accepted"
    # no guest of the fetched event is dropped without the flag
    assert set(THREE) <= set(emails)


def test_attendees_merge_dedupes_by_email_case_insensitively(tmp_path):
    fake = _three_guest_fake()
    _update(tmp_path, fake, attendees=["A@Example-Corp.com", "new@example-corp.com"])
    emails = [a["email"] for a in fake.called("patch")[0]["body"]["attendees"]]
    assert emails == THREE + ["new@example-corp.com"]


def test_replace_attendees_replaces_the_whole_list(tmp_path):
    fake = _three_guest_fake()
    _update(tmp_path, fake, attendees=["only@example-corp.com"],
            replace_attendees=True)
    (call,) = fake.called("patch")
    assert call["body"]["attendees"] == [{"email": "only@example-corp.com"}]
    assert call["sendUpdates"] == "all"


def test_recurring_insert_carries_start_timezone(tmp_path):
    def create(**extra):
        fake = FakeCalendar()
        with calendar_env(fake):
            google_service.calendar_create_event("bot-a", {
                "summary": "Weekly", "start": "2026-09-30T09:00:00-07:00",
                "end": "2026-09-30T09:30:00-07:00",
                "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=WE"], **extra},
                network(tmp_path))
        return fake.called("insert")[0]["body"]

    body = create()
    assert body["start"]["timeZone"] == "Etc/GMT+7" == body["end"]["timeZone"]
    body = create(timezone="America/Los_Angeles")
    assert body["start"]["timeZone"] == "America/Los_Angeles"

    fake = FakeCalendar()
    with calendar_env(fake):
        google_service.calendar_create_event("bot-a", {
            "summary": "One-off", "start": "2026-09-30T09:00:00-07:00",
            "end": "2026-09-30T09:30:00-07:00"}, network(tmp_path))
    assert "timeZone" not in fake.called("insert")[0]["body"]["start"]

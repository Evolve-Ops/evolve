"""calendar_create_event tells the truth about what it did (brief
google-calendar-delete-and-update-events, items 0a-0c; D-AS2).

0a — the insert sends the invitation (``sendUpdates``; the v3 default is
     "none", so before this no attendee was ever notified).
0b — the result carries what Google stored: start/end, resolved weekday,
     timezone, attendees, organizer, and the notification value used.
0c — an asserted weekday that disagrees with ``start`` is refused before
     any API call.
"""

from __future__ import annotations

import pytest

from evolve_admin import google_service

from ._calendar_fakes import OTHER, OWN, FakeCalendar, calendar_env, network


def _create(tmp_path, fake, **args):
    base = {
        "summary": "Planning sync",
        "start": "2026-09-25T10:30:00-07:00",
        "end": "2026-09-25T11:00:00-07:00",
    }
    base.update(args)
    with calendar_env(fake):
        return google_service.calendar_create_event(
            "bot-a", base, network(tmp_path))


def test_insert_passes_send_updates_all_when_attendees_present(tmp_path):
    fake = FakeCalendar()
    _create(tmp_path, fake, attendees=[OTHER, "guest@example-corp.com"])
    (insert,) = fake.called("insert")
    assert insert.get("sendUpdates") == "all"


def test_insert_passes_send_updates_none_when_no_attendees(tmp_path):
    fake = FakeCalendar()
    _create(tmp_path, fake)
    assert fake.called("insert")[0]["sendUpdates"] == "none"


def test_insert_with_only_the_bot_itself_as_attendee_notifies_nobody(tmp_path):
    fake = FakeCalendar()
    _create(tmp_path, fake, attendees=[OWN.upper()])
    assert fake.called("insert")[0]["sendUpdates"] == "none"


def test_explicit_send_updates_arg_overrides_the_computed_value(tmp_path):
    fake = FakeCalendar()
    result = _create(tmp_path, fake, attendees=[OTHER], send_updates="none")
    assert fake.called("insert")[0]["sendUpdates"] == "none"
    assert result["notified"] == "none"


def test_invalid_send_updates_refused_before_any_api_call(tmp_path):
    fake = FakeCalendar()
    with pytest.raises(ValueError, match="send_updates must be one of"):
        _create(tmp_path, fake, send_updates="everyone")
    assert fake.calls == []


def test_result_reports_notified_value_used(tmp_path):
    result = _create(tmp_path, FakeCalendar(), attendees=[OTHER])
    assert result["notified"] == "all"


def test_result_carries_start_end_weekday_timezone_attendees_organizer(tmp_path):
    result = _create(tmp_path, FakeCalendar(),
                     attendees=[OTHER, "guest@example-corp.com"])
    assert result["start"] == "2026-09-25T10:30:00-07:00"
    assert result["end"] == "2026-09-25T11:00:00-07:00"
    assert result["start_weekday"] == "Friday"
    assert result["timezone"] == "-07:00"
    assert result["attendees"] == [OTHER, "guest@example-corp.com"]
    assert result["organizer"] == OWN
    assert result["html_link"] == "https://calendar.example/evt-new"
    # Today's fields are kept.
    assert result["ok"] is True and result["id"] == "evt-new"
    assert result["calendar_id"] == "primary" and result["bot"] == "bot-a"


def test_asserted_weekday_mismatch_refuses_before_any_api_call(tmp_path):
    # The incident: "this Friday" from Tue 2026-09-22 resolved to 09-26.
    fake = FakeCalendar()
    with pytest.raises(ValueError) as exc:
        _create(tmp_path, fake, start="2026-09-26T10:30:00-07:00",
                end="2026-09-26T11:00:00-07:00", start_weekday="Friday",
                attendees=[OTHER])
    assert "start 2026-09-26 is a Saturday, not the Friday you asserted" in str(exc.value)
    assert fake.calls == []


def test_asserted_weekday_match_proceeds(tmp_path):
    fake = FakeCalendar()
    result = _create(tmp_path, fake, start_weekday="friday")
    assert len(fake.called("insert")) == 1
    assert result["start_weekday"] == "Friday"


def test_weekday_is_resolved_in_the_events_own_offset_not_utc(tmp_path):
    # 2026-09-25 18:30 at -07:00 is 2026-09-26 01:30 UTC (a Saturday in UTC).
    fake = FakeCalendar()
    result = _create(tmp_path, fake, start="2026-09-25T18:30:00-07:00",
                     end="2026-09-25T19:30:00-07:00", start_weekday="Fri")
    assert result["start_weekday"] == "Friday"
    with pytest.raises(ValueError, match="is a Friday, not the Saturday"):
        _create(tmp_path, FakeCalendar(), start="2026-09-25T18:30:00-07:00",
                end="2026-09-25T19:30:00-07:00", start_weekday="Saturday")


def test_unknown_weekday_word_is_refused(tmp_path):
    fake = FakeCalendar()
    with pytest.raises(ValueError, match="not a weekday name"):
        _create(tmp_path, fake, start_weekday="Fryday")
    assert fake.calls == []

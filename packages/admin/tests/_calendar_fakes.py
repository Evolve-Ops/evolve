"""Shared fakes for the Calendar event-tool tests
(test_google_calendar_{create_truthful,delete,update}.py, test_google_scopes.py).

``FakeCalendar`` stands in for the ``googleapiclient`` Calendar v3 service:
it records every call (method + kwargs) and serves events from an in-memory
dict, so a test can assert both what was sent to Google and that nothing was
sent at all (refusals must happen before any API call).

``calendar_env`` patches the three seams ``google_service`` goes through —
credential load, service build, and the admin audit writer — so no test
touches real credentials or appends to the real ``/Users/Shared/evolve``
audit log.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

OWN = "bot-a@example-corp.com"
OTHER = "operator@example-corp.com"


def network(tmp_path, scopes=None) -> dict:
    return {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "bot-a": {
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": OWN,
                    "service_account_secret_ref": "google-sa-example-corp",
                    "scopes": scopes if scopes is not None else [
                        "https://www.googleapis.com/auth/calendar",
                    ],
                },
            },
        },
    }


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeCalendar:
    def __init__(self, events: dict | None = None):
        self.store = dict(events or {})
        self.calls: list[tuple[str, dict]] = []

    # googleapiclient shape: service.events().insert(...).execute()
    def events(self):
        return self

    def calendarList(self):  # noqa: N802 — googleapiclient method name
        return SimpleNamespace(get=lambda **_kw: _Req(lambda: {"id": OWN}))

    def insert(self, **kw):
        self.calls.append(("insert", kw))
        body = kw["body"]
        tz = body["start"].get("timeZone")
        return _Req(lambda: {
            "id": "evt-new",
            "summary": body.get("summary"),
            "start": {"dateTime": body["start"]["dateTime"],
                      **({"timeZone": tz} if tz else {})},
            "end": {"dateTime": body["end"]["dateTime"]},
            "attendees": [dict(a, responseStatus="needsAction")
                          for a in body.get("attendees", [])],
            "organizer": {"email": OWN, "self": True},
            "recurrence": body.get("recurrence"),
            "htmlLink": "https://calendar.example/evt-new",
        })

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Req(lambda: dict(self.store[kw["eventId"]]))

    def delete(self, **kw):
        self.calls.append(("delete", kw))
        return _Req(lambda: "")

    def patch(self, **kw):
        self.calls.append(("patch", kw))

        def _apply():
            merged = dict(self.store[kw["eventId"]])
            merged.update(kw["body"])
            return merged
        return _Req(_apply)

    def called(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]


def event(event_id: str, organizer: str | None = OWN, **extra) -> dict:
    ev = {
        "id": event_id,
        "summary": f"Sync {event_id}",
        "start": {"dateTime": "2026-09-25T10:30:00-07:00"},
        "end": {"dateTime": "2026-09-25T11:00:00-07:00"},
        "attendees": [{"email": OTHER}],
        "htmlLink": f"https://calendar.example/{event_id}",
    }
    if organizer is not None:
        ev["organizer"] = {"email": organizer}
    ev.update(extra)
    return ev


@contextmanager
def calendar_env(fake: FakeCalendar, *, subject: str | None = OWN):
    """Patch creds/service/audit. Yields the list of audit entries written."""
    audits: list[dict] = []
    creds = SimpleNamespace(_subject=subject)
    with patch("evolve_admin.google_service._load_credentials_or_signal",
               return_value=creds), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake), \
         patch("evolve_admin.web.routes_shared._audit_log_entry",
               side_effect=lambda action, bot_id, details, **_: audits.append(
                   {"action": action, "bot_id": bot_id, **details})):
        yield audits

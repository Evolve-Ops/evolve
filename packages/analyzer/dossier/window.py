"""dossier.window — ISO-week arithmetic in the pod's timezone.

The edition id is the ISO week (``2026-W35``), and the window it names runs
Monday 00:00 → the following Monday 00:00 **in the pod's timezone**. Doing
this in UTC would put the pod's Sunday evening into the next edition for
every pod west of Greenwich — the same class of bug as the UTC-named turn
files. The pod timezone comes from ``evolve_admin.config.resolve_pod_timezone``
(explicit ``network.timezone`` → ``/etc/localtime`` → the historical
default), which is the helper the rest of the product formats timestamps
with; reading ``network["timezone"]`` directly would skip the detect step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Last-resort zone, matching ``evolve_admin.config.resolve_pod_timezone``'s
#: own historical default. Used only when that helper is unimportable (a bare
#: analyzer checkout) or names a zone this host has no tzdata for.
DEFAULT_TZ_NAME = "America/Los_Angeles"

#: ``2026-W35`` / ``2026-W05``. Week is always two digits so ids sort
#: lexicographically in the same order they sort chronologically — the
#: property a directory listing of the spine depends on.
EDITION_ID_RE = re.compile(r"^(\d{4})-W(\d{2})$")

WEEK_DAYS = 7


def resolve_timezone(network: dict[str, Any] | None) -> ZoneInfo:
    """The pod's effective timezone as a ``ZoneInfo``.

    Tolerant in both directions: an unimportable ``evolve_admin`` (bare
    analyzer checkout) and an unknown zone name both degrade to
    :data:`DEFAULT_TZ_NAME` rather than raising. A weekly writer that dies
    because a config field holds a typo writes no edition at all, and the
    missing week is unrecoverable.
    """
    name = ""
    try:
        from evolve_admin.config import resolve_pod_timezone  # type: ignore

        name = resolve_pod_timezone(network or {}) or ""
    except Exception:
        name = str((network or {}).get("timezone") or "").strip()
    try:
        return ZoneInfo(name or DEFAULT_TZ_NAME)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ_NAME)


def iso_z(moment: datetime) -> str:
    """``moment`` as UTC ISO-8601 with a ``Z`` suffix — the pod's stamp form.

    Stamps (``computed_at``) are UTC even though WINDOWS are pod-local: a
    window is a claim about the pod's calendar, a stamp is a claim about an
    instant, and instants in this codebase are UTC. Lives here rather than
    in ``edition`` because the modules layer stamps itself too, and one
    formatter is what keeps the two files' stamps comparable.
    """
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def edition_id(iso_year: int, iso_week: int) -> str:
    """``(2026, 35) -> "2026-W35"``."""
    return f"{iso_year:04d}-W{iso_week:02d}"


def parse_edition_id(raw: str) -> tuple[int, int]:
    """``"2026-W35" -> (2026, 35)``. Raises ``ValueError`` on anything else.

    Validates the week against the year's real week count so a typo
    (``2026-W53`` in a 52-week year) fails at the CLI rather than silently
    writing an edition whose window is a different year's week 1.
    """
    m = EDITION_ID_RE.match((raw or "").strip().upper())
    if not m:
        raise ValueError(f"not an ISO week id (expected YYYY-Www): {raw!r}")
    year, week = int(m.group(1)), int(m.group(2))
    if week < 1 or week > weeks_in_year(year):
        raise ValueError(
            f"{raw!r}: {year} has {weeks_in_year(year)} ISO weeks, not {week}"
        )
    return year, week


def weeks_in_year(iso_year: int) -> int:
    """52 or 53 — the number of ISO weeks in ``iso_year``.

    Dec 28 is in the last ISO week of its own year by construction, which is
    the standard way to get this without a table.
    """
    return date(iso_year, 12, 28).isocalendar()[1]


def monday_of(iso_year: int, iso_week: int) -> date:
    """The Monday that opens ISO week ``iso_week`` of ``iso_year``."""
    return date.fromisocalendar(iso_year, iso_week, 1)


@dataclass(frozen=True)
class EditionWindow:
    """The seven-day span one edition measures, in the pod's timezone.

    ``complete`` is the seal condition: the window has fully elapsed at the
    moment of computation. An edition computed mid-week is legitimate (that
    is what ``--now`` is for) but is explicitly marked incomplete so a later
    reader never mistakes a partial week for a quiet one.
    """

    edition_id: str
    iso_year: int
    iso_week: int
    timezone: str
    start: datetime  # inclusive, pod-local, 00:00 Monday
    end: datetime    # exclusive, pod-local, 00:00 the following Monday
    complete: bool

    @property
    def days(self) -> list[date]:
        """The seven pod-local dates the window covers, in order."""
        first = self.start.date()
        return [first + timedelta(days=i) for i in range(WEEK_DAYS)]

    def contains(self, moment: datetime) -> bool:
        """True when ``moment`` (tz-aware) falls inside ``[start, end)``."""
        return self.start <= moment.astimezone(self.start.tzinfo) < self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition_id": self.edition_id,
            "iso_year": self.iso_year,
            "iso_week": self.iso_week,
            "timezone": self.timezone,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": WEEK_DAYS,
            "first_date": self.days[0].isoformat(),
            "last_date": self.days[-1].isoformat(),
            "complete": self.complete,
        }


def window_for(
    iso_year: int, iso_week: int, tz: ZoneInfo, *, now: datetime
) -> EditionWindow:
    """Build the window for one ISO week, judged complete against ``now``.

    ``now`` must be timezone-aware; it is converted into ``tz`` before the
    comparison so the completeness test is made in pod-local time.
    """
    start = datetime.combine(monday_of(iso_year, iso_week), datetime.min.time(), tz)
    end = start + timedelta(days=WEEK_DAYS)
    return EditionWindow(
        edition_id=edition_id(iso_year, iso_week),
        iso_year=iso_year,
        iso_week=iso_week,
        timezone=str(tz),
        start=start,
        end=end,
        complete=now.astimezone(tz) >= end,
    )


def current_week(now: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """The ISO (year, week) containing ``now``, in pod-local time."""
    local = now.astimezone(tz)
    cal = local.isocalendar()
    return cal[0], cal[1]


def previous_week(now: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """The most recently COMPLETED ISO (year, week) as of ``now``.

    Stepping back to the Sunday before this week's Monday — rather than
    subtracting a fixed seven days from ``now`` — is what makes the answer
    the same whether the scheduled run fires on Monday morning or an
    operator re-runs it on Thursday.
    """
    local = now.astimezone(tz).date()
    last_sunday = local - timedelta(days=local.weekday() + 1)
    cal = last_sunday.isocalendar()
    return cal[0], cal[1]

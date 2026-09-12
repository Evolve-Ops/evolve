"""Tests for pod_time — pod-local TZ helpers for cap rollover."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pod_time import (
    _resolve_pod_tz,
    pod_iso_week,
    pod_local_day_iso,
    pod_local_day_start_utc,
    pod_now,
    pod_today,
    pod_today_str,
)


def _write_network(path: Path, tz: str | None) -> None:
    data: dict = {"pod": {}}
    if tz is not None:
        data["pod"]["timezone"] = tz
    path.write_text(json.dumps(data))


def test_resolve_pod_tz_reads_network_json(tmp_path):
    """When network.json sets pod.timezone, _resolve_pod_tz returns it."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, "America/Los_Angeles")
    tz = _resolve_pod_tz(netfile)
    assert tz is not None
    assert tz.key == "America/Los_Angeles"


def test_resolve_pod_tz_returns_none_when_unset(tmp_path):
    """No pod.timezone field → None, caller falls back to system local."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, None)
    assert _resolve_pod_tz(netfile) is None


def test_resolve_pod_tz_returns_none_when_file_missing(tmp_path):
    """No network.json at all → None, no exception."""
    netfile = tmp_path / "does-not-exist.json"
    assert _resolve_pod_tz(netfile) is None


def test_resolve_pod_tz_returns_none_when_invalid_name(tmp_path):
    """Garbage timezone string → None, no crash."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, "Not/A/Real/Zone")
    assert _resolve_pod_tz(netfile) is None


def test_resolve_pod_tz_returns_none_on_malformed_json(tmp_path):
    """Corrupted network.json → None, no crash."""
    netfile = tmp_path / "network.json"
    netfile.write_text("{not valid json")
    assert _resolve_pod_tz(netfile) is None


def test_pod_now_returns_aware_datetime(tmp_path):
    """pod_now is always timezone-aware so callers can do arithmetic safely."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, "Asia/Tokyo")
    now = pod_now(netfile)
    assert now.tzinfo is not None


def test_pod_today_str_matches_configured_tz(tmp_path):
    """The date string reflects pod's configured TZ, not UTC."""
    # Pick a TZ far from UTC so the date-line differs from UTC sometimes.
    netfile = tmp_path / "network.json"
    _write_network(netfile, "Pacific/Auckland")
    # We can't pin the date without freezing time, but we can verify the
    # string matches what zoneinfo says "today" is in that zone.
    expected = datetime.now(ZoneInfo("Pacific/Auckland")).date().isoformat()
    assert pod_today_str(netfile) == expected


def test_pod_today_str_falls_back_to_local_when_unset(tmp_path):
    """No config → system local TZ. The local date is what we get."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, None)
    # System local — compare to datetime.now().astimezone().date() which is
    # exactly what the helper falls back to.
    expected = datetime.now().astimezone().date().isoformat()
    assert pod_today_str(netfile) == expected


def test_pod_iso_week_format(tmp_path):
    """ISO week format is YYYY-WNN."""
    netfile = tmp_path / "network.json"
    _write_network(netfile, "UTC")
    wk = pod_iso_week(netfile)
    # Format: 4-digit year, "-W", 2-digit week.
    assert len(wk) == 8
    assert wk[4:6] == "-W"
    assert wk[:4].isdigit() and wk[6:].isdigit()


# ── pod_local_day_iso / pod_local_day_start_utc ─────────────────────────────
#
# The UTC-storage → pod-local-policy boundary. Turn JSONL filenames and ``ts``
# values are UTC; caps roll at pod-local midnight. Getting this wrong is a
# blackout, not a rounding error: comparing a UTC ``ts[:10]`` prefix against a
# pod-local date selects nothing at all for the pod's UTC offset worth of hours
# every day.

_PACIFIC = ZoneInfo("America/Los_Angeles")
_TOKYO = ZoneInfo("Asia/Tokyo")


@pytest.mark.parametrize(
    "ts,tz,expected",
    [
        # 21:00 PDT Sep 3 is already Sep 4 in UTC — still local Sep 3.
        ("2026-09-04T04:00:00Z", _PACIFIC, "2026-09-03"),
        ("2026-09-03T16:00:00Z", _PACIFIC, "2026-09-03"),
        # 2026-09-03T04:00Z is 21:00 PDT on Sep 2 — the mirror case.
        ("2026-09-03T04:00:00Z", _PACIFIC, "2026-09-02"),
        # East of UTC the local day opens on the PREVIOUS UTC date.
        ("2026-09-02T16:00:00Z", _TOKYO, "2026-09-03"),
        ("2026-09-03T15:30:00Z", _TOKYO, "2026-09-04"),
        # +00:00 offsets and naive (writer-contract UTC) both parse.
        ("2026-09-04T04:00:00+00:00", _PACIFIC, "2026-09-03"),
        ("2026-09-04T04:00:00", _PACIFIC, "2026-09-03"),
    ],
)
def test_pod_local_day_iso_converts_the_instant(ts, tz, expected):
    assert pod_local_day_iso(ts, tz) == expected


@pytest.mark.parametrize("ts", ["", "not-a-timestamp", "2026-13-45T99:00:00Z", None, 17, "2026-09"])
def test_pod_local_day_iso_returns_none_rather_than_guessing(ts):
    """A malformed ts must drop out of the day buckets, not land in one."""
    assert pod_local_day_iso(ts, _PACIFIC) is None


def test_pod_local_day_start_utc_is_local_midnight():
    assert pod_local_day_start_utc(date(2026, 9, 3), _PACIFIC) == datetime(
        2026, 9, 3, 7, 0, tzinfo=timezone.utc
    )
    assert pod_local_day_start_utc(date(2026, 9, 3), _TOKYO) == datetime(
        2026, 9, 2, 15, 0, tzinfo=timezone.utc
    )


def test_pod_local_day_start_utc_tracks_the_dst_offset():
    """PST is UTC-8, PDT is UTC-7 — a fixed offset would be an hour off for
    half the year, which is enough to misbucket a turn at either boundary."""
    assert pod_local_day_start_utc(date(2026, 1, 15), _PACIFIC) == datetime(
        2026, 1, 15, 8, 0, tzinfo=timezone.utc
    )


def test_helpers_agree_at_the_local_day_boundary():
    """The instant a local day starts belongs to that day; one microsecond
    earlier belongs to the previous one."""
    start = pod_local_day_start_utc(date(2026, 9, 3), _PACIFIC)
    assert pod_local_day_iso(start.isoformat(), _PACIFIC) == "2026-09-03"
    before = start.replace(microsecond=0) - datetime.resolution
    assert pod_local_day_iso(before.isoformat(), _PACIFIC) == "2026-09-02"

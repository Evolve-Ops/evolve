"""Tests for pod_time — pod-local TZ helpers for cap rollover."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pod_time import (
    _resolve_pod_tz,
    pod_iso_week,
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

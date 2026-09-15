"""Tests for pod_report.should_run — the schedule gate.

The launchd plist fires every hour at :00 (Minute=0, no Hour key). should_run
self-gates against `report_hour` from network.json so the user can change the
report time in the UI and it takes effect at the next hour boundary without
plist regeneration.

These tests pin:
  - disabled config returns (False, "")
  - hour-match returns (True, "Daily")
  - hour-mismatch returns (False, "")
  - frequency=weekdays skips Sat/Sun
  - frequency=weekly skips non-target days
  - default report_hour is 8
  - obsolete v1 keys (morning_enabled, etc.) are ignored
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_report  # noqa: E402


def _at(year, month, day, hour):
    """Patch datetime.now in pod_report to return the given local time."""
    fake = datetime(year, month, day, hour, 0, 0)

    class _Fake:
        @classmethod
        def now(cls, tz=None):
            return fake

    return patch.object(pod_report, "datetime", _Fake)


# ── Master switch ──


def test_disabled_returns_false():
    with _at(2026, 5, 7, 8):
        assert pod_report.should_run({"enabled": False}) == (False, "")


def test_missing_enabled_returns_false():
    """Missing 'enabled' defaults to False — never auto-on."""
    with _at(2026, 5, 7, 8):
        assert pod_report.should_run({}) == (False, "")


# ── Hour matching ──


def test_runs_at_configured_hour():
    cfg = {"enabled": True, "report_hour": 10}
    with _at(2026, 5, 7, 10):
        assert pod_report.should_run(cfg) == (True, "Daily")


def test_skips_other_hours():
    """Plist fires every hour at :00; should_run gates so we only run at the
    configured hour. This is the load-bearing v1→v2 change — v1 had two hardcoded
    times via separate plists; v2 has one plist that polls."""
    cfg = {"enabled": True, "report_hour": 10}
    for h in [0, 1, 5, 9, 11, 17, 23]:
        with _at(2026, 5, 7, h):
            assert pod_report.should_run(cfg) == (False, "")


def test_default_report_hour_is_8():
    """Backwards compat: missing report_hour defaults to 8 (matches v1's morning_hour default)."""
    cfg = {"enabled": True}
    with _at(2026, 5, 7, 8):
        assert pod_report.should_run(cfg) == (True, "Daily")
    with _at(2026, 5, 7, 18):
        assert pod_report.should_run(cfg) == (False, "")


# ── Frequency ──


def test_weekdays_skips_saturday():
    cfg = {"enabled": True, "report_hour": 8, "frequency": "weekdays"}
    # 2026-05-09 is a Saturday
    with _at(2026, 5, 9, 8):
        assert pod_report.should_run(cfg) == (False, "")


def test_weekdays_skips_sunday():
    cfg = {"enabled": True, "report_hour": 8, "frequency": "weekdays"}
    # 2026-05-10 is a Sunday
    with _at(2026, 5, 10, 8):
        assert pod_report.should_run(cfg) == (False, "")


def test_weekdays_runs_monday():
    cfg = {"enabled": True, "report_hour": 8, "frequency": "weekdays"}
    # 2026-05-11 is a Monday
    with _at(2026, 5, 11, 8):
        assert pod_report.should_run(cfg) == (True, "Daily")


def test_weekly_runs_only_on_target_day():
    # 2026-05-07 is a Thursday (weekday=3)
    cfg = {"enabled": True, "report_hour": 8, "frequency": "weekly", "weekly_day": 3}
    with _at(2026, 5, 7, 8):  # Thursday
        assert pod_report.should_run(cfg) == (True, "Daily")
    with _at(2026, 5, 6, 8):  # Wednesday
        assert pod_report.should_run(cfg) == (False, "")


# ── Obsolete v1 keys ignored ──


def test_v1_morning_keys_are_ignored():
    """A network.json with the old morning_/evening_ keys must not cause double
    runs or surprise behavior. The new code reads only report_hour."""
    cfg = {
        "enabled": True,
        "morning_enabled": True,    # obsolete
        "morning_hour": 8,           # obsolete
        "evening_enabled": True,     # obsolete
        "evening_hour": 18,          # obsolete
        "report_hour": 10,
    }
    # Must NOT fire at 8 (the obsolete morning_hour)
    with _at(2026, 5, 7, 8):
        assert pod_report.should_run(cfg) == (False, "")
    # Must NOT fire at 18 (the obsolete evening_hour)
    with _at(2026, 5, 7, 18):
        assert pod_report.should_run(cfg) == (False, "")
    # MUST fire at 10 (the new report_hour)
    with _at(2026, 5, 7, 10):
        assert pod_report.should_run(cfg) == (True, "Daily")

"""tests/test_oc_substrate_monitor.py — oc_substrate_monitor unit tests.

The monitor reads two files on disk and emits Signal specs when either
is missing or stale. Tests use tmp_path to write canned state and
exercise each branch of each detector without touching real
/Users/Shared paths.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_substrate_monitor as osm  # noqa: E402
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_macos_profile():
    """Most tests exercise the macOS detectors directly; pin the macOS
    profile so the ``collect()`` platform gate doesn't short-circuit on a
    Linux CI runner. The dedicated gate tests below override this with
    their own ``set_profile`` and so are unaffected (they restore on
    teardown)."""
    set_profile(MACOS)
    try:
        yield
    finally:
        set_profile(None)


# ── Updater detector ─────────────────────────────────────────────────────────


def _fresh_updater_state(now: datetime, version: str = "2026.5.27") -> dict:
    return {
        "last_check": now.isoformat(),
        "last_applied_version": version,
    }


def test_updater_fresh_returns_none(tmp_path):
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_fresh_updater_state(now)))
    assert osm.detect_updater_stale(state_file, now=now) is None


def test_updater_stale_fires_with_age_in_details(tmp_path):
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    last_check = now - timedelta(minutes=240)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_fresh_updater_state(last_check)))

    spec = osm.detect_updater_stale(state_file, now=now)

    assert spec is not None
    assert spec["type"] == osm.SIGNAL_TYPE_UPDATER_STALE
    assert spec["scope"] == "pod"
    assert spec["severity"] == "warn"
    assert spec["details"]["age_minutes"] == 240.0
    assert spec["details"]["threshold_minutes"] == 120
    assert spec["details"]["last_applied_version"] == "2026.5.27"
    assert "120 min" in spec["body"]


def test_updater_state_file_missing(tmp_path):
    spec = osm.detect_updater_stale(tmp_path / "absent.json")
    assert spec is not None
    assert spec["details"]["reason"] == "state_file_missing"
    assert "state file missing" in spec["title"].lower()


def test_updater_state_file_corrupt(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("not json {{{")
    spec = osm.detect_updater_stale(state_file)
    assert spec is not None
    assert spec["details"]["reason"] == "state_file_unreadable"


def test_updater_missing_last_check_field(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_applied_version": "2026.5.27"}))
    spec = osm.detect_updater_stale(state_file)
    assert spec is not None
    assert spec["details"]["reason"] == "missing_last_check"


def test_updater_malformed_last_check_iso(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_check": "yesterday-ish"}))
    spec = osm.detect_updater_stale(state_file)
    assert spec is not None
    assert spec["details"]["reason"] == "malformed_last_check"


def test_updater_threshold_boundary_inclusive(tmp_path):
    """Age exactly at threshold should NOT fire (≤ threshold passes)."""
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    at_threshold = now - timedelta(minutes=osm.UPDATER_MAX_AGE_MINUTES)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_fresh_updater_state(at_threshold)))
    assert osm.detect_updater_stale(state_file, now=now) is None


# ── Collector detector ──────────────────────────────────────────────────────


def _make_collector_file(usage_dir: Path, date_str: str, mtime_ts: float) -> Path:
    usage_dir.mkdir(parents=True, exist_ok=True)
    f = usage_dir / f"all-{date_str}.json"
    f.write_text("{}")
    import os
    os.utime(f, (mtime_ts, mtime_ts))
    return f


def test_collector_fresh_returns_none(tmp_path):
    # 19:00 UTC = 12pm Pacific (PDT) or 11am Pacific (PST) — safely past
    # the 9am grace hour regardless of DST.
    now = datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc)
    today = now.astimezone().strftime("%Y-%m-%d")
    _make_collector_file(tmp_path, today, now.timestamp() - 60)
    assert osm.detect_collector_stale(tmp_path, now=now) is None


def test_collector_stale_today_file(tmp_path):
    # 19:00 UTC = 12pm Pacific (PDT) or 11am Pacific (PST) — safely past
    # the 9am grace hour regardless of DST.
    now = datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc)
    today = now.astimezone().strftime("%Y-%m-%d")
    # 180 minutes stale — above the 120 threshold
    _make_collector_file(tmp_path, today, now.timestamp() - 180 * 60)

    spec = osm.detect_collector_stale(tmp_path, now=now)

    assert spec is not None
    assert spec["type"] == osm.SIGNAL_TYPE_COLLECTOR_STALE
    assert spec["scope"] == "pod"
    assert spec["details"]["reason"] == "today_file_stale"
    assert spec["details"]["age_minutes"] >= 179.0


def test_collector_today_file_missing_after_grace_hour(tmp_path):
    # 19:00 UTC = 12pm Pacific (PDT) or 11am Pacific (PST) — safely past
    # the 9am grace hour regardless of DST.
    now = datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc)
    # Make a stale prior-day file so the "latest file" branch is exercised.
    _make_collector_file(
        tmp_path, "2026-05-20", now.timestamp() - 12 * 24 * 3600,
    )
    spec = osm.detect_collector_stale(tmp_path, now=now)
    assert spec is not None
    assert spec["details"]["reason"] == "today_file_missing"
    assert spec["details"]["latest_file"] == "all-2026-05-20.json"


def test_collector_today_file_missing_before_grace_hour_is_quiet(tmp_path):
    # 5am local — before the grace hour. Missing-today should NOT fire.
    local_5am = datetime(2026, 6, 1, 5, 0).astimezone()
    spec = osm.detect_collector_stale(tmp_path, now=local_5am.astimezone(timezone.utc))
    assert spec is None


def test_collector_no_files_at_all_after_grace_hour(tmp_path):
    # 19:00 UTC = 12pm Pacific (PDT) or 11am Pacific (PST) — safely past
    # the 9am grace hour regardless of DST.
    now = datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc)
    spec = osm.detect_collector_stale(tmp_path, now=now)
    assert spec is not None
    assert spec["details"]["reason"] == "today_file_missing"
    assert spec["details"]["latest_file"] is None


# ── Runner integration ─────────────────────────────────────────────────────


def test_collect_returns_specs_for_each_failing_detector(monkeypatch, tmp_path):
    # Point both module paths at empty tmp dirs so both detectors fire.
    fake_state = tmp_path / "state.json"  # missing → fires
    fake_usage = tmp_path / "usage"        # missing → fires (after grace hr)
    monkeypatch.setattr(osm, "UPDATER_STATE_PATH", fake_state)
    monkeypatch.setattr(osm, "USAGE_COLLECTOR_DIR", fake_usage)

    # Freeze "now" at 7pm UTC (12pm Pacific) so the collector grace
    # check passes regardless of DST.
    real_dt = osm.datetime

    class FrozenDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 6, 1, 19, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(osm, "datetime", FrozenDT)

    specs = osm.collect()
    types = {s["type"] for s in specs}
    assert osm.SIGNAL_TYPE_UPDATER_STALE in types
    assert osm.SIGNAL_TYPE_COLLECTOR_STALE in types


def test_signatures_are_stable_across_runs(tmp_path):
    """Signatures must hash deterministically so observe() dedupes correctly."""
    spec1 = osm._spec(
        sig_type=osm.SIGNAL_TYPE_UPDATER_STALE,
        title="t",
        body="b",
        details={"x": 1},
    )
    spec2 = osm._spec(
        sig_type=osm.SIGNAL_TYPE_UPDATER_STALE,
        title="DIFFERENT TITLE",
        body="DIFFERENT BODY",
        details={"x": 999},
    )
    assert spec1["signature"] == spec2["signature"]


# ── Platform gate ──────────────────────────────────────────────────────────


def test_collect_is_noop_on_linux(monkeypatch, tmp_path):
    """On Linux the watched LaunchAgent state files are absent BY DESIGN
    (no systemd writer exists), so both detectors WOULD fire forever.
    The macOS gate in collect() makes it a clean no-op instead — this is
    the fresh-Linux-pod false-fire that PR closes. Both module paths point
    at missing files (which fire on macOS), proving the gate, not the
    files, is what suppresses Linux."""
    monkeypatch.setattr(osm, "UPDATER_STATE_PATH", tmp_path / "absent-state.json")
    monkeypatch.setattr(osm, "USAGE_COLLECTOR_DIR", tmp_path / "absent-usage")

    set_profile(LINUX)
    try:
        assert osm.collect() == []
    finally:
        set_profile(MACOS)  # autouse fixture restores to None on teardown


def test_collect_still_fires_on_macos(monkeypatch, tmp_path):
    """macOS behavior is byte-identical: with both source files missing,
    both detectors fire. This is the same assertion as
    test_collect_returns_specs_for_each_failing_detector, restated against
    an explicit macOS pin to pair with the Linux no-op above."""
    monkeypatch.setattr(osm, "UPDATER_STATE_PATH", tmp_path / "absent-state.json")
    monkeypatch.setattr(osm, "USAGE_COLLECTOR_DIR", tmp_path / "absent-usage")

    real_dt = osm.datetime

    class FrozenDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 6, 1, 19, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(osm, "datetime", FrozenDT)

    set_profile(MACOS)
    try:
        types = {s["type"] for s in osm.collect()}
        assert osm.SIGNAL_TYPE_UPDATER_STALE in types
        assert osm.SIGNAL_TYPE_COLLECTOR_STALE in types
    finally:
        set_profile(None)


def test_run_writes_no_signals_on_linux(monkeypatch, tmp_path):
    """End-to-end: run() on Linux fires nothing (and the producer is
    silent — no observe() calls), so a fresh Linux pod never sees the
    Signal at all."""
    monkeypatch.setattr(osm, "UPDATER_STATE_PATH", tmp_path / "absent-state.json")
    monkeypatch.setattr(osm, "USAGE_COLLECTOR_DIR", tmp_path / "absent-usage")

    observed: list = []
    monkeypatch.setattr(
        osm.signals_store, "observe",
        lambda *a, **k: observed.append((a, k)),
    )
    monkeypatch.setattr(
        osm.signals_store, "sweep_resolve",
        lambda *a, **k: [],
    )

    set_profile(LINUX)
    try:
        kept, n_fired, n_resolved = osm.run(tmp_path, dry_run=False)
        assert n_fired == 0
        assert kept == set()
        assert observed == []
    finally:
        set_profile(MACOS)

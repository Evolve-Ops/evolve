"""Tests for pod_report_baseline — the trailing-window stats helper that
replaces the v1 fixed-$ threshold ladder.

Pins three load-bearing behaviors:
  - end_date is exclusive (today's value isn't part of today's baseline)
  - missing days are skipped, not zeroed
  - is_cold flips at 14 days
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from pod_report_baseline import (  # noqa: E402
    Baseline,
    baseline_for_bot,
    is_high_anomaly,
    is_low_anomaly,
    ratio,
)


def _write(shared_dir: Path, bot_id: str, d: date, **fields):
    """Write a per-bot metrics file with the given fields."""
    folder = shared_dir / "metrics" / d.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 2, "bot_id": bot_id, "date": d.isoformat(), **fields}
    (folder / f"{bot_id}.json").write_text(json.dumps(payload))


# ── baseline_for_bot ──


def test_baseline_zero_when_no_data(tmp_path: Path):
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, date(2026, 5, 7))
    assert b == Baseline(metric="session_count", bot_id="team_bot_b",
                         mean=0.0, stddev=0.0, n=0)
    assert b.is_cold is True


def test_baseline_excludes_end_date(tmp_path: Path):
    """The day being judged must not be in its own baseline."""
    _write(tmp_path, "team_bot_b", date(2026, 5, 7), session_count=999)
    _write(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=10)
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, date(2026, 5, 7))
    assert b.n == 1
    assert b.mean == 10.0  # 999 was excluded


def test_baseline_skips_missing_days(tmp_path: Path):
    """A bot that didn't run on day X shouldn't count as zero on day X."""
    end = date(2026, 5, 7)
    _write(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=10)
    _write(tmp_path, "team_bot_b", date(2026, 5, 4), session_count=20)
    # 5/5 missing entirely
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert b.n == 2
    assert b.mean == 15.0  # not (10+0+20)/3


def test_baseline_uses_window_days(tmp_path: Path):
    end = date(2026, 5, 7)
    # Day 1 ago and day 31 ago — only the first should be included
    _write(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=10)
    _write(tmp_path, "team_bot_b", date(2026, 4, 6), session_count=999)
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, end, window_days=30)
    assert b.n == 1
    assert b.mean == 10.0


def test_baseline_stddev(tmp_path: Path):
    end = date(2026, 5, 7)
    for offset, v in enumerate([10, 20, 30], start=1):
        _write(tmp_path, "team_bot_b", date(2026, 5, 7 - offset), session_count=v)
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert b.n == 3
    assert b.mean == 20.0
    # population stddev for [10,20,30] = sqrt((100+0+100)/3) ≈ 8.165
    assert abs(b.stddev - 8.165) < 0.01


def test_is_cold_flips_at_14_days(tmp_path: Path):
    end = date(2026, 5, 30)
    for offset in range(1, 14):
        _write(tmp_path, "team_bot_b", date(2026, 5, 30 - offset), session_count=1)
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert b.n == 13
    assert b.is_cold is True
    _write(tmp_path, "team_bot_b", date(2026, 5, 16), session_count=1)
    b2 = baseline_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert b2.n == 14
    assert b2.is_cold is False


def test_baseline_handles_malformed_files_silently(tmp_path: Path):
    end = date(2026, 5, 7)
    _write(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=10)
    bad_dir = tmp_path / "metrics" / "2026-05-05"
    bad_dir.mkdir()
    (bad_dir / "team_bot_b.json").write_text("{not valid json")
    b = baseline_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert b.n == 1


# ── is_high_anomaly / is_low_anomaly ──


def test_high_anomaly_clean_case():
    # baseline mean=10, stddev=2, current=30 → 3× and well above mean+2σ
    b = Baseline("session_count", "team_bot_b", mean=10.0, stddev=2.0, n=20)
    assert is_high_anomaly(30, b, factor=2.0) is True
    assert is_high_anomaly(15, b, factor=2.0) is False  # < 2× mean


def test_high_anomaly_sigma_guard_suppresses_noisy_metric():
    """A high-variance metric where 3× mean is still inside ±2σ."""
    # baseline mean=10, stddev=20: mean+2σ=50; 25 is 2.5× but inside σ band
    b = Baseline("session_count", "team_bot_b", mean=10.0, stddev=20.0, n=20)
    assert is_high_anomaly(25, b, factor=2.0) is False
    # Larger value escapes both guards
    assert is_high_anomaly(60, b, factor=2.0) is True


def test_high_anomaly_min_mean_guard():
    """Quiet bot whose mean session count is < min_mean shouldn't trend."""
    b = Baseline("session_count", "personal_bot", mean=1.5, stddev=0.5, n=20)
    # 5 sessions = 3.3× mean and outside σ — but base too small
    assert is_high_anomaly(5, b, factor=2.0, min_mean=3.0) is False


def test_low_anomaly_clean_case():
    """Sessions dropped from 30 avg to 2."""
    b = Baseline("session_count", "team_bot_b", mean=30.0, stddev=5.0, n=20)
    assert is_low_anomaly(2, b, factor=0.3) is True
    assert is_low_anomaly(12, b, factor=0.3) is False  # > 0.3× mean


def test_low_anomaly_sigma_guard():
    """If quiet days are common (high σ), a single quiet day isn't anomalous."""
    b = Baseline("session_count", "team_bot_b", mean=30.0, stddev=20.0, n=20)
    # mean - 2σ = -10 → clipped to 0 → any positive value is above the guard
    assert is_low_anomaly(2, b, factor=0.3) is False


def test_low_anomaly_min_mean_guard():
    b = Baseline("session_count", "personal_bot", mean=1.5, stddev=0.5, n=20)
    assert is_low_anomaly(0, b, factor=0.3, min_mean=3.0) is False


def test_zero_baseline_never_anomaly():
    """Empty baseline (n=0) — trending must not fire on cold start."""
    b = Baseline("session_count", "newbot", mean=0.0, stddev=0.0, n=0)
    assert is_high_anomaly(100, b) is False
    assert is_low_anomaly(0, b) is False


def test_ratio_safe_on_zero_mean():
    b = Baseline("session_count", "team_bot_b", mean=0.0, stddev=0.0, n=5)
    assert ratio(50, b) == 0.0
    b2 = Baseline("session_count", "team_bot_b", mean=10.0, stddev=2.0, n=20)
    assert ratio(25, b2) == 2.5

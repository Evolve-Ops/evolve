"""Tests for ``spend_alert.emit_velocity_forecast_signal`` and its helper.

The velocity-forecast Signal is the intraday projection-vs-cap surface that
fires *before* the L1 cost breaker trip — the gap the 2026-06-04 security-bot
incident surfaced. Existing detectors fire on actuals or rolling windows;
this one extrapolates today's rate to midnight.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import spend_alert  # noqa: E402


# ── _projected_full_day_spend ───────────────────────────────────────────────


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 4, hour, minute, 0, tzinfo=timezone.utc)


def test_projection_at_noon_doubles_spend() -> None:
    projected, elapsed = spend_alert._projected_full_day_spend(2.50, _at(12))
    assert projected == pytest.approx(5.00)
    assert elapsed == pytest.approx(12.0)


def test_projection_floors_elapsed_at_one_hour_to_avoid_runaway_extrapolation() -> None:
    # At 00:30, true elapsed = 0.5h → 24/0.5 = 48× would multiply a $0.50
    # spend to $24, which is misleading-noisy for a sleepy hour.
    projected, elapsed = spend_alert._projected_full_day_spend(0.50, _at(0, 30))
    assert elapsed == pytest.approx(1.0)
    assert projected == pytest.approx(12.0)


def test_projection_at_midnight_treats_as_one_hour_elapsed() -> None:
    # At 23:59, spend should project close to itself (not blow up).
    projected, _ = spend_alert._projected_full_day_spend(10.0, _at(23, 59))
    # 24 / 23.98 ≈ 1.001 → projected ≈ $10.01
    assert projected == pytest.approx(10.01, abs=0.05)


# ── emit_velocity_forecast_signal ──────────────────────────────────────────
#
# We don't need a real signals_store backend for the boundary tests —
# patching emit_velocity_forecast_signal's signals_store import surface
# would couple too tightly. Instead we run against a real shared_dir
# (tmp_path) and observe whether a Signal was written.


def _shared(tmp_path: Path) -> Path:
    (tmp_path / "signals" / "firing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "signals" / "archived").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _firing_count(shared: Path) -> int:
    return len(list((shared / "signals" / "firing").glob("*.json")))


def test_velocity_forecast_fires_at_70pct_projection(tmp_path: Path) -> None:
    shared = _shared(tmp_path)
    # At noon, spent $1.80 → projects to $3.60. Cap $5 → 72% of cap → fire.
    sig_id = spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared,
        bot_id="security-bot",
        spend_usd=1.80,
        cap_usd=5.00,
        warn_fraction=0.70,
        today=date(2026, 6, 4),
        now=_at(12),
    )
    assert sig_id is not None
    assert _firing_count(shared) == 1


def test_velocity_forecast_silent_below_warn_fraction(tmp_path: Path) -> None:
    shared = _shared(tmp_path)
    # At noon, spent $0.50 → projects to $1.00. Cap $5 → 20% < 70% → silent.
    sig_id = spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared,
        bot_id="security-bot",
        spend_usd=0.50,
        cap_usd=5.00,
        warn_fraction=0.70,
        today=date(2026, 6, 4),
        now=_at(12),
    )
    assert sig_id is None
    assert _firing_count(shared) == 0


def test_velocity_forecast_silent_when_cap_already_crossed(tmp_path: Path) -> None:
    """Once actuals cross the cap, the breaker path takes over — no need to
    fire the projection Signal redundantly."""
    shared = _shared(tmp_path)
    sig_id = spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared,
        bot_id="security-bot",
        spend_usd=6.00,  # already over cap
        cap_usd=5.00,
        warn_fraction=0.70,
        today=date(2026, 6, 4),
        now=_at(12),
    )
    assert sig_id is None


def test_velocity_forecast_silent_when_cap_unset(tmp_path: Path) -> None:
    shared = _shared(tmp_path)
    sig_id = spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared,
        bot_id="security-bot",
        spend_usd=2.00,
        cap_usd=0.0,
        warn_fraction=0.70,
        today=date(2026, 6, 4),
        now=_at(12),
    )
    assert sig_id is None


def test_velocity_forecast_severity_alert_at_or_above_full_cap(tmp_path: Path) -> None:
    """When projection hits 100% of cap, severity escalates warn → alert.
    Cap not yet crossed (actuals below) so the signal still fires."""
    shared = _shared(tmp_path)
    # At noon, spent $3.00 → projects to $6.00. Cap $5 → 120% → alert.
    sig_id = spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared,
        bot_id="security-bot",
        spend_usd=3.00,
        cap_usd=5.00,
        warn_fraction=0.70,
        today=date(2026, 6, 4),
        now=_at(12),
    )
    assert sig_id is not None
    # Read the written Signal and inspect severity
    files = list((shared / "signals" / "firing").glob("*.json"))
    assert len(files) == 1
    import json
    payload = json.loads(files[0].read_text())
    assert payload["severity"] == "alert"
    assert payload["details"]["cap_fraction"] >= 1.0


def test_velocity_forecast_dedups_within_same_day(tmp_path: Path) -> None:
    """Second tick on the same (bot, day) updates the existing Signal rather
    than creating a duplicate."""
    shared = _shared(tmp_path)
    spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared, bot_id="security-bot", spend_usd=1.80, cap_usd=5.00,
        warn_fraction=0.70, today=date(2026, 6, 4), now=_at(12),
    )
    spend_alert.emit_velocity_forecast_signal(
        shared_dir=shared, bot_id="security-bot", spend_usd=2.40, cap_usd=5.00,
        warn_fraction=0.70, today=date(2026, 6, 4), now=_at(14),
    )
    # Same signature → one file
    assert _firing_count(shared) == 1

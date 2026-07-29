"""Tests for the cap-status helpers used by the Cost Measures chips:
``get_today_spend``, ``get_cap_warnings``, and
``get_recent_enforcement_history``.

The existing ``write_enforcement_flag`` and ``get_active_enforcement``
helpers in spend_caps.py are exercised indirectly here as setup, but the
focus is on the three new helpers added for the dashboard chip system.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import spend_caps as sc


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_metric(shared: Path, bot: str, d: date, total_cost: float) -> None:
    p = shared / "metrics" / d.isoformat() / f"{bot}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": 2,
        "bot_id": bot,
        "date": d.isoformat(),
        "total_cost_estimated": total_cost,
    }))


# ── get_today_spend ───────────────────────────────────────────────────────────


def test_get_today_spend_reads_metrics_file(tmp_path):
    today = date(2026, 5, 6)
    _write_metric(tmp_path, "team_bot_a", today, 3.50)
    assert sc.get_today_spend(tmp_path, "team_bot_a", today) == pytest.approx(3.50)


def test_get_today_spend_returns_none_when_missing(tmp_path):
    """No metrics file = "no data yet for today", not $0 spend."""
    assert sc.get_today_spend(tmp_path, "ghost", date(2026, 5, 6)) is None


# ── get_cap_warnings ──────────────────────────────────────────────────────────


def test_get_cap_warnings_fires_at_threshold(tmp_path):
    today = date(2026, 5, 6)
    _write_metric(tmp_path, "team_bot_a", today, 4.50)    # 90% of $5 cap
    _write_metric(tmp_path, "admin_bot", today, 2.00)  # 40% of $5 cap
    out = sc.get_cap_warnings(tmp_path, ["team_bot_a", "admin_bot"], cap=5.0, today=today)
    assert "team_bot_a" in out
    assert "admin_bot" not in out
    assert out["team_bot_a"]["pct"] == pytest.approx(0.90)


def test_get_cap_warnings_excludes_already_enforced(tmp_path):
    """When a bot's already in active_enforcement, don't double-surface it."""
    today = date(2026, 5, 6)
    _write_metric(tmp_path, "team_bot_a", today, 4.80)
    sc.write_enforcement_flag(
        tmp_path, "team_bot_a", action="downgrade-tier",
        spend_at_trigger=4.80, cap=5.0, today=today,
    )
    out = sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=5.0, today=today)
    assert out == {}


def test_get_cap_warnings_returns_empty_when_no_cap(tmp_path):
    """No cap configured → no warnings to fire."""
    today = date(2026, 5, 6)
    _write_metric(tmp_path, "team_bot_a", today, 100.0)
    assert sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=None, today=today) == {}
    assert sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=0, today=today) == {}


def test_get_cap_warnings_skips_bots_with_no_metrics(tmp_path):
    """A bot with no metrics file shouldn't crash or appear in the result."""
    today = date(2026, 5, 6)
    out = sc.get_cap_warnings(tmp_path, ["ghost"], cap=5.0, today=today)
    assert out == {}


def test_get_cap_warnings_threshold_pct_overridable(tmp_path):
    today = date(2026, 5, 6)
    _write_metric(tmp_path, "team_bot_a", today, 3.00)   # 60% of $5
    # Default 80% threshold → no warning.
    assert sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=5.0, today=today) == {}
    # Lower threshold → warning fires.
    out = sc.get_cap_warnings(
        tmp_path, ["team_bot_a"], cap=5.0, threshold_pct=0.50, today=today
    )
    assert "team_bot_a" in out


# ── get_recent_enforcement_history ────────────────────────────────────────────


def test_recent_history_counts_old_flags(tmp_path):
    today = date(2026, 5, 6)
    # Trip on day -3 and day -5
    for i in (3, 5):
        d = today - timedelta(days=i)
        sc.write_enforcement_flag(
            tmp_path, "team_bot_a", action="downgrade-tier",
            spend_at_trigger=10.0, cap=5.0, today=d,
        )
    out = sc.get_recent_enforcement_history(
        tmp_path, ["team_bot_a"], days=7, today=today
    )
    assert out == {"team_bot_a": 2}


def test_recent_history_excludes_active_today(tmp_path):
    """Today's active flag is shown by cap_active; don't double-count."""
    today = date(2026, 5, 6)
    sc.write_enforcement_flag(
        tmp_path, "team_bot_a", action="downgrade-tier",
        spend_at_trigger=10.0, cap=5.0, today=today,
    )
    out = sc.get_recent_enforcement_history(
        tmp_path, ["team_bot_a"], days=7, today=today
    )
    assert out == {}  # only flag is today's active one


def test_recent_history_includes_today_when_cleared(tmp_path):
    """Today's flag still counts toward history if it was cleared
    (the bot tripped the cap, even if it's no longer in force)."""
    today = date(2026, 5, 6)
    sc.write_enforcement_flag(
        tmp_path, "team_bot_a", action="downgrade-tier",
        spend_at_trigger=10.0, cap=5.0, today=today,
    )
    sc.clear_enforcement(tmp_path, "team_bot_a", today=today)
    out = sc.get_recent_enforcement_history(
        tmp_path, ["team_bot_a"], days=7, today=today
    )
    # Cleared flag is no longer "active" → not skipped → counted.
    assert out == {"team_bot_a": 1}


def test_recent_history_window_respected(tmp_path):
    """Flags older than `days` shouldn't be counted."""
    today = date(2026, 5, 6)
    # Trip 10 days ago — outside the 7d window.
    sc.write_enforcement_flag(
        tmp_path, "team_bot_a", action="downgrade-tier",
        spend_at_trigger=10.0, cap=5.0, today=today - timedelta(days=10),
    )
    out = sc.get_recent_enforcement_history(
        tmp_path, ["team_bot_a"], days=7, today=today
    )
    assert out == {}


def test_recent_history_skips_bots_with_no_trips(tmp_path):
    today = date(2026, 5, 6)
    out = sc.get_recent_enforcement_history(
        tmp_path, ["ghost"], days=7, today=today
    )
    assert out == {}


# ── get_active_breakers ───────────────────────────────────────────────────────


def _write_breaker(shared: Path, bot: str, *, tripped_at: str | None,
                   cleared_at: str | None = None, reason: str = "",
                   expires_at: str | None = None) -> None:
    p = shared / "breakers" / bot / "cost.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if tripped_at is not None:
        payload["tripped_at"] = tripped_at
    if cleared_at is not None:
        payload["cleared_at"] = cleared_at
    if reason:
        payload["reason"] = reason
    if expires_at is not None:
        payload["expires_at"] = expires_at
    p.write_text(json.dumps(payload))


def test_get_active_breakers_returns_tripped_bots(tmp_path):
    _write_breaker(tmp_path, "team_bot_a",
                   tripped_at="2026-06-01T10:00:00Z",
                   reason="daily_cap_usd exceeded")
    out = sc.get_active_breakers(tmp_path, ["team_bot_a", "admin_bot"])
    assert "team_bot_a" in out
    assert out["team_bot_a"]["tripped_at"] == "2026-06-01T10:00:00Z"
    assert out["team_bot_a"]["reason"] == "daily_cap_usd exceeded"
    # No file for admin_bot — should not appear.
    assert "admin_bot" not in out


def test_get_active_breakers_skips_cleared_breakers(tmp_path):
    """A breaker with cleared_at >= tripped_at is no longer active —
    that's the exact same invariant detect_breaker_tripped_chip enforces
    in cost_opt_tiles.py. Without this skip the summary band and the
    tile chip would disagree on resolved trips."""
    _write_breaker(tmp_path, "team_bot_a",
                   tripped_at="2026-06-01T10:00:00Z",
                   cleared_at="2026-06-01T11:00:00Z")
    out = sc.get_active_breakers(tmp_path, ["team_bot_a"])
    assert out == {}


def test_get_active_breakers_handles_re_trip_after_clear(tmp_path):
    """A re-trip writes a later tripped_at than the stored cleared_at;
    that bot is active again."""
    _write_breaker(tmp_path, "team_bot_a",
                   tripped_at="2026-06-01T12:00:00Z",
                   cleared_at="2026-06-01T11:00:00Z")
    out = sc.get_active_breakers(tmp_path, ["team_bot_a"])
    assert "team_bot_a" in out


def test_get_active_breakers_tolerates_missing_dir(tmp_path):
    """No breakers/ dir on a brand-new pod should yield an empty dict,
    not a crash."""
    assert sc.get_active_breakers(tmp_path, ["a", "b"]) == {}


def test_get_active_breakers_tolerates_malformed_json(tmp_path):
    p = tmp_path / "breakers" / "team_bot_a" / "cost.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json {")
    assert sc.get_active_breakers(tmp_path, ["team_bot_a"]) == {}


def test_get_active_breakers_skips_expired_ttl_trips(tmp_path):
    """An L1 breaker that auto-resets via expires_at TTL must drop out
    of the summary band as soon as the TTL passes, even if the file is
    still on disk (reaper hasn't swept it yet). Pre-fix this was the
    "2 breakers tripped" header chip + summary count lingering after
    the per-bot tile chips had already cleared — surfaced 2026-06-06."""
    # Trip 25h ago, TTL was 24h → now expired.
    past_trip = "2026-06-05T10:00:00+00:00"
    past_expiry = "2026-06-06T10:00:00+00:00"  # before "now"
    _write_breaker(
        tmp_path, "team_bot_a",
        tripped_at=past_trip,
        expires_at=past_expiry,
    )
    out = sc.get_active_breakers(tmp_path, ["team_bot_a"])
    assert out == {}, (
        f"expected expired TTL trip to be filtered out, got {out!r}"
    )


def test_get_active_breakers_keeps_indefinite_trips(tmp_path):
    """A breaker with no expires_at (manual trip, indefinite) stays
    active. Only TTL-expired records drop."""
    _write_breaker(
        tmp_path, "team_bot_a",
        tripped_at="2026-06-01T10:00:00Z",
        # expires_at omitted → indefinite
    )
    out = sc.get_active_breakers(tmp_path, ["team_bot_a"])
    assert "team_bot_a" in out


def test_get_active_breakers_keeps_future_expiry_trips(tmp_path):
    """A breaker tripped just now with a TTL in the future is still
    active — the operator needs to see the count rise."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
    _write_breaker(
        tmp_path, "team_bot_a",
        tripped_at=datetime.now(timezone.utc).isoformat(),
        expires_at=future,
    )
    out = sc.get_active_breakers(tmp_path, ["team_bot_a"])
    assert "team_bot_a" in out

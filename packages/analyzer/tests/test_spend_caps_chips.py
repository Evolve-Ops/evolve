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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import spend_caps as sc
import live_spend


# ── Helpers ───────────────────────────────────────────────────────────────────
#
# These used to stage ``metrics/<date>/<bot>.json`` files, because that is
# what ``get_today_spend`` read. It no longer does: measure.py writes that
# file at 01:00 pod-local for the day it NAMES and never regenerates it, so
# it holds roughly the first hour of the day — 3.8% of the pod's real spend
# on the day this was measured. The chip now reads live turn JSONL, so the
# fixtures stage turns.

UTC = timezone.utc


def _turn(ts: str, cost: float) -> dict:
    return {
        "ts": ts,
        "cost": cost,
        "source": "human",
        "channel": "telegram",
        "model": "anthropic/claude-sonnet-4-6",
    }


@pytest.fixture
def utc_pod(monkeypatch):
    """Pin the pod TZ to UTC so a bare ``YYYY-MM-DDT12:00:00Z`` lands on that
    date's bucket regardless of where the test host is."""
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: UTC)


@pytest.fixture
def spend(monkeypatch, utc_pod):
    """Give bots a midday spend on ``today``; unlisted bots have no turns."""
    def _install(per_bot: dict[str, float], d: date):
        turns = {
            bot: [_turn(f"{d.isoformat()}T12:00:00Z", usd)]
            for bot, usd in per_bot.items()
        }
        monkeypatch.setattr(
            live_spend, "load_live_turns",
            lambda bot_id, **kw: turns.get(bot_id, []),
        )
    return _install


def _at_noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)


# ── get_today_spend ───────────────────────────────────────────────────────────


def test_get_today_spend_reads_live_turns(tmp_path, spend):
    today = date(2026, 5, 6)
    spend({"team_bot_a": 3.50}, today)
    assert sc.get_today_spend(tmp_path, "team_bot_a", today) == pytest.approx(3.50)


def test_get_today_spend_is_zero_for_a_bot_with_no_turns(tmp_path, spend):
    """An idle bot spent $0.00 — a real measurement, distinct from None."""
    today = date(2026, 5, 6)
    spend({}, today)
    assert sc.get_today_spend(tmp_path, "ghost", today) == pytest.approx(0.0)


def test_get_today_spend_returns_none_when_discovery_fails(tmp_path, monkeypatch):
    """"I could not look" must not read as $0 spend — that is the silent zero
    the whole cap path keeps relapsing into."""
    monkeypatch.setattr(
        live_spend, "load_live_turns", lambda *a, **kw: live_spend.LIVE_LOAD_FAILED,
    )
    assert sc.get_today_spend(tmp_path, "team_bot_a", date(2026, 5, 6)) is None


def test_a_stale_metrics_file_no_longer_moves_the_answer(tmp_path, spend):
    """The regression pin: the near-blind rollup must not be consulted.

    ``metrics/<date>/<bot>.json`` is staged claiming $999. If the chip still
    reports the live figure, the old reader is genuinely gone.
    """
    today = date(2026, 5, 6)
    p = tmp_path / "metrics" / today.isoformat() / "team_bot_a.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"total_cost_estimated": 999.0}))

    spend({"team_bot_a": 3.50}, today)
    assert sc.get_today_spend(tmp_path, "team_bot_a", today) == pytest.approx(3.50)
    assert sc.get_cap_warnings(
        tmp_path, ["team_bot_a"], cap=5.0, today=today,
    ) == {}   # $3.50 of a $5 cap is 70% — under the 80% threshold


# ── get_cap_warnings ──────────────────────────────────────────────────────────


def test_get_cap_warnings_fires_at_threshold(tmp_path, spend):
    today = date(2026, 5, 6)
    spend({"team_bot_a": 4.50,   # 90% of $5 cap
           "admin_bot": 2.00},   # 40% of $5 cap
          today)
    out = sc.get_cap_warnings(tmp_path, ["team_bot_a", "admin_bot"], cap=5.0, today=today)
    assert "team_bot_a" in out
    assert "admin_bot" not in out
    assert out["team_bot_a"]["pct"] == pytest.approx(0.90)


def test_get_cap_warnings_excludes_already_enforced(tmp_path, spend):
    """When a bot's already in active_enforcement, don't double-surface it."""
    today = date(2026, 5, 6)
    spend({"team_bot_a": 4.80}, today)
    sc.write_enforcement_flag(
        tmp_path, "team_bot_a", action="downgrade-tier",
        spend_at_trigger=4.80, cap=5.0, today=today,
    )
    out = sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=5.0, today=today)
    assert out == {}


def test_get_cap_warnings_returns_empty_when_no_cap(tmp_path, spend):
    """No cap configured → no warnings to fire."""
    today = date(2026, 5, 6)
    spend({"team_bot_a": 100.0}, today)
    assert sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=None, today=today) == {}
    assert sc.get_cap_warnings(tmp_path, ["team_bot_a"], cap=0, today=today) == {}


def test_get_cap_warnings_skips_bots_with_no_turns(tmp_path, spend):
    """A bot with no turns shouldn't crash or appear in the result."""
    today = date(2026, 5, 6)
    spend({}, today)
    out = sc.get_cap_warnings(tmp_path, ["ghost"], cap=5.0, today=today)
    assert out == {}


def test_get_cap_warnings_threshold_pct_overridable(tmp_path, spend):
    today = date(2026, 5, 6)
    spend({"team_bot_a": 3.00}, today)   # 60% of $5
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

"""Tests for breakers.baseline — rolling per-bot baseline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from breakers.baseline import (
    DEFAULT_BASELINE_DAYS,
    MIN_BASELINE_DAYS,
    Baseline,
    compute_baseline,
)


def _ts(days_ago: float, *, as_of: datetime) -> str:
    """Helper: ISO ts for a turn N days before as_of."""
    return (as_of - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _heartbeat_turn(days_ago: float, *, as_of: datetime, model: str = "anthropic/claude-haiku-4-5") -> dict:
    return {
        "ts": _ts(days_ago, as_of=as_of),
        "source": "heartbeat",
        "channel": "heartbeat",
        "model": model,
    }


def _human_turn(days_ago: float, *, as_of: datetime, channel: str = "telegram") -> dict:
    return {
        "ts": _ts(days_ago, as_of=as_of),
        "source": "human",
        "channel": channel,
        "model": "anthropic/claude-sonnet-4-6",
    }


class TestComputeBaseline:
    def test_empty_turns_returns_cold_start_with_zero_rates(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        b = compute_baseline(bot_id="team_bot_a", turns=[], as_of=as_of)
        assert b.cold_start is True
        assert b.auto_rate_per_hr == 0.0
        assert b.human_rate_per_hr == 0.0
        assert b.auto_high_tier_share == 0.0
        assert b.auto_turns == 0
        assert b.days_with_data == 0

    def test_warm_baseline_with_hourly_heartbeat(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        # 7 days × 24 heartbeats/day = 168 turns, all haiku.
        # Excluding the most recent 1h means we lose at most 1 turn,
        # so ~167 over (7d - 1h) ≈ 167 hours = ~1.0 turn/hr.
        turns = [
            _heartbeat_turn(days_ago=d + h / 24.0, as_of=as_of)
            for d in range(7)
            for h in range(24)
        ]
        b = compute_baseline(bot_id="security_bot", turns=turns, as_of=as_of)
        assert b.cold_start is False
        assert b.days_with_data >= MIN_BASELINE_DAYS
        # Auto rate near 1/hr (allow tolerance for window edge effects).
        assert 0.9 <= b.auto_rate_per_hr <= 1.1
        assert b.human_rate_per_hr == 0.0
        # All-haiku baseline → 0% high tier.
        assert b.auto_high_tier_share == 0.0

    def test_cold_start_when_under_three_days(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        # Only 2 days of data.
        turns = [
            _heartbeat_turn(days_ago=d + h / 24.0, as_of=as_of)
            for d in range(2)
            for h in range(24)
        ]
        b = compute_baseline(bot_id="personal_bot", turns=turns, as_of=as_of)
        assert b.cold_start is True
        assert b.days_with_data == 2

    def test_high_tier_share_when_mixed_models(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        # 3 days, 4 turns/day: 1 sonnet (high) + 3 haiku (low).
        turns = []
        for d in range(3):
            turns.append(_heartbeat_turn(
                days_ago=d + 0.1, as_of=as_of,
                model="anthropic/claude-sonnet-4-6",
            ))
            for k in range(3):
                turns.append(_heartbeat_turn(
                    days_ago=d + 0.2 + k * 0.1, as_of=as_of,
                ))
        b = compute_baseline(bot_id="team_bot_a", turns=turns, as_of=as_of)
        assert b.cold_start is False
        assert b.auto_high_tier_share == pytest.approx(0.25)

    def test_human_turns_counted_separately(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        turns = [
            _human_turn(days_ago=d + 0.5, as_of=as_of)
            for d in range(4)
        ]
        b = compute_baseline(bot_id="admin_bot", turns=turns, as_of=as_of)
        assert b.auto_turns == 0
        assert b.human_turns == 4
        assert b.human_rate_per_hr > 0
        assert b.auto_rate_per_hr == 0.0

    def test_window_excludes_recent_hours(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        # A turn 30 minutes before as_of — should be EXCLUDED by the
        # default 1h recent-hours buffer.
        turns = [_heartbeat_turn(days_ago=0.5 / 24.0, as_of=as_of)]
        b = compute_baseline(bot_id="team_bot_a", turns=turns, as_of=as_of)
        assert b.auto_turns == 0

    def test_window_excludes_old_turns(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        # A turn 10 days ago — outside the 7-day window.
        turns = [_heartbeat_turn(days_ago=10, as_of=as_of)]
        b = compute_baseline(bot_id="team_bot_a", turns=turns, as_of=as_of)
        assert b.auto_turns == 0
        assert b.cold_start is True

    def test_untimestamped_turns_dropped(self) -> None:
        as_of = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        turns = [
            _heartbeat_turn(days_ago=1, as_of=as_of),
            {"source": "heartbeat", "channel": "heartbeat"},  # no ts
            {"ts": "garbage", "source": "heartbeat", "channel": "heartbeat"},
        ]
        b = compute_baseline(bot_id="team_bot_a", turns=turns, as_of=as_of)
        assert b.auto_turns == 1

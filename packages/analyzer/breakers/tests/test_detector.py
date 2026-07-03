"""Tests for breakers.detector — the activity-shape rule.

The synthetic scenarios here mirror the documented incidents in
docs/incident-cost-audit-2026-05-21.md:

  POSITIVES (should trip):
    - rate spike on high tier (security_bot 2026-05-20 shape)
    - tier shift only (team_bot_a 2026-04-17 shape)
    - mixed rate + tier shift

  NEGATIVES (should NOT trip):
    - human chat runaway through telegram/slack
    - normal haiku heartbeat at baseline rate
    - idle bot
    - high-tier auto activity but human also active (legit deep-research)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from breakers.baseline import Baseline
from breakers.detector import DEFAULT_CONFIG, evaluate_window


# Reference window: a 1-hour evaluation window ending at 2026-05-20T17:00Z.
WIN_END = datetime(2026, 5, 20, 17, 0, tzinfo=timezone.utc)
WIN_START = WIN_END - timedelta(hours=1)


def _turn_at(
    minutes_into_window: float,
    *,
    source: str,
    channel: str,
    model: str,
) -> dict:
    """Construct a turn whose ts lands inside the test window."""
    ts = WIN_START + timedelta(minutes=minutes_into_window)
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "source": source,
        "channel": channel,
        "model": model,
    }


def _heartbeat(minutes: float, model: str = "anthropic/claude-haiku-4-5") -> dict:
    return _turn_at(minutes, source="heartbeat", channel="heartbeat", model=model)


def _sonnet_heartbeat(minutes: float) -> dict:
    return _heartbeat(minutes, model="anthropic/claude-sonnet-4-6")


def _human_telegram(minutes: float) -> dict:
    return _turn_at(
        minutes, source="human", channel="telegram",
        model="anthropic/claude-sonnet-4-6",
    )


def _warm_haiku_baseline(bot_id: str = "security_bot", auto_rate: float = 1.0) -> Baseline:
    """A warm baseline: ~auto_rate auto turns/hr, all haiku, no humans."""
    return Baseline(
        bot_id=bot_id,
        as_of=WIN_END,
        window_start=WIN_END - timedelta(days=7),
        window_end=WIN_END - timedelta(hours=1),
        auto_rate_per_hr=auto_rate,
        human_rate_per_hr=0.0,
        auto_high_tier_share=0.0,
        auto_turns=int(auto_rate * 24 * 7),
        human_turns=0,
        days_with_data=7,
        cold_start=False,
    )


def _warm_mixed_baseline(
    bot_id: str = "admin_bot",
    auto_rate: float = 1.0,
    human_rate: float = 3.0,
) -> Baseline:
    """A warm baseline with active human chat."""
    return Baseline(
        bot_id=bot_id,
        as_of=WIN_END,
        window_start=WIN_END - timedelta(days=7),
        window_end=WIN_END - timedelta(hours=1),
        auto_rate_per_hr=auto_rate,
        human_rate_per_hr=human_rate,
        auto_high_tier_share=0.0,
        auto_turns=int(auto_rate * 24 * 7),
        human_turns=int(human_rate * 24 * 7),
        days_with_data=7,
        cold_start=False,
    )


def _cold_start_baseline(bot_id: str = "personal_bot") -> Baseline:
    return Baseline(
        bot_id=bot_id,
        as_of=WIN_END,
        window_start=WIN_END - timedelta(days=7),
        window_end=WIN_END - timedelta(hours=1),
        auto_rate_per_hr=0.5,
        human_rate_per_hr=0.0,
        auto_high_tier_share=0.0,
        auto_turns=2,
        human_turns=0,
        days_with_data=2,
        cold_start=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POSITIVES — should trip
# ─────────────────────────────────────────────────────────────────────────────


class TestPositives:
    def test_rate_spike_high_tier(self) -> None:
        """Security_bot 2026-05-20 shape: 30 sonnet-heartbeats in 1h, baseline ~1/hr haiku."""
        turns = [_sonnet_heartbeat(i * 2) for i in range(30)]  # 30 over 60min
        d = evaluate_window(
            bot_id="security_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(auto_rate=1.0),
        )
        assert d.trip, f"expected trip; reason: {d.reason}; metrics: {d.metrics}"
        assert d.metrics["clauses"]["A_rate_spike"] is True
        assert d.metrics["clauses"]["C_high_tier_floor"] is True
        assert d.metrics["clauses"]["D_human_quiescent"] is True

    def test_tier_shift_at_baseline_rate(self) -> None:
        """Team_bot_a 2026-04-17 shape: same rate (~1/hr), but tier flipped to sonnet.

        Tier-shift cases at low rates need a longer eval window to clear
        the min_auto_turns_gate. The audit shows team_bot_a 2026-04-17 had 24
        violating turns in a day; over an 8-hour window that's 8 turns
        — enough to clear the gate. Phase 1 backtest will tune window
        sizing against the full corpus.
        """
        long_end = WIN_END
        long_start = long_end - timedelta(hours=8)
        # 8 sonnet heartbeats, one per hour, across the window. Rate is
        # at baseline (1/hr), but tier has shifted from 0% high → 100% high.
        turns = [
            {
                "ts": (long_start + timedelta(hours=i, minutes=5))
                    .isoformat().replace("+00:00", "Z"),
                "source": "heartbeat",
                "channel": "heartbeat",
                "model": "anthropic/claude-sonnet-4-6",
            }
            for i in range(8)
        ]
        d = evaluate_window(
            bot_id="team_bot_a", turns=turns,
            window_start=long_start, window_end=long_end,
            baseline=_warm_haiku_baseline(bot_id="team_bot_a", auto_rate=1.0),
        )
        assert d.trip, f"expected trip; reason: {d.reason}; metrics: {d.metrics}"
        assert d.metrics["clauses"]["B_tier_shift"] is True
        assert d.metrics["clauses"]["C_high_tier_floor"] is True

    def test_mixed_rate_and_tier_shift(self) -> None:
        """Both prongs fire."""
        turns = [_sonnet_heartbeat(i * 1.5) for i in range(40)]  # 40 in 60min
        d = evaluate_window(
            bot_id="security_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(auto_rate=0.5),
        )
        assert d.trip
        assert d.metrics["clauses"]["A_rate_spike"] is True
        assert d.metrics["clauses"]["B_tier_shift"] is True

    def test_cold_start_absolute_floor(self) -> None:
        """A cold-start bot still trips on a clear absolute spike."""
        turns = [_sonnet_heartbeat(i * 3) for i in range(20)]  # 20/hr
        d = evaluate_window(
            bot_id="personal_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_cold_start_baseline(),
        )
        assert d.trip, f"expected trip; reason: {d.reason}"
        assert d.metrics["clauses"]["rate_threshold_source"] == "cold-start floor"


# ─────────────────────────────────────────────────────────────────────────────
# NEGATIVES — should NOT trip
# ─────────────────────────────────────────────────────────────────────────────


class TestNegatives:
    def test_human_chat_runaway_through_telegram(self) -> None:
        """Admin_bot 2026-04-08 shape: heavy telegram chat. NOT a runaway loop."""
        # 50 human turns in 1h on sonnet — bursty, but real chat.
        turns = [_human_telegram(i * 1.2) for i in range(50)]
        d = evaluate_window(
            bot_id="admin_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_mixed_baseline(human_rate=3.0),
        )
        assert not d.trip
        # Below gate (no auto turns), so the rule shouldn't even evaluate
        # past the gate.
        assert d.metrics["auto_turns"] == 0

    def test_normal_haiku_heartbeat_at_baseline(self) -> None:
        """A bot doing exactly what it's supposed to — hourly haiku heartbeat."""
        turns = [_heartbeat(30)]  # 1 turn at minute 30
        d = evaluate_window(
            bot_id="security_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(auto_rate=1.0),
        )
        assert not d.trip

    def test_idle_bot(self) -> None:
        """No turns at all in window."""
        d = evaluate_window(
            bot_id="evolve", turns=[],
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(),
        )
        assert not d.trip

    def test_high_tier_auto_but_human_also_active(self) -> None:
        """User is having a deep research session via slack — auto looks
        elevated because of agent spawns, but the human is active so
        we should NOT trip."""
        turns = [_sonnet_heartbeat(i * 2) for i in range(30)]
        turns += [_human_telegram(i * 2.5) for i in range(20)]  # 20/hr human
        d = evaluate_window(
            bot_id="team_bot_a", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_mixed_baseline(human_rate=2.0),
        )
        assert not d.trip
        assert d.metrics["clauses"]["D_human_quiescent"] is False

    def test_below_gate(self) -> None:
        """Only a few auto turns — under the min_auto_turns_gate."""
        turns = [_sonnet_heartbeat(i * 15) for i in range(3)]  # 3 turns
        d = evaluate_window(
            bot_id="team_bot_a", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(auto_rate=0.1),
        )
        assert not d.trip
        assert "below gate" in d.reason

    def test_low_tier_auto_burst_no_trip(self) -> None:
        """20 haiku heartbeats in 1h — high rate but cheap models.
        L1 cost breaker is about expensive misbehavior; cheap bursts
        don't qualify."""
        turns = [_heartbeat(i * 3) for i in range(20)]
        d = evaluate_window(
            bot_id="security_bot", turns=turns,
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(auto_rate=1.0),
        )
        assert not d.trip
        # Should fail clause C (high-tier floor).
        assert d.metrics["clauses"]["C_high_tier_floor"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_decision_carries_metrics_for_forensics(self) -> None:
        """The Decision's metrics dict must be populated even on no-trip
        — Phase 5's audit-of-cause relies on this for the recommendation."""
        d = evaluate_window(
            bot_id="team_bot_a", turns=[],
            window_start=WIN_START, window_end=WIN_END,
            baseline=_warm_haiku_baseline(),
        )
        assert "auto_turns" in d.metrics
        assert "baseline_auto_rate_per_hr" in d.metrics
        assert "cold_start" in d.metrics

    def test_naive_datetime_normalized_to_utc(self) -> None:
        """Callers may pass naive datetimes; detector treats them as UTC."""
        turns = [_sonnet_heartbeat(i * 2) for i in range(30)]
        naive_start = WIN_START.replace(tzinfo=None)
        naive_end = WIN_END.replace(tzinfo=None)
        d = evaluate_window(
            bot_id="security_bot", turns=turns,
            window_start=naive_start, window_end=naive_end,
            baseline=_warm_haiku_baseline(),
        )
        assert d.trip

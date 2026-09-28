"""Single-turn concentration, user-source detectors and the non-shrug fallback.

Fixture day built from internal/incident-post-mortem-2026-09-17-single-turn-
cache-thrash.md: 27 turns, $27.37, one user turn of $22.15 that read
5,382,819 and wrote 8,186,626 cache tokens. Placeholder names throughout.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from breakers.audit_generator import (
    _detect_cache_write_no_reuse,
    _detect_cache_write_no_reuse_user,
    _detect_runaway_session,
    _detect_runaway_session_user,
    _detect_single_turn_concentration,
    analyze_trip,
)

NOW = datetime(2026, 9, 17, 19, 52, 47, tzinfo=timezone.utc)
THRASH_TS = "2026-09-17T19:27:28Z"
THRASH_SESSION = "sess-thrash-0001"


def _turn(minutes_ago: int, *, source: str, channel: str, cost: float,
          session_id: str, read: int = 40_000, write: int = 2_000,
          model: str = "anthropic/claude-haiku-4-5", ts: str | None = None,
          **extra: Any) -> dict[str, Any]:
    stamp = ts or (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"ts": stamp, "source": source, "channel": channel, "model": model,
            "session_id": session_id, "cost": cost, "cache_read_tokens": read,
            "cache_write_tokens": write, **extra}


def incident_day() -> list[dict]:
    """27 turns: the $22.15 thrash turn, 21 heartbeat turns, 5 user turns."""
    turns = [_turn(0, source="user", channel="telegram", cost=22.15,
                   session_id=THRASH_SESSION, read=5_382_819, write=8_186_626,
                   model="anthropic/claude-sonnet-5", ts=THRASH_TS, calls=118)]
    # 5.22 spread over 26 turns — varied so the top-three list has an order.
    for i in range(21):
        turns.append(_turn(10 + i * 11, source="heartbeat", channel="heartbeat",
                           cost=0.18, session_id=f"sess-hb-{i:02d}"))
    for i, c in enumerate((0.39, 0.32, 0.25, 0.24, 0.24)):
        turns.append(_turn(3 + i * 40, source="user", channel="telegram",
                           cost=c, session_id="sess-user-0002",
                           model="anthropic/claude-sonnet-5"))
    return turns


def flat_day() -> list[dict]:
    return [_turn(5 + i * 8, source="heartbeat", channel="heartbeat",
                  cost=1.01, session_id=f"sess-flat-{i:02d}") for i in range(27)]


def _analyze(turns: list[dict], tmp_path: Path) -> tuple[str, str, str]:
    return analyze_trip(shared_dir=tmp_path, bot_id="placeholder_bot",
                        breaker_type="cost", now=NOW,
                        read_turns_fn=lambda *a, **kw: turns)


def test_fixture_matches_the_post_mortem_totals() -> None:
    day = incident_day()
    assert len(day) == 27
    assert round(sum(t["cost"] for t in day), 2) == 27.37


class TestSingleTurnConcentration:
    def test_fires_on_the_incident_day_and_names_the_turn(self, tmp_path: Path) -> None:
        summary, _rec, pattern = _analyze(incident_day(), tmp_path)
        assert pattern == "single_turn_concentration"
        for needle in (THRASH_SESSION, THRASH_TS, "claude-sonnet-5", "calls 118",
                       "cache read 5,382,819", "cache write 8,186,626",
                       "$22.15", "81%", "cache thrash"):
            assert needle in summary, needle

    def test_flat_day_does_not_fire(self) -> None:
        assert _detect_single_turn_concentration(flat_day()) is None

    def test_absolute_arm_fires_without_share(self) -> None:
        turns = flat_day() + [_turn(1, source="user", channel="telegram",
                                    cost=6.00, session_id="sess-big")]
        result = _detect_single_turn_concentration(turns)
        assert result is not None and "$6.00" in result[0]

    def test_share_arm_needs_a_dollar_floor(self) -> None:
        tiny = [_turn(1, source="user", channel="telegram", cost=0.02,
                      session_id="sess-tiny")]
        assert _detect_single_turn_concentration(tiny) is None

    def test_baseline_arm(self) -> None:
        turns = flat_day() + [_turn(1, source="user", channel="telegram",
                                    cost=2.50, session_id="sess-b")]
        assert _detect_single_turn_concentration(turns) is None
        result = _detect_single_turn_concentration(turns, baseline_per_user_turn=0.20)
        assert result is not None and "baseline" in result[0]


class TestUserSourceVariants:
    def test_auto_only_variants_still_ignore_the_user_turn(self) -> None:
        day = incident_day()
        assert _detect_cache_write_no_reuse(day) is None
        assert _detect_runaway_session(day) is None

    def test_cache_write_no_reuse_user_fires_on_one_turn(self) -> None:
        result = _detect_cache_write_no_reuse_user(incident_day())
        assert result is not None
        assert THRASH_SESSION in result[0] and "8,186,626" in result[0]

    def test_cache_write_no_reuse_user_ignores_read_dominated_turns(self) -> None:
        turns = [_turn(1, source="user", channel="telegram", cost=5.98,
                       session_id="s", read=2_254_343, write=2_129_495)]
        assert _detect_cache_write_no_reuse_user(turns) is None

    def test_runaway_session_user_threshold(self) -> None:
        turns = [_turn(i, source="user", channel="telegram", cost=0.05,
                       session_id="sess-chatty") for i in range(39)]
        assert _detect_runaway_session_user(turns) is None
        turns.append(_turn(40, source="user", channel="telegram", cost=0.05,
                           session_id="sess-chatty"))
        result = _detect_runaway_session_user(turns)
        assert result is not None and "user-source" in result[0]


class TestFallback:
    def test_lists_three_turns_and_unknown_predicates(self, tmp_path: Path) -> None:
        # Below every detector, and with no cache fields on any row.
        turns = []
        for i, c in enumerate((0.40, 0.35, 0.30, 0.20, 0.10)):
            t = _turn(5 + i * 30, source="user", channel="telegram", cost=c,
                      session_id=f"sess-f{i}")
            del t["cache_read_tokens"], t["cache_write_tokens"]
            turns.append(t)
        summary, _rec, pattern = _analyze(turns, tmp_path)
        assert pattern == "manual_review"
        assert "Top 3 turns by cost" in summary
        assert [ln for ln in summary.splitlines() if ln.startswith("  ")] == [
            ln for ln in summary.splitlines() if "sess-f" in ln
        ]
        assert "sess-f0" in summary and "sess-f2" in summary and "sess-f3" not in summary
        assert "cache read unknown" in summary
        unknown_line = next(ln for ln in summary.splitlines() if ln.startswith("unknown"))
        assert "cache_write_no_reuse (no cache_write_tokens, cache_read_tokens)" in unknown_line
        assert "cache_write_no_reuse_user" in unknown_line
        assert "single_turn_concentration" in summary.split("Evaluated, did not fire:")[1].splitlines()[0]

"""Tests for breakers.audit_generator — Phase 5c async audit-of-cause.

Each pattern detector exercised against synthetic turn fixtures shaped
like the incidents it's designed to catch. Plus the end-to-end
process_pending_audits flow: idempotency, pod-scope skip, no-turns
fallback, write-back via update_audit_fields.

The fixtures are minimal — only the fields classify.classify_turn and
the detector heuristics read. The point is to pin the SHAPE that
triggers each pattern, not to mirror full TurnObserver output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from breakers import audit_generator, store
from breakers.audit_generator import (
    AuditResult,
    DEFAULT_AUDIT_WINDOW_HOURS,
    _MIN_AUTO_TURNS_FOR_PATTERN,
    _detect_cache_write_no_reuse,
    _detect_heartbeat_wrong_model,
    _detect_runaway_session,
    analyze_trip,
    process_pending_audits,
)


# Reference "now" used across tests.
FIXED_NOW = datetime(2026, 5, 21, 16, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Turn fixture builders
# ─────────────────────────────────────────────────────────────────────────────


def _t(
    *,
    minutes_ago: int = 0,
    source: str = "heartbeat",
    channel: str = "heartbeat",
    model: str = "anthropic/claude-haiku-4-5",
    session_id: str | None = "sess-x",
    cost: float = 0.01,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> dict[str, Any]:
    ts = FIXED_NOW - timedelta(minutes=minutes_ago)
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "source": source,
        "channel": channel,
        "model": model,
        "session_id": session_id,
        "cost": cost,
        "cache_write_tokens": cache_write_tokens,
        "cache_read_tokens": cache_read_tokens,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pattern: heartbeat-on-wrong-model
# ─────────────────────────────────────────────────────────────────────────────


class TestHeartbeatWrongModel:
    def test_fires_when_high_tier_share_exceeds_floor(self) -> None:
        turns = []
        # 4 sonnet heartbeat turns + 6 haiku heartbeat turns → 40% high tier.
        for i in range(4):
            turns.append(_t(minutes_ago=10 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(6):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))
        result = _detect_heartbeat_wrong_model(turns)
        assert result is not None
        summary, recommendation = result
        # The summary should call out the high-tier count + share + model name.
        assert "sonnet" in summary.lower()
        assert "40%" in summary
        # The recommendation should point at heartbeat model config.
        assert "heartbeat" in recommendation.lower()
        assert "haiku" in recommendation.lower()

    def test_skips_when_below_high_tier_floor(self) -> None:
        # 1 sonnet + 9 haiku = 10% high tier, below 30% floor.
        turns = [_t(minutes_ago=10, model="anthropic/claude-sonnet-4-5")]
        for i in range(9):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))
        assert _detect_heartbeat_wrong_model(turns) is None

    def test_skips_when_below_min_auto_turns(self) -> None:
        # Only 3 auto turns — below the 5-turn floor regardless of tier.
        turns = [
            _t(minutes_ago=10, model="anthropic/claude-sonnet-4-5"),
            _t(minutes_ago=20, model="anthropic/claude-sonnet-4-5"),
            _t(minutes_ago=30, model="anthropic/claude-sonnet-4-5"),
        ]
        assert _detect_heartbeat_wrong_model(turns) is None

    def test_ignores_human_turns_in_share_calculation(self) -> None:
        # 4 sonnet heartbeats + 4 sonnet human turns + 6 haiku heartbeats.
        # Auto-source turns: 10; high-tier auto: 4 → 40% (fires).
        # Without the auto filter, 8/14 high = 57% (also fires, but wrong
        # mechanism). Pinning the auto-only filter.
        turns = []
        for i in range(4):
            turns.append(_t(minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(4):
            turns.append(_t(
                minutes_ago=15 + i,
                source="user",
                channel="slack",
                model="anthropic/claude-sonnet-4-5",
            ))
        for i in range(6):
            turns.append(_t(minutes_ago=30 + i, model="anthropic/claude-haiku-4-5"))
        result = _detect_heartbeat_wrong_model(turns)
        assert result is not None
        summary, _ = result
        # "4 of 10" not "8 of 14".
        assert "4 of 10" in summary or "of 10" in summary


# ─────────────────────────────────────────────────────────────────────────────
# Pattern: runaway-session
# ─────────────────────────────────────────────────────────────────────────────


class TestRunawaySession:
    def test_fires_when_single_session_burns_many_turns_in_short_window(self) -> None:
        # 25 turns in one session over 20 minutes — well past defaults.
        turns = []
        for i in range(25):
            turns.append(_t(
                minutes_ago=30 - (i * 20 / 25),
                session_id="runaway-1",
                source="heartbeat",
                cost=0.50,
            ))
        result = _detect_runaway_session(turns)
        assert result is not None
        summary, recommendation = result
        assert "runaway-" in summary
        assert "25" in summary
        # Should mention cost magnitude.
        assert "$" in summary
        assert "session" in recommendation.lower()

    def test_skips_when_turns_spread_across_many_sessions(self) -> None:
        # 30 turns spread across 30 distinct sessions — no single session
        # burns enough to trigger.
        turns = [
            _t(minutes_ago=i, session_id=f"sess-{i}")
            for i in range(30)
        ]
        assert _detect_runaway_session(turns) is None

    def test_skips_when_session_spans_too_long(self) -> None:
        # 25 turns but spread across 4 hours — sustained, not runaway.
        turns = []
        for i in range(25):
            turns.append(_t(
                minutes_ago=240 - (i * 240 / 25),
                session_id="long-session",
            ))
        assert _detect_runaway_session(turns) is None

    def test_skips_turns_without_session_id(self) -> None:
        # 25 fast turns but all session_id="" — bare heartbeats with no
        # session correlator.
        turns = [_t(minutes_ago=i, session_id="") for i in range(25)]
        assert _detect_runaway_session(turns) is None


# ─────────────────────────────────────────────────────────────────────────────
# Pattern: cache-write-no-reuse
# ─────────────────────────────────────────────────────────────────────────────


class TestCacheWriteNoReuse:
    def test_fires_on_regular_cadence_writes_with_zero_reads(self) -> None:
        # 6 auto turns at 15-min cadence, each writing 200k cache, zero read.
        turns = []
        for i in range(6):
            turns.append(_t(
                minutes_ago=15 * i,
                cache_write_tokens=200_000,
                cache_read_tokens=0,
                session_id=f"cron-{i}",
            ))
        result = _detect_cache_write_no_reuse(turns)
        assert result is not None
        summary, recommendation = result
        # 15-min cadence detection.
        assert "15" in summary or "min" in summary
        # Reads-zero diagnosis.
        assert "0" in summary or "no reuse" in summary.lower() or "read 0" in summary
        # Recommendation surfaces cron / cache TTL options.
        assert "cron" in recommendation.lower() or "cache" in recommendation.lower()

    def test_skips_when_cache_reads_present(self) -> None:
        # Same shape but with non-zero cache_read_tokens — cache is reused.
        turns = []
        for i in range(6):
            turns.append(_t(
                minutes_ago=15 * i,
                cache_write_tokens=200_000,
                cache_read_tokens=100_000,
            ))
        assert _detect_cache_write_no_reuse(turns) is None

    def test_skips_when_write_below_threshold(self) -> None:
        # Auto turns at regular cadence but only 1k cache writes each.
        turns = []
        for i in range(6):
            turns.append(_t(
                minutes_ago=15 * i,
                cache_write_tokens=1_000,
                cache_read_tokens=0,
            ))
        assert _detect_cache_write_no_reuse(turns) is None

    def test_skips_when_cadence_not_regular(self) -> None:
        # 6 writes at chaotic spacing — not a cron pattern.
        spacings = [1, 50, 100, 250, 600, 800]
        turns = []
        for s in spacings:
            turns.append(_t(
                minutes_ago=s,
                cache_write_tokens=200_000,
                cache_read_tokens=0,
            ))
        assert _detect_cache_write_no_reuse(turns) is None


# ─────────────────────────────────────────────────────────────────────────────
# analyze_trip — orchestration + fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestAnalyzeTrip:
    def test_no_turns_returns_no_turns_pattern(self, tmp_path: Path) -> None:
        summary, recommendation, pattern = analyze_trip(
            shared_dir=tmp_path,
            bot_id="team_bot_a",
            breaker_type="cost",
            now=FIXED_NOW,
            read_turns_fn=lambda shared_dir, bot_id, since, until: [],
        )
        assert pattern == "no_turns"
        assert summary is not None
        assert recommendation is not None
        # Operator-actionable text.
        assert "turn" in summary.lower()

    def test_heartbeat_pattern_wins_over_fallback(self, tmp_path: Path) -> None:
        turns = []
        for i in range(4):
            turns.append(_t(minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(6):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))
        summary, recommendation, pattern = analyze_trip(
            shared_dir=tmp_path,
            bot_id="team_bot_a",
            breaker_type="cost",
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: turns,
        )
        assert pattern == "heartbeat_wrong_model"
        assert summary is not None
        assert recommendation is not None

    def test_falls_through_to_manual_review_when_no_pattern_matches(
        self, tmp_path: Path,
    ) -> None:
        # 3 auto haiku turns — below all detector floors.
        turns = [_t(minutes_ago=10 + i) for i in range(3)]
        summary, recommendation, pattern = analyze_trip(
            shared_dir=tmp_path,
            bot_id="team_bot_a",
            breaker_type="cost",
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: turns,
        )
        assert pattern == "manual_review"
        assert summary is not None
        assert recommendation is not None
        assert "manual" in summary.lower() or "review" in summary.lower()

    def test_read_failed_returns_none_with_marker_pattern(self, tmp_path: Path) -> None:
        def _boom(*a, **kw):
            raise IOError("disk gone")

        summary, recommendation, pattern = analyze_trip(
            shared_dir=tmp_path,
            bot_id="team_bot_a",
            breaker_type="cost",
            now=FIXED_NOW,
            read_turns_fn=_boom,
        )
        assert pattern == "read_failed"
        assert summary is None
        assert recommendation is None

    def test_detector_exception_is_caught_and_next_runs(self, tmp_path: Path) -> None:
        # Build turns that would fire heartbeat-wrong-model normally;
        # monkey-patch the first detector to raise. The second detector
        # shouldn't match (single-shot test), so we expect manual_review
        # as the fallback — what matters is that the exception didn't
        # propagate.
        turns = []
        for i in range(4):
            turns.append(_t(minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(6):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))

        original = audit_generator._DETECTORS
        try:
            def _crash(turns_arg):
                raise RuntimeError("synthetic")
            audit_generator._DETECTORS = (_crash, *original[1:])
            summary, recommendation, pattern = analyze_trip(
                shared_dir=tmp_path,
                bot_id="team_bot_a",
                breaker_type="cost",
                now=FIXED_NOW,
                read_turns_fn=lambda *a, **kw: turns,
            )
        finally:
            audit_generator._DETECTORS = original

        # Crash caught → falls through. Either another detector
        # incidentally matched, or manual_review fallback ran. Both
        # outcomes are acceptable; what's pinned is that we got a result.
        assert pattern != "read_failed"
        assert summary is not None
        assert recommendation is not None


# ─────────────────────────────────────────────────────────────────────────────
# process_pending_audits — idempotency + write-back
# ─────────────────────────────────────────────────────────────────────────────


def _trip_bot(shared_dir: Path, bot_id: str) -> store.BreakerRecord:
    """Helper: trip a cost breaker for ``bot_id`` and return the record."""
    return store.trip(
        shared_dir=shared_dir,
        scope=bot_id,
        breaker_type="cost",
        duration=timedelta(hours=24),
        initiated_by="auto",
        reason="test trip",
        motivating_signals=["test-signal"],
        now=FIXED_NOW,
    )


class TestProcessPendingAudits:
    def test_writes_audit_fields_back_to_store(self, tmp_path: Path) -> None:
        _trip_bot(tmp_path, "team_bot_a")
        # Build turns that fire heartbeat-wrong-model.
        turns = []
        for i in range(4):
            turns.append(_t(minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(6):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))

        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: turns,
        )
        assert len(results) == 1
        r = results[0]
        assert r.bot_id == "team_bot_a"
        assert r.pattern == "heartbeat_wrong_model"
        assert not r.skip_reason

        # And the store now has the fields populated.
        reread = store.read_trip(tmp_path, "team_bot_a", "cost")
        assert reread is not None
        assert reread.audit_summary is not None
        assert reread.audit_recommendation is not None
        assert "sonnet" in reread.audit_summary.lower()

    def test_idempotent_skips_already_populated_trip(self, tmp_path: Path) -> None:
        _trip_bot(tmp_path, "team_bot_a")
        # Populate the audit fields manually first.
        store.update_audit_fields(
            shared_dir=tmp_path,
            scope="team_bot_a",
            breaker_type="cost",
            audit_summary="pre-existing summary",
            audit_recommendation="pre-existing rec",
        )

        # Sentinel: if process_pending calls analyze_trip, this will fire.
        calls: list[Any] = []

        def _read(*a, **kw):
            calls.append((a, kw))
            return []

        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=_read,
        )
        assert len(results) == 1
        r = results[0]
        assert r.skip_reason == "already populated"
        # No turn reads happened — we short-circuited before analyze_trip.
        assert calls == []

        # The fields are untouched.
        reread = store.read_trip(tmp_path, "team_bot_a", "cost")
        assert reread.audit_summary == "pre-existing summary"
        assert reread.audit_recommendation == "pre-existing rec"

    def test_skips_pod_scope_trips(self, tmp_path: Path) -> None:
        store.trip(
            shared_dir=tmp_path,
            scope="pod",
            breaker_type="full",
            duration=timedelta(hours=24),
            initiated_by="admin:pod_admin",
            reason="manual pod halt",
            now=FIXED_NOW,
        )
        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: [],
        )
        assert len(results) == 1
        assert results[0].bot_id == "pod"
        assert "pod-wide" in results[0].skip_reason

    def test_write_failure_recorded_in_skip_reason(self, tmp_path: Path) -> None:
        _trip_bot(tmp_path, "team_bot_a")
        turns = []
        for i in range(4):
            turns.append(_t(minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5"))
        for i in range(6):
            turns.append(_t(minutes_ago=20 + i, model="anthropic/claude-haiku-4-5"))

        def _boom_update(**kwargs):
            raise IOError("disk full")

        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: turns,
            update_fn=_boom_update,
        )
        assert len(results) == 1
        assert results[0].skip_reason.startswith("write_failed")
        # Original trip wasn't corrupted — fields stay None.
        reread = store.read_trip(tmp_path, "team_bot_a", "cost")
        assert reread.audit_summary is None

    def test_multi_bot_processes_each_independently(self, tmp_path: Path) -> None:
        _trip_bot(tmp_path, "team_bot_a")
        _trip_bot(tmp_path, "security_bot")

        # Pre-populate team_bot_a so it gets skipped, leave security_bot fresh.
        store.update_audit_fields(
            shared_dir=tmp_path,
            scope="team_bot_a",
            breaker_type="cost",
            audit_summary="team_bot_a already done",
        )

        turns_for_security_bot = []
        for i in range(4):
            turns_for_security_bot.append(_t(
                minutes_ago=5 + i, model="anthropic/claude-sonnet-4-5",
            ))
        for i in range(6):
            turns_for_security_bot.append(_t(
                minutes_ago=20 + i, model="anthropic/claude-haiku-4-5",
            ))

        read_calls: list[str] = []

        def _read(shared_dir, bot_id, since, until):
            read_calls.append(bot_id)
            if bot_id == "security_bot":
                return turns_for_security_bot
            return []

        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=_read,
        )
        # Two records, team_bot_a skipped, security_bot processed.
        bot_to_result = {r.bot_id: r for r in results}
        assert bot_to_result["team_bot_a"].skip_reason == "already populated"
        assert bot_to_result["security_bot"].pattern == "heartbeat_wrong_model"
        # Read was only called for security_bot — team_bot_a was short-circuited.
        assert read_calls == ["security_bot"]

    def test_no_active_trips_returns_empty(self, tmp_path: Path) -> None:
        results = process_pending_audits(
            shared_dir=tmp_path,
            now=FIXED_NOW,
            read_turns_fn=lambda *a, **kw: [],
        )
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke
# ─────────────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_main_once_returns_zero_with_no_trips(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = audit_generator.main([
            "--shared-dir", str(tmp_path),
            "--once",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        # Stderr summary line.
        assert "audit_generator" in captured.err
        assert "total=0" in captured.err

    def test_main_once_processes_a_trip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Indefinite trip so it stays active regardless of wall-clock
        # drift — the CLI uses datetime.now() internally, not FIXED_NOW.
        store.trip(
            shared_dir=tmp_path,
            scope="team_bot_a",
            breaker_type="cost",
            duration=None,
            initiated_by="auto",
            reason="cli test",
        )
        # No turn data — exercises the no_turns path via the real
        # backtest.read_turns (returns empty when no JSONLs exist).
        rc = audit_generator.main([
            "--shared-dir", str(tmp_path),
            "--once",
        ])
        assert rc == 0
        # The trip should now have the no_turns audit text.
        reread = store.read_trip(tmp_path, "team_bot_a", "cost")
        assert reread is not None
        assert reread.audit_summary is not None
        assert "audit window" in reread.audit_summary.lower()

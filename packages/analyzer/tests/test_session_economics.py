"""tests/test_session_economics.py — session_economics detector + runner tests.

Each detector is exercised in isolation with hand-rolled fixture events;
the runner is exercised end-to-end with monkeypatched read_events so we
can assert exactly which Signals get written to a tmp shared_dir.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import session_economics  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _evt(
    *,
    bot_id: str = "admin_bot",
    ts: str = "2026-05-09T12:00:00Z",
    cost_usd: float = 0.01,
    trigger_kind: str = "user_turn",
    cache_state: str = "warm",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 5000,
    cache_write_tokens: int = 0,
    session_id: str = "sess-1",
) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot_id,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "model": "claude-sonnet",
        "provider": "anthropic",
        "cache_state": cache_state,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
    }


# ── detect_cache_invalidation_elevated ───────────────────────────────────────


def test_invalidation_fires_when_ratio_exceeds_threshold():
    # 60 invalidated + 40 warm → 60% invalidation rate, well above 15%.
    events = [_evt(cache_state="invalidated") for _ in range(60)] + [
        _evt(cache_state="warm") for _ in range(40)
    ]
    out = session_economics.detect_cache_invalidation_elevated(
        "admin_bot",
        events,
        threshold_ratio=0.15,
        min_events=50,
        window_days=7,
    )
    assert len(out) == 1
    assert out[0]["type"] == "cache_invalidation_elevated"
    assert out[0]["bot_id"] == "admin_bot"
    assert out[0]["details"]["invalidated_count"] == 60
    assert out[0]["details"]["participating_count"] == 100
    assert out[0]["details"]["invalidated_ratio"] == 0.60


def test_invalidation_silent_when_ratio_below_threshold():
    # 10 invalidated + 90 warm → 10% invalidation rate, below 15%.
    events = [_evt(cache_state="invalidated") for _ in range(10)] + [
        _evt(cache_state="warm") for _ in range(90)
    ]
    out = session_economics.detect_cache_invalidation_elevated(
        "admin_bot",
        events,
        threshold_ratio=0.15,
        min_events=50,
        window_days=7,
    )
    assert out == []


def test_invalidation_silent_below_min_events():
    # 9 invalidated + 1 warm → 90% rate but only 10 participating; below min 50.
    events = [_evt(cache_state="invalidated") for _ in range(9)] + [
        _evt(cache_state="warm")
    ]
    out = session_economics.detect_cache_invalidation_elevated(
        "admin_bot",
        events,
        threshold_ratio=0.15,
        min_events=50,
        window_days=7,
    )
    assert out == []


def test_invalidation_ignores_fresh_and_unknown():
    # 20 invalidated + 30 warm + 100 fresh + 100 unknown.
    # Participating set is just warm+invalidated = 50; ratio is 20/50 = 40%.
    events = (
        [_evt(cache_state="invalidated") for _ in range(20)]
        + [_evt(cache_state="warm") for _ in range(30)]
        + [_evt(cache_state="fresh") for _ in range(100)]
        + [_evt(cache_state="unknown") for _ in range(100)]
    )
    out = session_economics.detect_cache_invalidation_elevated(
        "admin_bot",
        events,
        threshold_ratio=0.15,
        min_events=50,
        window_days=7,
    )
    assert len(out) == 1
    assert out[0]["details"]["participating_count"] == 50
    assert out[0]["details"]["invalidated_count"] == 20
    assert out[0]["details"]["invalidated_ratio"] == 0.40


# ── detect_cache_hit_rate_low ─────────────────────────────────────────────────


def test_hit_rate_fires_when_blend_below_threshold():
    # All invalidated: cache_read=0, cache_write=5000, input=1000.
    # Hit rate = 0 / 6000 = 0%. Fires below 50% threshold.
    events = [
        _evt(
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(60)
    ]
    out = session_economics.detect_cache_hit_rate_low(
        "admin_bot",
        events,
        threshold_ratio=0.50,
        min_events=50,
        window_days=7,
    )
    assert len(out) == 1
    assert out[0]["type"] == "cache_hit_rate_low"
    assert out[0]["details"]["hit_rate"] == 0.0


def test_hit_rate_silent_when_blend_above_threshold():
    # All warm: cache_read=5000, cache_write=0, input=200.
    # Hit rate = 5000/5200 ≈ 96%. Silent at 50% threshold.
    events = [
        _evt(
            cache_state="warm",
            cache_read_tokens=5000,
            cache_write_tokens=0,
            input_tokens=200,
        )
        for _ in range(60)
    ]
    out = session_economics.detect_cache_hit_rate_low(
        "admin_bot",
        events,
        threshold_ratio=0.50,
        min_events=50,
        window_days=7,
    )
    assert out == []


def test_hit_rate_silent_below_min_events():
    events = [
        _evt(
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(10)
    ]
    out = session_economics.detect_cache_hit_rate_low(
        "admin_bot",
        events,
        threshold_ratio=0.50,
        min_events=50,
        window_days=7,
    )
    assert out == []


def test_hit_rate_excludes_fresh_from_aggregation():
    # 50 warm with great hit rate + 1000 fresh events with huge inputs.
    # Fresh must not be counted in denominator or it would tank hit rate.
    events = [
        _evt(
            cache_state="warm",
            cache_read_tokens=5000,
            cache_write_tokens=0,
            input_tokens=100,
        )
        for _ in range(60)
    ] + [
        _evt(
            cache_state="fresh",
            cache_read_tokens=0,
            cache_write_tokens=0,
            input_tokens=10000,
        )
        for _ in range(1000)
    ]
    out = session_economics.detect_cache_hit_rate_low(
        "admin_bot",
        events,
        threshold_ratio=0.50,
        min_events=50,
        window_days=7,
    )
    assert out == []  # warm-only blend is ~98%, fresh excluded


# ── detect_bot_unused ─────────────────────────────────────────────────────────


def test_bot_unused_fires_when_no_user_turns():
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [_evt(trigger_kind="heartbeat") for _ in range(30)]
    out = session_economics.detect_bot_unused(
        "team_bot_b", events, unused_days=14, now=now
    )
    assert len(out) == 1
    assert out[0]["type"] == "bot_unused"
    assert out[0]["severity"] == "info"
    assert out[0]["bot_id"] == "team_bot_b"
    assert out[0]["details"]["automation_event_count"] == 30
    assert out[0]["details"]["last_user_turn_ts"] is None


def test_bot_unused_silent_when_any_user_turn_present():
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [_evt(trigger_kind="heartbeat") for _ in range(30)] + [
        _evt(trigger_kind="user_turn")
    ]
    out = session_economics.detect_bot_unused(
        "team_bot_b", events, unused_days=14, now=now
    )
    assert out == []


def test_bot_unused_silent_when_no_events_at_all():
    # Edge case: bot exists in config but has no events in the window.
    # Detector still fires (zero user turns is zero user turns) — operator
    # can silence per-bot via unused_days override if this is expected.
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    out = session_economics.detect_bot_unused(
        "team_bot_b", [], unused_days=14, now=now
    )
    assert len(out) == 1
    assert out[0]["details"]["automation_event_count"] == 0


# ── Threshold plumbing ────────────────────────────────────────────────────────


def test_per_bot_threshold_override():
    config = {
        "session_economics": {
            "defaults": {"unused_days": 14},
            "bots": {"evolve": {"unused_days": 9999}},
        }
    }
    evolve = session_economics._thresholds_for_bot("evolve", config)
    admin_bot = session_economics._thresholds_for_bot("admin_bot", config)
    assert evolve["unused_days"] == 9999
    assert admin_bot["unused_days"] == 14


def test_defaults_fall_through_when_no_config():
    out = session_economics._thresholds_for_bot("admin_bot", {})
    assert out["cache_window_days"] == 7
    assert out["invalidation_ratio_threshold"] == 0.15
    assert out["hit_rate_threshold"] == 0.50
    assert out["unused_days"] == 14


# ── Runner integration ───────────────────────────────────────────────────────


def test_collect_for_bot_combines_detectors(monkeypatch, tmp_path):
    """admin_bot has 60% invalidation + zero user turns → two signals.

    Cache hit rate also low (all invalidated → 0% hit rate) → three signals total.
    """
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(
            trigger_kind="heartbeat",
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(60)
    ] + [
        _evt(
            trigger_kind="heartbeat",
            cache_state="warm",
            cache_read_tokens=5000,
            cache_write_tokens=0,
            input_tokens=200,
        )
        for _ in range(40)
    ]

    monkeypatch.setattr(
        session_economics,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(events),
    )

    detections = session_economics.collect_for_bot(
        "admin_bot", tmp_path, config={}, now=now
    )
    types = sorted(d["type"] for d in detections)
    assert types == [
        "bot_unused",
        "cache_hit_rate_low",
        "cache_invalidation_elevated",
    ]


def test_run_for_bot_writes_signals_to_disk(monkeypatch, tmp_path):
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(
            trigger_kind="user_turn",
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(60)
    ]

    monkeypatch.setattr(
        session_economics,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(events),
    )

    kept, n = session_economics.run_for_bot(
        "admin_bot", tmp_path, config={}, now=now
    )
    # With user_turns present, bot_unused stays silent. Both cache signals fire.
    assert n == 2
    assert len(kept) == 2

    sigs = sorted(
        signals_store.iter_active(tmp_path, producer="session_economics"),
        key=lambda s: s.type,
    )
    types = [s.type for s in sigs]
    assert types == ["cache_hit_rate_low", "cache_invalidation_elevated"]
    for sig in sigs:
        assert sig.bot_id == "admin_bot"
        assert sig.signature in kept


def test_sweep_resolve_archives_cleared_signal(monkeypatch, tmp_path):
    """Run 1 fires a signal; Run 2's sweep resolves it when cleared."""
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

    # Run 1: 60% invalidation → fires
    bad_events = [
        _evt(
            trigger_kind="user_turn",
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(60)
    ] + [
        _evt(
            trigger_kind="user_turn",
            cache_state="warm",
            cache_read_tokens=5000,
            cache_write_tokens=0,
            input_tokens=100,
        )
        for _ in range(40)
    ]
    monkeypatch.setattr(
        session_economics,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(bad_events),
    )
    session_economics.run_for_bot("admin_bot", tmp_path, config={}, now=now)
    active_types = sorted(
        s.type
        for s in signals_store.iter_active(tmp_path, producer="session_economics")
    )
    assert "cache_invalidation_elevated" in active_types

    # Run 2: all warm → invalidation cleared, hit rate good. Sweep should
    # resolve both prior firings.
    good_events = [
        _evt(
            trigger_kind="user_turn",
            cache_state="warm",
            cache_read_tokens=5000,
            cache_write_tokens=0,
            input_tokens=100,
        )
        for _ in range(100)
    ]
    monkeypatch.setattr(
        session_economics,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(good_events),
    )
    kept2, _ = session_economics.run_for_bot(
        "admin_bot", tmp_path, config={}, now=now
    )
    signals_store.sweep_resolve(
        tmp_path,
        producer="session_economics",
        kept_signatures=kept2,
        reason="auto-resolve: cleared",
    )
    assert (
        list(signals_store.iter_active(tmp_path, producer="session_economics"))
        == []
    )


def test_dry_run_does_not_write(monkeypatch, tmp_path, capsys):
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _evt(
            trigger_kind="user_turn",
            cache_state="invalidated",
            cache_read_tokens=0,
            cache_write_tokens=5000,
            input_tokens=1000,
        )
        for _ in range(60)
    ]
    monkeypatch.setattr(
        session_economics,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(events),
    )
    kept, n = session_economics.run_for_bot(
        "admin_bot", tmp_path, config={}, dry_run=True, now=now
    )
    assert n == 2
    assert len(kept) == 2
    # Nothing written to disk
    assert (
        list(signals_store.iter_active(tmp_path, producer="session_economics"))
        == []
    )
    captured = capsys.readouterr()
    assert "would_observe" in captured.out

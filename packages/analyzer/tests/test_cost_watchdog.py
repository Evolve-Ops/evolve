"""tests/test_cost_watchdog.py — cost_watchdog detector + runner tests.

Each detector is exercised in isolation with hand-rolled fixture data;
the runner is exercised end-to-end with monkeypatched readers so we can
assert exactly which Signals get written to a tmp shared_dir.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_watchdog  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _evt(
    *,
    bot_id: str = "admin_bot",
    ts: str = "2026-05-09T12:00:00Z",
    cost_usd: float = 0.01,
    trigger_kind: str = "user_turn",
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
        "cache_state": "warm",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "cost_usd": cost_usd,
    }


def _cron(
    *,
    cron_id: str = "cron-1",
    name: str = "test-cron",
    enabled: bool = True,
    payload_kind: str = "systemEvent",
    payload_text: str = "/usr/local/bin/foo.sh",
    session_target: str = "main",
    wake_mode: str = "now",
    schedule_kind: str = "every",
    every_ms: int = 900_000,  # 15 min
    cron_expr: str | None = None,
) -> dict:
    schedule: dict = {"kind": schedule_kind}
    if schedule_kind == "every":
        schedule["everyMs"] = every_ms
    elif schedule_kind == "cron":
        schedule["expr"] = cron_expr or "* * * * *"
    payload: dict = {"kind": payload_kind}
    if payload_kind == "systemEvent":
        payload["text"] = payload_text
    elif payload_kind == "agentTurn":
        payload["message"] = "hello"
    return {
        "id": cron_id,
        "name": name,
        "enabled": enabled,
        "schedule": schedule,
        "sessionTarget": session_target,
        "wakeMode": wake_mode,
        "payload": payload,
    }


def _run_record(*, ts_ms: int, action: str = "finished") -> dict:
    return {"ts": ts_ms, "jobId": "cron-1", "action": action, "status": "ok"}


def _turn(
    *,
    bot_id: str = "security_bot",
    session_id: str = "sess-1",
    channel: str = "heartbeat",
    source: str = "heartbeat",
    model: str = "claude-haiku-4-5",
    cost: float = 0.05,
    ts: str = "2026-05-20T18:32:00Z",
) -> dict:
    return {
        "ts": ts,
        "instance": bot_id,
        "model": model,
        "provider": "anthropic",
        "auth_mode": "unknown",
        "source": source,
        "channel": channel,
        "user_id": None,
        "session_id": session_id,
        "input_tokens": 10,
        "output_tokens": 200,
        "cost": cost,
    }


# ── detect_daily_spend ────────────────────────────────────────────────────────


def test_daily_spend_fires_above_threshold():
    today = "2026-05-09"
    events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3  # $6
    out = cost_watchdog.detect_daily_spend(
        "admin_bot", events, threshold_usd=3.0, today=today
    )
    assert len(out) == 1
    assert out[0]["type"] == "daily_spend_high"
    assert out[0]["severity"] == "alert"  # ≥ 2× threshold
    assert out[0]["details"]["cost_usd"] == 6.0


def test_daily_spend_warn_severity_below_2x():
    today = "2026-05-09"
    events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=1.5)] * 3  # $4.5 → warn (>3, <6)
    out = cost_watchdog.detect_daily_spend(
        "admin_bot", events, threshold_usd=3.0, today=today
    )
    assert len(out) == 1
    assert out[0]["severity"] == "warn"


def test_daily_spend_silent_below_threshold():
    today = "2026-05-09"
    events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=0.5)] * 4  # $2
    out = cost_watchdog.detect_daily_spend(
        "admin_bot", events, threshold_usd=3.0, today=today
    )
    assert out == []


# ── detect_cost_spike ─────────────────────────────────────────────────────────


def test_cost_spike_fires_above_multiplier_and_floor():
    # cur 7d: $14, prior 7d: $3.50 → 4× spike, > $5 floor
    cur = [_evt(cost_usd=2.0) for _ in range(7)]
    prior = [_evt(cost_usd=0.50) for _ in range(7)]
    out = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "cost_spike"
    assert sig["severity"] == "warn"  # 4× < 5×, so warn not alert
    assert sig["details"]["cost_cur_usd"] == 14.0
    assert sig["details"]["cost_prior_usd"] == 3.5
    assert sig["details"]["ratio"] == 4.0


def test_cost_spike_alert_severity_at_5x():
    cur = [_evt(cost_usd=5.0) for _ in range(7)]   # $35
    prior = [_evt(cost_usd=1.0) for _ in range(7)] # $7  → 5×
    out = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert len(out) == 1
    assert out[0]["severity"] == "alert"


def test_cost_spike_quiet_below_floor():
    # 4× spike but absolute spend tiny ($0.20 → $0.80)
    cur = [_evt(cost_usd=0.80 / 7) for _ in range(7)]
    prior = [_evt(cost_usd=0.20 / 7) for _ in range(7)]
    out = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert out == []


def test_cost_spike_quiet_below_multiplier():
    # $10 → $12: above floor but only 1.2×
    cur = [_evt(cost_usd=12.0 / 7) for _ in range(7)]
    prior = [_evt(cost_usd=10.0 / 7) for _ in range(7)]
    out = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert out == []


def test_cost_spike_quiet_when_no_prior_baseline():
    # First week of spend ever — daily_spend_high covers this, not cost_spike
    cur = [_evt(cost_usd=2.0) for _ in range(7)]
    prior: list = []
    out = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert out == []


def test_cost_spike_signature_per_bot():
    cur = [_evt(cost_usd=2.0) for _ in range(7)]
    prior = [_evt(cost_usd=0.50) for _ in range(7)]
    out_a = cost_watchdog.detect_cost_spike(
        "admin_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    out_b = cost_watchdog.detect_cost_spike(
        "security_bot", cur, prior, multiplier=2.0, floor_usd=5.0
    )
    assert out_a[0]["signature"] != out_b[0]["signature"]
    assert "admin_bot" in out_a[0]["signature"]
    assert "security_bot" in out_b[0]["signature"]


# ── detect_maintenance_ratio_high ─────────────────────────────────────────────


def _metric(*, session_count: int = 10, maintenance_ratio: float = 0.20) -> dict:
    return {
        "schema_version": 2,
        "session_count": session_count,
        "maintenance_ratio": maintenance_ratio,
    }


def test_maintenance_ratio_fires_above_threshold():
    metrics = [_metric(maintenance_ratio=0.65) for _ in range(7)]
    out = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    assert len(out) == 1
    assert out[0]["type"] == "session_quality"
    assert out[0]["details"]["maintenance_ratio_avg"] == 0.65
    assert out[0]["details"]["qualifying_days"] == 7


def test_maintenance_ratio_quiet_at_threshold():
    metrics = [_metric(maintenance_ratio=0.50) for _ in range(7)]
    out = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    assert out == []


def test_maintenance_ratio_quiet_below_threshold():
    metrics = [_metric(maintenance_ratio=0.30) for _ in range(7)]
    out = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    assert out == []


def test_maintenance_ratio_skips_zero_session_days():
    """Days with session_count=0 don't dilute the average — mirrors the
    original ScoreboardAdapter rule."""
    metrics = [_metric(maintenance_ratio=0.65) for _ in range(3)]
    # 4 dead days that would pull a naive mean down
    metrics += [_metric(session_count=0, maintenance_ratio=0.0) for _ in range(4)]
    out = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    assert len(out) == 1
    assert out[0]["details"]["qualifying_days"] == 3


def test_maintenance_ratio_quiet_when_no_qualifying_days():
    """All days are zero-session. No data → no signal."""
    metrics = [_metric(session_count=0, maintenance_ratio=0.0) for _ in range(7)]
    out = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    assert out == []


def test_maintenance_ratio_signature_per_bot():
    metrics = [_metric(maintenance_ratio=0.65) for _ in range(7)]
    out_a = cost_watchdog.detect_maintenance_ratio_high(
        "admin_bot", metrics, threshold=0.50, window_days=7
    )
    out_b = cost_watchdog.detect_maintenance_ratio_high(
        "security_bot", metrics, threshold=0.50, window_days=7
    )
    assert out_a[0]["signature"] != out_b[0]["signature"]


# ── detect_automation_dominance ───────────────────────────────────────────────


def test_automation_dominance_fires_when_ratio_high():
    # 90 heartbeats + 1 user_turn = 90/91 ≈ 99% automation
    events = [_evt(trigger_kind="heartbeat") for _ in range(90)]
    events += [_evt(trigger_kind="user_turn")]
    out = cost_watchdog.detect_automation_dominance(
        "admin_bot",
        events,
        ratio_threshold=0.95,
        min_turns=50,
        window_days=3,
    )
    assert len(out) == 1
    assert out[0]["type"] == "automation_dominance"
    assert out[0]["details"]["automation_count"] == 90
    assert out[0]["details"]["user_turn_count"] == 1
    assert out[0]["details"]["top_automation_kinds"] == {"heartbeat": 90}


def test_automation_dominance_silent_below_min_turns():
    events = [_evt(trigger_kind="heartbeat") for _ in range(10)]
    out = cost_watchdog.detect_automation_dominance(
        "admin_bot",
        events,
        ratio_threshold=0.95,
        min_turns=50,
        window_days=3,
    )
    assert out == []


def test_automation_dominance_silent_when_user_active():
    # 60 heartbeats, 60 user_turns = 50% automation
    events = [_evt(trigger_kind="heartbeat") for _ in range(60)]
    events += [_evt(trigger_kind="user_turn") for _ in range(60)]
    out = cost_watchdog.detect_automation_dominance(
        "admin_bot",
        events,
        ratio_threshold=0.95,
        min_turns=50,
        window_days=3,
    )
    assert out == []


# ── detect_cron_wakes_agent ──────────────────────────────────────────────────


def test_cron_wakes_agent_fires_for_shell_only_with_main_target():
    crons = [
        _cron(
            cron_id="abc",
            name="gateway-selfheal",
            payload_kind="systemEvent",
            session_target="main",
            wake_mode="now",
        )
    ]
    out = cost_watchdog.detect_cron_wakes_agent("admin_bot", crons)
    assert len(out) == 1
    assert out[0]["type"] == "cron_wakes_agent"
    assert out[0]["details"]["cron_id"] == "abc"
    assert out[0]["details"]["session_target"] == "main"
    assert "admin_bot/abc" in out[0]["signature"]


def test_cron_wakes_agent_silent_for_isolated_target():
    crons = [_cron(payload_kind="systemEvent", session_target="isolated")]
    assert cost_watchdog.detect_cron_wakes_agent("admin_bot", crons) == []


def test_cron_wakes_agent_silent_for_agent_turn_payload():
    crons = [_cron(payload_kind="agentTurn", session_target="main")]
    assert cost_watchdog.detect_cron_wakes_agent("admin_bot", crons) == []


def test_cron_wakes_agent_silent_for_disabled():
    crons = [_cron(enabled=False, payload_kind="systemEvent", session_target="main")]
    assert cost_watchdog.detect_cron_wakes_agent("admin_bot", crons) == []


# ── detect_cron_overactive ────────────────────────────────────────────────────


def test_cron_overactive_fires_when_actual_exceeds_expected(monkeypatch):
    # Declared every 1 hour → expected ~24/day. Simulate 96 actual fires.
    crons = [_cron(cron_id="abc", every_ms=3_600_000, payload_kind="systemEvent")]
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    runs = [
        _run_record(ts_ms=now_ms - i * 15 * 60 * 1000)
        for i in range(96)
    ]
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: runs)
    out = cost_watchdog.detect_cron_overactive(
        "admin_bot",
        crons,
        factor=1.5,
        window_hours=24,
        now=now,
    )
    assert len(out) == 1
    assert out[0]["type"] == "cron_overactive"
    assert out[0]["details"]["actual_fires"] == 96
    assert out[0]["details"]["expected_fires"] == 24
    assert out[0]["details"]["ratio"] == 4.0


def test_cron_overactive_silent_at_declared_rate(monkeypatch):
    crons = [_cron(cron_id="abc", every_ms=3_600_000)]
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    runs = [_run_record(ts_ms=now_ms - i * 60 * 60 * 1000) for i in range(24)]
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: runs)
    out = cost_watchdog.detect_cron_overactive(
        "admin_bot", crons, factor=1.5, window_hours=24, now=now
    )
    assert out == []


def test_cron_overactive_skips_cron_expression_schedules(monkeypatch):
    # cron-expr schedules have variable cadence; we don't try to estimate.
    crons = [_cron(schedule_kind="cron", cron_expr="0 3 * * *")]
    monkeypatch.setattr(
        cost_watchdog, "read_cron_runs", lambda *a, **kw: [_run_record(ts_ms=0)] * 1000
    )
    out = cost_watchdog.detect_cron_overactive(
        "admin_bot",
        crons,
        factor=1.5,
        window_hours=24,
        now=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    assert out == []


# ── detect_context_bloat ──────────────────────────────────────────────────────


def test_context_bloat_fires_per_oversized_file():
    sizes = {
        "heartbeats.md": 60 * 1024,  # over 50 KB threshold
        "SOUL.md": 40 * 1024,        # over 30 KB threshold
        "AGENTS.md": 10 * 1024,      # under
        "TOOLS.md": 0,
    }
    thresholds = dict(cost_watchdog.DEFAULTS)
    out = cost_watchdog.detect_context_bloat("admin_bot", sizes, thresholds)
    types = [d["details"]["filename"] for d in out]
    assert sorted(types) == ["SOUL.md", "heartbeats.md"]


def test_context_bloat_uses_separate_threshold_for_heartbeats():
    # 40 KB heartbeats.md is over the 30 KB SOUL threshold but under the
    # 50 KB heartbeats threshold — should NOT fire.
    sizes = {"heartbeats.md": 40 * 1024, "SOUL.md": 0, "AGENTS.md": 0, "TOOLS.md": 0}
    out = cost_watchdog.detect_context_bloat(
        "admin_bot", sizes, dict(cost_watchdog.DEFAULTS)
    )
    assert out == []


# ── runner: collect_for_bot integration with mocked readers ──────────────────


def test_collect_for_bot_combines_detectors(monkeypatch, tmp_path):
    """End-to-end: admin_bot has a 90:1 automation ratio + a shell-cron-wakes-agent
    pattern + an oversized heartbeats.md. We expect three Signals.
    """
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=0.01)] * 5  # $0.05 — silent
    window_events = [_evt(trigger_kind="heartbeat") for _ in range(90)] + [
        _evt(trigger_kind="user_turn")
    ]
    crons = [
        _cron(
            cron_id="cron-bad",
            name="gateway-selfheal",
            payload_kind="systemEvent",
            session_target="main",
            wake_mode="now",
        )
    ]
    sizes = {"heartbeats.md": 80 * 1024, "SOUL.md": 0, "AGENTS.md": 0, "TOOLS.md": 0}

    call_kinds: list[int] = []

    def fake_read_events(bot_id, days=7, shared_dir=None, *, now=None):
        # First call (days=1) returns today_events; second (window) returns window_events.
        call_kinds.append(days)
        return iter(today_events if days == 1 else window_events)

    monkeypatch.setattr(cost_watchdog, "read_events", fake_read_events)
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: crons)
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "workspace_md_sizes", lambda *a, **kw: sizes)

    detections = cost_watchdog.collect_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    types = sorted(d["type"] for d in detections)
    assert types == [
        "automation_dominance",
        "context_bloat",
        "cron_wakes_agent",
    ]


def test_run_for_bot_writes_signals_and_returns_kept(monkeypatch, tmp_path):
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3  # $6, alert
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            today_events if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(
        cost_watchdog,
        "workspace_md_sizes",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)

    kept, n = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    assert n == 1
    assert len(kept) == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == "daily_spend_high"
    assert sig.severity == "alert"
    assert sig.bot_id == "admin_bot"
    assert sig.signature in kept


def test_sweep_resolve_archives_signals_no_longer_firing(monkeypatch, tmp_path):
    """First run fires a signal; second run with cleared condition resolves it."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)

    # Run 1: $6 spend → fires
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3 if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(
        cost_watchdog,
        "workspace_md_sizes",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)
    kept1, _ = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    assert len(list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))) == 1

    # Run 2: $1 spend → silent. Sweep should resolve the prior firing.
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            [_evt(ts=f"{today}T10:00:00Z", cost_usd=0.5)] * 2 if days == 1 else []
        ),
    )
    kept2, _ = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    assert kept2 == set()
    signals_store.sweep_resolve(
        tmp_path,
        producer="cost_watchdog",
        kept_signatures=kept2,
        reason="auto-resolve: cleared",
    )
    assert list(signals_store.iter_active(tmp_path, producer="cost_watchdog")) == []


# ── breaker suppression — spec §5.5 "don't fight the breaker" ────────────────


def _trip_cost_breaker(shared_dir, bot_id):
    """Helper — trip an indefinite L1 cost breaker on the given bot."""
    from breakers import store as _bstore  # type: ignore[import]
    return _bstore.trip(
        shared_dir=shared_dir,
        scope=bot_id,
        breaker_type="cost",
        duration=None,
        initiated_by="test",
        reason="suppression test",
    )


def test_cost_signal_suppressed_when_cost_breaker_tripped(monkeypatch, tmp_path):
    """daily_spend_high MUST NOT be observed for a bot whose cost breaker
    is tripped — that's the "don't pile on" contract."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3  # $6, would fire
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            today_events if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "workspace_md_sizes", lambda *a, **kw: {})
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)

    _trip_cost_breaker(tmp_path, "admin_bot")

    kept, _ = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    # Signature kept so sweep_resolve doesn't mistakenly mark cleared.
    assert len(kept) == 1
    # No signal written — suppression skipped the observe() call.
    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    assert sigs == []


def test_cost_signal_NOT_suppressed_for_other_bot(monkeypatch, tmp_path):
    """A breaker on security_bot must not suppress signals on admin_bot."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            today_events if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "workspace_md_sizes", lambda *a, **kw: {})
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)

    _trip_cost_breaker(tmp_path, "security_bot")   # different bot

    kept, _ = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    assert len(sigs) == 1
    assert sigs[0].bot_id == "admin_bot"


def test_cost_signal_suppressed_when_pod_full_tripped(monkeypatch, tmp_path):
    """Pod-wide L2 suppresses every per-bot cost signal."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            today_events if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "workspace_md_sizes", lambda *a, **kw: {})
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)

    from breakers import store as _bstore  # type: ignore[import]
    _bstore.trip(
        shared_dir=tmp_path, scope="pod", breaker_type="full",
        duration=None, initiated_by="test", reason="pod halt",
    )

    cost_watchdog.run_for_bot("admin_bot", tmp_path, config={}, today=today, now=now)
    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    assert sigs == []


def test_suppression_failure_is_fail_open(monkeypatch, tmp_path):
    """If the suppression check itself raises, we proceed with normal
    observe() — never silently lose alerts because the suppression
    helper broke."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    today_events = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0)] * 3
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter(
            today_events if days == 1 else []
        ),
    )
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "workspace_md_sizes", lambda *a, **kw: {})
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)

    # Force the suppression check to raise.
    from breakers import suppression as _sup
    monkeypatch.setattr(
        _sup, "find_suppressing_breaker",
        lambda *a, **kw: (_ for _ in ()).throw(IOError("synthetic")),
    )

    cost_watchdog.run_for_bot("admin_bot", tmp_path, config={}, today=today, now=now)
    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    # Fail-open — the signal still landed.
    assert len(sigs) == 1


# ── threshold config plumbing ────────────────────────────────────────────────


def test_per_bot_threshold_override():
    config = {
        "cost_watchdog": {
            "defaults": {"daily_spend_usd": 5.0},
            "bots": {"admin_bot": {"daily_spend_usd": 10.0}},
        }
    }
    admin_bot_t = cost_watchdog._thresholds_for_bot("admin_bot", config)
    assert admin_bot_t["daily_spend_usd"] == 10.0
    team_bot_c_t = cost_watchdog._thresholds_for_bot("team_bot_c", config)
    assert team_bot_c_t["daily_spend_usd"] == 5.0
    other_t = cost_watchdog._thresholds_for_bot("admin_bot", {})
    assert other_t["daily_spend_usd"] == cost_watchdog.DEFAULTS["daily_spend_usd"]


# ── readers: jobs.json + runs jsonl parse robustness ─────────────────────────


def test_read_cron_jobs_handles_dict_form(monkeypatch, tmp_path):
    """jobs.json stored as {version, jobs: [...]}."""
    bot_root = tmp_path / "bot_home"
    cron_dir = bot_root / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    jobs_path = cron_dir / "jobs.json"
    jobs_path.write_text(json.dumps({"version": 1, "jobs": [_cron()]}))
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: bot_root)
    out = cost_watchdog.read_cron_jobs("admin_bot")
    assert len(out) == 1
    assert out[0]["id"] == "cron-1"


def test_read_cron_jobs_returns_empty_on_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: tmp_path / "nope")
    monkeypatch.setattr(
        cost_watchdog, "_read_with_sudo_fallback", lambda p: None
    )
    assert cost_watchdog.read_cron_jobs("admin_bot") == []


def test_workspace_md_sizes_scans_all_md_files(monkeypatch, tmp_path):
    """Generalized scan picks up arbitrary *.md filenames, top-level only."""
    bot_root = tmp_path / "bot_home"
    workspace = bot_root / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "HEARTBEAT.md").write_text("x" * 1024)
    (workspace / "SECURITY_BOT_MANUAL.md").write_text("x" * 2048)
    (workspace / "AGENTS.md").write_text("x" * 100)
    (workspace / "main.py").write_text("not a markdown")  # filtered
    (workspace / "subdir").mkdir()  # filtered (dir)
    (workspace / "subdir" / "DEEP.md").write_text("x" * 99999)  # not recursed
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: bot_root)
    sizes = cost_watchdog.workspace_md_sizes("admin_bot")
    assert set(sizes) == {"HEARTBEAT.md", "SECURITY_BOT_MANUAL.md", "AGENTS.md"}
    assert sizes["HEARTBEAT.md"] == 1024
    assert sizes["SECURITY_BOT_MANUAL.md"] == 2048


def test_workspace_md_sizes_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: tmp_path / "nope")
    assert cost_watchdog.workspace_md_sizes("admin_bot") == {}


def test_threshold_kb_for_file_uses_heartbeat_bucket_case_insensitive():
    thresholds = dict(cost_watchdog.DEFAULTS)
    # HEARTBEAT.md (uppercase) should still hit the heartbeat bucket
    assert (
        cost_watchdog._threshold_kb_for_file("HEARTBEAT.md", thresholds)
        == thresholds["context_bloat_kb_heartbeats"]
    )
    assert (
        cost_watchdog._threshold_kb_for_file("heartbeats.md", thresholds)
        == thresholds["context_bloat_kb_heartbeats"]
    )
    assert (
        cost_watchdog._threshold_kb_for_file("SOUL.md", thresholds)
        == thresholds["context_bloat_kb"]
    )


def test_threshold_kb_for_file_per_file_override_wins():
    thresholds = dict(cost_watchdog.DEFAULTS)
    thresholds["context_bloat_files"] = {"USER.md": 200}
    assert cost_watchdog._threshold_kb_for_file("USER.md", thresholds) == 200
    # Default bucket otherwise
    assert (
        cost_watchdog._threshold_kb_for_file("AGENTS.md", thresholds)
        == thresholds["context_bloat_kb"]
    )


def test_context_bloat_scans_dynamic_filenames():
    """Detector now iterates the sizes dict directly — picks up novel names."""
    sizes = {
        "HEARTBEAT.md": 80 * 1024,        # over heartbeat threshold (50KB)
        "SECURITY_BOT_MANUAL.md": 60 * 1024,    # over default threshold (30KB)
        "USER.md": 20 * 1024,             # under default threshold
    }
    out = cost_watchdog.detect_context_bloat(
        "admin_bot", sizes, dict(cost_watchdog.DEFAULTS)
    )
    fired = sorted(d["details"]["filename"] for d in out)
    assert fired == ["HEARTBEAT.md", "SECURITY_BOT_MANUAL.md"]


def test_context_bloat_per_file_override_silences():
    """Per-bot override on a specific file silences a known-large doc."""
    sizes = {"USER.md": 200 * 1024}
    thresholds = dict(cost_watchdog.DEFAULTS)
    thresholds["context_bloat_files"] = {"USER.md": 500}  # 500KB cap
    assert cost_watchdog.detect_context_bloat("admin_bot", sizes, thresholds) == []


# ── detect_session_token_outlier ─────────────────────────────────────────────


def test_session_token_outlier_fires_for_runaway_session():
    """One session at $5 against a median of $0.10 is the smoking-gun case."""
    # 9 normal sessions, each with 5+ events at low cost
    events = []
    for sid_n in range(9):
        for _ in range(6):
            events.append(
                _evt(
                    session_id=f"normal-{sid_n}",
                    cost_usd=0.02,
                    trigger_kind="user_turn",
                )
            )
    # 1 outlier session at high cost
    for _ in range(40):
        events.append(
            _evt(session_id="runaway", cost_usd=0.20, trigger_kind="heartbeat")
        )
    out = cost_watchdog.detect_session_token_outlier(
        "admin_bot",
        events,
        factor=3.0,
        min_session_events=5,
        min_cost_usd=0.50,
        max_per_run=5,
    )
    assert len(out) == 1
    assert out[0]["details"]["session_id"] == "runaway"
    assert out[0]["details"]["ratio"] >= 3.0
    assert "admin_bot/runaway" in out[0]["signature"]


def test_session_token_outlier_silent_below_absolute_floor():
    """A 5× ratio on a tiny absolute cost must not fire (noise floor)."""
    events = [
        _evt(session_id="s1", cost_usd=0.01, trigger_kind="user_turn") for _ in range(6)
    ]
    events += [
        _evt(session_id="s2", cost_usd=0.01) for _ in range(6)
    ]
    events += [
        _evt(session_id="s3", cost_usd=0.01) for _ in range(6)
    ]
    events += [
        _evt(session_id="big", cost_usd=0.05) for _ in range(6)  # 5× median, but $0.30 absolute
    ]
    out = cost_watchdog.detect_session_token_outlier(
        "admin_bot",
        events,
        factor=3.0,
        min_session_events=5,
        min_cost_usd=0.50,
        max_per_run=5,
    )
    assert out == []


def test_session_token_outlier_caps_at_max_per_run():
    """Many simultaneous outliers cap at max_per_run (avoid spam)."""
    events = []
    for sid_n in range(15):
        for _ in range(6):
            events.append(_evt(session_id=f"normal-{sid_n}", cost_usd=0.02))
    # 10 outliers — should cap at 3
    for sid_n in range(10):
        for _ in range(40):
            events.append(_evt(session_id=f"outlier-{sid_n}", cost_usd=0.20))
    out = cost_watchdog.detect_session_token_outlier(
        "admin_bot",
        events,
        factor=3.0,
        min_session_events=5,
        min_cost_usd=0.50,
        max_per_run=3,
    )
    assert len(out) == 3


def test_session_token_outlier_silent_when_too_few_sessions():
    events = [_evt(session_id="s1", cost_usd=10.0) for _ in range(6)]
    out = cost_watchdog.detect_session_token_outlier(
        "admin_bot",
        events,
        factor=3.0,
        min_session_events=5,
        min_cost_usd=0.50,
        max_per_run=5,
    )
    assert out == []


# ── detect_heartbeat_no_model_override (retired 2026-06-04) ──────────────────
# ModelRouter (Evolve plugin) intercepts heartbeat model selection via the
# `before_model_resolve` hook and routes to tier3.models[0] from
# evolve-tiers.json — `agents.defaults.heartbeat.model` is dead config on the
# heartbeat path. The detector is a no-op stub; these tests pin that
# retirement so a future re-enable has to update the tests too.


def _oc_json(*, primary: str, hb_every: str | None, hb_model: str | None) -> dict:
    hb: dict = {}
    if hb_every is not None:
        hb["every"] = hb_every
    if hb_model is not None:
        hb["model"] = hb_model
    return {
        "agents": {
            "defaults": {
                "model": {"primary": primary},
                "heartbeat": hb if hb else {},
            }
        }
    }


def test_heartbeat_no_model_override_retired_returns_empty_for_sonnet_primary():
    """Retired: ModelRouter intercepts; the literal `heartbeat.model` is unused."""
    oc = _oc_json(primary="anthropic/claude-sonnet-4-6", hb_every="1h", hb_model=None)
    assert cost_watchdog.detect_heartbeat_no_model_override("admin_bot", oc) == []


def test_heartbeat_no_model_override_retired_returns_empty_with_override_set():
    oc = _oc_json(
        primary="anthropic/claude-sonnet-4-6",
        hb_every="1h",
        hb_model="anthropic/claude-haiku-4-5",
    )
    assert cost_watchdog.detect_heartbeat_no_model_override("admin_bot", oc) == []


def test_heartbeat_no_model_override_retired_returns_empty_when_heartbeat_disabled():
    oc = _oc_json(primary="anthropic/claude-sonnet-4-6", hb_every=None, hb_model=None)
    assert cost_watchdog.detect_heartbeat_no_model_override("admin_bot", oc) == []


def test_heartbeat_no_model_override_retired_returns_empty_when_primary_low_tier():
    oc = _oc_json(primary="anthropic/claude-haiku-4-5", hb_every="1h", hb_model=None)
    assert cost_watchdog.detect_heartbeat_no_model_override("admin_bot", oc) == []


def test_heartbeat_no_model_override_retired_returns_empty_when_openclaw_json_missing():
    assert cost_watchdog.detect_heartbeat_no_model_override("admin_bot", None) == []


def test_read_openclaw_json_handles_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: tmp_path / "nope")
    monkeypatch.setattr(cost_watchdog, "_read_with_sudo_fallback", lambda p: None)
    assert cost_watchdog.read_openclaw_json("admin_bot") is None


# ── detect_model_override_violated (retired 2026-06-04) ────────────────────────
# Same retirement rationale as detect_heartbeat_no_model_override: ModelRouter
# (Evolve plugin) intercepts heartbeat model selection. The detector's "leak"
# interpretation calls intentional tier-3 routing a violation — actively
# misleading. Stub returns []; these tests pin that retirement so a future
# re-enable has to update them too.


def _retired_mov_oc(*, override: str | None = "anthropic/claude-haiku-4-5") -> dict:
    hb: dict = {"every": "30m"}
    if override is not None:
        hb["model"] = override
    return {
        "agents": {
            "defaults": {
                "model": {"primary": "anthropic/claude-sonnet-4-6"},
                "heartbeat": hb,
            }
        }
    }


def _retired_mov_event(*, session_id: str, model: str, cost_usd: float = 0.10) -> dict:
    return {
        "session_id": session_id,
        "trigger_kind": "heartbeat",
        "provider": "anthropic",
        "model": model,
        "cost_usd": cost_usd,
    }


def test_model_override_violated_retired_no_fire_with_override_mismatch():
    """The historic positive-fire case (session billed on non-override model)
    is now a no-op — ModelRouter routes intentionally."""
    oc = _retired_mov_oc(override="anthropic/claude-haiku-4-5")
    events = [
        _retired_mov_event(session_id="s1", model="claude-sonnet-4-6", cost_usd=0.50),
    ]
    assert cost_watchdog.detect_model_override_violated(
        "security_bot", events, oc, min_cost_usd=0.10, max_per_run=5
    ) == []


def test_model_override_violated_retired_no_fire_without_override():
    oc = _retired_mov_oc(override=None)
    events = [_retired_mov_event(session_id="s1", model="claude-sonnet-4-6")]
    assert cost_watchdog.detect_model_override_violated(
        "security_bot", events, oc, min_cost_usd=0.10, max_per_run=5
    ) == []


def test_model_override_violated_retired_no_fire_when_openclaw_json_missing():
    assert cost_watchdog.detect_model_override_violated(
        "security_bot", [], None, min_cost_usd=0.10, max_per_run=5
    ) == []


# ── detect_heartbeat_session_bloat ───────────────────────────────────────────


def _bloat_kwargs(
    *, warn=5, alert=15, critical=30, max_per_run=10,
) -> dict:
    return dict(
        warn_turns=warn,
        alert_turns=alert,
        critical_turns=critical,
        max_per_run=max_per_run,
    )


def test_heartbeat_bloat_silent_at_or_below_warn():
    turns = [_turn(session_id="s") for _ in range(5)]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert out == []


def test_heartbeat_bloat_fires_warn_tier_above_threshold():
    turns = [_turn(session_id="s") for _ in range(7)]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert len(out) == 1
    d = out[0]
    assert d["type"] == "heartbeat_session_bloat"
    assert d["severity"] == "warn"
    assert d["details"]["tier"] == "warn"
    assert d["details"]["turn_count"] == 7
    assert d["details"]["magnitude"] == 1
    assert d["details"]["catalog_event"] == "cost.heartbeat_session_bloat"


def test_heartbeat_bloat_fires_alert_tier_above_alert_threshold():
    turns = [_turn(session_id="s") for _ in range(20)]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert len(out) == 1
    d = out[0]
    assert d["severity"] == "alert"
    assert d["details"]["tier"] == "alert"
    assert d["details"]["magnitude"] == 2
    assert d["details"]["catalog_event"] == "cost.heartbeat_session_bloat"


def test_heartbeat_bloat_fires_critical_tier_above_critical_threshold():
    turns = [_turn(session_id="s") for _ in range(40)]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert len(out) == 1
    d = out[0]
    # Signal-side severity caps at "alert" — only catalog goes CRITICAL.
    assert d["severity"] == "alert"
    assert d["details"]["tier"] == "critical"
    assert d["details"]["magnitude"] == 3
    # 2026-05-29 warn/critical collapse: single catalog event with tier
    # carried in details + producer-rendered level_emoji/trail.
    assert d["details"]["catalog_event"] == "cost.heartbeat_session_bloat"
    assert d["details"]["level_emoji"] == "🔴"
    assert "Likely runaway loop" in d["details"]["trail"]


def test_heartbeat_bloat_ignores_non_heartbeat_sessions():
    turns = [
        _turn(session_id="s", channel="telegram", source="human")
        for _ in range(40)
    ]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert out == []


def test_heartbeat_bloat_matches_on_source_only():
    # channel can be empty/blank — source=heartbeat alone is sufficient.
    turns = [
        _turn(session_id="s", channel="", source="heartbeat")
        for _ in range(8)
    ]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert len(out) == 1


def test_heartbeat_bloat_groups_independent_sessions():
    turns = (
        [_turn(session_id="big", model="claude-sonnet-4-6") for _ in range(20)]
        + [_turn(session_id="small") for _ in range(3)]   # below threshold
        + [_turn(session_id="medium") for _ in range(8)]
    )
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    # Only the two above-threshold sessions fire.
    assert len(out) == 2
    ids = sorted(d["details"]["session_id"] for d in out)
    assert ids == ["big", "medium"]


def test_heartbeat_bloat_signature_dedups_per_session():
    turns = [_turn(session_id="s") for _ in range(7)]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert len(out) == 1
    # Signature includes bot_id and session_id, so two ticks against the
    # same JSONL produce the same signature (Signal store will update
    # observation_count rather than create a duplicate).
    sig = out[0]["signature"]
    out2 = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert out2[0]["signature"] == sig
    # Different bot or different session → different signature.
    different_bot = cost_watchdog.detect_heartbeat_session_bloat(
        "team_bot_a", turns, **_bloat_kwargs()
    )
    assert different_bot[0]["signature"] != sig


def test_heartbeat_bloat_caps_at_max_per_run():
    turns: list[dict] = []
    for i in range(20):
        turns.extend(_turn(session_id=f"s{i}") for _ in range(7))
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs(max_per_run=5)
    )
    assert len(out) == 5


def test_heartbeat_bloat_ignores_turns_without_session_id():
    # Legacy/malformed turns with empty session_id should never produce
    # a signal — grouping them under "" would conflate every bot's
    # malformed turns into one bogus "session."
    turns = [_turn(session_id="") for _ in range(40)]
    assert cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    ) == []


def test_heartbeat_bloat_replay_security_bot_2026_05_20():
    """The actual incident — security_bot's 2026-05-20 turn JSONL.

    Replay against the captured fixture. The top heartbeat session
    (sid 51bac282 — the 18:32 burst) ran 58 turns in the JSONL on
    disk (the incident report's "40 turns" was a forensic estimate;
    the raw fixture has 58). Second-largest (aae2c6bf) ran 37 turns.
    The detector should classify the 58-turn session as critical
    (>30) and the 37-turn session also as critical, plus pick up
    smaller alert/warn-tier sessions.

    Assertions pin the exact top counts so a future fixture
    regeneration that loses the runaway tail can't slip past the
    `>= 30` lower bound — that's the bug the original review
    process missed when this test was first written.
    """
    fixture = (
        Path(__file__).parent / "fixtures" / "spike-2026-05-20" /
        "security_bot-turns-2026-05-20.jsonl"
    )
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    turns = [
        json.loads(line) for line in fixture.read_text().splitlines() if line.strip()
    ]
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs(max_per_run=20)
    )
    assert out, "expected at least one heartbeat bloat on the 2026-05-20 fixture"

    tiers = [d["details"]["tier"] for d in out]
    counts = sorted((d["details"]["turn_count"] for d in out), reverse=True)
    # Pin both top counts so a future fixture regeneration can't
    # silently weaken the test by capping at e.g. 35 turns.
    assert counts[0] == 58, (
        f"expected top security_bot heartbeat session at 58 turns "
        f"(sid 51bac282), got {counts[0]}"
    )
    assert counts[1] == 37, (
        f"expected second-largest security_bot heartbeat session at 37 turns "
        f"(sid aae2c6bf), got {counts[1]}"
    )
    assert "critical" in tiers, (
        f"expected critical tier on the May 20 fixture, got tiers={tiers}"
    )
    # The runaway sessions must carry Sonnet in models_seen — that's
    # the model-override leak surface from #1386 + the heartbeat-bloat
    # surface combined into a single regression check.
    critical = [d for d in out if d["details"]["tier"] == "critical"]
    sonnet_present = any(
        any("sonnet" in m.lower() for m in d["details"]["models_seen"])
        for d in critical
    )
    assert sonnet_present, (
        "expected at least one critical-tier session to carry Sonnet "
        "in models_seen (the leak surface from PR #1386)"
    )


def test_heartbeat_bloat_replay_silent_on_healthy_baseline():
    """Synthetic clean baseline — every session ≤3 turns, no fires.

    Confirms the detector doesn't produce noise on a normal day.
    """
    turns: list[dict] = []
    for i in range(50):
        turns.extend(
            _turn(session_id=f"sess-{i}")
            for _ in range(2)  # 2 turns per session — well below warn
        )
    out = cost_watchdog.detect_heartbeat_session_bloat(
        "security_bot", turns, **_bloat_kwargs()
    )
    assert out == []


def test_read_today_turns_returns_empty_on_import_failure(monkeypatch):
    # If usage_analytics is unavailable (e.g. PYTHONPATH wonkery on a
    # subprocess), the detector reads as "no data" rather than crashing.
    # We can't easily uninstall the module, but we can confirm the
    # function tolerates load_turns raising.
    import usage_analytics as _ua

    def fake_load_turns(*a, **kw):
        raise RuntimeError("simulated discovery failure")

    monkeypatch.setattr(_ua, "load_turns", fake_load_turns)
    out = cost_watchdog.read_today_turns("security_bot")
    assert out == []


def test_read_cron_runs_filters_by_since_ms(monkeypatch, tmp_path):
    bot_root = tmp_path / "bot_home"
    runs_dir = bot_root / ".openclaw" / "cron" / "runs"
    runs_dir.mkdir(parents=True)
    runs_file = runs_dir / "cron-1.jsonl"
    runs_file.write_text(
        "\n".join(
            json.dumps(_run_record(ts_ms=t))
            for t in [1_000_000, 2_000_000, 3_000_000]
        )
    )
    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: bot_root)
    out = cost_watchdog.read_cron_runs("admin_bot", "cron-1", since_ms=1_500_000)
    assert [r["ts"] for r in out] == [2_000_000, 3_000_000]


# ── Gap A: workspace_md_sizes recurses into known OC subdirs ─────────────────


def test_workspace_md_sizes_scans_memory_subdir(monkeypatch, tmp_path):
    """Security_bot's bloated 2026-05-*.md files live in workspace/memory/.
    The scan must descend into memory/ (and memory/journal/) but stay
    out of unknown subdirs.
    """
    workspace = tmp_path / "security_bot" / ".openclaw" / "workspace"
    (workspace / "memory" / "journal").mkdir(parents=True)
    (workspace / "apps" / "weather").mkdir(parents=True)

    (workspace / "HEARTBEAT.md").write_text("x" * 1000)
    (workspace / "memory" / "2026-05-02.md").write_text("x" * 196_000)
    (workspace / "memory" / "2026-05-03.md").write_text("x" * 1000)
    (workspace / "memory" / "journal" / "2026-05-15.md").write_text("x" * 5000)
    # Should be ignored — unknown subdir
    (workspace / "apps" / "weather" / "log.md").write_text("x" * 100_000)
    # Non-markdown file — ignored
    (workspace / "memory" / "main.sqlite").write_text("x" * 50_000)

    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: tmp_path / "security_bot")
    sizes = cost_watchdog.workspace_md_sizes("security_bot")

    assert "HEARTBEAT.md" in sizes
    assert sizes["HEARTBEAT.md"] == 1000
    assert "memory/2026-05-02.md" in sizes
    assert sizes["memory/2026-05-02.md"] == 196_000
    assert "memory/2026-05-03.md" in sizes
    assert "memory/journal/2026-05-15.md" in sizes
    # Unknown subdir not scanned
    assert "apps/weather/log.md" not in sizes
    # Non-md file not included
    assert "memory/main.sqlite" not in sizes


def test_workspace_md_sizes_missing_subdirs_silent(monkeypatch, tmp_path):
    """If memory/ doesn't exist (newly-deployed bot), scan returns top-level
    only without raising.
    """
    workspace = tmp_path / "team_bot_b" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "HEARTBEAT.md").write_text("x" * 500)

    monkeypatch.setattr(cost_watchdog, "_bot_home", lambda *a, **kw: tmp_path / "team_bot_b")
    sizes = cost_watchdog.workspace_md_sizes("team_bot_b")
    assert sizes == {"HEARTBEAT.md": 500}


def test_context_bloat_fires_on_memory_subdir_file():
    """Backtest the Security_bot shape: workspace/memory/2026-05-02.md at 196 KB
    should fire context_bloat via the (extended) workspace_md_sizes scan.
    """
    sizes = {
        "HEARTBEAT.md": 5 * 1024,           # under
        "memory/2026-05-02.md": 196 * 1024,  # way over 30 KB default
        "memory/2026-05-03.md": 10 * 1024,   # under
    }
    out = cost_watchdog.detect_context_bloat(
        "security_bot", sizes, dict(cost_watchdog.DEFAULTS)
    )
    filenames = sorted(d["details"]["filename"] for d in out)
    assert filenames == ["memory/2026-05-02.md"]
    # Magnitude should be 2 (196 KB is > 3× the 30 KB default threshold)
    assert out[0]["details"]["magnitude"] == 2


# ── Workspace snapshot persistence helpers ───────────────────────────────────


def test_workspace_snapshot_roundtrip(tmp_path):
    sizes_today = {"HEARTBEAT.md": 5000, "memory/2026-05-02.md": 196_000}
    cost_watchdog.write_workspace_snapshot(
        tmp_path, "security_bot", sizes_today, today="2026-05-28"
    )
    history = cost_watchdog.read_workspace_snapshots(
        tmp_path, "security_bot", days=7, today="2026-05-28"
    )
    assert len(history) == 1
    date, sizes = history[0]
    assert date == "2026-05-28"
    assert sizes == sizes_today


def test_workspace_snapshot_skips_empty(tmp_path):
    # Empty sizes shouldn't create an empty file.
    cost_watchdog.write_workspace_snapshot(tmp_path, "security_bot", {}, today="2026-05-28")
    history = cost_watchdog.read_workspace_snapshots(
        tmp_path, "security_bot", days=7, today="2026-05-28"
    )
    assert history == []


def test_workspace_snapshot_overwrites_same_day(tmp_path):
    cost_watchdog.write_workspace_snapshot(
        tmp_path, "security_bot", {"a.md": 100}, today="2026-05-28"
    )
    cost_watchdog.write_workspace_snapshot(
        tmp_path, "security_bot", {"a.md": 200}, today="2026-05-28"
    )
    history = cost_watchdog.read_workspace_snapshots(
        tmp_path, "security_bot", days=7, today="2026-05-28"
    )
    assert history == [("2026-05-28", {"a.md": 200})]


def test_workspace_snapshot_reads_window(tmp_path):
    for i, kb in enumerate([50, 100, 150, 200]):
        d = (datetime(2026, 5, 28) - timedelta(days=3 - i)).strftime("%Y-%m-%d")
        cost_watchdog.write_workspace_snapshot(
            tmp_path, "security_bot", {"memory/log.md": kb * 1024}, today=d
        )
    history = cost_watchdog.read_workspace_snapshots(
        tmp_path, "security_bot", days=7, today="2026-05-28"
    )
    # Sorted ascending; the oldest is 50 KB, newest 200 KB
    assert len(history) == 4
    assert history[0][1]["memory/log.md"] == 50 * 1024
    assert history[-1][1]["memory/log.md"] == 200 * 1024


# ── Gap B: detect_workspace_growth_rate ──────────────────────────────────────


def _growth_kwargs(**overrides):
    base = dict(
        growth_kb_per_day_threshold=3.0,
        min_window_days=5,
        min_current_kb=20.0,
        max_per_run=5,
    )
    base.update(overrides)
    return base


def test_workspace_growth_rate_fires_on_steady_growth():
    """File grew 50 KB → 200 KB over 10 days = 15 KB/day, above 3 KB/day
    threshold. Should fire with alert severity (>= 2× threshold)."""
    history = [
        ("2026-05-18", {"memory/2026-05-02.md": 50 * 1024}),
        ("2026-05-28", {"memory/2026-05-02.md": 200 * 1024}),
    ]
    sizes_today = {"memory/2026-05-02.md": 200 * 1024}
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot", sizes_today, history, **_growth_kwargs()
    )
    assert len(out) == 1
    d = out[0]["details"]
    assert d["filename"] == "memory/2026-05-02.md"
    assert d["growth_kb_per_day"] == 15.0
    assert d["window_days"] == 10
    assert out[0]["severity"] == "alert"
    assert d["magnitude"] == 2


def test_workspace_growth_rate_quiet_below_threshold():
    """5 KB → 10 KB over 10 days = 0.5 KB/day, under 3 KB/day default."""
    history = [
        ("2026-05-18", {"a.md": 5 * 1024}),
        ("2026-05-28", {"a.md": 10 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot", {"a.md": 10 * 1024}, history, **_growth_kwargs()
    )
    assert out == []


def test_workspace_growth_rate_quiet_below_min_current_kb():
    """File grew 1 KB → 16 KB at 1.5 KB/day, but current is below the
    20 KB floor — we don't want to fire on tiny rotating files."""
    history = [
        ("2026-05-18", {"a.md": 1 * 1024}),
        ("2026-05-28", {"a.md": 16 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot", {"a.md": 16 * 1024}, history, **_growth_kwargs()
    )
    assert out == []


def test_workspace_growth_rate_quiet_with_short_window():
    """Only 2 days of history — below the 5-day min."""
    history = [
        ("2026-05-27", {"big.md": 30 * 1024}),
        ("2026-05-28", {"big.md": 100 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot", {"big.md": 100 * 1024}, history, **_growth_kwargs()
    )
    assert out == []


def test_workspace_growth_rate_quiet_on_new_file():
    """File only exists in current snapshot, not in oldest — no baseline.
    Security_bot's daily memory logs rotate to new files; the *trajectory* of
    each new file should not retroactively trigger from-zero ratios."""
    history = [
        ("2026-05-18", {}),
        ("2026-05-28", {"memory/2026-05-25.md": 50 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot",
        {"memory/2026-05-25.md": 50 * 1024},
        history,
        **_growth_kwargs(),
    )
    assert out == []


def test_workspace_growth_rate_quiet_on_shrink():
    """Operator trimmed the file. No-op, not a growth signal."""
    history = [
        ("2026-05-18", {"big.md": 200 * 1024}),
        ("2026-05-28", {"big.md": 50 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot", {"big.md": 50 * 1024}, history, **_growth_kwargs()
    )
    assert out == []


def test_workspace_growth_rate_signature_per_file():
    """Different files must produce distinct signatures."""
    history = [
        ("2026-05-18", {"a.md": 50 * 1024, "b.md": 50 * 1024}),
        ("2026-05-28", {"a.md": 200 * 1024, "b.md": 200 * 1024}),
    ]
    out = cost_watchdog.detect_workspace_growth_rate(
        "security_bot",
        {"a.md": 200 * 1024, "b.md": 200 * 1024},
        history,
        **_growth_kwargs(),
    )
    sigs = {d["signature"] for d in out}
    assert len(sigs) == 2


# ── Gap C: detect_efficiency_drift ────────────────────────────────────────────


def _eff_kwargs(**overrides):
    base = dict(
        multiplier=2.0,
        cur_window_days=7,
        prior_window_days=21,
        min_cur_calls=20,
        min_prior_calls=50,
        max_per_run=4,
    )
    base.update(overrides)
    return base


def _haiku_evt(cost_usd: float) -> dict:
    e = _evt()
    e["model"] = "claude-haiku-4-5"
    e["cost_usd"] = cost_usd
    return e


def _sonnet_evt(cost_usd: float) -> dict:
    e = _evt()
    e["model"] = "claude-sonnet-4-6"
    e["cost_usd"] = cost_usd
    return e


def test_efficiency_drift_fires_when_low_tier_per_call_doubles():
    """Security_bot-shape: Haiku calls at $0.005/call baseline, now $0.07/call.
    14× ratio, well above 2× threshold; alert severity."""
    cur = [_haiku_evt(0.07) for _ in range(100)]   # 7d, 100 calls @ $0.07
    prior = [_haiku_evt(0.005) for _ in range(300)]  # 21d, 300 calls @ $0.005
    out = cost_watchdog.detect_efficiency_drift(
        "security_bot", cur, prior, **_eff_kwargs()
    )
    assert len(out) == 1
    d = out[0]["details"]
    assert d["tier"] == "low"
    assert d["ratio"] >= 10
    assert out[0]["severity"] == "alert"


def test_efficiency_drift_quiet_when_per_call_flat():
    cur = [_sonnet_evt(0.03) for _ in range(100)]
    prior = [_sonnet_evt(0.03) for _ in range(300)]
    out = cost_watchdog.detect_efficiency_drift(
        "admin_bot", cur, prior, **_eff_kwargs()
    )
    assert out == []


def test_efficiency_drift_quiet_below_min_calls():
    """Tiny sample → noisy ratio → suppress."""
    cur = [_haiku_evt(0.20) for _ in range(5)]
    prior = [_haiku_evt(0.01) for _ in range(5)]
    out = cost_watchdog.detect_efficiency_drift(
        "personal_bot", cur, prior, **_eff_kwargs()
    )
    assert out == []


def test_efficiency_drift_isolates_tier():
    """High-tier cost-per-call unchanged; only the low tier drifted.
    Result: one Signal scoped to (bot, low)."""
    cur = (
        [_sonnet_evt(0.03) for _ in range(100)]
        + [_haiku_evt(0.07) for _ in range(100)]
    )
    prior = (
        [_sonnet_evt(0.03) for _ in range(300)]
        + [_haiku_evt(0.005) for _ in range(300)]
    )
    out = cost_watchdog.detect_efficiency_drift(
        "security_bot", cur, prior, **_eff_kwargs()
    )
    assert len(out) == 1
    assert out[0]["details"]["tier"] == "low"


def test_efficiency_drift_skips_unknown_tier():
    """Unknown-tier model — uninterpretable, suppress."""
    unknown = _evt()
    unknown["model"] = "mystery-model-9000"
    cur = [{**unknown, "cost_usd": 0.20} for _ in range(100)]
    prior = [{**unknown, "cost_usd": 0.01} for _ in range(300)]
    out = cost_watchdog.detect_efficiency_drift(
        "personal_bot", cur, prior, **_eff_kwargs()
    )
    assert out == []


# ── Gap D: detect_cache_write_volume ─────────────────────────────────────────


def _cache_kwargs(**overrides):
    base = dict(
        multiplier=2.0,
        cur_window_days=7,
        prior_window_days=21,
        min_cur_calls=20,
        min_prior_calls=50,
        min_cur_tokens_per_call=5000,
    )
    base.update(overrides)
    return base


def _evt_with_cache(cache_write: int) -> dict:
    e = _evt()
    e["cache_write_tokens"] = cache_write
    return e


def test_cache_write_volume_fires_on_envelope_growth():
    """Security_bot-shape: 5K → 25K cache_write_tokens/call. 5× ratio."""
    cur = [_evt_with_cache(25_000) for _ in range(100)]
    prior = [_evt_with_cache(5_000) for _ in range(300)]
    out = cost_watchdog.detect_cache_write_volume(
        "security_bot", cur, prior, **_cache_kwargs()
    )
    assert len(out) == 1
    d = out[0]["details"]
    assert d["cur_tokens_per_call"] == 25_000
    assert d["prior_tokens_per_call"] == 5_000
    assert d["ratio"] == 5.0
    assert out[0]["severity"] == "alert"


def test_cache_write_volume_quiet_when_flat():
    cur = [_evt_with_cache(10_000) for _ in range(100)]
    prior = [_evt_with_cache(10_000) for _ in range(300)]
    out = cost_watchdog.detect_cache_write_volume(
        "admin_bot", cur, prior, **_cache_kwargs()
    )
    assert out == []


def test_cache_write_volume_quiet_below_floor():
    """Mathematically a 5× jump (100→500 tokens/call) but below the
    5K-tokens-per-call floor: operationally irrelevant."""
    cur = [_evt_with_cache(500) for _ in range(100)]
    prior = [_evt_with_cache(100) for _ in range(300)]
    out = cost_watchdog.detect_cache_write_volume(
        "admin_bot", cur, prior, **_cache_kwargs()
    )
    assert out == []


def test_cache_write_volume_quiet_when_prior_zero():
    """No baseline → silent (cold-start protection)."""
    cur = [_evt_with_cache(20_000) for _ in range(100)]
    prior = [_evt_with_cache(0) for _ in range(300)]
    out = cost_watchdog.detect_cache_write_volume(
        "personal_bot", cur, prior, **_cache_kwargs()
    )
    assert out == []


def test_cache_write_volume_quiet_below_min_calls():
    cur = [_evt_with_cache(50_000) for _ in range(5)]
    prior = [_evt_with_cache(5_000) for _ in range(10)]
    out = cost_watchdog.detect_cache_write_volume(
        "admin_bot", cur, prior, **_cache_kwargs()
    )
    assert out == []


# ── collect_for_bot wires the new detectors ──────────────────────────────────


def test_collect_for_bot_fires_phase1_detectors_on_security_bot_shape(
    monkeypatch, tmp_path
):
    """End-to-end backtest of the Security_bot case: a bot with a large memory file,
    a growth-rate signal in its snapshot history, cache writes that doubled,
    and per-call cost that doubled. Should fire workspace_growth +
    efficiency_drift + cache_envelope_growth + context_bloat all on the
    same bot.
    """
    today = "2026-05-28"
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)

    sizes_today = {"memory/2026-05-02.md": 196 * 1024}

    # 14 days of history showing growth from 30 KB → 196 KB.
    for i in range(15):
        d = (datetime(2026, 5, 14) + timedelta(days=i)).strftime("%Y-%m-%d")
        kb = 30 + (i * 12)  # ≈12 KB/day growth
        cost_watchdog.write_workspace_snapshot(
            tmp_path, "security_bot", {"memory/2026-05-02.md": kb * 1024}, today=d
        )

    # Events: current 7d has high cache writes + high per-call cost;
    # prior 21d has low cache writes + low per-call cost.
    cur_events = [
        {**_haiku_evt(0.07), "cache_write_tokens": 25_000} for _ in range(100)
    ]
    prior_events = [
        {**_haiku_evt(0.005), "cache_write_tokens": 5_000} for _ in range(300)
    ]

    def fake_read_events(bot_id, days=7, shared_dir=None, *, now=None):
        # Days mapping: cost_spike cur=7, cost_spike prior=7-with-offset,
        # efficiency cur=7, efficiency prior=21.
        # We assume any 7d read at "now" returns cur_events; any 21d read
        # returns prior_events. Daily-spend (days=1) gets empty.
        if days == 1:
            return iter([])
        if days >= 21:
            return iter(prior_events)
        return iter(cur_events)

    monkeypatch.setattr(cost_watchdog, "read_events", fake_read_events)
    monkeypatch.setattr(cost_watchdog, "read_cron_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(cost_watchdog, "read_cron_runs", lambda *a, **kw: [])
    monkeypatch.setattr(
        cost_watchdog, "workspace_md_sizes", lambda *a, **kw: sizes_today
    )
    monkeypatch.setattr(cost_watchdog, "read_openclaw_json", lambda *a, **kw: None)
    monkeypatch.setattr(cost_watchdog, "read_today_turns", lambda *a, **kw: [])
    monkeypatch.setattr(
        cost_watchdog, "read_daily_metric", lambda *a, **kw: None
    )

    detections = cost_watchdog.collect_for_bot(
        "security_bot", tmp_path, config={}, today=today, now=now
    )
    types = {d["type"] for d in detections}

    # All four diagnostic axes should fire on the same bot.
    assert "context_bloat" in types
    assert "workspace_growth" in types
    assert "efficiency_drift" in types
    assert "cache_envelope_growth" in types


# ── config_drift_snapshot detector + snapshot helpers ───────────────────────


def _oc_with(*, primary: str = "anthropic/claude-haiku-4-5",
             heartbeat_model: str | None = None,
             heartbeat_every: str | None = None,
             exec_security: str | None = None) -> dict:
    """Build a minimal openclaw.json shape carrying the watched fields."""
    hb: dict = {}
    if heartbeat_model is not None:
        hb["model"] = heartbeat_model
    if heartbeat_every is not None:
        hb["every"] = heartbeat_every
    config: dict = {
        "agents": {
            "defaults": {
                "model": {"primary": primary},
                "heartbeat": hb,
            },
        },
    }
    if exec_security is not None:
        config["tools"] = {"exec": {"security": exec_security}}
    return config


def test_collect_config_snapshot_plucks_watched_fields():
    oc = _oc_with(
        primary="anthropic/claude-haiku-4-5",
        heartbeat_model="anthropic/claude-haiku-4-5",
        heartbeat_every="6h",
        exec_security="allowlist",
    )
    snap = cost_watchdog.collect_config_snapshot(oc)
    assert snap["agents.defaults.model.primary"] == "anthropic/claude-haiku-4-5"
    assert snap["agents.defaults.heartbeat.model"] == "anthropic/claude-haiku-4-5"
    assert snap["agents.defaults.heartbeat.every"] == "6h"
    assert snap["tools.exec.security"] == "allowlist"


def test_collect_config_snapshot_handles_missing_fields():
    """Missing fields snapshot as None — distinguishes 'never had it'
    from 'just removed it' in the diff."""
    snap = cost_watchdog.collect_config_snapshot({"agents": {"defaults": {}}})
    assert snap["agents.defaults.model.primary"] is None
    assert snap["agents.defaults.heartbeat.model"] is None


def test_collect_config_snapshot_handles_none():
    snap = cost_watchdog.collect_config_snapshot(None)
    # All watched dotpaths present with None values
    for dotpath, _ in cost_watchdog._CONFIG_DRIFT_DOTPATHS:
        assert dotpath in snap
        assert snap[dotpath] is None


def test_config_snapshot_roundtrip(tmp_path):
    snap = {"agents.defaults.model.primary": "anthropic/claude-haiku-4-5"}
    cost_watchdog.write_config_snapshot(tmp_path, "security_bot", snap, today="2026-05-28")
    prior = cost_watchdog.read_prior_config_snapshot(
        tmp_path, "security_bot", today="2026-05-29",
    )
    assert prior is not None
    date, data = prior
    assert date == "2026-05-28"
    assert data == snap


def test_config_snapshot_today_is_excluded_from_prior(tmp_path):
    """read_prior_config_snapshot must skip today itself — comparing today
    to today would always show no diff."""
    cost_watchdog.write_config_snapshot(
        tmp_path, "security_bot", {"x": 1}, today="2026-05-29",
    )
    prior = cost_watchdog.read_prior_config_snapshot(
        tmp_path, "security_bot", today="2026-05-29",
    )
    assert prior is None


def test_config_snapshot_returns_most_recent_prior(tmp_path):
    """When multiple priors exist, return the newest one."""
    cost_watchdog.write_config_snapshot(
        tmp_path, "security_bot", {"x": "old"}, today="2026-05-20",
    )
    cost_watchdog.write_config_snapshot(
        tmp_path, "security_bot", {"x": "recent"}, today="2026-05-27",
    )
    prior = cost_watchdog.read_prior_config_snapshot(
        tmp_path, "security_bot", today="2026-05-29",
    )
    assert prior is not None
    date, data = prior
    assert date == "2026-05-27"
    assert data == {"x": "recent"}


def _drift_kwargs(**overrides):
    base = dict(max_per_run=5)
    base.update(overrides)
    return base


def test_detect_config_drift_quiet_with_no_prior():
    """First-ever snapshot — no comparison available, no Signal."""
    current = cost_watchdog.collect_config_snapshot(_oc_with())
    out = cost_watchdog.detect_config_drift(
        "security_bot", current, None, **_drift_kwargs(),
    )
    assert out == []


def test_detect_config_drift_quiet_when_unchanged():
    snap = cost_watchdog.collect_config_snapshot(_oc_with())
    out = cost_watchdog.detect_config_drift(
        "security_bot", snap, ("2026-05-27", snap), **_drift_kwargs(),
    )
    assert out == []


def test_detect_config_drift_fires_alert_on_primary_change():
    """Security_bot's actual case: primary reverted haiku → sonnet. ALERT
    severity because primary is the critical-impact dotpath."""
    prior = cost_watchdog.collect_config_snapshot(
        _oc_with(primary="anthropic/claude-haiku-4-5"),
    )
    current = cost_watchdog.collect_config_snapshot(
        _oc_with(primary="anthropic/claude-sonnet-4-6"),
    )
    out = cost_watchdog.detect_config_drift(
        "security_bot", current, ("2026-05-21", prior), **_drift_kwargs(),
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "config_drift"
    assert sig["severity"] == "alert"
    d = sig["details"]
    assert d["dotpath"] == "agents.defaults.model.primary"
    assert d["prior_value"] == "anthropic/claude-haiku-4-5"
    assert d["current_value"] == "anthropic/claude-sonnet-4-6"
    assert d["prior_snapshot_date"] == "2026-05-21"


def test_detect_config_drift_fires_warn_on_heartbeat_cadence_change():
    """Non-critical fields fire warn, not alert."""
    prior = cost_watchdog.collect_config_snapshot(
        _oc_with(heartbeat_every="6h"),
    )
    current = cost_watchdog.collect_config_snapshot(
        _oc_with(heartbeat_every="1h"),
    )
    out = cost_watchdog.detect_config_drift(
        "team_bot_a", current, ("2026-05-27", prior), **_drift_kwargs(),
    )
    assert len(out) == 1
    assert out[0]["severity"] == "warn"
    assert out[0]["details"]["dotpath"] == "agents.defaults.heartbeat.every"


def test_detect_config_drift_fires_per_changed_field():
    """Multiple changes → multiple Signals (one per dotpath), each with
    its own signature for the Signal store."""
    prior = cost_watchdog.collect_config_snapshot(_oc_with(
        primary="anthropic/claude-haiku-4-5",
        heartbeat_every="6h",
    ))
    current = cost_watchdog.collect_config_snapshot(_oc_with(
        primary="anthropic/claude-sonnet-4-6",
        heartbeat_every="1h",
    ))
    out = cost_watchdog.detect_config_drift(
        "security_bot", current, ("2026-05-21", prior), **_drift_kwargs(),
    )
    assert len(out) == 2
    dotpaths = sorted(s["details"]["dotpath"] for s in out)
    assert dotpaths == [
        "agents.defaults.heartbeat.every",
        "agents.defaults.model.primary",
    ]
    # Signatures distinct so the Signal store doesn't dedup them
    assert len({s["signature"] for s in out}) == 2


def test_detect_config_drift_quiet_when_field_was_always_missing():
    """If neither prior nor current has the field, no Signal — there's
    no actual change to surface."""
    prior = cost_watchdog.collect_config_snapshot({"agents": {"defaults": {}}})
    current = cost_watchdog.collect_config_snapshot({"agents": {"defaults": {}}})
    out = cost_watchdog.detect_config_drift(
        "personal_bot", current, ("2026-05-27", prior), **_drift_kwargs(),
    )
    assert out == []


def test_detect_config_drift_caps_at_max_per_run():
    """Defensive cap so a malformed snapshot can't flood the run."""
    prior = cost_watchdog.collect_config_snapshot(_oc_with(
        primary="x", heartbeat_model="x", heartbeat_every="x",
        exec_security="x",
    ))
    current = cost_watchdog.collect_config_snapshot(_oc_with(
        primary="y", heartbeat_model="y", heartbeat_every="y",
        exec_security="y",
    ))
    out = cost_watchdog.detect_config_drift(
        "security_bot", current, ("2026-05-27", prior),
        max_per_run=2,
    )
    assert len(out) == 2


def test_detect_config_drift_title_is_short_and_carries_diff_in_body():
    """Title should be a short canonical form ("<bot>: <field> changed
    (since <date>)") so the Alerts row doesn't get wrecked by JSON-dumped
    value lists. The full diff lives in body + details. This regression
    test guards the fix for the model-fallbacks long-title case."""
    fallbacks_old = [
        "openai/gpt-5.5", "google/gemini-2.5-pro",
        "anthropic/claude-sonnet-4-6", "openai/gpt-5.4-mini",
        "google/gemini-2.0-flash", "anthropic/claude-opus-4-7",
    ]
    fallbacks_new = [
        "openai/gpt-4o", "google/gemini-2.5-pro",
        "anthropic/claude-haiku-4-5", "openai/gpt-4o-mini",
        "google/gemini-2.0-flash", "anthropic/claude-opus-4-7",
    ]
    prior_oc = {"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": fallbacks_old,
    }}}}
    current_oc = {"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": fallbacks_new,
    }}}}
    prior = cost_watchdog.collect_config_snapshot(prior_oc)
    current = cost_watchdog.collect_config_snapshot(current_oc)
    out = cost_watchdog.detect_config_drift(
        "team-bot-b", current, ("2026-05-29", prior), **_drift_kwargs(),
    )
    assert len(out) == 1
    sig = out[0]
    # Title is short — well under the soft limit and definitely under the
    # hard cap.
    assert sig["title"] == "team-bot-b: model fallbacks changed (since 2026-05-29)"
    assert len(sig["title"]) < 80
    # The actual model-id arrays must not appear in the title.
    assert "gpt-5.5" not in sig["title"]
    assert "→" not in sig["title"]
    # The diff is in body + details, not lost.
    assert "gpt-5.5" in sig["body"]
    assert sig["details"]["prior_value"] == fallbacks_old
    assert sig["details"]["current_value"] == fallbacks_new


def test_detect_config_drift_fallback_change_is_info_tier():
    """Fallback list reordering rarely affects behavior (dormant until
    primary fails). Demoted to info-tier so it doesn't take up Alerts page
    space by default — operators can toggle Show Info to see it."""
    prior_oc = {"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5",
        "fallbacks": ["openai/gpt-4o"],
    }}}}
    current_oc = {"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5",
        "fallbacks": ["openai/gpt-4o", "google/gemini-2.0-flash"],
    }}}}
    prior = cost_watchdog.collect_config_snapshot(prior_oc)
    current = cost_watchdog.collect_config_snapshot(current_oc)
    out = cost_watchdog.detect_config_drift(
        "team-bot-b", current, ("2026-05-29", prior), **_drift_kwargs(),
    )
    assert len(out) == 1
    assert out[0]["severity"] == "info"
    assert out[0]["details"]["dotpath"] == "agents.defaults.model.fallbacks"


def test_detect_config_drift_exec_security_change_is_alert_tier():
    """tools.exec.security changes are a security-posture move (e.g.
    relaxing from 'standard' to 'full'); promoted to alert tier so the
    operator sees them next to the primary-model alerts."""
    prior = cost_watchdog.collect_config_snapshot(_oc_with(exec_security="standard"))
    current = cost_watchdog.collect_config_snapshot(_oc_with(exec_security="full"))
    out = cost_watchdog.detect_config_drift(
        "team-bot-b", current, ("2026-05-29", prior), **_drift_kwargs(),
    )
    assert len(out) == 1
    assert out[0]["severity"] == "alert"
    assert out[0]["details"]["dotpath"] == "tools.exec.security"


# ─────────────────────────────────────────────────────────────────────────────
# Schema v2 enrichment — PR E: who/where/when on cost Signal details
# ─────────────────────────────────────────────────────────────────────────────


def _evt_v2(
    *,
    bot_id: str = "team-bot-a",
    ts: str = "2026-05-29T00:52:00Z",
    timestamp_local: str | None = "2026-05-28T17:52:00-07:00",
    cost_usd: float = 0.45,
    trigger_kind: str = "user_turn",
    session_id: str = "3d5cde22-1111-2222-3333-444455556666",
    user_id: str | None = "U0518A544N5",
    user_display_name: str | None = "Peter",
    channel_id: str | None = "D0AKX41HELU",
    channel_kind: str | None = "slack_dm",
) -> dict:
    return {
        "schema_version": 2,
        "type": "cost_event",
        "ts": ts,
        "timestamp_local": timestamp_local,
        "bot_id": bot_id,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "cache_state": "warm",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "cost_usd": cost_usd,
        "user_id": user_id,
        "user_display_name": user_display_name,
        "channel_id": channel_id,
        "channel_kind": channel_kind,
    }


def test_v2_context_from_event_passes_through():
    """Helper returns the five v2 fields directly off the event."""
    event = _evt_v2()
    ctx = cost_watchdog._v2_context_from_event(event)
    assert ctx == {
        "timestamp_local": "2026-05-28T17:52:00-07:00",
        "user_id": "U0518A544N5",
        "user_display_name": "Peter",
        "channel_id": "D0AKX41HELU",
        "channel_kind": "slack_dm",
    }


def test_v2_context_from_event_handles_v1_record():
    """Old v1 records produce all-None context — UI shows '(unknown)'."""
    v1 = {"ts": "2026-05-01T00:00:00Z", "cost_usd": 0.05}
    ctx = cost_watchdog._v2_context_from_event(v1)
    assert all(v is None for v in ctx.values())


def test_v2_context_from_session_events_picks_earliest_user_event():
    """When multiple turns exist, pick the earliest with a user_id."""
    early = _evt_v2(ts="2026-05-29T00:50:00Z", user_id="U_FIRST")
    middle = _evt_v2(ts="2026-05-29T00:51:00Z", user_id="U_SECOND")
    last = _evt_v2(ts="2026-05-29T00:52:00Z", user_id=None)
    ctx = cost_watchdog._v2_context_from_session_events([last, early, middle])
    assert ctx["user_id"] == "U_FIRST"


def test_v2_context_from_day_aggregates_top_user():
    """Day-level rollup picks the user_id with the most events."""
    events = (
        [_evt_v2(user_id="U_DOM", session_id=f"s{i}") for i in range(5)]
        + [_evt_v2(user_id="U_ONCE", session_id="s-other")]
        + [_evt_v2(user_id=None, trigger_kind="heartbeat",
                   channel_kind="internal", session_id="hb")]
    )
    ctx = cost_watchdog._v2_context_from_day(events)
    assert ctx["user_id"] == "U_DOM"
    assert ctx["user_display_name"] == "Peter"
    assert ctx["channel_kind"] == "slack_dm"


def test_v2_context_from_day_all_automation_no_user():
    """Automation-only days have no user — context user fields are None."""
    events = [
        _evt_v2(user_id=None, user_display_name=None, channel_id=None,
                channel_kind="internal", trigger_kind="heartbeat",
                session_id=f"hb-{i}") for i in range(3)
    ]
    ctx = cost_watchdog._v2_context_from_day(events)
    assert ctx["user_id"] is None
    assert ctx["user_display_name"] is None
    # timestamp_local should still be populated from the most recent.
    assert ctx["timestamp_local"] is not None


def test_daily_spend_signal_carries_v2_context():
    """detect_daily_spend embeds the v2 context in details for the renderer."""
    today = "2026-05-28"
    events = [_evt_v2(ts=f"{today}T17:52:00Z", cost_usd=2.5) for _ in range(4)]  # $10
    out = cost_watchdog.detect_daily_spend(
        "team-bot-a", events, threshold_usd=5.0, today=today
    )
    assert len(out) == 1
    d = out[0]["details"]
    assert d["user_id"] == "U0518A544N5"
    assert d["user_display_name"] == "Peter"
    assert d["channel_kind"] == "slack_dm"
    assert d["channel_id"] == "D0AKX41HELU"
    assert "timestamp_local" in d


def test_session_token_outlier_signal_carries_v2_context():
    """detect_session_token_outlier names the user who triggered the runaway."""
    # Build a session that's 5× median.
    big_session = [
        _evt_v2(session_id="3d5cde22", ts=f"2026-05-28T17:5{i}:00Z", cost_usd=1.0)
        for i in range(5)
    ]
    # Several smaller sessions to anchor the median.
    background = []
    for i in range(5):
        background += [
            _evt_v2(
                session_id=f"smaller-{i}",
                ts=f"2026-05-28T12:0{i}:00Z",
                cost_usd=0.05,
                user_id="U_DIFFERENT",
                user_display_name="Bob",
            )
            for _ in range(5)
        ]
    out = cost_watchdog.detect_session_token_outlier(
        "team-bot-a",
        big_session + background,
        factor=3.0,
        min_session_events=3,
        min_cost_usd=0.5,
        max_per_run=5,
    )
    assert len(out) >= 1
    # The 5× outlier session is 3d5cde22 — must surface Peter, not Bob.
    outlier = next(s for s in out if s["details"]["session_id"] == "3d5cde22")
    d = outlier["details"]
    assert d["user_id"] == "U0518A544N5"
    assert d["user_display_name"] == "Peter"
    assert d["channel_kind"] == "slack_dm"


def test_old_v1_records_dont_break_daily_spend():
    """Backward compat: detector still works when events lack v2 fields."""
    today = "2026-05-28"
    v1_only = [_evt(ts=f"{today}T10:00:00Z", cost_usd=2.0) for _ in range(3)]  # $6
    out = cost_watchdog.detect_daily_spend(
        "admin_bot", v1_only, threshold_usd=3.0, today=today,
    )
    assert len(out) == 1
    d = out[0]["details"]
    # All v2 keys present with None values — consistent shape for UI.
    assert d["user_id"] is None
    assert d["user_display_name"] is None
    assert d["channel_kind"] is None
    assert d["channel_id"] is None

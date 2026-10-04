"""tests/test_efficiency_hawk_from_signals.py

End-to-end coverage for the new signal-consumer path on efficiency_hawk:
cost_watchdog writes a Signal → efficiency_hawk._observe_from_signals
reads it → produces a Proposal with motivating_signals[] linking back.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_watchdog  # noqa: E402
from generators.efficiency_hawk import signal_proposals  # noqa: E402
from generators.efficiency_hawk.observe import (  # noqa: E402
    EfficiencyHawkContext,
    _observe_from_signals,
)
from observations.access import window as obs_window  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixture helpers ──────────────────────────────────────────────────────────


def _evt(*, ts: str, cost_usd: float = 0.01, trigger_kind: str = "user_turn") -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": "admin_bot",
        "session_id": "sess-1",
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


def _ctx(shared_dir: Path, bot_id: str = "admin_bot") -> EfficiencyHawkContext:
    """Build a minimal context that exercises only _observe_from_signals."""
    return EfficiencyHawkContext(
        bot_id=bot_id,
        window=obs_window(bot_id, days=1, shared_dir=shared_dir),
        shared_dir=shared_dir,
    )


# ── Signal-type → Proposal factories ─────────────────────────────────────────


def test_daily_spend_signal_produces_proposal():
    sig = type("Signal", (), {})()
    sig.id = "sig-1"
    sig.bot_id = "admin_bot"
    sig.severity = "alert"
    sig.details = {
        "bot_id": "admin_bot",
        "date": "2026-05-09",
        "cost_usd": 6.0,
        "threshold_usd": 3.0,
        "event_count": 90,
    }
    p = signal_proposals.make_daily_spend_proposal(sig)
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "efficiency_hawk"
    assert p.motivating_signals == ["sig-1"]
    assert p.trigger_observations == ["daily_spend_high:admin_bot"]
    assert p.action.kind == "Investigation"
    assert p.urgency == "operational_urgent"
    # Headline is action-led; problem stays the symptom.
    # Phase C-5 (2026-06-04) humanized title.
    assert p.admin_surface_summary == "admin_bot blew through its daily spend cap"
    assert p.admin_surface_summary != p.problem
    assert len(p.admin_surface_summary) <= 120


def test_cron_wakes_agent_signal_produces_proposal():
    sig = type("Signal", (), {})()
    sig.id = "sig-2"
    sig.bot_id = "admin_bot"
    sig.details = {
        "cron_id": "abc-123",
        "cron_name": "gateway-selfheal",
        "cadence": "15min",
        "session_target": "main",
        "wake_mode": "now",
        "shell": "/Users/admin_bot/bin/gateway-selfheal.sh",
    }
    p = signal_proposals.make_cron_wakes_agent_proposal(sig)
    assert p.motivating_signals == ["sig-2"]
    assert "admin_bot/abc-123" in p.trigger_observations[0]
    assert "gateway-selfheal" in p.problem
    assert "isolated" in p.action.context  # mentions the recommended fix
    # Headline is the action ("Set sessionTarget=isolated …"), not the symptom.
    # Phase C-5 (2026-06-04) humanized title.
    assert p.admin_surface_summary.startswith("Stop admin_bot's")
    assert "cron from waking the agent" in p.admin_surface_summary
    assert "gateway-selfheal" in p.admin_surface_summary
    assert p.admin_surface_summary != p.problem


def test_context_bloat_proposal_includes_filename_specific_advice():
    sig = type("Signal", (), {})()
    sig.id = "sig-3"
    sig.bot_id = "admin_bot"
    sig.details = {
        "filename": "heartbeats.md",
        "size_kb": 80,
        "threshold_kb": 50,
    }
    p = signal_proposals.make_context_bloat_proposal(sig)
    assert "heartbeats.md" in p.problem
    # heartbeats.md branch suggests rotation/trimming:
    assert "rolling" in p.action.context.lower() or "archive" in p.action.context.lower()
    # Headline is "Trim …" not the raw symptom.
    # Phase C-5 (2026-06-04) humanized title.
    assert p.admin_surface_summary.startswith("Trim admin_bot's")
    assert "heartbeats.md" in p.admin_surface_summary
    assert p.admin_surface_summary != p.problem


def test_session_token_outlier_signal_produces_proposal():
    sig = type("Signal", (), {})()
    sig.id = "sig-outlier-1"
    sig.bot_id = "admin_bot"
    sig.details = {
        "session_id": "abc-123-runaway",
        "cost_usd": 5.40,
        "median_session_cost_usd": 0.18,
        "ratio": 30.0,
        "event_count": 119,
        "trigger_kinds": ["heartbeat", "subagent"],
        "first_ts": "2026-05-09T01:36:00Z",
        "last_ts": "2026-05-09T01:39:45Z",
    }
    p = signal_proposals.make_session_token_outlier_proposal(sig)
    assert p.motivating_signals == ["sig-outlier-1"]
    assert p.trigger_observations == ["session_token_outlier:admin_bot/abc-123-runaway"]
    assert "abc-123-runaway" in p.action.context
    assert "stuck loop" in p.action.context.lower()
    # Headline is "Inspect …" not the raw symptom.
    # Phase C-5 (2026-06-04) humanized title.
    assert p.admin_surface_summary.startswith("One admin_bot session cost")
    assert p.admin_surface_summary != p.problem


def test_heartbeat_no_model_override_signal_produces_proposal():
    sig = type("Signal", (), {})()
    sig.id = "sig-hb-1"
    sig.bot_id = "admin_bot"
    sig.details = {
        "primary_model": "anthropic/claude-sonnet-4-6",
        "heartbeat_every": "1h",
        "light_context": True,
        "isolated_session": True,
    }
    p = signal_proposals.make_heartbeat_no_model_override_proposal(sig)
    assert p.motivating_signals == ["sig-hb-1"]
    assert p.trigger_observations == ["heartbeat_no_model_override:admin_bot"]
    assert "haiku" in p.action.context.lower()
    assert "openclaw.json" in p.action.context
    assert "sonnet" in p.action.context.lower()
    # Headline is the action ("Route … to Haiku"); problem no longer carries the fix.
    # Phase C-5 (2026-06-04) humanized title.
    assert (
        p.admin_surface_summary == "Route admin_bot's heartbeat to a cheaper model"
    )
    assert "Haiku" not in p.problem  # action was lifted out of the symptom
    assert p.admin_surface_summary != p.problem


def test_automation_dominance_headline_is_action_led():
    sig = type("Signal", (), {})()
    sig.id = "sig-auto-1"
    sig.bot_id = "team_bot_c"
    sig.details = {
        "automation_count": 24,
        "user_turn_count": 6,
        "automation_ratio": 0.8,
        "window_days": 3,
        "top_automation_kinds": {"heartbeat": 18, "cron": 6},
    }
    p = signal_proposals.make_automation_dominance_proposal(sig)
    # Phase C-5 (2026-06-04) humanized title — the assertion intent is
    # "title leads with the action/observation, not the rule slug."
    assert p.admin_surface_summary == "team_bot_c is mostly talking to itself"
    assert p.admin_surface_summary != p.problem
    assert len(p.admin_surface_summary) <= 120


def test_cron_overactive_headline_is_action_led():
    sig = type("Signal", (), {})()
    sig.id = "sig-over-1"
    sig.bot_id = "admin_bot"
    sig.details = {
        "cron_id": "abc-123",
        "cron_name": "gateway-selfheal",
        "actual_fires": 41,
        "expected_fires": 24,
        "window_hours": 24,
        "every_ms": 3_600_000,
    }
    p = signal_proposals.make_cron_overactive_proposal(sig)
    # Phase C-5 (2026-06-04) humanized title. Was "Investigate <bot>
    # cron '<name>' over-firing — NNx in Nh vs ~M expected"; the
    # actual numbers now live in the proposal body / summary, not
    # the title.
    assert p.admin_surface_summary.startswith("admin_bot's")
    assert "gateway-selfheal" in p.admin_surface_summary
    assert "firing too often" in p.admin_surface_summary
    assert p.admin_surface_summary != p.problem
    assert len(p.admin_surface_summary) <= 120


def test_unknown_signal_type_is_silently_ignored(tmp_path):
    """A signal with an unknown type must not crash _observe_from_signals."""
    signals_store.observe(
        tmp_path,
        signature="cost_watchdog:novel_thing:admin_bot",
        producer="cost_watchdog",
        type="novel_thing",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="Something new",
        body="never seen before",
        details={},
    )
    proposals = _observe_from_signals(_ctx(tmp_path))
    assert proposals == []


def test_signal_with_bad_payload_does_not_break_other_signals(tmp_path):
    """One malformed signal shouldn't suppress proposals from sibling signals."""
    # Good signal — daily_spend_high with valid details.
    good = signals_store.observe(
        tmp_path,
        signature="cost_watchdog:daily_spend_high:admin_bot",
        producer="cost_watchdog",
        type="daily_spend_high",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="t",
        body="b",
        details={
            "bot_id": "admin_bot",
            "date": "2026-05-09",
            "cost_usd": 4.0,
            "threshold_usd": 3.0,
            "event_count": 10,
        },
    )
    proposals = _observe_from_signals(_ctx(tmp_path))
    assert len(proposals) == 1
    assert proposals[0].motivating_signals == [good.id]


# ── Filtering by bot ─────────────────────────────────────────────────────────


def test_observer_only_picks_up_signals_for_its_bot(tmp_path):
    signals_store.observe(
        tmp_path,
        signature="cost_watchdog:daily_spend_high:admin_bot",
        producer="cost_watchdog",
        type="daily_spend_high",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="t",
        body="b",
        details={"bot_id": "admin_bot", "cost_usd": 4.0, "threshold_usd": 3.0},
    )
    signals_store.observe(
        tmp_path,
        signature="cost_watchdog:daily_spend_high:team_bot_c",
        producer="cost_watchdog",
        type="daily_spend_high",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="team_bot_c",
        title="t",
        body="b",
        details={"bot_id": "team_bot_c", "cost_usd": 4.0, "threshold_usd": 3.0},
    )
    p_admin_bot = _observe_from_signals(_ctx(tmp_path, "admin_bot"))
    p_team_bot_c = _observe_from_signals(_ctx(tmp_path, "team_bot_c"))
    assert {p.bot_id for p in p_admin_bot} == {"admin_bot"}
    assert {p.bot_id for p in p_team_bot_c} == {"team_bot_c"}


# ── End-to-end: monitor → generator with shared_dir ──────────────────────────


def test_e2e_monitor_run_then_generator_produces_proposal(tmp_path, monkeypatch):
    """Run cost_watchdog over fixture telemetry, then run efficiency_hawk's
    signal-consumer path; assert the resulting Proposal references the
    monitor's Signal id.
    """
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)

    # Fixtures: $6 spend → fires daily_spend_high.
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

    cost_watchdog.run_for_bot("admin_bot", tmp_path, config={}, today=today, now=now)

    sigs = list(signals_store.iter_active(tmp_path, producer="cost_watchdog"))
    assert len(sigs) == 1
    monitor_signal_id = sigs[0].id

    # Now run the generator's signal-consumer.
    proposals = _observe_from_signals(_ctx(tmp_path))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == "admin_bot"
    assert p.motivating_signals == [monitor_signal_id]
    assert p.action.kind == "Investigation"


def test_e2e_resolved_signals_do_not_produce_proposals(tmp_path, monkeypatch):
    """After sweep_resolve, the firing signal becomes resolved; the
    generator should ignore it on the next run."""
    today = "2026-05-09"
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)

    # Run 1: fires.
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
    kept, _ = cost_watchdog.run_for_bot(
        "admin_bot", tmp_path, config={}, today=today, now=now
    )
    assert len(_observe_from_signals(_ctx(tmp_path))) == 1

    # Run 2: condition cleared. Sweep-resolves the prior signal.
    monkeypatch.setattr(
        cost_watchdog,
        "read_events",
        lambda bot_id, days=7, shared_dir=None, *, now=None: iter([]),
    )
    cost_watchdog.run_for_bot("admin_bot", tmp_path, config={}, today=today, now=now)
    signals_store.sweep_resolve(
        tmp_path,
        producer="cost_watchdog",
        kept_signatures=set(),
        reason="cleared in test",
    )
    assert _observe_from_signals(_ctx(tmp_path)) == []

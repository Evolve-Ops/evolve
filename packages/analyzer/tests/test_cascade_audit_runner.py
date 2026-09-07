"""Regression tests for cascade.audit_runner.

The runner translates cascade telemetry spans into Signals + labels.
Three Signal types — anomaly_*, dangerous_combo, runaway_rate_tripped
— plus per-day labeled-outcome persistence for the Phase 4 tuner.

Most tests synthesize spans in-memory and call the per-detector
functions directly; the end-to-end run() is exercised by a smoke
test that uses a real JsonlBackend on a tmp shared-dir.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from cascade.audit_runner import (  # noqa: E402
    PRODUCER,
    _TIER_AUDIT_DIVERGENCE_THRESHOLD,
    _TIER_AUDIT_MIN_TURNS,
    _TIER_AUDIT_REFUSE_SENTINEL,
    _UNKNOWN_TRIGGER_ALERT_THRESHOLD,
    _UNKNOWN_TRIGGER_MIN_TURNS,
    _collect_anomaly_signals,
    _collect_dangerous_combo_signals,
    _collect_runaway_rate_signals,
    _collect_tier_routing_disagreement_signals,
    _collect_unknown_trigger_rate_signals,
    _compute_bot_baselines,
    _expected_tier_for,
    _spans_in_window,
    run,
)


def _now():
    return datetime.now(timezone.utc)


def _span(
    *,
    bot_id="team_bot_a",
    session_id="s1",
    turn_index=0,
    end_time=None,
    cost=0.10,
    prompt_tokens=5000,
    tier_used="tier2",
    tier_chosen_by="classifier",
    trigger_kind="user_turn",
    consent_source=None,
    holdout=False,
    extras=None,
):
    end_time = end_time or _now().isoformat()
    attrs = {
        "session_id": session_id,
        "turn_index": turn_index,
        "cascade.tier_used": tier_used,
        "cascade.tier_chosen_by": tier_chosen_by,
        "cascade.trigger_kind": trigger_kind,
        "cascade.holdout": holdout,
    }
    if consent_source:
        attrs["cascade.consent_source"] = consent_source
    if extras:
        attrs.update(extras)
    return {
        "bot_id": bot_id,
        "start_time": end_time,
        "end_time": end_time,
        "total_cost": cost,
        "usage": {"prompt_tokens": prompt_tokens},
        "attributes": attrs,
    }


# ── Dangerous-combo detector ────────────────────────────────────────────────


def test_dangerous_combo_emits_one_signal_per_session():
    """One matched span → one Signal. Multiple matched spans for the
    same session → still one Signal (the signature dedupes)."""
    spans = [
        _span(
            session_id="combo-1",
            turn_index=0,
            extras={
                "cascade.dangerous_combo.matched": True,
                "cascade.dangerous_combo.context_tokens": 100_000,
            },
        ),
        _span(
            session_id="combo-1",
            turn_index=1,
            extras={
                "cascade.dangerous_combo.matched": True,
                "cascade.dangerous_combo.context_tokens": 120_000,
            },
        ),
    ]
    out = _collect_dangerous_combo_signals(spans)
    assert len(out) == 1
    assert out[0]["type"] == "dangerous_combo"
    assert out[0]["severity"] == "warn"
    assert out[0]["scope"] == "bot"
    assert "combo-1" in out[0]["signature"]


def test_dangerous_combo_ignores_unmatched_spans():
    """Spans without `cascade.dangerous_combo.matched` are skipped."""
    spans = [
        _span(session_id="ok-1"),
        _span(session_id="ok-2", extras={"cascade.dangerous_combo.matched": False}),
    ]
    out = _collect_dangerous_combo_signals(spans)
    assert out == []


def test_dangerous_combo_signature_is_stable_across_runs():
    """Same input → same signature, so signals.store.observe()
    deduplicates rather than duplicating."""
    spans = [
        _span(
            session_id="combo-X",
            extras={
                "cascade.dangerous_combo.matched": True,
                "cascade.dangerous_combo.context_tokens": 80_000,
            },
        ),
    ]
    sig1 = _collect_dangerous_combo_signals(spans)[0]["signature"]
    sig2 = _collect_dangerous_combo_signals(spans)[0]["signature"]
    assert sig1 == sig2
    assert sig1.startswith(f"{PRODUCER}:dangerous_combo:")


# ── Runaway-rate detector ───────────────────────────────────────────────────


def test_runaway_rate_emits_one_signal_per_session():
    """Multiple trip-flagged spans for one session → one Signal."""
    spans = [
        _span(
            session_id="run-1", turn_index=0,
            extras={
                "cascade.runaway_rate.tripped": True,
                "cascade.runaway_rate.severity": "warning",
                "cascade.runaway_rate.total_usd": 22.5,
            },
        ),
        _span(
            session_id="run-1", turn_index=1,
            extras={
                "cascade.runaway_rate.tripped": True,
                "cascade.runaway_rate.severity": "warning",
                "cascade.runaway_rate.total_usd": 24.0,
            },
        ),
    ]
    out = _collect_runaway_rate_signals(spans)
    assert len(out) == 1
    assert out[0]["type"] == "runaway_rate_tripped"
    assert out[0]["severity"] == "warn"
    assert out[0]["details"]["total_usd"] == 24.0


def test_runaway_rate_escalates_warning_to_critical():
    """If any span in the session escalated to critical, the Signal
    fires at alert severity. The escalation direction is one-way per
    spec § 2.6 — once critical, stays critical."""
    spans = [
        _span(
            session_id="run-2", turn_index=0,
            extras={
                "cascade.runaway_rate.tripped": True,
                "cascade.runaway_rate.severity": "warning",
                "cascade.runaway_rate.total_usd": 22.0,
            },
        ),
        _span(
            session_id="run-2", turn_index=1,
            extras={
                "cascade.runaway_rate.tripped": True,
                "cascade.runaway_rate.severity": "critical",
                "cascade.runaway_rate.total_usd": 31.0,
            },
        ),
    ]
    out = _collect_runaway_rate_signals(spans)
    assert len(out) == 1
    assert out[0]["severity"] == "alert"
    assert out[0]["details"]["plugin_severity"] == "critical"


def test_runaway_rate_ignores_untripped_spans():
    """Spans without the tripped flag — even with a runaway-rate
    severity field hanging around — are ignored."""
    spans = [
        _span(session_id="ok-1"),
        _span(
            session_id="ok-2",
            extras={"cascade.runaway_rate.severity": "warning"},
        ),
    ]
    assert _collect_runaway_rate_signals(spans) == []


# ── Unknown-trigger-rate detector (audit #68) ───────────────────────────────


def _mixed_trigger_spans(bot_id, unknown_count, known_count, trigger_kind_known="user_turn"):
    """Build N=unknown_count + known_count spans for one bot, split between
    unknown trigger_kind and a known one. Each span gets a unique session
    id so the detector counts turns rather than de-duping by session."""
    spans = []
    for i in range(unknown_count):
        spans.append(_span(
            bot_id=bot_id, session_id=f"unk-{i}", trigger_kind="unknown",
        ))
    for i in range(known_count):
        spans.append(_span(
            bot_id=bot_id, session_id=f"kn-{i}", trigger_kind=trigger_kind_known,
        ))
    return spans


def test_unknown_trigger_emits_signal_above_threshold():
    """REGRESSION (audit #68): when a bot's unknown_trigger_kind rate
    exceeds 25% over >= 20 turns, emit a per-bot Signal. This catches
    a future OC release that stops populating trigger_kind."""
    # 8 unknown + 22 known = 30 total, 26.7% unknown — over threshold
    spans = _mixed_trigger_spans("team_bot_a", unknown_count=8, known_count=22)
    out = _collect_unknown_trigger_rate_signals(spans)
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "unknown_trigger_rate_spike"
    assert sig["bot_id"] == "team_bot_a"
    assert sig["scope"] == "bot"
    assert sig["severity"] == "warn"
    assert sig["details"]["unknown_count"] == 8
    assert sig["details"]["total_count"] == 30
    assert abs(sig["details"]["unknown_rate"] - 8 / 30) < 1e-6
    # Title surfaces the actual percentage so operators don't have to
    # divide in their heads.
    assert "26.7" in sig["title"]


def test_unknown_trigger_does_not_fire_below_min_sample():
    """Even at 100% unknown, fewer than _UNKNOWN_TRIGGER_MIN_TURNS
    samples is too noisy to alert. Operators would otherwise get
    surprise alerts from new bots that have only run a handful of
    turns so far."""
    assert _UNKNOWN_TRIGGER_MIN_TURNS == 20  # pin the constant
    # 10 turns, all unknown → 100% rate but below sample threshold
    spans = _mixed_trigger_spans("new-bot", unknown_count=10, known_count=0)
    assert _collect_unknown_trigger_rate_signals(spans) == []


def test_unknown_trigger_does_not_fire_below_rate_threshold():
    """Bots with a low ambient unknown rate (e.g. some channels not
    yet mapped) should NOT alert constantly. Threshold is 25%."""
    assert _UNKNOWN_TRIGGER_ALERT_THRESHOLD == 0.25  # pin the constant
    # 4 unknown + 46 known = 50 total, 8% unknown — well below threshold
    spans = _mixed_trigger_spans("admin_bot", unknown_count=4, known_count=46)
    assert _collect_unknown_trigger_rate_signals(spans) == []


def test_unknown_trigger_counts_empty_string_and_missing_as_unknown():
    """trigger_kind can be missing entirely or empty-string — both
    count as 'no anchor fired,' same as the literal 'unknown' string."""
    spans = [
        # Literal "unknown"
        _span(bot_id="team_bot_a", session_id="s1", trigger_kind="unknown"),
        # Empty string
        _span(bot_id="team_bot_a", session_id="s2", trigger_kind=""),
        # 18 known turns to clear the min sample
        *_mixed_trigger_spans("team_bot_a", unknown_count=0, known_count=18),
    ]
    # Add a span with NO trigger_kind attribute at all
    span_no_attr = _span(bot_id="team_bot_a", session_id="s3", trigger_kind="user_turn")
    del span_no_attr["attributes"]["cascade.trigger_kind"]
    spans.append(span_no_attr)
    # 3 unknown-equivalent + 18 known = 21 total, 14.3% — below threshold
    # Bump to 6 unknown-equivalent + 18 known = 24, 25% — at threshold
    for i in range(3):
        spans.append(_span(
            bot_id="team_bot_a", session_id=f"more-unk-{i}", trigger_kind="unknown",
        ))
    out = _collect_unknown_trigger_rate_signals(spans)
    assert len(out) == 1
    assert out[0]["details"]["unknown_count"] == 6
    assert out[0]["details"]["total_count"] == 24


def test_unknown_trigger_per_bot_scoping():
    """One bot above threshold + another below → only the over-threshold
    bot gets a Signal. Counts are per-bot, not pod-wide."""
    spans = (
        # team_bot_a: 8/30 = 26.7% (over)
        _mixed_trigger_spans("team_bot_a", unknown_count=8, known_count=22)
        # admin_bot: 2/30 = 6.7% (under)
        + _mixed_trigger_spans("admin_bot", unknown_count=2, known_count=28)
    )
    out = _collect_unknown_trigger_rate_signals(spans)
    assert len(out) == 1
    assert out[0]["bot_id"] == "team_bot_a"


def test_unknown_trigger_signature_is_per_bot_stable():
    """Same bot + same condition on two runs → same signature, so the
    Signal store's observe() find-or-creates rather than duplicating."""
    s1 = _collect_unknown_trigger_rate_signals(
        _mixed_trigger_spans("team_bot_a", unknown_count=10, known_count=20),
    )[0]["signature"]
    s2 = _collect_unknown_trigger_rate_signals(
        _mixed_trigger_spans("team_bot_a", unknown_count=10, known_count=20),
    )[0]["signature"]
    assert s1 == s2


# ── Anomaly detector ────────────────────────────────────────────────────────


def test_anomaly_fires_when_cost_per_turn_exceeds_threshold():
    """Build a baseline at $0.10/turn over 60 baseline spans, then
    submit 5 recent spans at $5.00/turn. Origin user_initiated has
    warn threshold of 10x — 50x ratio fires warn."""
    now = _now()
    old_ts = (now - timedelta(days=10)).isoformat()
    recent_ts = now.isoformat()
    spans = (
        [_span(end_time=old_ts, cost=0.10, session_id=f"old-{i}") for i in range(60)]
        + [_span(end_time=recent_ts, cost=5.00, session_id=f"new-{i}") for i in range(5)]
    )
    baselines = _compute_bot_baselines(spans, window_days=30, earliest_span_at=None)
    assert baselines["team_bot_a"].cost_per_turn.n >= 60
    assert baselines["team_bot_a"].cost_per_turn.mean == pytest.approx(0.10, abs=0.5)
    recent = _spans_in_window(spans, minutes=65, now=now)
    out = _collect_anomaly_signals(recent, baselines)
    cost_anom = [a for a in out if a["type"] == "anomaly_cost_per_turn"]
    assert len(cost_anom) >= 1
    assert cost_anom[0]["severity"] == "warn"


def test_anomaly_skips_bot_with_insufficient_data():
    """When a bot has no baseline (and no pod-median fallback can
    save it — i.e. all bots have insufficient data), the anomaly
    detector should silently skip rather than fire spurious Signals
    against zero baseline."""
    now = _now()
    recent_ts = now.isoformat()
    # Only 3 spans for team_bot_a — well below min_observations (50).
    spans = [_span(end_time=recent_ts, cost=5.00, session_id=f"s-{i}") for i in range(3)]
    baselines = _compute_bot_baselines(spans, window_days=30, earliest_span_at=now)
    recent = _spans_in_window(spans, minutes=65, now=now)
    out = _collect_anomaly_signals(recent, baselines)
    assert out == []


def test_anomaly_origin_affects_threshold():
    """ui_chip origin has no `inform` threshold and a warn at 10x.
    user_initiated has inform at 3x. A 5x ratio fires inform for
    user_initiated, but nothing for ui_chip."""
    now = _now()
    old_ts = (now - timedelta(days=10)).isoformat()
    recent_ts = now.isoformat()

    # Build baseline at $0.10. Then 5 recent spans at $0.50 (5x).
    # One bot for each origin so the signals can be distinguished.
    baseline_spans = [
        _span(end_time=old_ts, cost=0.10, session_id=f"base-{i}") for i in range(60)
    ]
    # 5x baseline = 0.50 per turn.
    ui_chip_recent = [
        _span(
            end_time=recent_ts, cost=0.50, session_id=f"ui-{i}",
            consent_source="ui_chip",
        )
        for i in range(5)
    ]
    user_recent = [
        _span(end_time=recent_ts, cost=0.50, session_id=f"user-{i}")
        for i in range(5)
    ]
    spans = baseline_spans + ui_chip_recent + user_recent
    baselines = _compute_bot_baselines(spans, window_days=30, earliest_span_at=now)
    recent = _spans_in_window(spans, minutes=65, now=now)
    out = _collect_anomaly_signals(recent, baselines)
    cost_anoms = [a for a in out if a["type"] == "anomaly_cost_per_turn"]
    by_origin = {a["details"]["origin"]: a for a in cost_anoms}
    # user_initiated has inform threshold 3.0 → 5x fires inform
    assert "user_initiated" in by_origin
    assert by_origin["user_initiated"]["details"]["detector_severity"] == "inform"
    # ui_chip has no inform threshold and warn at 10x → 5x stays quiet
    assert "ui_chip" not in by_origin, (
        "ui_chip with 5x ratio should NOT fire — operator-driven cost "
        f"has high forbearance. Got: {by_origin.get('ui_chip')}"
    )


def test_anomaly_signature_excludes_metric_redundancy():
    """The metric is already in the type field; scope_key should not
    repeat it. Pin the signature shape so a future rename doesn't
    silently break the dedup contract."""
    now = _now()
    old_ts = (now - timedelta(days=10)).isoformat()
    recent_ts = now.isoformat()
    spans = (
        [_span(end_time=old_ts, cost=0.10, session_id=f"old-{i}") for i in range(60)]
        + [_span(end_time=recent_ts, cost=5.00, session_id=f"new-{i}") for i in range(5)]
    )
    baselines = _compute_bot_baselines(spans, window_days=30, earliest_span_at=None)
    recent = _spans_in_window(spans, minutes=65, now=now)
    out = _collect_anomaly_signals(recent, baselines)
    for a in out:
        sig = a["signature"]
        # The metric name should appear EXACTLY ONCE in the signature
        # (in the type field). If it appears twice the scope_key is
        # double-encoding.
        if a["type"] == "anomaly_cost_per_turn":
            assert sig.count("cost_per_turn") == 1, sig


# ── End-to-end run ──────────────────────────────────────────────────────────


def test_run_on_empty_shared_dir_is_a_noop():
    """A brand-new pod with no cascade spans → no Signals, no
    failures. The runner must not throw."""
    with tempfile.TemporaryDirectory() as d:
        report = run(Path(d), dry_run=False)
        assert report["signals_fired"] == 0
        assert report["signals_resolved"] == 0
        assert report["observe_failures"] == 0
        assert report["spans_total"] == 0


def test_run_dry_run_does_not_write_signals():
    """Dry-run mode prints would-be Signals but does NOT touch the
    signal store. Useful for operator preview of what an hourly run
    would emit."""
    with tempfile.TemporaryDirectory() as d:
        report = run(Path(d), dry_run=True)
        # Even with no spans, signals_fired counts collected
        # detections; signals_resolved should stay 0 in dry-run.
        assert report["signals_resolved"] == 0


# ── plugin_telemetry_failure detector ─────────────────────────────────────


def _write_heartbeat(shared: Path, bot_id: str, age_seconds: int) -> Path:
    """Drop a recent-transcripts.json with a chosen mtime — simulates
    the plugin writing it `age_seconds` ago.
    """
    import os
    metrics = shared / "metrics" / bot_id
    metrics.mkdir(parents=True, exist_ok=True)
    path = metrics / "recent-transcripts.json"
    path.write_text("[]")
    mtime = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def test_plugin_telemetry_failure_fires_when_heartbeat_fresh_spans_zero():
    """Bot's plugin is alive (heartbeat 5min ago) but wrote zero
    cascade spans today → fire plugin_telemetry_failure Signal."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=300)  # 5 min ago
        signals = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=[], now=datetime.now(timezone.utc),
        )
        assert len(signals) == 1
        sig = signals[0]
        assert sig["type"] == "plugin_telemetry_failure"
        assert sig["bot_id"] == "admin_bot"
        assert sig["severity"] == "warn"
        # heartbeat_age_min should be ~5
        assert 4 <= sig["details"]["heartbeat_age_min"] <= 6


def test_plugin_telemetry_failure_quiet_when_bot_inactive():
    """Bot's heartbeat is >1h old (inactive). Don't yell about a
    quiet bot's missing spans — silence is correct here."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=7200)  # 2 hours ago
        signals = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=[], now=datetime.now(timezone.utc),
        )
        assert signals == []


def test_plugin_telemetry_failure_quiet_when_spans_present():
    """Bot's heartbeat is fresh AND today's spans exist → quiet.
    This is the normal healthy state."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=300)
        # Synthesize one span TODAY for admin_bot.
        now = datetime.now(timezone.utc)
        spans = [{
            "bot_id": "admin_bot",
            "end_time": now.isoformat(),
            "start_time": now.isoformat(),
            "attributes": {"session_id": "s1"},
        }]
        signals = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=spans, now=now,
        )
        assert signals == []


def test_plugin_telemetry_failure_per_bot_independence():
    """Some bots healthy, some broken → fire only on the broken ones.
    Per-bot signature → no collision."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=300)   # broken
        _write_heartbeat(shared, "team_bot_c", age_seconds=300)   # healthy
        _write_heartbeat(shared, "team_bot_a", age_seconds=300)     # broken
        now = datetime.now(timezone.utc)
        # Only team_bot_c has a span today.
        spans = [{
            "bot_id": "team_bot_c",
            "end_time": now.isoformat(),
            "start_time": now.isoformat(),
            "attributes": {"session_id": "r1"},
        }]
        signals = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=spans, now=now,
        )
        affected = sorted(s["bot_id"] for s in signals)
        assert affected == ["admin_bot", "team_bot_a"]


def test_plugin_telemetry_failure_signature_stable():
    """Same bot + same broken state → same signature on repeat runs,
    so signals.store.observe() deduplicates instead of duplicating."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=300)
        now = datetime.now(timezone.utc)
        sig1 = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=[], now=now,
        )[0]["signature"]
        sig2 = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=[], now=now,
        )[0]["signature"]
        assert sig1 == sig2
        assert sig1.startswith("cascade_audit:plugin_telemetry_failure:admin_bot")


def test_plugin_telemetry_failure_excludes_yesterday_spans():
    """Spans from yesterday don't count — we want today's emission to
    light up the detector. Otherwise a bot that wrote spans yesterday
    but broke today would show as healthy."""
    from cascade.audit_runner import _collect_plugin_telemetry_failure_signals

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        _write_heartbeat(shared, "admin_bot", age_seconds=300)
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        spans = [{
            "bot_id": "admin_bot",
            "end_time": yesterday.isoformat(),
            "start_time": yesterday.isoformat(),
            "attributes": {"session_id": "s_yesterday"},
        }]
        signals = _collect_plugin_telemetry_failure_signals(
            shared, recent_spans=spans, now=now,
        )
        assert len(signals) == 1, "yesterday's spans must not satisfy today's check"


# ── payload_drift detector ───────────────────────────────────────────────


def _drift_span(bot_id, drift_reason=None):
    """Build a span with cascade.struggle.score set (qualifies for the
    detector's denominator) and optionally a payload_drift reason."""
    attrs = {
        "session_id": f"s-{bot_id}",
        "cascade.struggle.score": 0.5,
    }
    if drift_reason:
        attrs["cascade.struggle.payload_drift"] = drift_reason
    return {"bot_id": bot_id, "attributes": attrs}


def test_payload_drift_fires_when_rate_above_threshold():
    """11 drifted out of 25 measured turns = 44% drift → Signal fires."""
    from cascade.audit_runner import _collect_payload_drift_signals
    spans = (
        [_drift_span("admin_bot", "no_messages") for _ in range(8)]
        + [_drift_span("admin_bot", "messages_not_array") for _ in range(3)]
        + [_drift_span("admin_bot") for _ in range(14)]
    )
    signals = _collect_payload_drift_signals(spans)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "struggle_detector_blind"
    assert sig["bot_id"] == "admin_bot"
    assert sig["severity"] == "warn"
    assert sig["details"]["drifted_count"] == 11
    assert sig["details"]["total_measured"] == 25
    assert sig["details"]["rate_pct"] == 44.0
    # Reasons broken out by frequency.
    assert sig["details"]["reasons"]["no_messages"] == 8
    assert sig["details"]["reasons"]["messages_not_array"] == 3


def test_payload_drift_quiet_under_threshold():
    """2 drifted out of 25 = 8% < 10% threshold → no Signal."""
    from cascade.audit_runner import _collect_payload_drift_signals
    spans = (
        [_drift_span("admin_bot", "no_messages") for _ in range(2)]
        + [_drift_span("admin_bot") for _ in range(23)]
    )
    assert _collect_payload_drift_signals(spans) == []


def test_payload_drift_quiet_on_tiny_sample():
    """100% drift on 10 turns shouldn't fire — sample too small."""
    from cascade.audit_runner import _collect_payload_drift_signals
    spans = [_drift_span("admin_bot", "no_messages") for _ in range(10)]
    assert _collect_payload_drift_signals(spans) == []


def test_payload_drift_ignores_spans_without_struggle_score():
    """Spans without `cascade.struggle.score` aren't counted (those are
    turns where struggle wasn't even attempted, not drift)."""
    from cascade.audit_runner import _collect_payload_drift_signals
    spans = [
        # 30 spans WITH score, none drifted → denominator 30, num 0
        *[_drift_span("admin_bot") for _ in range(30)],
        # 30 spans WITHOUT score → excluded entirely
        *[{"bot_id": "admin_bot", "attributes": {}} for _ in range(30)],
    ]
    assert _collect_payload_drift_signals(spans) == []


def test_payload_drift_per_bot():
    """Each bot evaluated independently."""
    from cascade.audit_runner import _collect_payload_drift_signals
    # admin_bot: 50% drift (fires); team_bot_c: 5% (silent)
    spans = (
        [_drift_span("admin_bot", "no_messages") for _ in range(12)]
        + [_drift_span("admin_bot") for _ in range(12)]
        + [_drift_span("team_bot_c", "no_messages") for _ in range(1)]
        + [_drift_span("team_bot_c") for _ in range(19)]
    )
    signals = _collect_payload_drift_signals(spans)
    affected = [s["bot_id"] for s in signals]
    assert affected == ["admin_bot"]


def test_payload_drift_signature_stable():
    """Same input → same signature, so observe() dedupes across runs."""
    from cascade.audit_runner import _collect_payload_drift_signals
    spans = (
        [_drift_span("admin_bot", "no_messages") for _ in range(15)]
        + [_drift_span("admin_bot") for _ in range(15)]
    )
    s1 = _collect_payload_drift_signals(spans)[0]["signature"]
    s2 = _collect_payload_drift_signals(spans)[0]["signature"]
    assert s1 == s2
    assert s1.startswith("cascade_audit:struggle_detector_blind:admin_bot")


# ── Tier-routing disagreement detector ──────────────────────────────────────


def _routing_span(
    *,
    bot_id="team_bot_a",
    session_id="s1",
    chosen_by="classifier",
    trigger_kind="user_turn",
    tier_used="tier2",
    model=None,
):
    """Compact span factory for tier-routing-disagreement tests.

    Same shape as _span() above but with the fields this detector
    actually cares about hoisted to keyword args. ``model`` is included
    so the spend_cap/runaway refuse-sentinel path can be exercised.
    """
    attrs = {
        "session_id": session_id,
        "cascade.tier_used": tier_used,
        "cascade.tier_chosen_by": chosen_by,
        "cascade.trigger_kind": trigger_kind,
    }
    if model is not None:
        attrs["cascade.model"] = model
    end_time = _now().isoformat()
    return {
        "bot_id": bot_id,
        "start_time": end_time,
        "end_time": end_time,
        "attributes": attrs,
    }


def _config(*, default_tier=None, background_tier="tier3", maintenance_tier="tier3"):
    """Build a minimal evolve-tiers.json-shaped dict for the loader stub."""
    cfg: dict = {
        "routing": {
            "backgroundTier": background_tier,
            "maintenanceTier": maintenance_tier,
        },
    }
    if default_tier is not None:
        cfg["userTierOverride"] = {"defaultTier": default_tier}
    return cfg


# ── _expected_tier_for (pure helper) ───────────────────────────────────────


def test_expected_tier_classifier_background_returns_tier3():
    assert _expected_tier_for("classifier", "heartbeat", _config()) == "tier3"
    assert _expected_tier_for("classifier", "cron_app", _config()) == "tier3"


def test_expected_tier_classifier_maintenance_returns_tier3():
    for trigger in ("subagent", "summarizer", "classifier",
                    "task_extractor", "fallback"):
        assert _expected_tier_for("classifier", trigger, _config()) == "tier3", (
            f"trigger={trigger} should map to tier3"
        )


def test_expected_tier_classifier_user_turn_returns_none():
    """user_turn / unknown classifier turns have no static expectation
    — they fall through to bot default, which depends on tier1/2/3
    config that varies. Skipped from the audit."""
    assert _expected_tier_for("classifier", "user_turn", _config()) is None
    assert _expected_tier_for("classifier", None, _config()) is None


def test_expected_tier_classifier_routing_override_honored():
    """routing.backgroundTier / maintenanceTier overrides the default
    tier3 — the audit must follow the operator's actual config, not a
    hardcoded tier3."""
    cfg = _config(background_tier="tier2", maintenance_tier="tier1")
    assert _expected_tier_for("classifier", "heartbeat", cfg) == "tier2"
    assert _expected_tier_for("classifier", "summarizer", cfg) == "tier1"


@pytest.mark.parametrize("choice,expected_tier", [
    ("fast", "tier3"),
    ("standard", "tier2"),
    ("power", "tier1"),
])
def test_expected_tier_operator_default_maps_choice_to_tier(choice, expected_tier):
    cfg = _config(default_tier=choice)
    assert _expected_tier_for("operator_default", None, cfg) == expected_tier


def test_expected_tier_operator_default_auto_returns_none():
    """'auto' means 'no override' — operator_default driver shouldn't
    fire in that state. If a span shows up with chosen_by=operator_
    default and auto config, that's a separate bug class (stale
    config); the detector skips it cleanly."""
    assert _expected_tier_for("operator_default", None,
                              _config(default_tier="auto")) is None
    assert _expected_tier_for("operator_default", None, _config()) is None


def test_expected_tier_spend_cap_and_runaway_return_tier3():
    assert _expected_tier_for("spend_cap", None, _config()) == "tier3"
    assert _expected_tier_for("runaway", "heartbeat", _config()) == "tier3"


def test_expected_tier_unhandled_drivers_return_none():
    """user_request, user_default, cascade depend on per-session state
    we can't recover passively. Detector skips them rather than emit
    false-positive divergences."""
    for driver in ("user_request", "user_default", "cascade"):
        assert _expected_tier_for(driver, "user_turn", _config()) is None


# ── _collect_tier_routing_disagreement_signals ─────────────────────────────


def test_tier_audit_fires_when_background_lands_on_wrong_tier():
    """REGRESSION (PR #1737 shape): heartbeat sessions landing on tier2
    instead of tier3 must trip the alert. This is the silent class —
    operator only sees it on the cost chart days later without this."""
    # 25 heartbeats, 20 of them landed on tier2 (the regression bug);
    # 5 correctly on tier3. 20/25 = 80% wrong, well over the 25%
    # threshold.
    spans = [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier2",
        )
        for i in range(20)
    ] + [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier3",
        )
        for i in range(5)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "tier_routing_disagreement"
    assert sig["bot_id"] == "team_bot_a"
    assert sig["scope"] == "bot"
    assert sig["severity"] == "warn"
    # Worst bucket surfaces the regression cleanly
    assert sig["details"]["worst_bucket"]["driver"] == "classifier"
    assert sig["details"]["worst_bucket"]["trigger_kind"] == "heartbeat"
    assert sig["details"]["worst_bucket"]["expected_tier"] == "tier3"
    # And the body lists the wrong-vs-total
    assert "20/25" in sig["body"]


def test_tier_audit_fires_when_operator_default_diverges():
    """Operator set defaultTier=fast (→ tier3) in the UI, but turns
    with chosen_by=operator_default are landing on tier2 — config and
    plugin in disagreement, classic Phase A drift signal."""
    spans = [
        _routing_span(
            bot_id="admin_bot", session_id=f"s-{i}",
            chosen_by="operator_default", trigger_kind="user_turn",
            tier_used="tier2",  # wrong: config says fast → tier3
        )
        for i in range(_TIER_AUDIT_MIN_TURNS)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(default_tier="fast"),
    )
    assert len(out) == 1
    assert out[0]["details"]["worst_bucket"]["driver"] == "operator_default"
    assert out[0]["details"]["worst_bucket"]["expected_tier"] == "tier3"


def test_tier_audit_quiet_when_agreement():
    """Correctly-routed turns produce ZERO Signals. The detector is
    silent in the happy path — load-bearing for the daemon's signal-
    to-noise ratio."""
    spans = [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier3",
        )
        for i in range(50)
    ] + [
        _routing_span(
            bot_id="team_bot_a", session_id=f"od-{i}",
            chosen_by="operator_default", trigger_kind="user_turn",
            tier_used="tier3",
        )
        for i in range(30)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(default_tier="fast"),
    )
    assert out == []


def test_tier_audit_below_min_turns_does_not_fire():
    """Below _TIER_AUDIT_MIN_TURNS (20), the rate is too noisy to
    trust. Even at 100% disagreement, fewer than 20 spans → quiet."""
    assert _TIER_AUDIT_MIN_TURNS == 20
    spans = [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier2",  # all wrong
        )
        for i in range(_TIER_AUDIT_MIN_TURNS - 1)  # one short
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert out == []


def test_tier_audit_below_divergence_threshold_does_not_fire():
    """A small flare (e.g. one user manually overriding) shouldn't trip
    the alert — only sustained divergence does."""
    assert _TIER_AUDIT_DIVERGENCE_THRESHOLD == 0.25
    # 50 turns; 5 wrong (10%) — below the 25% threshold
    spans = [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier3",
        )
        for i in range(45)
    ] + [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-w-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier2",
        )
        for i in range(5)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert out == []


def test_tier_audit_safety_net_refuse_sentinel_is_correct_outcome():
    """REGRESSION (PR #1777): when the safety net fires AND tier3 is
    unconfigured, the plugin returns the refuse sentinel and OC fails
    to resolve. tier_used=null + model=sentinel is NOT a divergence —
    it's the breaker working as designed. Detector must not flag it."""
    # 25 spend_cap turns, all returning the sentinel (tier_used=null,
    # model=sentinel). Should be silent.
    spans = [
        _routing_span(
            bot_id="team_bot_c", session_id=f"sc-{i}",
            chosen_by="spend_cap", trigger_kind=None,
            tier_used=None, model=_TIER_AUDIT_REFUSE_SENTINEL,
        )
        for i in range(25)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert out == []


def test_tier_audit_safety_net_actual_divergence_fires():
    """If spend_cap fires but the turn lands on tier2 (not tier3, not
    sentinel) that IS a divergence — the breaker is leaking. This is
    the L5 audit miss class the detector exists to catch."""
    spans = [
        _routing_span(
            bot_id="team_bot_c", session_id=f"sc-{i}",
            chosen_by="spend_cap", trigger_kind=None,
            tier_used="tier2",  # leak: should be tier3 or sentinel
        )
        for i in range(25)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert len(out) == 1
    assert out[0]["details"]["worst_bucket"]["driver"] == "spend_cap"


def test_tier_audit_per_bot_independence():
    """One bot diverging + one healthy → one Signal, scoped to the
    affected bot. Other bots' Signals must not get cross-contaminated."""
    spans = (
        # team_bot_a: 25 background turns ALL landing on tier2 (broken)
        [_routing_span(bot_id="team_bot_a", session_id=f"k-{i}",
                       chosen_by="classifier", trigger_kind="heartbeat",
                       tier_used="tier2") for i in range(25)]
        # admin_bot: 25 background turns landing on tier3 (correct)
        + [_routing_span(bot_id="admin_bot", session_id=f"s-{i}",
                         chosen_by="classifier", trigger_kind="heartbeat",
                         tier_used="tier3") for i in range(25)]
    )
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert len(out) == 1
    assert out[0]["bot_id"] == "team_bot_a"


def test_tier_audit_signature_per_bot_stable():
    """Same condition on two runs → same signature so the Signal
    store's observe() find-or-creates instead of duplicating. Required
    for the sweep-resolve auto-clear to work."""
    spans = [
        _routing_span(
            bot_id="team_bot_a", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier2",
        )
        for i in range(25)
    ]
    s1 = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )[0]["signature"]
    s2 = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )[0]["signature"]
    assert s1 == s2
    assert s1.startswith("cascade_audit:tier_routing_disagreement:team_bot_a")


def test_tier_audit_unreadable_config_is_treated_as_empty():
    """When the loader fails (file missing, ACL denied), the detector
    falls back to defaults (routing.backgroundTier=tier3). Bots that
    have never been configured shouldn't crash the audit pass."""
    spans = [
        _routing_span(
            bot_id="new-bot", session_id=f"hb-{i}",
            chosen_by="classifier", trigger_kind="heartbeat",
            tier_used="tier3",
        )
        for i in range(25)
    ]
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: {},
    )
    # Defaults: heartbeat → tier3; tier_used=tier3 → all agree, quiet.
    assert out == []


def test_tier_audit_lists_all_divergent_buckets_in_body():
    """When a bot has divergences on multiple (driver, trigger)
    buckets, the body lists every one (operator should see the full
    picture). Single Signal per bot — they triage by bot, not by
    bucket."""
    spans = (
        # heartbeat → wrong tier
        [_routing_span(bot_id="team_bot_a", session_id=f"hb-{i}",
                       chosen_by="classifier", trigger_kind="heartbeat",
                       tier_used="tier2") for i in range(25)]
        # operator_default → wrong tier
        + [_routing_span(bot_id="team_bot_a", session_id=f"od-{i}",
                         chosen_by="operator_default", trigger_kind="user_turn",
                         tier_used="tier1") for i in range(25)]
    )
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(default_tier="fast"),
    )
    assert len(out) == 1
    body = out[0]["body"]
    # Both diverging buckets surfaced
    assert "trigger=heartbeat" in body
    assert "chosen_by=operator_default" in body
    assert "2 buckets" in out[0]["title"]


def test_tier_audit_empty_string_trigger_normalized_to_none():
    """trigger_kind="" / "unknown" / missing all normalize to None
    before bucket assembly — matches the _expected_tier_for(None)
    behavior so we don't double-bucket the same conceptual case."""
    spans = (
        # 10 spans with trigger="" (normalized to None → classifier
        # branch returns None → skipped, not divergent)
        [_routing_span(bot_id="team_bot_a", session_id=f"e-{i}",
                       chosen_by="classifier", trigger_kind="",
                       tier_used="tier1") for i in range(15)]
        # 15 spans with trigger="unknown" (also normalized to None →
        # bucketed together with empty-string)
        + [_routing_span(bot_id="team_bot_a", session_id=f"u-{i}",
                         chosen_by="classifier", trigger_kind="unknown",
                         tier_used="tier1") for i in range(15)]
    )
    # Detector should not emit — these are user_turn-equivalent classifier
    # turns with no static expectation.
    out = _collect_tier_routing_disagreement_signals(
        spans, config_loader=lambda bot: _config(),
    )
    assert out == []

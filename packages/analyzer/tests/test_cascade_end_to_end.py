"""Cross-module end-to-end regression test for the cascade pipeline.

Catches the bug class that the per-module unit tests miss: the
*plugin writes spans to per-bot dirs, but Python readers go to the
central observability dir*. Every individual unit test passes
against its own synthetic layout, but on the real mini the pipeline
produces zero output because writers and readers use different paths.

This test writes a span the SAME way the plugin would (per-bot path,
OpikSpan-compatible JSON shape, all the attributes the audit pipeline
expects to read) and then runs:

  - audit_runner.run() — should detect anomalies + dangerous-combo +
    runaway-rate Signals and persist labels
  - pressure_watchdog.run_once() — should read the span and write
    pressure_flags.json with non-zero counters
  - routes_cascade._compute_health() — should see the spans in its
    today-count and disagreement-rate
  - labeler.label_spans() — should produce a labeled outcome

Any of these returning zero output against a span the plugin would
actually write = a cross-module path contract bug.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for p in (_ANALYZER_DIR, _ADMIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _write_plugin_span(
    shared: Path,
    *,
    bot_id: str,
    end_time: datetime,
    attributes: dict,
    cost: float = 0.10,
    input_tokens: int = 5000,
):
    """Write a span EXACTLY the way packages/plugin/.../CascadeTelemetry.ts
    would. The path layout and JSON shape are the contract — if either
    drifts, the readers go blind even though their own tests pass.
    """
    spans_dir = shared / bot_id / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    day = end_time.date().isoformat()
    iso = end_time.isoformat()
    span = {
        "name": "bot_session_turn",
        "producer": "cascade_telemetry",
        "bot_id": bot_id,
        "start_time": iso,
        "end_time": iso,
        "type": "general",
        "total_cost": cost,
        "usage": {"input_tokens": input_tokens, "output_tokens": 500},
        "attributes": attributes,
    }
    path = spans_dir / f"spans-{day}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(span) + "\n")
    return path


# ── End-to-end: plugin span → audit_runner ─────────────────────────────────


def test_audit_runner_sees_per_bot_plugin_spans():
    """audit_runner.run() against a synthesized plugin-style per-bot
    span should report spans_total > 0. Earlier this returned 0
    because audit_runner read only `observability/spans/`."""
    from cascade.audit_runner import run

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        now = datetime.now(timezone.utc)
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "s1",
                "turn_index": 0,
                "cascade.trigger_kind": "user_turn",
                "cascade.tier_used": "tier2",
                "cascade.tier_chosen_by": "classifier",
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.holdout": False,
            },
        )
        report = run(shared, dry_run=True, now=now)
        assert report["spans_total"] >= 1, (
            "audit_runner returned zero spans — per-bot path contract is broken. "
            f"report={report}"
        )


def test_audit_runner_emits_dangerous_combo_signal():
    """Plugin-style dangerous-combo span → audit_runner emits a Signal.
    In dry-run mode the signal is recorded in `signals_fired` but not
    written to disk."""
    from cascade.audit_runner import run

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        now = datetime.now(timezone.utc)
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "combo-1",
                "turn_index": 0,
                "cascade.trigger_kind": "heartbeat",
                "cascade.tier_used": "tier1",
                "cascade.tier_chosen_by": "cascade",
                "cascade.dangerous_combo.matched": True,
                "cascade.dangerous_combo.context_tokens": 120_000,
                "cascade.holdout": False,
            },
            input_tokens=120_000,
            cost=0.50,
        )
        report = run(shared, dry_run=True, now=now)
        assert report["signals_fired"] >= 1


def test_audit_runner_persists_labels_to_per_day_file():
    """label_spans() through audit_runner.run() writes labels to
    {shared}/cascade/labels/<day>.jsonl when a span fires Signal #1."""
    from cascade.audit_runner import run

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        now = datetime.now(timezone.utc)
        # Operator UI-chip override picking tier1 (Power) — Signal #1
        # for "should have escalated."
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "ui-1",
                "turn_index": 0,
                "cascade.trigger_kind": "user_turn",
                "cascade.tier_used": "tier1",
                "cascade.tier_chosen_by": "user_request",
                "cascade.consent_source": "ui_chip",  # required after fix #4
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.holdout": False,
            },
            cost=0.30,
        )
        # Need a non-ui-chip span so the labeler sees both, but
        # label_session only requires the ui_chip span to fire.
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "ui-1",
                "turn_index": 1,
                "cascade.trigger_kind": "user_turn",
                "cascade.tier_used": "tier1",
                "cascade.tier_chosen_by": "user_request",
                "cascade.consent_source": "ui_chip",
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.holdout": False,
            },
        )
        # Real-run (not dry-run) so write_labels actually fires.
        report = run(shared, dry_run=False, now=now)
        assert report["labels_persisted"] >= 1, (
            f"labels_persisted={report['labels_persisted']} — labeler may not be "
            "wired to plugin-style spans. report={report}"
        )
        labels_path = shared / "cascade" / "labels" / f"{now.date().isoformat()}.jsonl"
        assert labels_path.is_file()
        line = labels_path.read_text(encoding="utf-8").strip().splitlines()[0]
        outcome = json.loads(line)
        assert outcome["label"] in ("should_have_escalated", "should_have_demoted")
        assert outcome["source"] == "ui_chip_override"


# ── End-to-end: plugin span → pressure_watchdog ──────────────────────────


def test_pressure_watchdog_reads_per_bot_spans():
    """pressure_watchdog.run_once() against a tier1 span should count
    it in pod_tier1_active_sessions. Earlier this returned 0 because
    the watchdog read only the central observability dir."""
    from cascade.pressure_watchdog import run_once

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        now = datetime.now(timezone.utc)
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "live-tier1-1",
                "turn_index": 0,
                "cascade.trigger_kind": "user_turn",
                "cascade.tier_used": "tier1",
                "cascade.tier_chosen_by": "cascade",
                "cascade.holdout": False,
            },
            cost=0.20,
        )
        report = run_once(shared, now=now)
        assert report["summary"]["pod_tier1_active_sessions"] >= 1, (
            "watchdog returned 0 tier1 sessions despite a tier1 span existing — "
            f"path contract broken. report={report}"
        )


# ── End-to-end: plugin span → routes_cascade ─────────────────────────────


def test_routes_cascade_health_sees_per_bot_spans():
    """`_compute_health` should see plugin-written per-bot spans and
    produce a non-no_data state with non-zero spans_today."""
    from evolve_admin.web.routes_cascade import _compute_health

    with tempfile.TemporaryDirectory() as d:
        shared = Path(d)
        now = datetime.now(timezone.utc)
        # Watchdog heartbeat present so we don't fall into no_data.
        cascade_dir = shared / "cascade"
        cascade_dir.mkdir(parents=True)
        (cascade_dir / "pressure_flags.json").write_text(json.dumps({
            "watchdog_heartbeat": now.isoformat(),
        }))
        _write_plugin_span(
            shared,
            bot_id="team_bot_a",
            end_time=now,
            attributes={
                "session_id": "s1",
                "turn_index": 0,
                "cascade.trigger_kind": "user_turn",
                "cascade.tier_used": "tier2",
                "cascade.tier_chosen_by": "classifier",
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.shadow_verdict.disagrees": False,
                "cascade.holdout": False,
            },
        )
        snap = _compute_health(shared, now=now)
        assert snap["state"] != "no_data"
        assert snap["spans_today"] >= 1, (
            f"routes_cascade saw 0 spans today — per-bot path contract broken. "
            f"snap={snap}"
        )
        assert snap["disagreement"]["total"] >= 1


# ── Token field name contract ─────────────────────────────────────────────


def test_audit_runner_anomaly_uses_input_tokens_not_prompt_tokens():
    """The plugin writes `usage.input_tokens`. audit_runner reads it
    when computing context-token anomalies. Earlier the runner read
    `prompt_tokens` (a guess) → context-token anomaly Signals never
    fired. Pin the field name."""
    from cascade.audit_runner import _collect_anomaly_signals
    from cascade.anomaly_detector import BotBaseline, BaselineStats

    now = datetime.now(timezone.utc)
    span = {
        "bot_id": "team_bot_a",
        "end_time": now.isoformat(),
        "start_time": now.isoformat(),
        "total_cost": 0.10,
        # Plugin writes input_tokens. If the runner reads prompt_tokens
        # it would see 0 here and not count the value at all.
        "usage": {"input_tokens": 50_000},
        "attributes": {
            "cascade.trigger_kind": "user_turn",
            "cascade.tier_used": "tier2",
            "cascade.tier_chosen_by": "classifier",
            "cascade.consent_source": None,
        },
    }
    baseline = BotBaseline(
        bot_id="team_bot_a",
        window_days=30,
        context_tokens_per_turn=BaselineStats(n=100, mean=5000, median=5000, p95=10000),
        source="bot_specific",
    )
    out = _collect_anomaly_signals([span], {"team_bot_a": baseline})
    ctx_anoms = [a for a in out if a["type"] == "anomaly_context_tokens_per_turn"]
    assert len(ctx_anoms) >= 1, (
        "context-tokens anomaly didn't fire on a 10x baseline span — "
        "the runner is probably still reading the wrong usage field. "
        f"all signals: {[a['type'] for a in out]}"
    )


# ── Labeler consent_source discrimination ─────────────────────────────────


def test_labeler_excludes_bot_initiated_from_ui_chip_label():
    """Labeler must distinguish ui_chip from bot_initiated /
    ask_hint_agreed even though all three carry
    tier_chosen_by=user_request. Earlier this conflation polluted
    the Phase 4 calibration ground truth."""
    from cascade.labeler import label_spans, LabelSource

    now = datetime.now(timezone.utc).isoformat()
    # bot_initiated tier1 grant — must NOT produce a UI_CHIP_OVERRIDE
    # label.
    bot_init_span = {
        "name": "bot_session_turn",
        "producer": "cascade_telemetry",
        "bot_id": "team_bot_a",
        "start_time": now,
        "end_time": now,
        "attributes": {
            "session_id": "bot-init-1",
            "turn_index": 0,
            "cascade.tier_used": "tier1",
            "cascade.tier_chosen_by": "user_request",
            "cascade.consent_source": "bot_initiated",
        },
    }
    outcomes = list(label_spans([bot_init_span]))
    ui_chip_outcomes = [o for o in outcomes if o.source == LabelSource.UI_CHIP_OVERRIDE]
    assert ui_chip_outcomes == [], (
        f"bot_initiated tier1 grant produced a UI_CHIP_OVERRIDE label — "
        f"consent_source discrimination is broken. outcomes={outcomes}"
    )

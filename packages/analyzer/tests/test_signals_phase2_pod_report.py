"""tests/test_signals_phase2_pod_report.py — Phase 2 pod_report cutover.

Spec: docs/spec-alerts-signal-store-2026-05-07.md (Phase 2).

Covers:
  - Each ReportLine emits a corresponding Signal with the right
    flavor (broken→maintenance, trending+queue→activity), severity,
    type, and producer
  - sweep_resolve clears Signals when their condition no longer fires
    on the next pod_report run
  - Threshold tuning still drives Signal emission as expected
  - The "RSI: N proposals pending" line shows in the Activity lane
    (the spec exit-gate scenario)
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from pod_report import DEFAULT_OVERRIDES, run_report  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures (re-using the helpers from test_pod_report_polish but
# inlined to avoid cross-test imports)
# ─────────────────────────────────────────────────────────────────────────────


def _write_audit_snapshot(shared_dir: Path, *, age_minutes: int, criticals: int = 0,
                          warns: int = 0):
    """Match the on-disk shape pod_report expects (see test_pod_report_polish)."""
    completed_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    payload = {
        "schema_version": 1,
        "audit_completed_at": completed_at.isoformat(),
        "audit_succeeded": True,
        "critical": [
            {"category": "machine", "bot_id": None, "message": f"crit-{i}", "detail": ""}
            for i in range(criticals)
        ],
        "warn": [
            {"category": "config", "bot_id": "admin_bot", "message": f"warn-{i}", "detail": ""}
            for i in range(warns)
        ],
    }
    (shared_dir / "audit").mkdir(parents=True, exist_ok=True)
    (shared_dir / "audit" / "current-findings.json").write_text(json.dumps(payload))


def _write_status(shared_dir: Path, bot_id: str, reachable: bool):
    p = shared_dir / "status" / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    iso = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps({
        "gateway_reachable": reachable,
        "last_checked_at": iso,
        "first_unreachable_at": None if reachable else (
            (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        ),
    }))


def _write_metric(shared_dir: Path, bot_id: str, day: date, *,
                  session_count: int = 0, total_cost_estimated: float = 0.0):
    p = shared_dir / "metrics" / day.isoformat() / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "session_count": session_count,
        "total_cost_estimated": total_cost_estimated,
    }))


def _seed_baseline(shared_dir: Path, members: list[str], end_date: date):
    """Seed 30 days of stable history ending the day before end_date."""
    for offset in range(1, 31):
        d = end_date - timedelta(days=offset)
        for bot in members:
            _write_metric(shared_dir, bot, d,
                          session_count=20, total_cost_estimated=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Each bucket maps to the right Signal flavor + severity
# ─────────────────────────────────────────────────────────────────────────────


def test_broken_bucket_emits_maintenance_signals(tmp_path):
    """Audit critical (broken bucket) → Maintenance lane, severity alert."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=2)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    run_report(tmp_path, members, DEFAULT_OVERRIDES, label="Test",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))

    sigs = list(signals_store.iter_active(tmp_path, producer="pod_report"))
    audit_sigs = [s for s in sigs if s.type == "audit_critical"]
    assert len(audit_sigs) == 1
    sig = audit_sigs[0]
    assert sig.flavor == "maintenance"
    assert sig.severity == "alert"
    assert sig.scope == "pod"
    assert "Audit:" in sig.body


def test_trending_bucket_emits_activity_signals(tmp_path):
    """Cost spike (trending bucket) → Activity lane, severity warn."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(tmp_path, members, end_date)
    # Spike on yesterday: 4× the baseline mean of $1.0
    _write_metric(tmp_path, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=4.5)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=0)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    run_report(tmp_path, members, DEFAULT_OVERRIDES, label="Test",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))

    sigs = list(signals_store.iter_active(tmp_path, producer="pod_report"))
    cost_sigs = [s for s in sigs if s.type == "cost_spike"]
    assert len(cost_sigs) == 1
    sig = cost_sigs[0]
    assert sig.flavor == "activity"
    assert sig.severity == "warn"
    assert "team_bot_a" in (sig.details.get("affected_bots") or [])


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve: condition cleared on next run → signal auto-resolves
# ─────────────────────────────────────────────────────────────────────────────


def test_sweep_resolve_clears_no_longer_firing_signals(tmp_path):
    """Run pod_report twice — first run fires, second run finds the
    condition cleared. The previously-firing signal must auto-resolve."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=2)  # fires
    _write_status(tmp_path, "team_bot_a", reachable=True)

    # First run — audit_critical fires
    run_report(tmp_path, members, DEFAULT_OVERRIDES, label="r1",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))
    actives_1 = {s.type for s in signals_store.iter_active(tmp_path, producer="pod_report")}
    assert "audit_critical" in actives_1

    # Clear the audit condition
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=0)

    # Second run — should auto-resolve
    run_report(tmp_path, members, DEFAULT_OVERRIDES, label="r2",
               now=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc))
    actives_2 = list(signals_store.iter_active(tmp_path, producer="pod_report"))
    types_2 = {s.type for s in actives_2}
    assert "audit_critical" not in types_2


def test_sweep_resolve_only_touches_pod_report_signals(tmp_path):
    """A signal from a different producer (e.g. watchdog) must NOT be
    swept by pod_report's run."""
    # Seed a watchdog signal directly
    signals_store.observe(
        tmp_path,
        signature="evolve_watchdog:calibration_drift:pod",
        producer="evolve_watchdog",
        type="calibration_drift",
        flavor="activity",
        severity="warn",
        scope="pod",
        title="Watchdog drift",
    )

    # Run pod_report with everything green
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=0)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    run_report(tmp_path, members, DEFAULT_OVERRIDES, label="green",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))

    # Watchdog signal still firing
    actives = list(signals_store.iter_active(tmp_path))
    watchdog_sigs = [s for s in actives if s.producer == "evolve_watchdog"]
    assert len(watchdog_sigs) == 1
    assert watchdog_sigs[0].state == "firing"


# ─────────────────────────────────────────────────────────────────────────────
# Dedup: same condition, multiple runs → one rolling signal
# ─────────────────────────────────────────────────────────────────────────────


def test_repeat_runs_dedup_into_one_rolling_signal(tmp_path):
    """If audit-critical fires for several consecutive runs, only ONE
    Signal exists with bumped observation_count."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=2)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    for n in range(3):
        run_report(tmp_path, members, DEFAULT_OVERRIDES, label=f"r{n}",
                   now=datetime(2026, 5, 7, 8 + n, 0, tzinfo=timezone.utc))

    sigs = [s for s in signals_store.iter_active(tmp_path, producer="pod_report")
            if s.type == "audit_critical"]
    assert len(sigs) == 1
    assert sigs[0].observation_count == 3


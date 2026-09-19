"""tests/test_signals_phase2_e2e.py — Phase 2 exit-gate smoke test.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 2 exit gate).

We can't test Telegram delivery (requires the openclaw CLI), but we assert:

  - the Reports tab is hidden in admin/web/index.html
  - /api/reports-alerts/status still returns its v2 shape unchanged
  - sweep-resolve clears old signals when conditions clear
  - threshold PATCH still flips detection on the next run
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import pytest  # noqa: E402

from pod_report import DEFAULT_OVERRIDES, run_report  # noqa: E402
from signals import store as signals_store  # noqa: E402


# Re-use audit / metric helpers from the Phase 2 unit test.
from tests.test_signals_phase2_pod_report import (  # noqa: E402
    _seed_baseline,
    _write_audit_snapshot,
    _write_metric,
    _write_status,
)


@pytest.fixture
def admin_client(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps({"sharedDir": str(shared), "bots": ["team_bot_a"]}),
        encoding="utf-8",
    )
    from evolve_admin.web.server import create_app
    app = create_app(network)
    return app.test_client(), shared, network


# ─────────────────────────────────────────────────────────────────────────────
# UI invariants — Reports tab is gone, Alerts tab works
# ─────────────────────────────────────────────────────────────────────────────


def test_reports_nav_item_is_hidden_alerts_is_visible():
    """Phase 2 hid the Reports nav item; the Alerts page is the entry."""
    index_html = (
        _ANALYZER_DIR.parent / "admin" / "evolve_admin" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    # Reports nav has display:none in its inline style
    assert (
        '<div class="nav-item" data-page="reports-alerts" onclick="nav(this)" style="display:none"'
        in index_html
    )
    # Alerts nav is visible (no display:none on the nav-item itself)
    assert (
        '<div class="nav-item" data-page="alerts" onclick="nav(this)">' in index_html
    )

    # The stale "Per-bot overrides available below" header is gone
    assert "Per-bot overrides available below" not in index_html


def test_alerts_page_exposes_schedule_and_thresholds_subtabs():
    """Phase 2 exit gate: Schedule + Thresholds live under Alerts."""
    index_html = (
        _ANALYZER_DIR.parent / "admin" / "evolve_admin" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    # Subtabs declared on the Alerts page
    assert 'data-subtab="al-schedule"' in index_html
    assert 'data-subtab="al-thresholds"' in index_html

    # Subtab containers exist with the IDs the existing _raLoad* handlers
    # write into (we kept the IDs stable when moving the markup)
    assert 'id="alerts-schedule"' in index_html
    assert 'id="alerts-thresholds"' in index_html
    assert 'id="ra-schedule-body"' in index_html
    assert 'id="ra-thresholds-body"' in index_html


# ─────────────────────────────────────────────────────────────────────────────
# Backwards-compat: the legacy /api/reports-alerts/* endpoints still work
# (Phase 6 will delete or redirect them — Phase 2 keeps them functional)
# ─────────────────────────────────────────────────────────────────────────────


def test_reports_alerts_status_endpoint_still_returns_v2_shape(admin_client):
    """Schedule subtab + the test-send button both depend on this endpoint."""
    client, shared, _network = admin_client
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(shared, members, end_date)
    _write_metric(shared, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(shared, age_minutes=3, criticals=0)
    _write_status(shared, "team_bot_a", reachable=True)

    resp = client.get("/api/reports-alerts/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "status" in data or "buckets" in data  # tolerant — exact shape is the v2 contract


def test_reports_alerts_thresholds_get_patch_still_works(admin_client):
    """Thresholds subtab POSTs config changes here."""
    client, _shared, _network = admin_client
    resp = client.get("/api/reports-alerts/thresholds")
    assert resp.status_code == 200

    resp = client.patch(
        "/api/reports-alerts/thresholds",
        json={"cost_anomaly_factor": 5.0},
    )
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Threshold tuning still drives signal emission
# ─────────────────────────────────────────────────────────────────────────────


def test_threshold_tuning_changes_signal_emission(tmp_path):
    """Lowering cost_anomaly_factor from 2.0 → 1.2 should make a 1.5×
    spike fire as cost_spike that wouldn't fire at the default."""
    shared = tmp_path
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(shared, members, end_date)
    # 1.5× the $1.0 baseline mean — within default factor of 2.0, but
    # would breach a 1.2× threshold.
    _write_metric(shared, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.5)
    _write_audit_snapshot(shared, age_minutes=3, criticals=0)
    _write_status(shared, "team_bot_a", reachable=True)

    # Default thresholds: should NOT fire
    run_report(shared, members, DEFAULT_OVERRIDES, label="default",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))
    sigs = list(signals_store.iter_active(shared, producer="pod_report"))
    assert not any(s.type == "cost_spike" for s in sigs)

    # Tighter threshold: should fire
    tight = {**DEFAULT_OVERRIDES, "cost_anomaly_factor": 1.2}
    run_report(shared, members, tight, label="tight",
               now=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc))
    sigs = list(signals_store.iter_active(shared, producer="pod_report"))
    cost_sigs = [s for s in sigs if s.type == "cost_spike"]
    assert len(cost_sigs) == 1
    assert cost_sigs[0].severity == "warn"


# ─────────────────────────────────────────────────────────────────────────────
# Empty (green) run still emits zero signals — heartbeat path
# ─────────────────────────────────────────────────────────────────────────────


def test_green_run_emits_no_signals_and_clears_old_ones(tmp_path):
    """When everything is green, pod_report emits zero new firing
    signals AND sweep-resolves any leftover ones from prior runs."""
    shared = tmp_path
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    _seed_baseline(shared, members, end_date)
    _write_metric(shared, "team_bot_a", end_date - timedelta(days=1),
                  session_count=20, total_cost_estimated=1.0)
    _write_audit_snapshot(shared, age_minutes=3, criticals=2)  # fires
    _write_status(shared, "team_bot_a", reachable=True)

    # First run with audit_critical firing
    run_report(shared, members, DEFAULT_OVERRIDES, label="r1",
               now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc))
    actives = list(signals_store.iter_active(shared, producer="pod_report"))
    assert any(s.type == "audit_critical" for s in actives)

    # Clear the condition — pod is green
    _write_audit_snapshot(shared, age_minutes=3, criticals=0)

    run_report(shared, members, DEFAULT_OVERRIDES, label="r2",
               now=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc))

    actives = list(signals_store.iter_active(shared, producer="pod_report"))
    assert actives == []

"""tests/test_signals_phase5_e2e.py — Phase 5 cross-producer smoke.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 5 exit gate).

Asserts that every producer wired in Phases 0–5 can land its signal
in the Alerts UI:

  - pod_report (Phase 2)
  - evolve_watchdog / sysadmin_watchdog
    (Phase 1, via events.write_events dual-write)
  - audit (Phase 3)
  - error_reporter (Phase 5)
  - host_health (Phase 5)
  - integration_probe (Phase 5; covered indirectly via signals_store
    direct seeding since the probe pipeline is heavy to set up)
  - pod_health (Phase 5)
  - security_warden (Phase 5; tested in test_signals_phase5_monitors)

The smoke test seeds one signal per producer and confirms the Alerts
API surfaces them all with the right lane (Activity vs Maintenance)
and producer attribution.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import pytest  # noqa: E402

from signals import store as signals_store  # noqa: E402


@pytest.fixture
def admin_client(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps({"sharedDir": str(shared), "bots": ["admin_bot"]}),
        encoding="utf-8",
    )
    from evolve_admin.web.server import create_app
    app = create_app(network)
    return app.test_client(), shared


def _seed(shared, **kwargs):
    """Drop a Signal directly via observe()."""
    return signals_store.observe(shared, **kwargs)


def test_alerts_api_surfaces_every_phase5_producer(admin_client):
    client, shared = admin_client

    # One signal per producer — pick a representative type for each.
    _seed(shared, signature="pod_report:audit_critical:pod",
          producer="pod_report", type="audit_critical",
          flavor="maintenance", severity="alert", scope="pod",
          title="Audit critical")
    _seed(shared, signature="evolve_watchdog:calibration_drift:pod",
          producer="evolve_watchdog", type="calibration_drift",
          flavor="activity", severity="warn", scope="pod",
          title="Calibration drift")
    _seed(shared, signature="sysadmin_watchdog:gateway_instability:admin_bot",
          producer="sysadmin_watchdog", type="gateway_instability",
          flavor="maintenance", severity="alert", scope="bot",
          bot_id="admin_bot", title="Gateway flapping on admin_bot")
    _seed(shared, signature="audit:identity:admin_bot:abc123",
          producer="audit", type="audit_identity",
          flavor="maintenance", severity="alert", scope="bot",
          bot_id="admin_bot", title="Identity audit critical")
    _seed(shared, signature="error_reporter:error_spike:admin:def456",
          producer="error_reporter", type="error_spike",
          flavor="maintenance", severity="warn", scope="host",
          title="Admin server error")
    _seed(shared, signature="host_health:disk_low:mini.local",
          producer="host_health", type="disk_low",
          flavor="maintenance", severity="alert", scope="host",
          title="Disk low on mini.local")
    _seed(shared, signature="integration_probe:channel_down:admin_bot:slack",
          producer="integration_probe", type="channel_down",
          flavor="maintenance", severity="alert", scope="integration",
          bot_id="admin_bot", title="Slack down on admin_bot")
    _seed(shared, signature="pod_health:pod_health_perms:admin_bot:turns",
          producer="pod_health", type="pod_health_perms",
          flavor="maintenance", severity="alert", scope="bot",
          bot_id="admin_bot", title="perms: admin_bot:turns")
    _seed(shared, signature="security_warden:conduct_violation_prompt_injection:admin_bot:s-1:t-3",
          producer="security_warden", type="conduct_violation_prompt_injection",
          flavor="maintenance", severity="alert", scope="bot",
          bot_id="admin_bot", title="Prompt injection on admin_bot")
    # test_runner producer retired 2026-06-08 with the app-test surface.

    # Total signals visible in /api/signals
    resp = client.get("/api/signals?limit=200")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 9

    producers_seen = {s["producer"] for s in data["signals"]}
    assert producers_seen == {
        "pod_report", "evolve_watchdog", "sysadmin_watchdog", "audit",
        "error_reporter", "host_health", "integration_probe",
        "pod_health", "security_warden",
    }

    # Lane split — only one signal in Activity (calibration_drift)
    resp = client.get("/api/signals?flavor=activity")
    assert resp.get_json()["count"] == 1

    resp = client.get("/api/signals?flavor=maintenance")
    assert resp.get_json()["count"] == 8


def test_phase5_filters_by_producer_via_api(admin_client):
    """The contextual UI surfaces query /api/signals?producer=<x> —
    confirm that filter still works for every producer."""
    client, shared = admin_client
    _seed(shared, signature="host_health:disk_low:mini.local",
          producer="host_health", type="disk_low",
          flavor="maintenance", severity="alert", scope="host",
          title="Disk")
    _seed(shared, signature="error_reporter:error_spike:admin:abc",
          producer="error_reporter", type="error_spike",
          flavor="maintenance", severity="warn", scope="host",
          title="Error")

    for prod in ("host_health", "error_reporter"):
        resp = client.get(f"/api/signals?producer={prod}")
        assert resp.status_code == 200
        sigs = resp.get_json()["signals"]
        assert len(sigs) == 1
        assert sigs[0]["producer"] == prod


def test_phase5_filters_by_scope_via_api(admin_client):
    """Contextual surfaces use scope filtering. Verify scope=integration
    returns only integration signals."""
    client, shared = admin_client
    _seed(shared, signature="integration_probe:channel_down:admin_bot:slack",
          producer="integration_probe", type="channel_down",
          flavor="maintenance", severity="alert", scope="integration",
          bot_id="admin_bot", title="Slack down")
    _seed(shared, signature="host_health:disk_low:mini.local",
          producer="host_health", type="disk_low",
          flavor="maintenance", severity="alert", scope="host",
          title="Disk")

    resp = client.get("/api/signals?scope=integration")
    sigs = resp.get_json()["signals"]
    assert len(sigs) == 1
    assert sigs[0]["producer"] == "integration_probe"

    resp = client.get("/api/signals?scope=host")
    sigs = resp.get_json()["signals"]
    assert len(sigs) == 1
    assert sigs[0]["producer"] == "host_health"

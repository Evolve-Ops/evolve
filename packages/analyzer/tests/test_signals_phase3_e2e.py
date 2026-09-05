"""tests/test_signals_phase3_e2e.py — Phase 3 exit-gate smoke test.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 3 exit gate).

The exit gate from §10:

  > audit findings appear in Maintenance lane and as inline badges on
  > the security pages; Telegram still fires for CRITICAL.

We assert:
  - audit findings show up in /api/signals?flavor=maintenance (the
    Alerts page Maintenance lane)
  - the Security page in admin/web/index.html includes the contextual
    chip strip wired to /api/signals?producer=audit
  - the Telegram dispatch path is preserved (one combined alert per
    run, with deduplication) AND each emitted Signal carries a
    Delivery audit entry — the spec's "single delivery path, not two"
    requirement
  - snooze on an audit Signal persists across the next audit run as
    long as the underlying finding is still firing
  - resolving an audit finding clears the Signal automatically
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

import pytest  # noqa: E402

from audit import Finding, dispatch_findings  # noqa: E402
from signals import store as signals_store  # noqa: E402


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    """Audit's _send_security_alert hits Telegram on real runs — stub it."""
    monkeypatch.setattr("audit._send_security_alert", lambda *a, **kw: True)
    monkeypatch.setattr("audit._send_telegram_direct", lambda *a, **kw: True)


@pytest.fixture
def admin_client(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps({"sharedDir": str(shared), "bots": ["admin_bot", "team_bot_a"]}),
        encoding="utf-8",
    )
    from evolve_admin.web.server import create_app
    app = create_app(network)
    return app.test_client(), shared


# ─────────────────────────────────────────────────────────────────────────────
# UI invariant — the contextual chip strip is wired up
# ─────────────────────────────────────────────────────────────────────────────


def test_security_page_includes_audit_signals_chip_strip():
    index_html = (
        _ANALYZER_DIR.parent / "admin" / "evolve_admin" / "web" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="security-audit-signals-strip"' in index_html
    # Loader function exists
    assert 'function _loadSecurityAuditSignals(' in index_html
    # Loader is invoked when the page activates
    assert "_loadSecurityAuditSignals();" in index_html


# ─────────────────────────────────────────────────────────────────────────────
# Findings → Maintenance lane via the API
# ─────────────────────────────────────────────────────────────────────────────


def test_findings_appear_in_maintenance_lane(admin_client):
    client, shared = admin_client

    findings = [
        Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="ssh key 0644 — must be 0600", detail=""),
        Finding(level="warn", category="config", bot_id="team_bot_a",
                message="openclaw.json missing model field", detail=""),
        Finding(level="critical", finding_kind="event", category="machine", bot_id=None,
                message="evolve user has no admin sudo grant", detail=""),
    ]
    dispatch_findings(findings, shared, config={}, dry_run=False)

    resp = client.get("/api/signals?flavor=maintenance&producer=audit")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 3

    # Activity lane is empty (audit is a maintenance-only producer)
    resp = client.get("/api/signals?flavor=activity&producer=audit")
    assert resp.get_json()["count"] == 0


def test_signals_are_scoped_correctly(admin_client):
    """Bot-level findings are bot-scoped; machine-level are pod-scoped."""
    client, shared = admin_client
    findings = [
        Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="bot-level issue", detail=""),
        Finding(level="critical", finding_kind="event", category="machine", bot_id=None,
                message="pod-level issue", detail=""),
    ]
    dispatch_findings(findings, shared, config={}, dry_run=False)

    resp = client.get("/api/signals?scope=bot&bot_id=admin_bot&producer=audit")
    assert resp.get_json()["count"] == 1

    resp = client.get("/api/signals?scope=pod&producer=audit")
    assert resp.get_json()["count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Telegram preserved + audited as Signal Delivery
# ─────────────────────────────────────────────────────────────────────────────


def test_critical_findings_dispatch_and_record_delivery(admin_client):
    """Telegram alert fires once per run; each critical Signal carries
    a corresponding Delivery audit entry."""
    client, shared = admin_client
    findings = [
        Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="ssh key wrong perms", detail=""),
        Finding(level="critical", finding_kind="event", category="config", bot_id="team_bot_a",
                message="auth-profiles drift", detail=""),
    ]
    dispatch_findings(findings, shared, config={}, dry_run=False)

    resp = client.get("/api/signals?producer=audit")
    sigs = resp.get_json()["signals"]
    assert len(sigs) == 2
    # Both critical → both have a Delivery to Telegram
    for s in sigs:
        assert len(s["deliveries"]) == 1
        assert s["deliveries"][0]["channel"] == "telegram"
        assert s["deliveries"][0]["suppressed_reason"] is None


def test_repeat_run_is_silent_standing_finding(admin_client):
    """Second run with the same finding: page-on-transition keeps the
    channel silent — one Delivery from the original page, nothing new.
    The firing Signal on the Alerts page is the standing record."""
    client, shared = admin_client
    f = Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="ssh key wrong perms", detail="")
    dispatch_findings([f], shared, config={}, dry_run=False)
    dispatch_findings([f], shared, config={}, dry_run=False)

    resp = client.get("/api/signals?producer=audit")
    sigs = resp.get_json()["signals"]
    assert len(sigs) == 1
    deliveries = sigs[0]["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["suppressed_reason"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Snooze persists across audit runs (the operator UX promise)
# ─────────────────────────────────────────────────────────────────────────────


def test_snooze_persists_across_audit_run(admin_client):
    """Operator snoozes a recurring CRITICAL ('we know about that, fixing
    it'). Next audit run still sees the finding firing — but the Signal
    state stays snoozed because observe() doesn't reopen state."""
    client, shared = admin_client
    f = Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="ssh key wrong perms", detail="")

    dispatch_findings([f], shared, config={}, dry_run=False)
    sigs = client.get("/api/signals?producer=audit").get_json()["signals"]
    sig_id = sigs[0]["id"]

    resp = client.post(f"/api/signals/{sig_id}/snooze", json={"duration": "24h"})
    assert resp.status_code == 200

    # Second audit run with the same finding still firing
    dispatch_findings([f], shared, config={}, dry_run=False)

    detail = client.get(f"/api/signals/{sig_id}").get_json()["signal"]
    assert detail["state"] == "snoozed"
    assert detail["observation_count"] >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Resolved finding auto-clears the chip
# ─────────────────────────────────────────────────────────────────────────────


def test_fixed_finding_clears_from_security_chip(admin_client):
    """The Security page chip strip reads firing audit signals only.
    Once a finding is fixed, the next audit run sweep-resolves it and
    the chip strip would render empty."""
    client, shared = admin_client
    f = Finding(level="critical", finding_kind="event", category="identity", bot_id="admin_bot",
                message="ssh key wrong perms", detail="")

    dispatch_findings([f], shared, config={}, dry_run=False)
    assert client.get("/api/signals?producer=audit").get_json()["count"] == 1

    # Operator fixes the issue; next run reports no findings
    dispatch_findings([], shared, config={}, dry_run=False)
    assert client.get("/api/signals?producer=audit").get_json()["count"] == 0

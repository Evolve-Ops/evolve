"""Tests for the evo-fail trail-link URL builder (audit-extensions Item 3).

Closes the deferred per-user admin lookup from PR #1217. Verifies that:
  - Pod admins get the trail link rendered in their diagnosis reply.
  - Non-admin users do NOT see the link, regardless of URL availability.
  - The URL builder follows the handover.pod_host resolution order
    (pod.public_host → tunnel.remote_host → localhost:5050).
  - The /investigations/<id> landing route is wired and 200s.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))


# ── Admin detection ────────────────────────────────────────────────────────


def test_pod_user_admin_is_detected() -> None:
    from evolve_admin.applications import audit_poller
    network = {
        "pod": {
            "admins": {"pod_users": ["pod_admin"], "external_ids": {}},
        },
    }
    assert audit_poller._requesting_user_is_pod_admin("pod:pod_admin", network) is True
    assert audit_poller._requesting_user_is_pod_admin("pod:somebody", network) is False


def test_external_id_admin_is_detected() -> None:
    from evolve_admin.applications import audit_poller
    network = {
        "pod": {
            "admins": {
                "pod_users": [],
                "external_ids": {"slack": ["U123", "U456"]},
            },
        },
    }
    assert audit_poller._requesting_user_is_pod_admin(
        "ext:slack:U123", network,
    ) is True
    assert audit_poller._requesting_user_is_pod_admin(
        "ext:slack:U999", network,
    ) is False
    # Different channel — not admin even if the id matches.
    assert audit_poller._requesting_user_is_pod_admin(
        "ext:telegram:U123", network,
    ) is False


def test_anon_user_never_admin() -> None:
    from evolve_admin.applications import audit_poller
    network = {"pod": {"admins": {"pod_users": ["anyone"]}}}
    assert audit_poller._requesting_user_is_pod_admin("anon:team_bot_a", network) is False
    assert audit_poller._requesting_user_is_pod_admin("", network) is False


# ── pod_host resolution ────────────────────────────────────────────────────


def test_pod_host_prefers_explicit_public_host(monkeypatch) -> None:
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "http://pod:5050",
        "pod": {"public_host": "evolve.example.com"},
        "tunnel": {"remote_host": "should-not-be-used"},
    }
    assert audit_poller._pod_host_for_dashboard(network) == (
        "http", "evolve.example.com",
    )


def test_pod_host_falls_back_to_tunnel(monkeypatch) -> None:
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "http://pod:5050",
        "tunnel": {"remote_host": "tunnel.example.com"},
    }
    assert audit_poller._pod_host_for_dashboard(network) == (
        "http", "tunnel.example.com",
    )


def test_pod_host_falls_through_to_resolve_admin_base_url(monkeypatch) -> None:
    """Empty network → scheme + host parsed from resolve_admin_base_url's
    derived default (gethostname-based). The unified helper is the single
    source for any URL operators tap, and the scheme rides along with it."""
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "mini.local")
    assert audit_poller._pod_host_for_dashboard({}) == ("http", "mini:5050")


# ── scheme preservation (PR #1259 / spec-pwa-phase0-https §3.3) ────────────


def test_trail_url_preserves_https_from_admin_base_url(monkeypatch) -> None:
    """Explicit ``https://pod.local:5050`` in adminBaseUrl must NOT downgrade
    to http — the old host-shape heuristic silently dropped the scheme."""
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "https://pod.local:5050"}
    url = audit_poller._build_investigation_trail_url(network, "inv-abc")
    assert url == "https://pod.local:5050/investigations/inv-abc"


def test_trail_url_preserves_http_from_admin_base_url(monkeypatch) -> None:
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "http://pod:5050"}
    url = audit_poller._build_investigation_trail_url(network, "inv-def")
    assert url == "http://pod:5050/investigations/inv-def"


def test_trail_url_preserves_https_for_tailnet_no_port(monkeypatch) -> None:
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {"adminBaseUrl": "https://pod.tail-net.ts.net"}
    url = audit_poller._build_investigation_trail_url(network, "inv-ghi")
    assert url == "https://pod.tail-net.ts.net/investigations/inv-ghi"


def test_trail_url_bare_override_inherits_https_scheme(monkeypatch) -> None:
    """A bare-hostname ``pod.public_host`` should inherit the HTTPS scheme
    from ``adminBaseUrl`` rather than re-guessing from host shape."""
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "https://pod.local:5050",
        "pod": {"public_host": "override.example.com"},
    }
    url = audit_poller._build_investigation_trail_url(network, "inv-jkl")
    assert url == "https://override.example.com/investigations/inv-jkl"


def test_trail_url_bare_tunnel_inherits_https_scheme(monkeypatch) -> None:
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "https://pod.local:5050",
        "tunnel": {"remote_host": "tunnel.example.com"},
    }
    url = audit_poller._build_investigation_trail_url(network, "inv-mno")
    assert url == "https://tunnel.example.com/investigations/inv-mno"


def test_trail_url_full_url_override_keeps_own_scheme(monkeypatch) -> None:
    """An override that includes its own scheme (e.g. ``https://`` prefix)
    must keep that scheme regardless of what ``adminBaseUrl`` says."""
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    network = {
        "adminBaseUrl": "http://pod:5050",
        "pod": {"public_host": "https://override.example.com"},
    }
    url = audit_poller._build_investigation_trail_url(network, "inv-pqr")
    assert url == "https://override.example.com/investigations/inv-pqr"


def test_trail_url_localhost_fallback_when_nothing_configured(monkeypatch) -> None:
    """No adminBaseUrl, no overrides, no gethostname → localhost:5050."""
    from evolve_admin.applications import audit_poller
    monkeypatch.delenv("EVOLVE_ADMIN_BASE_URL", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "")
    url = audit_poller._build_investigation_trail_url({}, "inv-stu")
    assert url == "http://localhost:5050/investigations/inv-stu"


# ── Round-trip: diagnosis notification rendering ────────────────────────────


def test_diagnosis_notification_includes_trail_link_for_admin(tmp_path, monkeypatch) -> None:
    """Pod-admin user submits evo fail → notification body has trail URL."""
    from evolve_admin.applications import audit_poller

    captured: dict = {}

    def _fake_append_event(shared_dir, requesting_user, **kw):
        captured["detail"] = kw.get("detail")
        captured["requesting_user"] = requesting_user
        return None

    network = {
        "pod": {
            "public_host": "evolve.example.com",
            "admins": {
                "pod_users": ["pod_admin"],
                "external_ids": {},
            },
        },
    }

    monkeypatch.setattr(audit_poller, "load_network", lambda *_a, **_kw: network, raising=False)

    fake_notif = type("M", (), {"append_event": staticmethod(_fake_append_event)})

    # Inject our stub for the lazy import inside _ingest_investigation_diagnosis.
    monkeypatch.setitem(sys.modules, "evolve_admin.evo.notifications", fake_notif)
    # Also stub the lazy "from ..config import load_network" by overriding
    # the audit_poller-module-level reference (the actual import path).
    import evolve_admin.config as _cfg
    monkeypatch.setattr(_cfg, "load_network", lambda *_a, **_kw: network)

    record = {
        "requesting_user": "pod:pod_admin",
        "bot_id": "team_bot_a",
        "investigation_id": "inv-xyz",
        "user_description": "morning briefing missing",
        "diagnosis": "Gmail token expired three days ago.",
        "suggested_fix": "Re-authorize gmail in Settings -> Plugins.",
        "confidence": "high",
        "status": "diagnosed",
    }

    ok = audit_poller._ingest_investigation_diagnosis(record, tmp_path)
    assert ok is True
    detail = captured.get("detail") or ""
    assert "evolve.example.com/investigations/inv-xyz" in detail, detail
    assert "Full trail:" in detail


def test_diagnosis_notification_omits_trail_link_for_non_admin(tmp_path, monkeypatch) -> None:
    from evolve_admin.applications import audit_poller

    captured: dict = {}

    def _fake_append_event(shared_dir, requesting_user, **kw):
        captured["detail"] = kw.get("detail")
        return None

    network = {
        "pod": {
            "public_host": "evolve.example.com",
            "admins": {"pod_users": ["pod_admin"], "external_ids": {}},
        },
    }

    fake_notif = type("M", (), {"append_event": staticmethod(_fake_append_event)})
    monkeypatch.setitem(sys.modules, "evolve_admin.evo.notifications", fake_notif)
    import evolve_admin.config as _cfg
    monkeypatch.setattr(_cfg, "load_network", lambda *_a, **_kw: network)

    record = {
        "requesting_user": "pod:someone_else",  # NOT in admins.pod_users
        "bot_id": "team_bot_a",
        "investigation_id": "inv-xyz",
        "user_description": "morning briefing missing",
        "diagnosis": "Gmail token expired three days ago.",
        "suggested_fix": "Re-authorize gmail.",
        "confidence": "high",
        "status": "diagnosed",
    }
    ok = audit_poller._ingest_investigation_diagnosis(record, tmp_path)
    assert ok is True
    detail = captured.get("detail") or ""
    assert "evolve.example.com" not in detail
    assert "Full trail:" not in detail


# ── /investigations/<id> landing route ──────────────────────────────────────


def test_investigation_landing_route_200s(tmp_path) -> None:
    from evolve_admin.web.server import create_app

    network = {"bots": {}}
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/investigations/inv-test-1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The injected marker the dashboard JS reads on load.
    assert 'window._pendingInvestigationId = "inv-test-1"' in body

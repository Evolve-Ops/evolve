"""Route test for POST /api/admin/applications/coherence-check.

The endpoint is mounted in ``admin_bot_routes`` so it inherits the unix-
socket peer-auth gate. This file uses Flask's test_client with the
peer-auth gate patched to allow the call, then asserts the endpoint
routes through to ``coherence_c3_dispatcher.dispatch_c3`` and returns
its structured result.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Flask test_client with peer-auth patched to allow the test."""
    from evolve_admin.web.admin_bot_routes import register_admin_bot_routes

    # Bypass peer-auth: the trust check normally requires a unix-socket
    # transport and a peer uid set by the unix-socket WSGI server.
    # Patch the resolver to accept the runner's uid and inject the
    # expected environ keys before the decorator inspects them.
    import os
    runner_uid = os.getuid()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids", lambda _names: {runner_uid},
    )

    app = Flask(__name__)

    @app.before_request
    def _inject_peer():
        from flask import request
        request.environ["REMOTE_TRANSPORT"] = "unix-socket"
        request.environ["REMOTE_PEER_UID"] = str(runner_uid)

    # Use a fake network.json path — the endpoint shouldn't read it
    # because the dispatcher is mocked.
    network_path = tmp_path / "network.json"
    network_path.write_text('{"sharedDir": "%s"}' % (tmp_path / "shared"))
    register_admin_bot_routes(app, network_path)
    return app.test_client()


def test_endpoint_validates_required_fields(client):
    """Missing bot_id / app_id / trigger → 400."""
    resp = client.post("/api/admin/applications/coherence-check", json={})
    assert resp.status_code == 400
    assert "bot_id" in resp.get_json()["error"]

    resp = client.post("/api/admin/applications/coherence-check",
                       json={"bot_id": "x"})
    assert resp.status_code == 400
    assert "app_id" in resp.get_json()["error"]

    resp = client.post("/api/admin/applications/coherence-check",
                       json={"bot_id": "x", "app_id": "j"})
    assert resp.status_code == 400
    assert "trigger" in resp.get_json()["error"]


def test_endpoint_calls_dispatcher_and_returns_result(client, monkeypatch):
    """Body forwarded correctly → dispatcher result returned verbatim."""
    from evolve_admin.applications.coherence_c3_dispatcher import (
        DispatchResult,
    )
    from evolve_admin.applications.coherence_pass_c3 import CapabilityCheck

    seen = []

    def _stub(**kwargs):
        seen.append(kwargs)
        return DispatchResult(
            ok=True, skipped=False,
            check=CapabilityCheck(
                severity="feasible", rationale="ok",
                checked_at="2026-06-07T00:00:00Z",
                triggered_by="on_demand",
            ),
            model="anthropic/claude-haiku-4-5",
            cost_estimate_usd=0.005,
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub,
    )

    resp = client.post(
        "/api/admin/applications/coherence-check",
        json={"bot_id": "bot-x", "app_id": "j", "trigger": "on_demand"},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["check"]["severity"] == "feasible"
    assert body["model"] == "anthropic/claude-haiku-4-5"
    assert body["cost_estimate_usd"] == 0.005
    assert len(seen) == 1
    assert seen[0]["bot_id"] == "bot-x"
    assert seen[0]["app_id"] == "j"
    assert seen[0]["trigger"] == "on_demand"


def test_endpoint_passes_through_skipped_result(client, monkeypatch):
    """Rate-limited / structural skip → ok=false, skipped=true."""
    from evolve_admin.applications.coherence_c3_dispatcher import (
        DispatchResult,
    )

    def _stub(**kwargs):
        return DispatchResult(
            ok=False, skipped=True,
            reason="rate-limited (already ran within 24h)",
        )

    monkeypatch.setattr(
        "evolve_admin.applications.coherence_c3_dispatcher.dispatch_c3",
        _stub,
    )

    resp = client.post(
        "/api/admin/applications/coherence-check",
        json={"bot_id": "bot-x", "app_id": "j", "trigger": "on_demand"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert body["skipped"] is True
    assert "rate-limited" in body["reason"]

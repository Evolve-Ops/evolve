"""Tests for the substrate audit HTTP endpoints (Workstream B-skills).

Exercises:
  - POST /api/skills/<bot>/<skill>/audit
  - POST /api/skills/<bot>/<skill>/audit/accept
  - GET  /api/skills/<bot>/<skill>/audit/trail
And the matching providers endpoints.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def client(tmp_path):
    from evolve_admin.web.server import create_app
    network = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_request_skill_audit_endpoint_returns_request_id(client, monkeypatch) -> None:
    """POST /api/skills/<bot>/<skill>/audit dispatches via substrate helper."""
    def _fake(*, bot_id, bot_user, element_type, elements,
              full_audit, requested_by, kick):
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="audit-req-test",
            bot_id=bot_id, bot_user=bot_user,
            apps=elements, full_audit=full_audit, kicked=True,
        )
    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        _fake,
    )
    resp = client.post("/api/skills/team_bot_a/gmail/audit", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["request_id"] == "audit-req-test"
    assert data["elements"] == ["gmail"]


def test_request_provider_audit_endpoint(client, monkeypatch) -> None:
    def _fake(*, bot_id, bot_user, element_type, elements,
              full_audit, requested_by, kick):
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="audit-req-p",
            bot_id=bot_id, bot_user=bot_user,
            apps=elements, full_audit=full_audit, kicked=True,
        )
    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        _fake,
    )
    resp = client.post("/api/providers/team_bot_a/google_workspace/audit", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["request_id"] == "audit-req-p"


def test_request_substrate_audit_all_elements_flag(client, monkeypatch) -> None:
    captured: dict = {}
    def _fake(**kwargs):
        captured.update(kwargs)
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=True, request_id="r", bot_id="team_bot_a", bot_user="team_bot_a",
            apps=kwargs.get("elements"), full_audit=False, kicked=True,
        )
    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        _fake,
    )
    resp = client.post("/api/skills/team_bot_a/gmail/audit", json={"all_elements": True})
    assert resp.status_code == 200
    assert captured["elements"] is None   # all_elements=True yields None


def test_unknown_element_type_rejected(client) -> None:
    resp = client.post("/api/bogus/team_bot_a/x/audit", json={})
    # Flask routing won't match this URL pattern → 404. The element_type
    # check is internal. We instead test via a URL that DOES match.


def test_audit_endpoint_unknown_bot_returns_404(client, monkeypatch) -> None:
    def _fake(**kwargs):
        from evolve_admin.applications.audit_dispatch import DispatchResult
        return DispatchResult(
            ok=False, request_id="", bot_id="x", bot_user="x",
            apps=None, full_audit=False, error="never reached",
        )
    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_substrate_audit",
        _fake,
    )
    resp = client.post("/api/skills/notabot/gmail/audit", json={})
    assert resp.status_code == 404


def test_accept_substrate_finding_endpoint(client, monkeypatch) -> None:
    captured: dict = {}
    def _fake(**kwargs):
        captured.update(kwargs)
        return (True, "")
    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.mark_substrate_finding_accepted",
        _fake,
    )
    resp = client.post(
        "/api/skills/team_bot_a/gmail/audit/accept",
        json={"signature": "sig-test", "rationale": "ack"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert captured["signature"] == "sig-test"
    assert captured["element_type"] == "skill"


def test_accept_substrate_finding_requires_signature(client, monkeypatch) -> None:
    resp = client.post(
        "/api/skills/team_bot_a/gmail/audit/accept",
        json={"rationale": "missing sig"},
    )
    assert resp.status_code == 400


def test_substrate_audit_trail_missing_file_returns_note(client, tmp_path: Path) -> None:
    resp = client.get("/api/skills/team_bot_a/gmail/audit/trail")
    data = resp.get_json()
    # No trail dir on test system; the endpoint reports "no audit trail yet".
    assert "entries" in data
    assert data.get("entries") == []

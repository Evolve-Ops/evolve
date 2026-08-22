"""Tests for the per-bot messaging-channel admin routes (M1-B4b).

`GET /api/admin/bots/<bot>/channels` and `POST .../channels/add` — the HTTP
seam over ``evolve_admin.channel_provisioning``. The service layer's own
behaviour is covered in ``test_channel_provisioning``; these tests pin the
route contract a future Users-page control would code against, and the one
safety property that must never regress: **the route does not restart a
gateway unless the request asks for it.**

Placeholder bot names + fake tokens only (docs/PLACEHOLDER_NAMING.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import channel_provisioning as cp  # noqa: E402
from evolve_admin import channel_registry as cr  # noqa: E402
from evolve_admin.web import routes_bot_channels as rbc  # noqa: E402

BOT = "lex"
FAKE_TOKEN = "placeholder-not-a-real-token"


def _core_id() -> str:
    return cr.ids_where(
        lambda c: c.install == cr.INSTALL_CORE and c.messaging_integration
    )[0]


def _plugin_id() -> str:
    return cr.ids_where(
        lambda c: c.install == cr.INSTALL_OFFICIAL_PLUGIN and c.messaging_integration
    )[0]


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {BOT: {"role": "member", "user": "placeholder-user"}},
    }))
    return p


@pytest.fixture
def app(network_path, monkeypatch):
    # Never read a real bot's openclaw.json from a test.
    monkeypatch.setattr(
        "evolve_admin.skills._oc_install_common.read_oc_config",
        lambda bot_id: ({"channels": {_core_id(): {"enabled": True}}}, None),
    )
    a = Flask(__name__)
    rbc.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ── GET /channels ───────────────────────────────────────────────────────


def test_list_unknown_bot_404s(client):
    assert client.get("/api/admin/bots/nope/channels").status_code == 404


def test_list_reports_enabled_and_available(client):
    r = client.get(f"/api/admin/bots/{BOT}/channels")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] == [_core_id()]
    ids = {c["id"] for c in body["available"]}
    assert ids == {c.id for c in cp.provisionable_channels()}


def test_list_marks_plugin_requirement_from_the_registry(client):
    body = client.get(f"/api/admin/bots/{BOT}/channels").get_json()
    by_id = {c["id"]: c for c in body["available"]}
    assert by_id[_core_id()]["plugin_required"] is False
    assert by_id[_plugin_id()]["plugin_required"] is True


def test_list_marks_already_enabled(client):
    body = client.get(f"/api/admin/bots/{BOT}/channels").get_json()
    by_id = {c["id"]: c for c in body["available"]}
    assert by_id[_core_id()]["enabled"] is True
    assert by_id[_plugin_id()]["enabled"] is False


# ── POST /channels/add ──────────────────────────────────────────────────


@pytest.fixture
def spy(monkeypatch):
    """Capture the kwargs the route hands the service layer."""
    calls: list[tuple] = []

    def _fake(bot_id, channel_id, **kw):
        calls.append((bot_id, channel_id, kw))
        return cp.AddChannelOutcome(
            ok=True, bot_id=bot_id, channel_id=channel_id,
            config_changed=True, restart_required=True,
        )

    monkeypatch.setattr(rbc.cp, "add_channel_to_bot", _fake)
    return calls


def test_add_unknown_bot_404s(client, spy):
    r = client.post("/api/admin/bots/nope/channels/add",
                    json={"channel": _core_id()})
    assert r.status_code == 404
    assert spy == []


def test_add_requires_a_channel(client, spy):
    r = client.post(f"/api/admin/bots/{BOT}/channels/add", json={})
    assert r.status_code == 400
    assert spy == []


def test_add_rejects_non_object_channel_fields(client, spy):
    r = client.post(f"/api/admin/bots/{BOT}/channels/add",
                    json={"channel": _core_id(), "channel_fields": ["nope"]})
    assert r.status_code == 400
    assert spy == []


def test_add_does_not_restart_by_default(client, spy):
    r = client.post(f"/api/admin/bots/{BOT}/channels/add",
                    json={"channel": _plugin_id()})
    assert r.status_code == 200
    assert r.get_json()["restart_required"] is True
    _bot, _ch, kw = spy[0]
    assert kw["restart_gateway"] is False


def test_add_passes_explicit_restart_opt_in(client, spy):
    client.post(f"/api/admin/bots/{BOT}/channels/add",
                json={"channel": _plugin_id(), "restart_gateway": True})
    assert spy[0][2]["restart_gateway"] is True


def test_add_forwards_credential_and_fields(client, spy):
    client.post(f"/api/admin/bots/{BOT}/channels/add", json={
        "channel": _plugin_id(),
        "credential": FAKE_TOKEN,
        "credential_field": "bot_token",
        "channel_fields": {"mode": "socket"},
        "install_plugin": False,
    })
    _bot, ch, kw = spy[0]
    assert ch == _plugin_id()
    assert kw["credential"] == FAKE_TOKEN
    assert kw["channel_fields"] == {"mode": "socket"}
    assert kw["install_plugin"] is False


def test_add_failure_is_a_400_with_the_outcome_body(client, monkeypatch):
    monkeypatch.setattr(
        rbc.cp, "add_channel_to_bot",
        lambda bot_id, channel_id, **kw: cp.AddChannelOutcome(
            ok=False, bot_id=bot_id, channel_id=channel_id,
            error="unknown channel: 'nope'",
        ),
    )
    r = client.post(f"/api/admin/bots/{BOT}/channels/add",
                    json={"channel": "nope"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unknown channel: 'nope'"


def test_audit_payload_carries_no_credential(client, monkeypatch):
    logged: list[tuple] = []
    monkeypatch.setattr(
        "evolve_admin.web.server._audit_log_entry",
        lambda action, bot, payload: logged.append((action, bot, payload)),
    )
    monkeypatch.setattr(
        rbc.cp, "add_channel_to_bot",
        lambda bot_id, channel_id, **kw: cp.AddChannelOutcome(
            ok=True, bot_id=bot_id, channel_id=channel_id, config_changed=True,
        ),
    )
    client.post(f"/api/admin/bots/{BOT}/channels/add",
                json={"channel": _plugin_id(), "credential": FAKE_TOKEN})
    assert logged, "channel add was not audited"
    assert FAKE_TOKEN not in json.dumps(logged[0][2])

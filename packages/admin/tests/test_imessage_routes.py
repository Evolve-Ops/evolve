"""test_imessage_routes.py — the iMessage wizard's HTTP surface (D-IM3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

from evolve_admin import imessage_channel as ic
from evolve_admin.web import imessage_routes
from evolve_admin.web.connections_routes import register_connections_routes

BOT = "personal-bot"


@pytest.fixture
def client(tmp_path, monkeypatch):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path), "admin_user": "pod-admin-user",
        "bots": {BOT: {"primary_user": {"external_ids": {"imessage": ["+15555550101"]}}}},
    }))
    monkeypatch.setattr(imessage_routes, "supported_on_host", lambda _e: True)
    monkeypatch.setattr(imessage_routes, "get_bot_user", lambda b, _n: f"{b}-user")
    monkeypatch.setattr(imessage_routes, "_audit_log_entry", lambda *a, **k: None)
    app = Flask(__name__)
    register_connections_routes(app, net)
    return app.test_client()


def test_connect_rejects_a_typed_handle(client):
    r = client.post(f"/api/connections/{BOT}/imessage/connect",
                    json={"handle": "x@example.com", "allow_from": []})
    assert r.status_code == 400 and r.get_json()["error"] == "handle_is_read_back"


def test_connect_passes_the_optin_flags_and_defaults_to_keeping_telegram(client, monkeypatch):
    seen = {}

    def fake_connect(**kw):
        seen.update(kw)
        return ic.ConnectResult(True, "done")

    monkeypatch.setattr(ic, "connect", fake_connect)
    r = client.post(f"/api/connections/{BOT}/imessage/connect", json={"allow_from": ["+15555550101"]})
    assert r.status_code == 200
    assert seen["retire_telegram"] is False and seen["apple_id_mode"] == "own"
    assert seen["macos_user"] == f"{BOT}-user"
    client.post(f"/api/connections/{BOT}/imessage/connect",
                json={"allow_from": [], "retire_telegram": True, "apple_id_mode": "shared"})
    assert seen["retire_telegram"] is True and seen["macos_user"] == "pod-admin-user"


def test_a_refused_connect_is_422_with_the_reason(client, monkeypatch):
    monkeypatch.setattr(ic, "connect", lambda **kw: ic.ConnectResult(False, "open_policy", "no"))
    r = client.post(f"/api/connections/{BOT}/imessage/connect", json={"allow_from": []})
    assert r.status_code == 422 and r.get_json()["stage"] == "open_policy"


def test_unknown_bot_and_wrong_platform(client, monkeypatch):
    assert client.get("/api/connections/ghost/imessage").status_code == 404
    monkeypatch.setattr(imessage_routes, "supported_on_host", lambda _e: False)
    r = client.get(f"/api/connections/{BOT}/imessage")
    assert r.status_code == 409 and r.get_json()["error"] == "skill_unavailable_on_platform"


def test_state_carries_readback_prefill_and_telegram_offer(client, monkeypatch):
    fake = ic.Seams(
        signed_in_handle=lambda u, b: "personal-bot@example.com",
        read_config=lambda b: ({"channels": {"telegram": {"enabled": True, "botToken": "t"}}}, None),
    )
    monkeypatch.setattr(ic, "Seams", lambda: fake)
    body = client.get(f"/api/connections/{BOT}/imessage").get_json()
    assert body["signed_in_handle"] == "personal-bot@example.com"
    assert body["prefill_allow_from"] == ["+15555550101"]
    assert body["telegram_binding"] is True and body["open_policy"] == []
    assert body["signin"]["macos_user"] == f"{BOT}-user"
    assert body["row"] is None


def test_probe_without_a_row_is_404(client):
    assert client.post(f"/api/connections/{BOT}/imessage/probe").status_code == 404

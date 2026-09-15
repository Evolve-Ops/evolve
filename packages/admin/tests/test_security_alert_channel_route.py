"""Routes for the opt-in independent security-alert channel.

META:footprint F-bite. Pins the contract the Settings → Pod Config card
consumes, and the secret-handling invariants:

  GET  /api/config/security-alert         status (presence + chat id, NEVER token)
  POST /api/config/security-alert         {op: set|clear|test}

The set path writes the two keystore files audit.py's CRITICAL
direct-Telegram fallback reads (``security-alert-token`` /
``security-alert-chat-id``) at mode 0600; clear removes them.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "evolve"
    (shared / "signals").mkdir(parents=True)
    network = {
        "members": [],
        "bots": {},
        "sharedDir": str(shared),
        "alerts": {"channel": "telegram", "chatId": "12345"},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Stub the irrelevant signal/host_health probes from server.py so
    # create_app doesn't try to walk a real shared dir (mirrors
    # test_alerts_subscription_routes).
    from evolve_admin import host_health as _host_health
    from evolve_admin import reporter as _reporter
    from evolve_admin import health as _health

    monkeypatch.setattr(_host_health, "collect_host_health", lambda: {"ok": True})
    monkeypatch.setattr(_host_health, "emit_signals_from_snapshot", lambda s, p: 0)
    monkeypatch.setattr(_reporter, "emit_error_signals", lambda p: 0)

    class _R:
        checks = []

        def as_dict(self):
            return {"checks": []}

    monkeypatch.setattr(_health, "run_health_check", lambda **kw: _R())

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "shared": shared}


def _client(app_fixture):
    return app_fixture["app"].test_client()


def _paths(shared: Path):
    return (
        shared / "keystore" / "security-alert-token",
        shared / "keystore" / "security-alert-chat-id",
    )


# ── GET status ──────────────────────────────────────────────────────────────


def test_status_unconfigured_by_default(app):
    c = _client(app)
    body = c.get("/api/config/security-alert").get_json()
    assert body["securityAlertConfigured"] is False
    assert body["chatId"] is None


# ── set ───────────────────────────────────────────────────────────────────


def test_set_writes_both_keystore_files_at_0600(app):
    c = _client(app)
    tok_path, chat_path = _paths(app["shared"])

    resp = c.post(
        "/api/config/security-alert",
        json={"op": "set", "token": "999:SECRET-TOKEN", "chatId": "67890"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["securityAlertConfigured"] is True
    assert body["chatId"] == "67890"
    # The token is never echoed back to the client.
    assert "999:SECRET-TOKEN" not in json.dumps(body)
    assert "token" not in body

    # Files written with the exact content audit.py reads.
    assert tok_path.read_text() == "999:SECRET-TOKEN"
    assert chat_path.read_text() == "67890"

    # Token-bearing → must be mode 0600.
    for p in (tok_path, chat_path):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"{p.name} mode={oct(mode)} expected 0o600"

    # And the status read reflects it without leaking the token.
    status = c.get("/api/config/security-alert").get_json()
    assert status["securityAlertConfigured"] is True
    assert status["chatId"] == "67890"


def test_set_requires_both_token_and_chat_id(app):
    c = _client(app)
    tok_path, chat_path = _paths(app["shared"])

    r1 = c.post("/api/config/security-alert", json={"op": "set", "token": "x"})
    assert r1.status_code == 400
    r2 = c.post("/api/config/security-alert", json={"op": "set", "chatId": "1"})
    assert r2.status_code == 400
    # Nothing written on validation failure.
    assert not tok_path.exists()
    assert not chat_path.exists()


# ── clear ─────────────────────────────────────────────────────────────────


def test_clear_removes_both_files(app):
    c = _client(app)
    tok_path, chat_path = _paths(app["shared"])

    c.post(
        "/api/config/security-alert",
        json={"op": "set", "token": "t", "chatId": "c"},
    )
    assert tok_path.exists() and chat_path.exists()

    resp = c.post("/api/config/security-alert", json={"op": "clear"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["securityAlertConfigured"] is False

    assert not tok_path.exists()
    assert not chat_path.exists()


def test_clear_is_idempotent_when_unconfigured(app):
    c = _client(app)
    resp = c.post("/api/config/security-alert", json={"op": "clear"})
    assert resp.status_code == 200
    assert resp.get_json()["securityAlertConfigured"] is False


# ── test (getMe) ────────────────────────────────────────────────────────────


def test_test_op_validates_token_via_getme(app, monkeypatch):
    from evolve_admin.web import routes_alerts

    monkeypatch.setattr(routes_alerts, "_telegram_getme", lambda tok: "sec_bot")
    c = _client(app)
    resp = c.post(
        "/api/config/security-alert",
        json={"op": "test", "token": "123:abc"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["username"] == "sec_bot"


def test_test_op_falls_back_to_stored_token(app, monkeypatch):
    from evolve_admin.web import routes_alerts

    seen = {}

    def _fake(tok):
        seen["tok"] = tok
        return "stored_bot"

    monkeypatch.setattr(routes_alerts, "_telegram_getme", _fake)
    c = _client(app)
    c.post(
        "/api/config/security-alert",
        json={"op": "set", "token": "STORED:TOK", "chatId": "9"},
    )
    # No token in the body → server reads the saved one.
    resp = c.post("/api/config/security-alert", json={"op": "test"})
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "stored_bot"
    assert seen["tok"] == "STORED:TOK"


def test_test_op_reports_failure(app, monkeypatch):
    from evolve_admin.web import routes_alerts

    monkeypatch.setattr(routes_alerts, "_telegram_getme", lambda tok: None)
    c = _client(app)
    resp = c.post(
        "/api/config/security-alert",
        json={"op": "test", "token": "bad"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ── bad op ────────────────────────────────────────────────────────────────


def test_unknown_op_rejected(app):
    c = _client(app)
    resp = c.post("/api/config/security-alert", json={"op": "frobnicate"})
    assert resp.status_code == 400

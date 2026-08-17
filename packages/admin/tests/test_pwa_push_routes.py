"""PWA Phase 1.2 — HTTP route contracts for /api/push/*.

Pins the surface the admin UI's "Enable on this device" flow calls:

  GET  /api/push/vapid-public-key       → public_key (b64url, no pad)
  POST /api/push/subscribe              → adds row, returns subscription_id
  POST /api/push/unsubscribe            → removes by id; 404 unknown
  GET  /api/push/subscriptions          → list for Devices UI
  POST /api/push/channel                → toggle pwa_push channel
  POST /api/push/test                   → single device or full fanout
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ── Shared fake-pywebpush + app fixtures ───────────────────────────────────


@pytest.fixture
def fake_pywebpush(monkeypatch):
    """Mirror the fixture in test_pwa_push.py — single source of truth
    would be nice but pytest scope rules + the test-isolation fixture
    in conftest.py make a copy here cleaner than cross-file imports."""
    calls: list[dict] = []
    next_results: list[dict] = []

    class WebPushException(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    class _FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    def webpush(*, subscription_info, data, vapid_private_key,
                vapid_claims, timeout=None):
        calls.append({
            "subscription_info": subscription_info,
            "data": data,
        })
        if next_results:
            r = next_results.pop(0)
            if r.get("raise"):
                raise WebPushException(
                    "fake",
                    response=_FakeResponse(r.get("status_code", 500)),
                )

    mod = types.ModuleType("pywebpush")
    mod.webpush = webpush  # type: ignore[attr-defined]
    mod.WebPushException = WebPushException  # type: ignore[attr-defined]
    mod.calls = calls  # type: ignore[attr-defined]
    mod.next_results = next_results  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", mod)
    return mod


@pytest.fixture
def app(tmp_path, monkeypatch):
    from evolve_admin.web.server import create_app
    from evolve_admin.alerts import dispatcher

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

    monkeypatch.setattr(
        dispatcher, "_dispatch_via_openclaw",
        lambda channel, chat_id, message, gateway_port=None: (True, None),
    )
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: forced"),
    )

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


# ── /api/push/vapid-public-key ─────────────────────────────────────────────


def test_vapid_public_key_returns_b64url(app):
    c = _client(app)
    resp = c.get("/api/push/vapid-public-key")
    assert resp.status_code == 200
    body = resp.get_json()
    pub = body["public_key"]
    assert pub and "=" not in pub and "+" not in pub and "/" not in pub


def test_vapid_public_key_is_stable_across_requests(app):
    c = _client(app)
    a = c.get("/api/push/vapid-public-key").get_json()["public_key"]
    b = c.get("/api/push/vapid-public-key").get_json()["public_key"]
    assert a == b, "VAPID public key must be stable across reads"


# ── /api/push/subscribe + /api/push/unsubscribe + /api/push/subscriptions ──


def test_subscribe_stores_and_unsubscribe_removes(app):
    c = _client(app)
    body = c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/aaa",
        "keys":     {"p256dh": "P", "auth": "A"},
        "device_label": "Pod_admin iPhone",
    }).get_json()
    assert body["subscription_id"].startswith("sub_")

    listed = c.get("/api/push/subscriptions").get_json()
    assert len(listed["subscriptions"]) == 1
    assert listed["subscriptions"][0]["device_label"] == "Pod_admin iPhone"
    # Auto-enabling the channel on first subscribe spares the operator
    # an extra click — most won't think to flip the channel toggle after
    # registering their first device.
    assert listed["pwa_push_enabled"] is True

    sid = body["subscription_id"]
    resp = c.post("/api/push/unsubscribe", json={"subscription_id": sid})
    assert resp.status_code == 200
    assert c.get("/api/push/subscriptions").get_json()["subscriptions"] == []


def test_subscribe_validates_required_fields(app):
    c = _client(app)
    assert c.post("/api/push/subscribe", json={}).status_code == 400
    assert c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/x",
    }).status_code == 400
    assert c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/x",
        "keys": {"p256dh": "P"},
    }).status_code == 400


def test_unsubscribe_unknown_id_returns_404(app):
    c = _client(app)
    assert c.post("/api/push/unsubscribe", json={"subscription_id": "sub_nope"}).status_code == 404


# ── /api/push/channel ──────────────────────────────────────────────────────


def test_channel_toggle_persists(app):
    c = _client(app)
    out = c.post("/api/push/channel", json={"enabled": True}).get_json()
    assert out["pwa_push_enabled"] is True
    out = c.post("/api/push/channel", json={"enabled": False}).get_json()
    assert out["pwa_push_enabled"] is False


# ── /api/push/test ─────────────────────────────────────────────────────────


def test_test_single_device(app, fake_pywebpush):
    c = _client(app)
    sid = c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/x",
        "keys":     {"p256dh": "P", "auth": "A"},
        "device_label": "iPhone",
    }).get_json()["subscription_id"]

    resp = c.post("/api/push/test", json={"subscription_id": sid})
    assert resp.status_code == 200
    out = resp.get_json()
    assert out["result"] == "sent"
    assert len(fake_pywebpush.calls) == 1


def test_test_single_device_410_marks_invalid(app, fake_pywebpush):
    c = _client(app)
    sid = c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/x",
        "keys":     {"p256dh": "P", "auth": "A"},
        "device_label": "iPhone",
    }).get_json()["subscription_id"]

    fake_pywebpush.next_results.append({"raise": True, "status_code": 410})
    resp = c.post("/api/push/test", json={"subscription_id": sid})
    assert resp.status_code == 502
    listed = c.get("/api/push/subscriptions").get_json()
    assert listed["subscriptions"][0]["invalid"] is True


def test_test_fanout_all_devices(app, fake_pywebpush):
    c = _client(app)
    c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/a",
        "keys":     {"p256dh": "P", "auth": "A"},
        "device_label": "A",
    })
    c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/b",
        "keys":     {"p256dh": "P", "auth": "A"},
        "device_label": "B",
    })
    resp = c.post("/api/push/test", json={})
    assert resp.status_code == 200
    out = resp.get_json()
    assert out["attempted"] == 2
    assert out["sent"] == 2


# ── Subscription label derivation from User-Agent ──────────────────────────


def test_subscribe_falls_back_to_ua_label(app):
    """When device_label is omitted, the server picks a sensible label
    from the user-agent so the Devices UI never shows a blank row."""
    c = _client(app)
    body = c.post("/api/push/subscribe", json={
        "endpoint": "https://push.apple.com/x",
        "keys":     {"p256dh": "P", "auth": "A"},
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
            "Mobile/15E148 Safari/604.1"
        ),
    }).get_json()
    assert body["device_label"] == "iPhone Safari", (
        f"expected UA-derived 'iPhone Safari' label; got {body['device_label']!r}"
    )

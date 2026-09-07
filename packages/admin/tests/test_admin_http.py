"""admin_http.urlopen_admin — socket-first transport for evo tools (roadmap 2.6).

Verifies the drop-in replacement for urllib.request.urlopen: it routes the
admin-API request over the peer-authed unix socket when reachable, falls back
to TCP when not, and preserves urlopen's response / HTTPError contract so the
tools' unchanged parsing code keeps working.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.evo import admin_http  # noqa: E402


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.Request(url, data=data, method=method)


def test_socket_success_returns_readable_response(monkeypatch):
    seen = {}

    def fake_daemon(method, path, body=None, timeout=10):
        seen.update(method=method, path=path, body=body)
        return True, 200, {"ok": True, "value": 7}

    monkeypatch.setattr(admin_http, "try_daemon_call", fake_daemon, raising=False)
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)

    with admin_http.urlopen_admin(
        _req("POST", "http://127.0.0.1:5050/api/admin/keys/bot/anthropic",
             {"key_value": "x"})) as resp:
        payload = json.loads(resp.read().decode())

    assert payload == {"ok": True, "value": 7}
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/admin/keys/bot/anthropic"
    assert seen["body"] == {"key_value": "x"}


def test_socket_carries_query_string(monkeypatch):
    seen = {}

    def fake_daemon(method, path, body=None, timeout=10):
        seen["path"] = path
        return True, 200, {"ok": True}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    with admin_http.urlopen_admin(
        _req("POST", "http://127.0.0.1:5050/api/applications/scan?bot=team_bot_a")):
        pass
    assert seen["path"] == "/api/applications/scan?bot=team_bot_a"


def test_socket_non_2xx_raises_httperror(monkeypatch):
    def fake_daemon(method, path, body=None, timeout=10):
        return True, 403, {"error": "nope"}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    with pytest.raises(urllib.error.HTTPError) as ei:
        admin_http.urlopen_admin(_req("POST", "http://127.0.0.1:5050/api/x", {}))
    assert ei.value.code == 403
    assert b"nope" in ei.value.read()


def test_falls_back_to_tcp_when_socket_unreachable(monkeypatch):
    def fake_daemon(method, path, body=None, timeout=10):
        return False, None, None  # socket down

    calls = {}

    class _Resp:
        def read(self): return b'{"via":"tcp"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        calls["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    monkeypatch.setattr(admin_http.urllib.request, "urlopen", fake_urlopen)

    with admin_http.urlopen_admin(_req("GET", "http://127.0.0.1:5050/api/y")) as r:
        assert json.loads(r.read()) == {"via": "tcp"}
    assert calls["url"] == "http://127.0.0.1:5050/api/y"


def test_get_with_no_body(monkeypatch):
    def fake_daemon(method, path, body=None, timeout=10):
        assert body is None
        return True, 200, {"ok": True}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    with admin_http.urlopen_admin(_req("GET", "http://127.0.0.1:5050/api/z")) as r:
        assert json.loads(r.read()) == {"ok": True}


def test_delete_method_reaches_socket_as_delete(monkeypatch):
    """A DELETE request keeps its verb through urlopen_admin → try_daemon_call
    (roadmap 2.6 — the keys-remove regression)."""
    seen = {}

    def fake_daemon(method, path, body=None, timeout=10):
        seen["method"] = method
        return True, 200, {"ok": True}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    with admin_http.urlopen_admin(
        _req("DELETE", "http://127.0.0.1:5050/api/admin/keys/bot/anthropic",
             {"profile_id": "p"})) as r:
        assert json.loads(r.read()) == {"ok": True}
    assert seen["method"] == "DELETE"


def test_external_host_never_socket_routed(monkeypatch):
    """A non-loopback host (a provider API) must go straight to TCP — the
    socket transport would otherwise strip the host and POST the bare path
    at the local daemon (the #2621 wizard-extractor outage: every LLM-
    extraction wizard died because api.anthropic.com calls were rerouted
    to the admin socket)."""
    def fake_daemon(method, path, body=None, timeout=10):
        raise AssertionError("external host must never reach the socket")

    calls = {}

    class _Resp:
        def read(self): return b'{"via":"tcp"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        calls["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    monkeypatch.setattr(admin_http.urllib.request, "urlopen", fake_urlopen)

    with admin_http.urlopen_admin(
        _req("POST", "https://api.anthropic.com/v1/messages",
             {"model": "m", "messages": []})) as r:
        assert json.loads(r.read()) == {"via": "tcp"}
    assert calls["url"] == "https://api.anthropic.com/v1/messages"


def test_loopback_hostname_still_socket_routed(monkeypatch):
    """localhost spelling of the daemon host keeps the socket path."""
    def fake_daemon(method, path, body=None, timeout=10):
        return True, 200, {"via": "socket"}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", fake_daemon, raising=False)
    with admin_http.urlopen_admin(
        _req("GET", "http://localhost:5050/api/y")) as r:
        assert json.loads(r.read()) == {"via": "socket"}

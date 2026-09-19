"""Faithful end-to-end tests for evo_path_probe_monitor against the REAL gate.

These stand the analyzer probe up against a Flask app wired with the REAL
device-auth primitives (``admin_auth.is_auth_enabled`` /
``verify_device_token`` / the ``before_request`` gate mirrored line-for-line
from ``server.py:_enforce_device_auth``) and a dispatch route returning a real
``DispatchResult`` envelope — bound over a REAL loopback TCP port AND the REAL
admin-daemon unix socket (``unix_socket_server.start_in_background``).

This is the literal DoD reproduction of the #3257 outage in both directions:

  * auth enforced + ``/api/evo/dispatch`` NOT exempt (the pre-fix state) →
    the probe's cookieless TCP call gets 401 → RED.
  * the path exempt (or auth off) (the fix) → 200 + envelope → GREEN.

and it validates the socket-transport design decision: a 401 over the socket
(an untrusted peer uid) is still GREEN, because the socket probe asserts
transport reachability, not auth — while a missing socket (ENOENT) is RED.

The unit-level coverage of the probe's own logic lives in
packages/analyzer/tests/test_evo_path_probe_monitor.py.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evo_path_probe_monitor as probe  # noqa: E402
from evolve_admin.evo.dispatch import DispatchResult  # noqa: E402
from evolve_admin.web import admin_auth, peer_auth  # noqa: E402
from evolve_admin.web.unix_socket_server import start_in_background  # noqa: E402


def _build_app(*, shared_dir: Path, exempt_dispatch: bool) -> Flask:
    """A Flask app with the REAL auth gate + a real-envelope dispatch route.

    The before_request mirrors server.py:_enforce_device_auth exactly, using
    the real admin_auth + peer_auth primitives. ``exempt_dispatch`` simulates
    whether ``/api/evo/dispatch`` has been added to the auth-exempt set (the
    shape of the #3257 fix) vs. left unexempted (the outage).
    """
    app = Flask(__name__)
    exempt = {"/pair", "/api/pair", "/api/health"}
    if exempt_dispatch:
        exempt.add("/api/evo/dispatch")

    @app.before_request
    def _enforce_device_auth():
        if not admin_auth.is_auth_enabled(shared_dir):
            return None
        if (request.environ.get("REMOTE_TRANSPORT") or "tcp") == "unix-socket":
            try:
                peer_uid = int(request.environ.get("REMOTE_PEER_UID", -1))
                if peer_uid >= 0 and peer_uid in peer_auth._resolve_uids(
                    peer_auth.DEFAULT_TRUSTED_USERS
                ):
                    return None
            except Exception:  # noqa: BLE001 — fail closed to the cookie gate
                pass
        if request.path in exempt or request.path.startswith("/static/"):
            return None
        if admin_auth.verify_device_token(
            shared_dir, request.cookies.get(admin_auth.DEVICE_COOKIE_NAME)
        ):
            return None
        if (
            request.path.startswith("/api/")
            or request.accept_mimetypes.best == "application/json"
        ):
            return jsonify({"error": "device not paired", "pair_url": "/pair"}), 401
        return redirect("/pair")

    @app.post("/api/evo/dispatch")
    def _dispatch():
        body = request.get_json(silent=True) or {}
        if not body.get("bot_id"):
            return jsonify({"error": "bot_id is required"}), 400
        if not body.get("raw_text"):
            return jsonify({"error": "raw_text is required"}), 400
        result = DispatchResult(
            subcommand="help",
            role="primary",
            mode="speak",
            system_append="Respond verbatim: available evo commands…",
            direct_send_message="Available evo commands:\n • evo help",
            subcommand_brief="show available evo commands",
        )
        return jsonify(result.to_dict())

    @app.get("/api/health")
    def _health():
        return jsonify({"ok": True})

    return app


# ── TCP fixtures ──────────────────────────────────────────────────────────────


class _TCPServer:
    def __init__(self, app: Flask):
        self._srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self._srv.server_port
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _real_auth_env(monkeypatch):
    """The admin test conftest force-disables device auth
    (``EVOLVE_ADMIN_AUTH_DISABLED=1``) so ordinary tests need not pair. These
    tests are ABOUT the auth gate, so drop the override and let the real
    ``is_auth_enabled`` logic govern (on unless an opt-out marker exists)."""
    monkeypatch.delenv("EVOLVE_ADMIN_AUTH_DISABLED", raising=False)


@pytest.fixture
def shared(tmp_path) -> Path:
    return tmp_path / "shared"


# ── TCP both-directions (#3257) ───────────────────────────────────────────────


def test_tcp_red_when_auth_enforced_and_dispatch_unexempted(shared):
    """The #3257 outage: auth on by default, plugin sends no cookie, the path
    is not exempt → the probe's cookieless TCP call gets 401 → RED. An
    in-process call would have skipped this gate entirely and stayed green."""
    assert admin_auth.is_auth_enabled(shared) is True  # on by default
    srv = _TCPServer(_build_app(shared_dir=shared, exempt_dispatch=False))
    try:
        out = probe.probe_tcp(bot_id="evolve", port=srv.port, timeout=3)
    finally:
        srv.close()
    assert out.ok is False
    assert out.http_status == 401


def test_tcp_green_when_dispatch_exempted(shared):
    """The fix: /api/evo/dispatch added to the auth-exempt set → the cookieless
    call reaches dispatch → 200 + well-formed envelope → GREEN."""
    srv = _TCPServer(_build_app(shared_dir=shared, exempt_dispatch=True))
    try:
        out = probe.probe_tcp(bot_id="evolve", port=srv.port, timeout=3)
    finally:
        srv.close()
    assert out.ok is True
    assert out.http_status == 200
    assert out.envelope_ok is True
    assert out.mode == "speak"


def test_tcp_green_when_auth_disabled(shared):
    """An un-paired pod (operator opted out) → gate is open → GREEN even with
    the path unexempted. This is why the break is latent: fine until a device
    pairs, then silently 401s."""
    admin_auth.record_optout(shared, by="test", reason="probe integration test")
    assert admin_auth.is_auth_enabled(shared) is False
    srv = _TCPServer(_build_app(shared_dir=shared, exempt_dispatch=False))
    try:
        out = probe.probe_tcp(bot_id="evolve", port=srv.port, timeout=3)
    finally:
        srv.close()
    assert out.ok is True
    assert out.envelope_ok is True


# ── unix socket: reachability vs auth ─────────────────────────────────────────


@pytest.fixture
def socket_path():
    import tempfile
    fd, name = tempfile.mkstemp(prefix="evopi-", suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(name)
    yield name
    if Path(name).exists():
        try:
            os.unlink(name)
        except OSError:
            pass


def test_socket_green_when_peer_trusted(shared, socket_path, monkeypatch):
    """A reachable socket serving the app is GREEN.

    On macOS the daemon extracts the peer uid (getpeereid(3)) and the trusted
    peer reaches dispatch → 200. On Linux CI getpeereid is absent from glibc,
    so REMOTE_PEER_UID is -1, the peer exemption can't apply, and dispatch
    returns 401 — but the socket still round-trips an HTTP response, which is
    the transport contract the probe asserts (ok=True). So the platform-robust
    invariant is reachability, not the specific status.
    """
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids", lambda _names: {os.getuid()}
    )
    app = _build_app(shared_dir=shared, exempt_dispatch=False)
    thread, server = start_in_background(app, socket_path, enable_watchdog=False)
    time.sleep(0.05)
    try:
        out = probe.probe_unix_socket(socket_path, bot_id="evolve", timeout=3)
    finally:
        server.shutdown()
        server.server_close()
    assert out.ok is True
    assert out.http_status in (200, 401)


def test_socket_green_when_untrusted_uid_gets_401(shared, socket_path, monkeypatch):
    """The probe runs as ``evolve``; on a cut-over pod the socket trusts only
    the ``evo`` uid, so the gate falls through to the cookie check and 401s
    over the socket. That is an AUTH outcome on a HEALTHY socket — the
    transport probe must treat it as GREEN (reachability), not RED."""
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids", lambda _names: set()
    )
    app = _build_app(shared_dir=shared, exempt_dispatch=False)
    thread, server = start_in_background(app, socket_path, enable_watchdog=False)
    time.sleep(0.05)
    try:
        out = probe.probe_unix_socket(socket_path, bot_id="evolve", timeout=3)
    finally:
        server.shutdown()
        server.server_close()
    assert out.ok is True
    assert out.http_status == 401


def test_socket_red_when_socket_missing(socket_path):
    """The Linux ENOENT/path-break class: nothing bound at the socket path."""
    out = probe.probe_unix_socket(socket_path, bot_id="evolve", timeout=3)
    assert out.ok is False
    assert out.error == "connect:ENOENT"

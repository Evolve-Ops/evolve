"""Tests for evolve_admin.web.peer_auth — the @require_trusted_peer decorator.

Spec: docs/spec-evo-account-separation-2026-05-25.md §3.

The decorator gates routes on (a) the request arrived via the unix
socket, AND (b) the peer effective uid matches one of an allowed list
of macOS users. Both checks must pass; otherwise 403.

These tests use Flask's test client + manually-set request.environ keys
to simulate unix-socket vs TCP requests without standing up a real
socket server.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask, jsonify

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

import json

from evolve_admin.web.peer_auth import (
    DEFAULT_TRUSTED_USERS,
    PRE_SEPARATION_FALLBACK_USERS,
    _default_trusted_users,
    _evo_gateway_user,
    _resolve_uids,
    require_trusted_peer,
)


def _make_app_with_protected_route(*trust_args: str) -> Flask:
    """Build a tiny Flask app whose only route uses the decorator."""
    app = Flask(__name__)

    @app.get("/admin/protected")
    @require_trusted_peer(*trust_args)
    def _protected():
        return jsonify({"ok": True})

    return app


# ── _resolve_uids ────────────────────────────────────────────────────────────


def test_resolve_uids_real_user():
    """A real user on the system resolves to its uid."""
    me = pwd.getpwuid(os.getuid()).pw_name
    uids = _resolve_uids((me,))
    assert uids == {os.getuid()}


def test_resolve_uids_unknown_user_silently_dropped():
    """Names that don't exist on this system are silently skipped.

    This is the right behavior for the Phase E migration: ``evo`` may
    not exist before Phase E.2.a runs, and the trust list including it
    shouldn't crash — it just falls through to only matching ``evolve``.
    """
    uids = _resolve_uids(("no_such_user_anywhere_xyz",))
    assert uids == set()


def test_resolve_uids_mixed_known_and_unknown():
    me = pwd.getpwuid(os.getuid()).pw_name
    uids = _resolve_uids((me, "no_such_user_anywhere_xyz"))
    assert uids == {os.getuid()}


def test_default_trust_list_is_evo_only():
    """The dual-trust migration window closed 2026-06-10 (roadmap 2.11):
    a separated pod trusts only the ``evo`` peer; ``evolve`` must NOT
    retain standing socket access."""
    assert DEFAULT_TRUSTED_USERS == ("evo",)
    assert "evolve" not in DEFAULT_TRUSTED_USERS


def test_pre_separation_fallback_is_evolve_only():
    assert PRE_SEPARATION_FALLBACK_USERS == ("evolve",)


def _write_network(tmp_path, *, primary_user: "str | None", primary_id: str = "evo"):
    """Write a minimal network.json with one primary bot."""
    entry: dict = {"role": "primary"}
    if primary_user is not None:
        entry["user"] = primary_user
    net = {"primary": primary_id, "bots": {primary_id: entry}}
    path = tmp_path / "network.json"
    path.write_text(json.dumps(net))
    return path


# ── _evo_gateway_user — the separation signal ───────────────────────────────


def test_gateway_user_cut_over_pod(tmp_path):
    """After migrate-evo-account-cutover, bots.<primary>.user == "evo"."""
    path = _write_network(tmp_path, primary_user="evo")
    assert _evo_gateway_user(path) == "evo"


def test_gateway_user_pre_cutover_pod(tmp_path):
    """Fresh installs pin user="evolve" (account ≠ bot id) until cutover."""
    path = _write_network(tmp_path, primary_user="evolve")
    assert _evo_gateway_user(path) == "evolve"


def test_gateway_user_undeterminable(tmp_path):
    assert _evo_gateway_user(tmp_path / "missing.json") is None


def test_gateway_user_falls_back_to_bot_id(tmp_path):
    """No explicit ``user`` field → primary_bot_user semantics: the bot
    runs under an account named after itself. A legacy pod whose primary
    is literally named "evolve" must keep trusting the evolve uid even
    if the (unused) evo account has been provisioned (E.2.a without
    E.2.b)."""
    path = _write_network(tmp_path, primary_user=None, primary_id="evolve")
    assert _evo_gateway_user(path) == "evolve"
    assert _default_trusted_users(path) == ("evolve",)

    path2 = _write_network(tmp_path, primary_user=None, primary_id="evo")
    assert _evo_gateway_user(path2) == "evo"


def test_default_trust_resolution_cut_over_pod(tmp_path):
    path = _write_network(tmp_path, primary_user="evo")
    assert _default_trusted_users(path) == ("evo",)


def test_default_trust_resolution_pre_cutover_pod(tmp_path, monkeypatch):
    """The realistic fresh-install state: the ``evo`` ACCOUNT exists (the
    wizard provisions it at setup) but the gateway still runs as
    ``evolve``. Account existence must NOT flip trust — only the
    network.json gateway user may."""
    path = _write_network(tmp_path, primary_user="evolve")

    class _Entry:
        pw_uid = 7301

    # Simulate the evo account existing — it must be ignored.
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth.pwd.getpwnam", lambda name: _Entry()
    )
    assert _default_trusted_users(path) == ("evolve",)


def test_default_trust_undeterminable_tiebreaks_on_account(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"

    def no_account(name):
        raise KeyError(name)

    monkeypatch.setattr("evolve_admin.web.peer_auth.pwd.getpwnam", no_account)
    assert _default_trusted_users(missing) == ("evolve",)

    class _Entry:
        pw_uid = 7301

    monkeypatch.setattr(
        "evolve_admin.web.peer_auth.pwd.getpwnam", lambda name: _Entry()
    )
    assert _default_trusted_users(missing) == ("evo",)


# ── End-to-end through the decorator ────────────────────────────────────────


def _make_network_routed_app(network_path) -> Flask:
    app = Flask(__name__)

    @app.get("/admin/protected")
    @require_trusted_peer(network_path=network_path)
    def _protected():
        return jsonify({"ok": True})

    return app


def test_cut_over_pod_rejects_evolve_peer(tmp_path, monkeypatch):
    """End-to-end: on a cut-over pod, a unix-socket request from the
    ``evolve`` uid is rejected — the decommissioned migration window
    must not silently survive."""
    evo_uid, evolve_uid = 7301, 7302
    path = _write_network(tmp_path, primary_user="evo")

    def fake_getpwnam(name):
        class _Entry:
            pw_uid = {"evo": evo_uid, "evolve": evolve_uid}[name]
        if name in ("evo", "evolve"):
            return _Entry()
        raise KeyError(name)

    monkeypatch.setattr("evolve_admin.web.peer_auth.pwd.getpwnam", fake_getpwnam)
    app = _make_network_routed_app(path)

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": evo_uid,
        })
        assert resp.status_code == 200
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": evolve_uid,
        })
        assert resp.status_code == 403


def test_pre_cutover_pod_still_trusts_evolve_peer(tmp_path, monkeypatch):
    """End-to-end: before the cutover (evo account may already exist!),
    the evolve peer — which is what evo's gateway runs as — keeps
    access, and the evo uid (nothing legitimately runs as it yet) does
    not gain it."""
    evo_uid, evolve_uid = 7301, 7302
    path = _write_network(tmp_path, primary_user="evolve")

    def fake_getpwnam(name):
        class _Entry:
            pw_uid = {"evo": evo_uid, "evolve": evolve_uid}[name]
        if name in ("evo", "evolve"):
            return _Entry()
        raise KeyError(name)

    monkeypatch.setattr("evolve_admin.web.peer_auth.pwd.getpwnam", fake_getpwnam)
    app = _make_network_routed_app(path)

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": evolve_uid,
        })
        assert resp.status_code == 200
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": evo_uid,
        })
        assert resp.status_code == 403


# ── TCP requests are always rejected ─────────────────────────────────────────


def test_tcp_request_rejected_even_with_correct_uid(monkeypatch):
    """TCP requests can't reach protected endpoints — auth surface for
    those routes is the unix socket only, by design."""
    app = _make_app_with_protected_route()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids",
        lambda names: {os.getuid()},  # current uid IS trusted
    )

    with app.test_client() as client:
        # Flask's test client doesn't set REMOTE_TRANSPORT — that's the
        # default "tcp" state.
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_PEER_UID": os.getuid(),
        })
    assert resp.status_code == 403
    assert b"unix socket" in resp.data.lower() or b"unix-socket" in resp.data.lower()


def test_tcp_request_rejected_when_explicitly_set(monkeypatch):
    """Even when environ explicitly says transport=tcp."""
    app = _make_app_with_protected_route()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids",
        lambda names: {os.getuid()},
    )

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "tcp",
            "REMOTE_PEER_UID": os.getuid(),
        })
    assert resp.status_code == 403


# ── Unix-socket requests — peer-uid gating ───────────────────────────────────


def test_unix_socket_request_with_trusted_uid_passes(monkeypatch):
    """The happy path: transport is unix-socket, peer uid is trusted → 200."""
    app = _make_app_with_protected_route()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids",
        lambda names: {os.getuid()},
    )

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid(),
        })
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_unix_socket_request_with_untrusted_uid_rejected(monkeypatch):
    """Peer uid 99999 isn't in the trust set → 403."""
    app = _make_app_with_protected_route()
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids",
        lambda names: {os.getuid()},  # current uid is trusted; 99999 isn't
    )

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": 99999,
        })
    assert resp.status_code == 403
    assert b"not in trusted set" in resp.data


def test_unix_socket_request_without_peer_uid_rejected():
    """Missing peer uid (extraction failure) is rejected even on the
    unix socket transport. Prevents accidental open-by-default."""
    app = _make_app_with_protected_route()

    with app.test_client() as client:
        resp = client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            # No REMOTE_PEER_UID → default -1
        })
    assert resp.status_code == 403
    assert b"peer credentials unavailable" in resp.data


def test_decorator_resolves_trust_at_request_time(monkeypatch):
    """Trust resolution happens per request, not at decoration. So a
    user (like evo) that didn't exist when the daemon started but is
    created later via Phase E.2.a is picked up without a restart."""
    call_count = {"count": 0}

    def fake_resolve(names):
        call_count["count"] += 1
        return {os.getuid()}

    monkeypatch.setattr("evolve_admin.web.peer_auth._resolve_uids", fake_resolve)
    app = _make_app_with_protected_route()

    with app.test_client() as client:
        client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid(),
        })
        client.get("/admin/protected", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid(),
        })

    # Resolved twice — once per request.
    assert call_count["count"] == 2


def test_explicit_trust_list_overrides_default(monkeypatch):
    """Passing names to the decorator pins the trust set."""
    app = Flask(__name__)

    @app.get("/admin/explicit")
    @require_trusted_peer("nobody")  # explicit empty-resolves
    def _protected():
        return jsonify({"ok": True})

    # nobody → uid 4294967294 typically on macOS; resolve to that or skip
    nobody_uid = None
    try:
        nobody_uid = pwd.getpwnam("nobody").pw_uid
    except KeyError:
        pytest.skip("'nobody' user not present on this system")

    with app.test_client() as client:
        # Current uid is NOT in the explicit "nobody" trust list → 403
        resp = client.get("/admin/explicit", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid(),
        })
        assert resp.status_code == 403

        # nobody's uid IS in the trust list → 200
        resp = client.get("/admin/explicit", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": nobody_uid,
        })
        assert resp.status_code == 200

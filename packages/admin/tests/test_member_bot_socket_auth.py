"""Member-bot RPC socket auth — device-gate exemption + per-route identity bind.

THE SECURITY-CRITICAL surface (companion to test_google_bot_routes.py): once
the gateway plugin's EvoDispatchClient + BetterEngineClient move onto the
peer-authed unix socket, a *member* bot's request must clear the device-cookie
gate (it carries no browser cookie) WITHOUT widening access. This file asserts:

  (B) exemption     — each member-bot RPC route returns non-401 over the socket
      from a recognized member-bot peer uid; still 401 from an unrecognized uid,
      over TCP, and a primary-only / sensitive route stays 401 over the socket.
  (C) identity bind — a route that trusts an authoritative body/query ``bot_id``
      binds it to the kernel peer uid: a member bot acting AS ANOTHER bot gets
      403; acting as itself passes; the trusted evo peer is never constrained.

Technique mirrors test_google_bot_routes / test_peer_auth: Flask test client
with REMOTE_TRANSPORT/REMOTE_PEER_UID environ overrides simulating peer creds.
The member bot's macOS user is the *current* test user so the real resolver
maps os.getuid() → that bot end-to-end. ``_resolve_uids`` is pinned to a fixed
TRUSTED sentinel uid (distinct from os.getuid) so the trusted-vs-member split is
deterministic regardless of whether the host has an evo/evolve account.
"""
from __future__ import annotations

import json
import os
import pwd
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.web import admin_auth, peer_auth  # noqa: E402


_ME = pwd.getpwuid(os.getuid()).pw_name
_TRUSTED_UID = 999_777   # sentinel: the "evo" trusted peer uid (≠ os.getuid())
_UNKNOWN_UID = 4_242_424  # maps to no bot, not trusted
_DAEMON_UID = 999_888    # sentinel: the daemon's own uid (≠ os.getuid())


# Member-bot RPC routes the exemption must cover (Task B). Each tuple is
# (method, path, json_body) chosen so the device gate is the ONLY thing that
# could 401 — the handler's own validation (400/404) or a clean 200 proves the
# gate was bypassed. No authoritative bot_id is supplied here (that is Task C).
_EXEMPT_ROUTES = [
    ("POST", "/api/evo/dispatch", {}),
    ("GET", "/api/evo/wizard/active", None),
    ("POST", "/api/evo/wizard/turn", {}),
    ("GET", "/api/better/recommendations", None),
    ("GET", "/api/better/recommendations/top", None),
    ("POST", "/api/better/recommendations/some-id/accept", {}),
    ("POST", "/api/better/recommendations/some-id/reject", {}),
    ("POST", "/api/better/recommendations/some-id/snooze", {}),
    ("POST", "/api/better/pending-admin-tasks", {}),
    ("GET", "/api/better/pending-admin-tasks", None),
]

# Routes that MUST stay gated over the socket from a member peer — proves the
# exemption never widens to the primary-only surface or the admin API.
_NON_EXEMPT_ROUTES = [
    ("GET", "/api/network", None),                       # admin API
    ("GET", "/api/primary/state/signals", None),         # primary-only (role-gated in plugin)
    ("POST", "/api/evo/help/search", {"q": "x"}),        # primary-only
    ("POST", "/api/evo/intake", {"kind": "bug", "body": "x"}),  # primary-only
    ("GET", "/api/evo/wizard/state", None),              # not a member-bot RPC route
]


@pytest.fixture(autouse=True)
def _enforce_real_auth(monkeypatch):
    """Auth on-by-default (2.6) — clear the suite-wide env escape so this file
    exercises real enforcement, then pin the trusted-uid resolution."""
    monkeypatch.delenv(admin_auth._AUTH_DISABLED_ENV, raising=False)
    # The trusted set ("evo"/"evolve") resolves to a fixed sentinel uid that is
    # NOT the current test user — so os.getuid() is always a *member* peer, and
    # _TRUSTED_UID is always the trusted evo peer. Deterministic on any host.
    monkeypatch.setattr(peer_auth, "_resolve_uids", lambda names: {_TRUSTED_UID})
    # The device gate also exempts the daemon's OWN uid (same-user local
    # tooling, e.g. the gallery-verify harness) — in this test process that
    # would be os.getuid(), the very uid simulating the member peer. Pin the
    # daemon uid to its own sentinel so a member peer stays a member peer.
    monkeypatch.setattr(peer_auth, "_daemon_uid", lambda: _DAEMON_UID)


@pytest.fixture
def app(tmp_path):
    """Full admin app with auth enabled + a member bot 'lex' owned by the test
    user (so os.getuid() resolves to lex) and a second bot 'rex' (unprovisioned
    account → never the peer)."""
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    net = {
        "sharedDir": str(shared),
        # No primary/role=primary/"evolve" bot → _default_trusted_users can't
        # bind the member uid; combined with the _resolve_uids pin above, the
        # member peer is never in the trusted set.
        "bots": {
            "lex": {"role": "member", "user": _ME},
            "rex": {"role": "member", "user": "no_such_account_xyz"},
        },
        "members": ["lex", "rex"],
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(net))
    application = create_app(network_path=net_file)
    application.config["TESTING"] = True
    application.config["_TEST_NETWORK_PATH"] = str(net_file)
    admin_auth.ensure_key(shared)  # enforcement ON
    return application


def _socket_env(uid: int) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": uid}


def _call(client, method: str, path: str, body, env: dict | None):
    kw = {"environ_overrides": env} if env is not None else {}
    if method == "GET":
        return client.get(path, **kw)
    return client.post(path, json=(body or {}), **kw)


# ── (B) exemption ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,body", _EXEMPT_ROUTES)
def test_member_peer_reaches_exempt_route(app, method, path, body):
    """A recognized member-bot peer-uid clears the device-cookie gate (non-401)."""
    with app.test_client() as c:
        r = _call(c, method, path, body, _socket_env(os.getuid()))
    assert r.status_code != 401, (path, r.status_code, r.get_data(as_text=True))


@pytest.mark.parametrize("method,path,body", _EXEMPT_ROUTES)
def test_unknown_peer_uid_still_gated(app, method, path, body):
    """An unrecognized socket peer uid (maps to no bot, not trusted) → 401."""
    with app.test_client() as c:
        r = _call(c, method, path, body, _socket_env(_UNKNOWN_UID))
    assert r.status_code == 401, (path, r.status_code)


@pytest.mark.parametrize("method,path,body", _EXEMPT_ROUTES)
def test_tcp_request_still_gated(app, method, path, body):
    """No REMOTE_TRANSPORT (TCP) → no peer creds → device gate 401s. Proves no
    TCP exemption was added — a browser session can't reach these."""
    with app.test_client() as c:
        r = _call(c, method, path, body, None)
    assert r.status_code == 401, (path, r.status_code)


@pytest.mark.parametrize("method,path,body", _NON_EXEMPT_ROUTES)
def test_non_exempt_route_stays_gated_for_member_peer(app, method, path, body):
    """A member peer over the socket is NOT exempt on the admin API or the
    primary-only surface — the exemption stays narrow."""
    with app.test_client() as c:
        r = _call(c, method, path, body, _socket_env(os.getuid()))
    assert r.status_code == 401, (path, r.status_code)


def test_trusted_peer_reaches_non_exempt_route(app):
    """The trusted evo peer DOES reach the admin API over the socket (the 2.6
    enabler) — proves the member-peer 401 above is the exemption being narrow,
    not the socket being dead."""
    with app.test_client() as c:
        r = c.get("/api/network", environ_overrides=_socket_env(_TRUSTED_UID))
    assert r.status_code != 401


# ── (C) per-route identity bind ───────────────────────────────────────────────

_FAKE_DISPATCH = SimpleNamespace(
    network_dirty=False, to_dict=lambda: {"ok": True})


def test_dispatch_member_acting_as_another_bot_403(app):
    """lex's uid POSTing dispatch with bot_id='rex' (another bot) → 403. The
    body bot_id is not authoritative; identity is the kernel peer uid."""
    with app.test_client() as c:
        r = c.post("/api/evo/dispatch",
                   json={"bot_id": "rex", "raw_text": "evo status"},
                   environ_overrides=_socket_env(os.getuid()))
    assert r.status_code == 403, r.get_data(as_text=True)
    assert "does not match" in r.get_json().get("error", "")


def test_dispatch_member_acting_as_itself_passes(app):
    """lex's uid POSTing dispatch with bot_id='lex' (its own) clears the bind."""
    with patch("evolve_admin.web.evo_routes._dispatch.dispatch",
               return_value=_FAKE_DISPATCH):
        with app.test_client() as c:
            r = c.post("/api/evo/dispatch",
                       json={"bot_id": "lex", "raw_text": "evo status"},
                       environ_overrides=_socket_env(os.getuid()))
    assert r.status_code not in (401, 403), r.get_data(as_text=True)


def test_dispatch_trusted_evo_peer_acts_as_any_bot(app):
    """The trusted evo peer is NEVER constrained — its central dispatcher fans
    out to any bot. bot_id='rex' from the trusted uid must not 403."""
    with patch("evolve_admin.web.evo_routes._dispatch.dispatch",
               return_value=_FAKE_DISPATCH):
        with app.test_client() as c:
            r = c.post("/api/evo/dispatch",
                       json={"bot_id": "rex", "raw_text": "evo status"},
                       environ_overrides=_socket_env(_TRUSTED_UID))
    assert r.status_code not in (401, 403), r.get_data(as_text=True)


def test_wizard_turn_member_acting_as_another_bot_403(app):
    with app.test_client() as c:
        r = c.post("/api/evo/wizard/turn",
                   json={"bot_id": "rex", "wizard_session_id": "ext:telegram:1",
                         "user_message": "hi"},
                   environ_overrides=_socket_env(os.getuid()))
    assert r.status_code == 403


def test_wizard_active_member_acting_as_another_bot_403(app):
    with app.test_client() as c:
        r = c.get("/api/evo/wizard/active?bot_id=rex",
                  environ_overrides=_socket_env(os.getuid()))
    assert r.status_code == 403


def test_pending_admin_task_member_acting_as_another_bot_403(app):
    """A member bot may queue a pending-admin-task only into its OWN workspace."""
    with app.test_client() as c:
        r = c.post("/api/better/pending-admin-tasks",
                   json={"bot_id": "rex", "title": "x", "detail": "y"},
                   environ_overrides=_socket_env(os.getuid()))
    assert r.status_code == 403


def test_pending_admin_task_member_acting_as_itself_passes(app):
    with app.test_client() as c:
        r = c.post("/api/better/pending-admin-tasks",
                   json={"bot_id": "lex", "title": "x", "detail": "y"},
                   environ_overrides=_socket_env(os.getuid()))
    assert r.status_code not in (401, 403), r.get_data(as_text=True)


# ── member_peer_bot_id unit (the authority-binding primitive) ─────────────────


def test_member_peer_bot_id_none_for_tcp(app):
    with app.test_request_context(environ_overrides={"REMOTE_PEER_UID": os.getuid()}):
        # No REMOTE_TRANSPORT → TCP → None (admin UI acts on any bot).
        assert peer_auth.member_peer_bot_id(_netp(app)) is None


def test_member_peer_bot_id_none_for_trusted_peer(app):
    with app.test_request_context(environ_overrides=_socket_env(_TRUSTED_UID)):
        assert peer_auth.member_peer_bot_id(_netp(app)) is None


def test_member_peer_bot_id_binds_member(app):
    with app.test_request_context(environ_overrides=_socket_env(os.getuid())):
        assert peer_auth.member_peer_bot_id(_netp(app)) == "lex"


def test_member_peer_bot_id_none_for_unknown_uid(app):
    with app.test_request_context(environ_overrides=_socket_env(_UNKNOWN_UID)):
        assert peer_auth.member_peer_bot_id(_netp(app)) is None


def _netp(app) -> Path:
    """The network.json path the app was created with (for the real resolver)."""
    # create_app stashes it on the config-less closure; reconstruct from the
    # shared dir written by the fixture.
    return Path(app.config["_TEST_NETWORK_PATH"])

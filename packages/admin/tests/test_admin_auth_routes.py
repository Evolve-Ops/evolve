"""Device-pairing auth gate wired into the admin server (roadmap 2.1).

Verifies the bootstrap (open until paired), enforcement (401/redirect once a key
exists), the exempt set (pairing + static + PWA shell), and the pairing round-trip
(valid code → cookie → access).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.web import admin_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _enforce_real_auth(monkeypatch):
    """Auth is on-by-default (2.6); clear the suite-wide env escape so this
    file exercises real enforcement."""
    monkeypatch.delenv(admin_auth._AUTH_DISABLED_ENV, raising=False)


@pytest.fixture
def app_shared(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps({"bots": {}, "sharedDir": str(shared)}))
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    return app, shared


def test_enforced_by_default_unpaired(app_shared):
    # 2.6: a fresh pod enforces with no key and no opt-out — the API 401s
    # until the operator pairs.
    app, _shared = app_shared
    with app.test_client() as c:
        assert c.get("/api/network").status_code == 401


def test_optout_marker_reopens(app_shared):
    app, shared = app_shared
    admin_auth.record_optout(shared, by="pod-admin", reason="dev")
    with app.test_client() as c:
        assert c.get("/api/network").status_code != 401


def test_api_blocked_once_enabled_and_unpaired(app_shared):
    app, shared = app_shared
    admin_auth.ensure_key(shared)  # operator ran `pair` → enforcement on
    with app.test_client() as c:
        assert c.get("/api/network").status_code == 401


def test_browser_navigation_redirects_to_pair(app_shared):
    app, shared = app_shared
    admin_auth.ensure_key(shared)
    with app.test_client() as c:
        r = c.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/pair")


def test_exempt_paths_open_when_enabled(app_shared):
    app, shared = app_shared
    admin_auth.ensure_key(shared)
    with app.test_client() as c:
        assert c.get("/pair").status_code == 200  # pairing page itself
        assert c.get("/manifest.json").status_code != 401
        assert c.get("/sw.js").status_code != 401
        assert c.get("/static/css/base.css").status_code != 401  # may 404, not 401


def test_pair_with_valid_code_sets_cookie(app_shared):
    app, shared = app_shared
    code = admin_auth.current_pairing_code(shared)  # generates key + enables
    with app.test_client() as c:
        r = c.post("/api/pair", json={"code": code})
        assert r.status_code == 200
        assert admin_auth.DEVICE_COOKIE_NAME in r.headers.get("Set-Cookie", "")


def _a_wrong_code(shared) -> str:
    """A 6-digit code guaranteed not to match any accepted window (deterministic
    — no 1-in-a-million flake on a derived-code collision)."""
    key = admin_auth._load_key(shared)
    now = int(time.time() // admin_auth.PAIRING_WINDOW_SECONDS)
    valid = {admin_auth._code_for_window(key, now + d) for d in (-1, 0, 1)}
    for i in range(len(valid) + 1):
        candidate = f"{i:06d}"
        if candidate not in valid:
            return candidate
    raise AssertionError("unreachable")


def test_pair_with_bad_code_rejected(app_shared):
    app, shared = app_shared
    admin_auth.ensure_key(shared)
    with app.test_client() as c:
        assert c.post("/api/pair", json={"code": _a_wrong_code(shared)}).status_code == 403


def test_pair_throttles_after_repeated_failures(app_shared):
    # The pairing code is short by design; this throttle — not entropy — is the
    # brute-force wall. After the grace count, the endpoint 429s without even
    # checking the code, so a guesser gets zero attempts during the cooldown.
    app, shared = app_shared
    admin_auth.ensure_key(shared)
    bad = _a_wrong_code(shared)
    grace = admin_auth.PairThrottle.FREE_ATTEMPTS
    with app.test_client() as c:
        # grace failures + the one that arms the cooldown all return 403…
        for _ in range(grace + 1):
            assert c.post("/api/pair", json={"code": bad}).status_code == 403
        # …then the endpoint is throttled: 429 with a Retry-After hint.
        r = c.post("/api/pair", json={"code": bad})
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0


def test_pair_throttle_does_not_block_first_attempts(app_shared):
    # A correct code on the first try is never throttled (grace window).
    app, shared = app_shared
    code = admin_auth.current_pairing_code(shared)
    with app.test_client() as c:
        assert c.post("/api/pair", json={"code": code}).status_code == 200


def test_paired_cookie_grants_access(app_shared):
    app, shared = app_shared
    code = admin_auth.current_pairing_code(shared)
    with app.test_client() as c:  # the test client retains cookies across calls
        assert c.post("/api/pair", json={"code": code}).status_code == 200
        assert c.get("/api/network").status_code != 401  # now authenticated


def test_health_probe_exempt_when_enforced(app_shared):
    # The liveness probe must answer even on an unpaired enforced pod — a 401
    # there would make a healthy daemon look down (health.py / probes.py).
    app, _shared = app_shared
    with app.test_client() as c:
        assert c.get("/api/health").status_code != 401


def test_unix_socket_trusted_peer_exempt(app_shared, monkeypatch):
    # evo's tool runtime reaches the daemon over the peer-authed unix socket;
    # a TRUSTED peer-uid bypasses the device-cookie gate (the 2.6 enabler).
    app, _shared = app_shared
    from evolve_admin.web import peer_auth
    monkeypatch.setattr(peer_auth, "_resolve_uids", lambda names: {4242})
    with app.test_client() as c:
        r = c.get("/api/network", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 4242})
        assert r.status_code != 401


def test_unix_socket_self_uid_exempt(app_shared, monkeypatch):
    # The daemon's OWN uid bypasses the device-cookie gate even when it is
    # not in the trusted-peer set: a local process already running as the
    # daemon's user can read its token store anyway, so the exemption widens
    # nothing. This is what admits same-user local tooling — the Gate-2
    # gallery-verify harness runs as the ``evolve`` service user and 401ed
    # on cut-over pods (trust list is evo-only) before this exemption.
    app, _shared = app_shared
    from evolve_admin.web import peer_auth
    # Trusted set deliberately excludes the runner's uid, so self-uid must
    # clear the gate on its own.
    monkeypatch.setattr(
        peer_auth, "_resolve_uids", lambda names: {os.getuid() + 1})
    with app.test_client() as c:
        r = c.get("/api/network", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": os.getuid()})
        assert r.status_code != 401


def test_unix_socket_untrusted_peer_still_gated(app_shared, monkeypatch):
    # A socket request from an UNTRUSTED uid (neither in the trusted set nor
    # the daemon's own uid) is NOT exempt — the exemption must not widen
    # access beyond evo + the daemon's own user.
    app, _shared = app_shared
    from evolve_admin.web import peer_auth
    monkeypatch.setattr(peer_auth, "_resolve_uids", lambda names: {4242})
    untrusted_uid = 9999 if os.getuid() != 9999 else 9998
    with app.test_client() as c:
        r = c.get("/api/network", environ_overrides={
            "REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": untrusted_uid})
        assert r.status_code == 401

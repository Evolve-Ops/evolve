"""CSRF / Origin / Host defense (roadmap 2.7).

Two layers of coverage:
  * Pure-function unit tests of ``csrf.check_request`` (no Flask context).
  * Integration tests through ``create_app``: the gate is wired after the
    auth gate, the CSRF cookie is issued, and the two pairing-poll routes
    are POST (not GET).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.web import admin_auth  # noqa: E402
from evolve_admin.web import csrf  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _enforce_real_auth(monkeypatch):
    """The CSRF gate is scoped to authenticated requests; auth is on-by-default
    (roadmap 2.6) but the conftest disables it suite-wide via an env escape.
    Clear it here so the paired_client fixture actually enforces and the CSRF
    gate engages."""
    monkeypatch.delenv(admin_auth._AUTH_DISABLED_ENV, raising=False)


# ── pure-function unit tests ────────────────────────────────────────────────


def _hdr(d):
    return lambda name: d.get(name)


def _ck(d):
    return lambda name: d.get(name)


def _check(**kw):
    base = dict(
        method="POST",
        path="/api/foo",
        transport="tcp",
        is_authenticated=True,
        request_host="127.0.0.1:5050",
        header_get=_hdr({}),
        cookie_get=_ck({}),
        network={},
    )
    base.update(kw)
    return csrf.check_request(**base)


def test_safe_method_always_allowed():
    ok, _ = _check(method="GET")
    assert ok


def test_unix_socket_exempt():
    ok, _ = _check(transport="unix-socket")
    assert ok


def test_unauthenticated_exempt():
    # No device cookie → no ambient authority to ride.
    ok, _ = _check(is_authenticated=False)
    assert ok


def test_cross_origin_rejected():
    ok, reason = _check(
        header_get=_hdr({"Origin": "http://evil.example"}),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "t"}),
    )
    assert not ok and "cross-origin" in reason


def test_same_origin_with_token_allowed():
    ok, _ = _check(
        header_get=_hdr({
            "Origin": "http://127.0.0.1:5050",
            csrf.CSRF_HEADER_NAME: "tok123",
        }),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "tok123"}),
    )
    assert ok


def test_same_origin_missing_token_rejected():
    ok, reason = _check(
        header_get=_hdr({"Origin": "http://127.0.0.1:5050"}),
        cookie_get=_ck({}),
    )
    assert not ok and "CSRF token" in reason


def test_same_origin_token_mismatch_rejected():
    ok, reason = _check(
        header_get=_hdr({
            "Origin": "http://127.0.0.1:5050",
            csrf.CSRF_HEADER_NAME: "aaa",
        }),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "bbb"}),
    )
    assert not ok and "mismatch" in reason


def test_no_origin_unknown_host_rejected():
    # Origin-less mutating request to an unrecognized Host = rebinding signature.
    ok, reason = _check(
        request_host="attacker.example",
        header_get=_hdr({csrf.CSRF_HEADER_NAME: "t"}),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "t"}),
    )
    assert not ok and "rebinding" in reason


def test_no_origin_loopback_host_with_token_allowed():
    ok, _ = _check(
        request_host="127.0.0.1:5050",
        header_get=_hdr({csrf.CSRF_HEADER_NAME: "t"}),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "t"}),
    )
    assert ok


def test_no_origin_configured_adminbaseurl_host_allowed():
    ok, _ = _check(
        request_host="pod.example:5050",
        network={"adminBaseUrl": "https://pod.example:5050"},
        header_get=_hdr({csrf.CSRF_HEADER_NAME: "t"}),
        cookie_get=_ck({csrf.CSRF_COOKIE_NAME: "t"}),
    )
    assert ok


def test_pair_path_exempt_from_token_but_not_origin():
    # /api/pair has no CSRF cookie yet (bootstrap) — token skipped…
    ok, _ = _check(path="/api/pair", header_get=_hdr({
        "Origin": "http://127.0.0.1:5050"}), cookie_get=_ck({}))
    assert ok
    # …but a cross-origin pair attempt is still rejected.
    ok, reason = _check(path="/api/pair",
                        header_get=_hdr({"Origin": "http://evil.example"}),
                        cookie_get=_ck({}))
    assert not ok and "cross-origin" in reason


# ── integration through create_app ──────────────────────────────────────────


@pytest.fixture
def paired_client(tmp_path):
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps({"bots": {}, "sharedDir": str(shared)}))
    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    code = admin_auth.current_pairing_code(shared)  # enables auth
    c = app.test_client()
    assert c.post("/api/pair", json={"code": code}).status_code == 200
    return c


def test_csrf_cookie_is_issued(paired_client):
    # The pair response (and any response) sets the readable CSRF cookie.
    cookie = paired_client.get_cookie(csrf.CSRF_COOKIE_NAME)
    assert cookie is not None and cookie.value


def _csrf_value(client):
    ck = client.get_cookie(csrf.CSRF_COOKIE_NAME)
    return ck.value if ck else ""


def test_authed_mutation_without_token_rejected(paired_client):
    # Strip the X-CSRF-Token header → 403 even though device-authenticated.
    r = paired_client.post("/api/network", json={}, headers={
        "Origin": "http://localhost"})
    assert r.status_code == 403
    assert "request blocked" in r.get_json().get("error", "")


def test_authed_mutation_with_matching_token_passes_csrf(paired_client):
    tok = _csrf_value(paired_client)
    # Same-origin + matching token → the CSRF gate lets it through (the
    # route itself may 404/400/etc, but NOT 403-from-csrf).
    r = paired_client.post(
        "/api/skills/install/whatsapp/pair/nope",
        headers={"Origin": "http://localhost", csrf.CSRF_HEADER_NAME: tok},
    )
    assert r.status_code != 403


def test_cross_origin_mutation_rejected(paired_client):
    tok = _csrf_value(paired_client)
    r = paired_client.post("/api/network", json={}, headers={
        "Origin": "http://evil.example", csrf.CSRF_HEADER_NAME: tok})
    assert r.status_code == 403


def test_pairing_poll_routes_are_post_not_get(paired_client):
    tok = _csrf_value(paired_client)
    hdr = {"Origin": "http://localhost", csrf.CSRF_HEADER_NAME: tok}
    # GET is no longer routed (405 Method Not Allowed), POST is.
    assert paired_client.get(
        "/api/skills/install/whatsapp/pair/x").status_code == 405
    assert paired_client.get(
        "/api/skills/install/signal/pair/x").status_code == 405
    assert paired_client.post(
        "/api/skills/install/whatsapp/pair/x", headers=hdr).status_code != 405


def test_safe_get_needs_no_token(paired_client):
    # A read still works with no CSRF header.
    assert paired_client.get("/api/network").status_code == 200

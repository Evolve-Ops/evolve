"""End-to-end tests for the admin pairing wizard Flask routes.

Covers ``routes_pairing``:

  GET  /api/admin/pairing/config
  GET  /api/admin/bots/<bot>/pairing/lookup?code=XYZ
  POST /api/admin/bots/<bot>/pairing/commit
  GET  /api/admin/bots/<bot>/pairing/state

The commit endpoint is the load-bearing one — its role routing
determines where the operator's ID lands in network.json (pod
admins vs the bot's primary user vs nothing extra). Test all three
roles explicitly.

Same tmp_path bot-home redirection as test_routes_bot_users — the
commit step calls into routes_bot_users._approve which writes the
bot's allowFrom file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import external_ids as _external_ids  # noqa: E402
from evolve_admin.web import routes_pairing as rp  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path) -> Path:
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["atlas", "team_bot_a"],
        "bots": {
            "atlas": {
                "role": "member", "port": 19010, "multiUser": False,
            },
            "team_bot_a": {
                "role": "member", "port": 19002, "multiUser": True,
                "primary_user": {"name": "Sam Sample", "external_ids": {}},
            },
        },
        # Telegram 999 is a pod admin (grants bot.roster.mutate); 333 is an
        # unclaimed identity that resolves to ``participant`` (grants none).
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    return p


def _bot_creds_dir(tmp_path: Path, bot: str) -> Path:
    d = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, payload: dict) -> None:
    p.write_text(json.dumps(payload))


def _seed_pending(tmp_path: Path, bot: str, channel: str, *,
                  id_: str, code: str, meta: dict | None = None) -> Path:
    creds = _bot_creds_dir(tmp_path, bot)
    pairing_path = creds / f"{channel}-pairing.json"
    pairing_path.write_text(json.dumps({
        "version": 1,
        "requests": [{
            "id": id_, "code": code, "createdAt": "2026-06-01T17:00:00Z",
            "meta": meta or {},
        }],
    }))
    # Empty allowFrom so commit can demonstrate the move.
    (creds / f"{channel}-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    return pairing_path


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    network_path = _seed_network(tmp_path)
    # Both modules use ``bot_home`` to resolve credentials dir; redirect
    # both so the test exercises the real file-write paths without sudo.
    monkeypatch.setattr(
        rbu, "bot_home",
        lambda bot, net: tmp_path / "Users" / bot,
    )
    a = Flask(__name__)
    rp.register_routes(a, network_path)
    rbu.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a, network_path


# ── Config endpoint ─────────────────────────────────────────────────────────


def test_config_endpoint_returns_known_channels(app):
    a, _ = app
    with a.test_client() as c:
        data = c.get("/api/admin/pairing/config").get_json()
    chs = {row["channel"] for row in data["channels"]}
    assert chs == {"telegram", "slack", "discord", "whatsapp"}
    # Each row carries the keys the JS modal consumes.
    for row in data["channels"]:
        assert "id_validator_pattern" in row
        assert "discovery_method" in row
        assert "has_deeplink" in row


# ── Lookup ──────────────────────────────────────────────────────────────────


def test_lookup_finds_pending_by_code(app, tmp_path):
    a, _ = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ",
                  meta={"firstName": "Pod", "lastName": "Admin",
                        "username": "pod_admin"})
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/lookup?code=WX42YZ").get_json()
    assert data["found"] is True
    assert data["channel"] == "telegram"
    assert data["id"] == "1260193629"
    assert data["code"] == "WX42YZ"
    # display_name derived from telegram firstName + lastName
    assert data["display_name"] == "Pod Admin"
    assert data["meta"]["username"] == "pod_admin"


def test_lookup_returns_found_false_when_no_match(app, tmp_path):
    a, _ = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/lookup?code=NOPENOPE").get_json()
    assert data == {"found": False}


def test_lookup_400s_on_missing_code(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/atlas/pairing/lookup")
    assert resp.status_code == 400


def test_lookup_404s_on_unknown_bot(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.get("/api/admin/bots/nonesuch/pairing/lookup?code=X")
    assert resp.status_code == 404


def test_lookup_searches_all_channels(app, tmp_path):
    """A pasted code with no channel hint should match across all
    channels — the modal's primary input is paste-the-code, not
    pick-the-channel."""
    a, _ = app
    _seed_pending(tmp_path, "team_bot_a", "slack",
                  id_="U01ABCDE2FG", code="SLK99",
                  meta={"real_name": "Sam Sample"})
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/team_bot_a/pairing/lookup?code=SLK99").get_json()
    assert data["found"] is True
    assert data["channel"] == "slack"
    assert data["display_name"] == "Sam Sample"


# ── Commit: pod_admin role ──────────────────────────────────────────────────


def test_commit_pod_admin_writes_external_ids_and_resolved_names(app, tmp_path):
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ",
                  meta={"firstName": "Pod"})
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/pairing/commit",
            json={
                "channel": "telegram", "id": "1260193629",
                "role": "pod_admin", "name": "Pod Admin",
            },
        )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True
    net = json.loads(network_path.read_text())
    # Pod-wide promotion: ID lands in admins.external_ids.telegram.
    assert "1260193629" in net["pod"]["admins"]["external_ids"]["telegram"]
    # And the resolved_names cache gets a name entry so the Users
    # page renders without a channel-API round trip.
    assert net["pod"]["admins"]["resolved_names"]["telegram:1260193629"]["name"] == "Pod Admin"
    # Per-bot approve also lands — allowFrom now contains the ID.
    allow = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "1260193629" in allow["allowFrom"]
    # Pending request is cleared.
    pairing = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-pairing.json").read_text())
    assert pairing["requests"] == []


def test_commit_pod_admin_is_idempotent(app, tmp_path):
    """Re-pairing the same admin shouldn't duplicate IDs in
    external_ids — the operator might run the wizard twice."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="9991112222", code="A1", meta={})
    with a.test_client() as c:
        c.post("/api/admin/bots/atlas/pairing/commit",
               json={"channel": "telegram", "id": "9991112222",
                     "role": "pod_admin"})
    # Manual re-pair via the API
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="9991112222", code="A2", meta={})
    with a.test_client() as c:
        c.post("/api/admin/bots/atlas/pairing/commit",
               json={"channel": "telegram", "id": "9991112222",
                     "role": "pod_admin"})
    net = json.loads(network_path.read_text())
    assert net["pod"]["admins"]["external_ids"]["telegram"].count("9991112222") == 1


# ── Commit: primary role ────────────────────────────────────────────────────


def test_commit_primary_user_writes_per_bot_only(app, tmp_path):
    a, network_path = app
    _seed_pending(tmp_path, "team_bot_a", "telegram",
                  id_="8001234567", code="P1", meta={"firstName": "Sam"})
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/team_bot_a/pairing/commit",
            json={"channel": "telegram", "id": "8001234567",
                  "role": "primary", "name": "Sam Sample"},
        )
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    # Per-bot primary_user.external_ids gets the new ID.
    assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] == ["8001234567"]
    assert net["bots"]["team_bot_a"]["primary_user"]["name"] == "Sam Sample"
    # NOT promoted to pod admin.
    assert "8001234567" not in (
        net["pod"]["admins"]["external_ids"].get("telegram") or [])
    # But IS approved for THIS bot's allowFrom.
    allow = json.loads(
        (tmp_path / "Users" / "team_bot_a" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "8001234567" in allow["allowFrom"]


def test_commit_primary_replaces_existing_primary_id(app, tmp_path):
    """Operator deliberately re-pairing the primary should win — the
    old primary's ID stays in allowFrom (can be revoked from the
    Users page) but is no longer the recorded primary."""
    a, network_path = app
    # Pre-seed an existing primary in network.json (any string — only
    # the new value is regex-validated)
    net = json.loads(network_path.read_text())
    net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] = "1111111111"
    network_path.write_text(json.dumps(net, indent=2))

    _seed_pending(tmp_path, "team_bot_a", "telegram",
                  id_="2222222222", code="P2")
    with a.test_client() as c:
        c.post("/api/admin/bots/team_bot_a/pairing/commit",
               json={"channel": "telegram", "id": "2222222222",
                     "role": "primary"})
    net = json.loads(network_path.read_text())
    assert net["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] == ["2222222222"]


# ── Commit: other role ──────────────────────────────────────────────────────


def test_commit_other_only_approves_no_network_change(app, tmp_path):
    a, network_path = app
    before = network_path.read_text()
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="7770001234", code="X1")
    with a.test_client() as c:
        resp = c.post(
            "/api/admin/bots/atlas/pairing/commit",
            json={"channel": "telegram", "id": "7770001234",
                  "role": "other"},
        )
    assert resp.status_code == 200, resp.get_json()
    # network.json unchanged — no pod-admin or primary promotion.
    assert network_path.read_text() == before
    # But ID is approved into the bot's allowFrom.
    allow = json.loads(
        (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
         / "telegram-default-allowFrom.json").read_text())
    assert "7770001234" in allow["allowFrom"]


# ── Commit validation ──────────────────────────────────────────────────────


def test_commit_rejects_unknown_channel(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "signal", "id": "999",
                            "role": "pod_admin"})
    assert resp.status_code == 400


def test_commit_rejects_invalid_id_format(app):
    a, _ = app
    with a.test_client() as c:
        # Telegram ID validator wants 6-12 digits; "abc" fails.
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "telegram", "id": "abc",
                            "role": "pod_admin"})
    assert resp.status_code == 400


def test_commit_rejects_unknown_role(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json={"channel": "telegram", "id": "1260193629",
                            "role": "superuser"})
    assert resp.status_code == 400


def test_commit_404s_on_unknown_bot(app):
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/nonesuch/pairing/commit",
                      json={"channel": "telegram", "id": "1260193629",
                            "role": "pod_admin"})
    assert resp.status_code == 404


# ── State ──────────────────────────────────────────────────────────────────


def test_state_reports_unpaired_then_paired(app, tmp_path):
    a, _ = app
    _bot_creds_dir(tmp_path, "atlas")
    (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
     / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))

    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/state?channel=telegram").get_json()
    assert data["channels"]["telegram"]["paired"] is False

    # Add an approved ID; state should flip.
    (tmp_path / "Users" / "atlas" / ".openclaw" / "credentials"
     / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629"]}))
    with a.test_client() as c:
        data = c.get(
            "/api/admin/bots/atlas/pairing/state?channel=telegram").get_json()
    assert data["channels"]["telegram"]["paired"] is True
    assert data["channels"]["telegram"]["approved_count"] == 1


# ── Commit capability gate (CBR-1.2 sibling of the #3642 DM routes) ─────────
#
# ``pairing/commit`` reaches the SAME ``_approve`` mutation the Users-page
# approve route reaches (that route is ungated on main; #3642 proposes the
# gate and is still OPEN — this route does not depend on it), plus two writes
# with a strictly larger blast radius that the caller selects with an
# unauthenticated body field:
#
#   role=pod_admin → pod.admins.external_ids.<ch>  (POD-WIDE promotion: the id
#                    is auto-approved for every future bot, not just this one)
#   role=primary   → bots.<bot>.primary_user       (per-bot ownership change)
#   role=other     → allowFrom only                (plain admission)
#
# The route had ZERO capability checks. It is not in server's
# ``_AUTH_EXEMPT_PATHS``, so an untrusted TCP peer 401s at the device gate —
# but the evo peer uid over the admin unix socket is exempted from that gate
# by ``peer_auth.device_gate_trusted_peer()`` on EVERY pod, and on an
# auth-DISABLED pod ``_enforce_device_auth`` returns None for everyone. Both
# reached the mutation header-free with no capability check.
#
# Every deny test below asserts the SIDE EFFECTS did not happen as well as the
# 403 — a 403 that already wrote is not a fix.

_SOCKET_ENV = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 0}


def _creds(tmp_path: Path, bot: str) -> Path:
    return (tmp_path / "Users" / bot / ".openclaw" / "credentials")


def _assert_no_write(tmp_path: Path, network_path: Path, *, bot: str,
                     ext_id: str, channel: str = "telegram") -> None:
    """Assert a denied commit left every mutation surface untouched.

    Four surfaces, matching the four writes ``_commit_pairing`` can make:
    pod-wide admin promotion, per-bot primary ownership, the OC allowFrom
    admission, and the pending-request drop that ``_approve`` performs.
    """
    net = json.loads(network_path.read_text())
    # 1. No pod-wide promotion (the seeded 999 admin must be the only entry).
    assert net["pod"]["admins"]["external_ids"].get(channel) == ["999"]
    assert "resolved_names" not in net["pod"]["admins"]
    # 2. No primary-user change. Read through ``ids_for`` rather than a raw
    #    ``.get``: external_ids tolerates a legacy SCALAR shape (this file
    #    seeds one in test_commit_primary_replaces_existing_primary_id), and
    #    ``x not in "8001234567"`` is substring containment, not membership —
    #    a raw check would silently pass/fail for the wrong reason.
    for b in net["bots"].values():
        assert ext_id not in _external_ids.ids_for(
            (b.get("primary_user") or {}).get("external_ids"), channel)
    # 3. Not admitted into the bot's OC allowFrom.
    # 4. The pending request is still pending — ``_approve`` drops it, so a
    #    cleared queue would prove the mutation ran before the 403.
    # Both files are written by ``_seed_pending``; assert their presence so a
    # future deny test that forgets to seed fails with a clear message rather
    # than a bare FileNotFoundError.
    allow_p = _creds(tmp_path, bot) / f"{channel}-default-allowFrom.json"
    pairing_p = _creds(tmp_path, bot) / f"{channel}-pairing.json"
    assert allow_p.exists() and pairing_p.exists(), (
        f"deny test must seed a pending request for {bot}/{channel} — "
        f"otherwise there is no write for this helper to rule out")
    assert ext_id not in json.loads(allow_p.read_text())["allowFrom"]
    pairing = json.loads(pairing_p.read_text())
    assert [r["id"] for r in pairing["requests"]] == [ext_id]


def _commit_body(role: str, ext_id: str = "1260193629") -> dict:
    return {"channel": "telegram", "id": ext_id, "role": role,
            "name": "Attacker"}


# ── Deny matrix on the highest-privilege role (pod_admin) ───────────────────


def test_commit_pod_admin_header_absent_over_socket_denied(app, tmp_path):
    """A bot gateway on the admin unix socket that omits X-Requester-Identity
    is NOT the trusted UI — 403, and no pod-wide promotion happened."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json=_commit_body("pod_admin"),
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "forbidden"
    assert "no requester identity" in body["detail"]
    _assert_no_write(tmp_path, network_path, bot="atlas", ext_id="1260193629")


def test_commit_pod_admin_header_absent_unauthenticated_tcp_denied(
        app, tmp_path):
    """Header-absent over TCP with device-auth ENFORCED and no valid cookie →
    403. Fail-closed off the socket too when the caller can't be proven to be
    the authenticated UI."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    import evolve_admin.web.admin_auth as _aa
    # conftest disables auth globally via env, so patch the module attributes
    # to model an auth-enabled pod with no paired device.
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: False)
    try:
        with a.test_client() as c:
            resp = c.post("/api/admin/bots/atlas/pairing/commit",
                          json=_commit_body("pod_admin"))
        assert resp.status_code == 403
        assert "no requester identity" in resp.get_json()["detail"]
        _assert_no_write(tmp_path, network_path, bot="atlas",
                         ext_id="1260193629")
    finally:
        mp.undo()


def test_commit_pod_admin_header_absent_authenticated_ui_allowed(
        app, tmp_path):
    """Working path: header-absent over the authenticated admin-UI HTTP
    transport (device-auth enforced, VALID device cookie) → 200. This is the
    live SPA pairing modal — including the install-wizard Done screen, which
    cannot reach this route without a device cookie anyway (the path is not in
    server's ``_AUTH_EXEMPT_PATHS``, so a cookie-less TCP caller 401s at the
    device gate before the handler runs)."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    import evolve_admin.web.admin_auth as _aa
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: True)
    try:
        with a.test_client() as c:
            resp = c.post("/api/admin/bots/atlas/pairing/commit",
                          json=_commit_body("pod_admin"))
        assert resp.status_code == 200, resp.get_json()
    finally:
        mp.undo()
    net = json.loads(network_path.read_text())
    assert "1260193629" in net["pod"]["admins"]["external_ids"]["telegram"]


def test_commit_pod_admin_malformed_header_denied(app, tmp_path):
    """A PRESENT but unparseable X-Requester-Identity is denied on EVERY
    transport, including the authenticated-UI one — an asserted-but-broken
    identity must never fall through to the trusted-UI path (WO-H1-2)."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json=_commit_body("pod_admin"),
                      headers={"X-Requester-Identity": "not-a-valid-identity"})
    assert resp.status_code == 403
    assert "malformed" in resp.get_json()["detail"]
    _assert_no_write(tmp_path, network_path, bot="atlas", ext_id="1260193629")


def test_commit_pod_admin_participant_identity_denied(app, tmp_path):
    """A real, well-formed participant identity over the socket is refused by
    the capability check — ``participant`` grants no bot.* built-ins, so it
    cannot promote itself (or anyone) to pod admin."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json=_commit_body("pod_admin"),
                      headers={"X-Requester-Identity": "telegram:333"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    assert "bot.roster.mutate" in resp.get_json()["detail"]
    _assert_no_write(tmp_path, network_path, bot="atlas", ext_id="1260193629")


def test_commit_pod_admin_valid_admin_identity_over_socket_allowed(
        app, tmp_path):
    """Working path: a gateway presenting a VALID pod-admin identity over the
    socket is still allowed — the socket is not blanket-denied, only the
    header-absent and under-privileged cases flip to deny."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="1260193629", code="WX42YZ")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json=_commit_body("pod_admin"),
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    assert "1260193629" in net["pod"]["admins"]["external_ids"]["telegram"]


# ── The single route-level gate covers the other two role branches ─────────


def test_commit_primary_role_denied_over_socket_without_header(app, tmp_path):
    """``role=primary`` (per-bot ownership change) is gated by the same
    route-level check — the gate runs before ``_commit_pairing`` dispatches
    on role, so no branch is reachable un-gated."""
    a, network_path = app
    _seed_pending(tmp_path, "team_bot_a", "telegram",
                  id_="8001234567", code="P1")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/team_bot_a/pairing/commit",
                      json=_commit_body("primary", ext_id="8001234567"),
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
    _assert_no_write(tmp_path, network_path, bot="team_bot_a",
                     ext_id="8001234567")
    # The pre-existing primary name is untouched.
    net = json.loads(network_path.read_text())
    assert net["bots"]["team_bot_a"]["primary_user"]["name"] == "Sam Sample"


def test_commit_other_role_denied_over_socket_without_header(app, tmp_path):
    """``role=other`` writes no network.json, but still admits an id into the
    bot's allowFrom — the same mutation #3642 gated on the DM routes."""
    a, network_path = app
    _seed_pending(tmp_path, "atlas", "telegram",
                  id_="7770001234", code="X1")
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/atlas/pairing/commit",
                      json=_commit_body("other", ext_id="7770001234"),
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
    _assert_no_write(tmp_path, network_path, bot="atlas", ext_id="7770001234")


def test_commit_gate_runs_after_bot_existence_check(app):
    """404-before-403 parity with every sibling handler in routes_bot_users:
    an unknown bot answers 404 even for a caller the gate would deny."""
    a, _ = app
    with a.test_client() as c:
        resp = c.post("/api/admin/bots/nonesuch/pairing/commit",
                      json=_commit_body("pod_admin"),
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 404

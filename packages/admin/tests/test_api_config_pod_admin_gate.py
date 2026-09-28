"""Capability gate on the pod-admin-tier branches of POST /api/config.

#3647 gated the identity-mutation ROUTES (``claim-admin`` / ``revoke-admin``
in routes_identity.py). It closed one door onto ``pod.admins.external_ids``.
``POST /api/config`` reached the SAME write through a second, fully ungated
one — the whole ``api_config`` handler had ZERO capability checks — so until
this landed #3647 was a fix with a bypass:

  action="podAdmins"  {"op":"add", ...}  → _external_ids.write_external_ids
                        → pod["admins"] = admins → save_network
                        i.e. byte-for-byte the pod-wide admin promotion
                        claim-admin performs. op="remove" is the inverse
                        (lockout / denial-of-service primitive).

  action="pod"        → pod.admin_passphrase / pod.primary_passphrase
                        a ONE-HOP PIVOT to the same escalation: set
                        pod.admin_passphrase, and evo.identity
                        .matches_admin_passphrase now returns True, so
                        `evo claim <phrase>` over DM reaches evo/dispatch.py's
                        IN-PROCESS claim_admin — which has no capability check
                        — and the attacker is a pod admin.
                        (test_pod_passphrase_write_is_the_claim_admin_pivot
                        proves that hop rather than asserting it.)

Why the blast radius is pod-wide rather than one bot's: roster_overlay
.resolve_role checks ``stable_id in _pod_admin_ids_for(network, platform)``
INDEPENDENT of bot_id, and capabilities.DEFAULT_ROLE_CAPABILITIES["admin"] is
["*"]. So one pod-admin claim makes the attacker resolve to ``admin`` — every
capability — on every bot, re-opening every gate #3647 / #3643 / #3642
install. test_promoted_admin_resolves_to_admin_role_on_every_bot walks that
chain end to end.

EXPOSURE. /api/config is NOT in server's _AUTH_EXEMPT_PATHS, so an untrusted
TCP peer 401s at the device gate. Two callers reach it anyway — the same two
#3647 names:
  1. the trusted evo peer uid on the admin unix socket, exempted from the
     device-cookie gate by peer_auth.device_gate_trusted_peer() on EVERY pod;
  2. an auth-DISABLED pod, where _enforce_device_auth returns None for
     everyone.

SCOPE OF THE FIX, stated precisely and measured rather than assumed: this
closes the UNIX-SOCKET caller on every pod. That is the whole of it.

  * Unauthenticated TCP on an auth-ENABLED pod was ALREADY closed, one layer
    out, by _enforce_device_auth returning 401 before the handler runs —
    test_pod_config_unauthenticated_tcp_stopped_by_device_gate records that
    rather than claiming the capability gate did it. (#3647's sibling tests
    assert 403 for this case; they mount on a bare Flask app with no device
    gate, so the outer layer is invisible to them.)
  * Header-less TCP on an auth-DISABLED pod stays OPEN by design:
    _is_authenticated_ui_request returns True for everyone there because
    is_auth_enabled is False. That is the settled inherited semantic (#3435 /
    #3638), the operator's explicit opt-out, and deliberately not
    re-litigated here.

The socket case is not a small residue: peer_auth.device_gate_trusted_peer()
exempts the trusted evo peer uid from the 401 on EVERY pod, so before this
change that caller reached the pod-admin write with nothing in its way.

Every deny test asserts the SIDE EFFECTS did not happen as well as the 403 —
a 403 that already wrote is not a fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin import external_ids as _external_ids  # noqa: E402
from evolve_admin.web.server import create_app  # noqa: E402

_SOCKET_ENV = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 0}

# The id an attacker is trying to install. Never legitimately present.
_ATTACKER = "1260193629"

# Seeded passphrases. Deliberately NOT the "charles"/"darwin" defaults that
# config._apply_pod_defaults backfills, so "unchanged" is a real observation
# rather than a value the loader would have produced anyway.
_ADMIN_PP = "seeded-admin-word"
_PRIMARY_PP = "seeded-primary-word"
_SSH_TARGET = "pod-admin-user@evolve-pod"


def _seed_network(tmp_path: Path, admins: dict | None = None) -> Path:
    """Pod with two admins (telegram 999, 555) and explicit passphrases, so
    every allow-case has something real to mutate and every deny-case has a
    concrete prior value to compare against."""
    path = tmp_path / "network.json"
    path.write_text(json.dumps({
        "networkId": "test-pod",
        "primary": "team_bot_a",
        "members": ["team_bot_a", "team_bot_c", "security_bot"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a", "role": "member",
                           "multiUser": True},
            "team_bot_c": {"user": "team_bot_c", "role": "member",
                           "multiUser": True},
            "security_bot": {"user": "security_bot", "role": "member",
                             "multiUser": False},
        },
        "sharedDir": str(tmp_path / "shared"),
        "pod": {
            "admins": {"external_ids": (
                {"telegram": ["999", "555"]} if admins is None else admins)},
            "admin_passphrase": _ADMIN_PP,
            "primary_passphrase": _PRIMARY_PP,
            "ssh_target": _SSH_TARGET,
        },
    }, indent=2))
    (tmp_path / "shared").mkdir(exist_ok=True)
    return path


def _as_authenticated_ui(app, body: dict, mp: pytest.MonkeyPatch):
    """POST as the real admin SPA on an auth-ENABLED pod.

    Unlike #3647's routes_identity tests — which mount the blueprint on a bare
    Flask app — these go through the WHOLE create_app middleware stack, so the
    authenticated-UI path must also satisfy server._enforce_csrf. That gate
    fires only for cookie-authed TCP (it exempts the unix socket and returns
    early when there is "no session to forge"), so it shapes this allow case
    and none of the deny cases.

    The double-submit token + same-origin header mirror exactly what
    static/js/core/api.js sends on every mutating request, so a pass here says
    the real SPA caller still works rather than that a gate was bypassed.
    """
    from evolve_admin.web import admin_auth as _aa
    from evolve_admin.web.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, new_csrf_token

    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: True)
    token = new_csrf_token()
    with app.test_client() as c:
        c.set_cookie(CSRF_COOKIE_NAME, token)
        return c.post("/api/config", json=body, headers={
            CSRF_HEADER_NAME: token, "Origin": "http://localhost"})


@pytest.fixture
def gate_app(tmp_path: Path):
    network_path = _seed_network(tmp_path)
    app = create_app(network_path)
    app.testing = True
    # Snapshot AFTER create_app so the whole-file backstop compares against
    # the exact bytes on disk at request time (create_app may normalize).
    baseline = json.loads(network_path.read_text())
    return app, network_path, baseline


# name, body, detail-fragment naming the action in the 403
_GATED = [
    ("podAdmins-add",
     {"action": "podAdmins",
      "podAdmins": {"op": "add", "channel": "telegram",
                    "external_id": _ATTACKER}},
     "pod admin list edit"),
    ("podAdmins-remove",
     {"action": "podAdmins",
      "podAdmins": {"op": "remove", "channel": "telegram",
                    "external_id": "555"}},
     "pod admin list edit"),
    ("pod-passphrase",
     {"action": "pod",
      "pod": {"admin_passphrase": "hunter2",
              "primary_passphrase": "hunter3",
              "ssh_target": "attacker@evil.example"}},
     "pod identity edit"),
]
_GATED_IDS = [g[0] for g in _GATED]


def _assert_no_pod_write(network_path: Path, baseline: dict) -> None:
    """Assert a denied pod-scoped mutation left every write surface untouched.

    Whole-file equality against the seeded baseline is the backstop — it
    catches ANY network.json write, including a field a future branch starts
    touching that the targeted checks below don't name. The targeted checks
    then make a failure read as "the pod admin list changed" rather than "a
    dict differs somewhere".
    """
    after = json.loads(network_path.read_text())
    assert after == baseline, (
        "denied request mutated network.json (whole-file backstop)")

    # 1. No pod-wide promotion or removal — both seeded admins intact, the
    #    attacker absent. Read through the tolerant reader, NEVER a raw
    #    .get(channel): external_ids tolerates a legacy SCALAR shape, and
    #    `x not in "8001234567"` is substring containment, not membership —
    #    a raw check would pass or fail for the wrong reason.
    admin_ids = _external_ids.ids_for(after["pod"]["admins"], "telegram")
    assert admin_ids == ["999", "555"], (
        f"pod admin list changed on a denied request: {admin_ids}")
    assert _ATTACKER not in admin_ids

    # 2. No passphrase rotation — the one-hop pivot to the same escalation.
    assert after["pod"]["admin_passphrase"] == _ADMIN_PP
    assert after["pod"]["primary_passphrase"] == _PRIMARY_PP
    # 3. ssh_target rides the same branch and is operator-facing (every alert
    #    and health hint interpolates it into a copy-paste `ssh …` string), so
    #    a denial must not move it either.
    assert after["pod"]["ssh_target"] == _SSH_TARGET


# ── Deny matrix ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_header_absent_over_socket_denied(
        gate_app, _name, body, detail):
    """THE PoC. A bot gateway on the admin unix socket that omits
    X-Requester-Identity is NOT the trusted UI — 403, and no pod write.

    Before the gate this returned 200 and landed the id in
    pod.admins.external_ids.telegram / rotated pod.admin_passphrase.
    """
    app, network_path, baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["error"] == "forbidden"
    assert "no requester identity" in resp.get_json()["detail"]
    assert detail in resp.get_json()["detail"]
    _assert_no_pod_write(network_path, baseline)


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_unauthenticated_tcp_stopped_by_device_gate(
        gate_app, _name, body, detail):
    """Header-absent over TCP with device-auth ENFORCED and no valid cookie is
    refused with **401 at the device gate**, before the handler runs at all.

    Recorded deliberately rather than asserted as a 403, because it pins which
    LAYER is doing the work. #3647's sibling tests assert 403 here, but they
    mount the blueprint on a bare Flask app that has no _enforce_device_auth
    before_request; going through the real create_app shows the outer gate
    answering first. So this transport was never the capability gate's to
    close — it was already shut — and this change's real contribution is the
    unix-socket caller (see the _over_socket tests above), which
    peer_auth.device_gate_trusted_peer() exempts from this very 401 on EVERY
    pod.

    Asserted anyway, with the no-write check, so that if a future edit ever
    exempts /api/config from the device gate the loss of layer 1 surfaces
    here rather than silently.
    """
    app, network_path, baseline = gate_app
    import evolve_admin.web.admin_auth as _aa
    # conftest disables auth globally via EVOLVE_ADMIN_AUTH_DISABLED=1, so
    # patch the module attributes to model an auth-enabled pod with no
    # paired device.
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: False)
    try:
        with app.test_client() as c:
            resp = c.post("/api/config", json=body)
        assert resp.status_code == 401, resp.get_json()
        _assert_no_pod_write(network_path, baseline)
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_malformed_header_denied(gate_app, _name, body, detail):
    """A PRESENT but unparseable X-Requester-Identity is denied on EVERY
    transport, including the authenticated-UI one — an asserted-but-broken
    identity must never fall through to the trusted-UI path (WO-H1-2)."""
    app, network_path, baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      headers={"X-Requester-Identity": "not-a-valid-identity"})
    assert resp.status_code == 403, resp.get_json()
    assert "malformed" in resp.get_json()["detail"]
    _assert_no_pod_write(network_path, baseline)


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_participant_identity_denied(
        gate_app, _name, body, detail):
    """A real, well-formed participant identity over the socket is refused.

    telegram:333 is not a pod admin, has no overlay entry, and is not any
    bot's primary — it resolves to ``participant``, which is not the
    pod-admin tier.
    """
    app, network_path, baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      headers={"X-Requester-Identity": "telegram:333"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    assert "not a pod admin" in resp.get_json()["detail"]
    _assert_no_pod_write(network_path, baseline)


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_blocked_pod_admin_denied_at_pod_scope(
        gate_app, tmp_path, _name, body, detail):
    """The sticky block is honored at pod scope — the DENY-precedence rule
    #3647's review caught, inherited here rather than re-derived.

    roster_overlay.resolve_role checks is_blocked BEFORE the pod-admin
    branch, so a blocked identity gets no capabilities on that bot. The
    pod-scoped helper has no single overlay, so it checks every bot's and
    denies an identity blocked on ANY of them.

    Without this the /api/config door would be strictly LOOSER than the
    per-bot gate: a blocked pod admin, refused everywhere else, could still
    mint a second UNBLOCKED pod-admin id here and walk straight back in on
    every bot.
    """
    from evolve_admin import roster_overlay as _ro

    app, network_path, baseline = gate_app
    shared = tmp_path / "shared"
    for bot_id in ("team_bot_a", "team_bot_c", "security_bot"):
        ov = _ro.load_overlay(shared, bot_id)
        _ro.block_identity(ov, "telegram", "999", reason="rogue",
                           by="ui:admin")
        _ro.save_overlay(shared, bot_id, ov)

    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    assert "blocked" in resp.get_json()["detail"]
    _assert_no_pod_write(network_path, baseline)


def test_blocked_on_a_SINGLE_bot_is_enough_to_deny(gate_app, tmp_path):
    """"Blocked on ANY bot" — the load-bearing half of the DENY-precedence
    rule, and the half the all-bots test above cannot see.

    That test blocks telegram:999 on all three bots, so it passes whether the
    helper iterates every overlay or just happens to check one. Blocking on
    exactly ONE bot — and deliberately NOT the first one iterated — is what
    distinguishes them: a helper that checked a single overlay, or stopped at
    the first clean one, would return 200 here.
    """
    from evolve_admin import roster_overlay as _ro

    app, network_path, baseline = gate_app
    shared = tmp_path / "shared"
    ov = _ro.load_overlay(shared, "security_bot")   # last bot in insertion order
    _ro.block_identity(ov, "telegram", "999", reason="rogue", by="ui:admin")
    _ro.save_overlay(shared, "security_bot", ov)

    with app.test_client() as c:
        resp = c.post("/api/config", json=_GATED[0][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403, resp.get_json()
    assert "security_bot" in resp.get_json()["detail"]
    _assert_no_pod_write(network_path, baseline)


# ── Allow matrix — the legitimate callers still work ───────────────────────


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_header_absent_authenticated_ui_allowed(
        gate_app, _name, body, detail):
    """Working path: header-absent over the authenticated admin-UI HTTP
    transport (device-auth enforced, VALID device cookie) → 200.

    This is the ONLY production caller of these two actions — the Pod Config
    → Settings page in the admin SPA (static/js/pages/settings.js:
    addPodAdmin / removePodAdmin at ~578/~594, savePodIdentity at ~534),
    which never sends X-Requester-Identity. Preserving it is the whole
    compatibility story for this change.
    """
    app, _network_path, _baseline = gate_app
    mp = pytest.MonkeyPatch()
    try:
        resp = _as_authenticated_ui(app, body, mp)
        assert resp.status_code == 200, resp.get_json()
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,body,detail", _GATED, ids=_GATED_IDS)
def test_pod_config_pod_admin_identity_over_socket_allowed(
        gate_app, _name, body, detail):
    """Working path: a caller presenting a VALID pod-admin identity over the
    socket is still allowed — the socket is not blanket-denied, only the
    header-absent and under-privileged cases flip to deny."""
    app, _network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()


# ── The writes the deny tests rule out are real (non-vacuity proof) ────────


def test_pod_admins_add_allowed_actually_promotes(gate_app):
    """The promotion the deny tests assert did NOT happen is a real write on
    the allow path — otherwise _assert_no_pod_write would prove nothing."""
    app, network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=_GATED[0][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    assert _ATTACKER in _external_ids.ids_for(net["pod"]["admins"], "telegram")


def test_pod_admins_remove_allowed_actually_revokes(gate_app):
    """Non-vacuity proof for the inverse (lockout) write."""
    app, network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=_GATED[1][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    net = json.loads(network_path.read_text())
    assert _external_ids.ids_for(net["pod"]["admins"], "telegram") == ["999"]


def test_pod_identity_allowed_actually_writes(gate_app):
    """Non-vacuity proof for all three fields of the pod branch."""
    app, network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=_GATED[2][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()
    pod = json.loads(network_path.read_text())["pod"]
    assert pod["admin_passphrase"] == "hunter2"
    assert pod["primary_passphrase"] == "hunter3"
    assert pod["ssh_target"] == "attacker@evil.example"


# ── The external_ids refactor the gate rode in on ──────────────────────────
#
# The podAdmins branch used to hand-roll its list edit inline; it now calls
# external_ids.add_external_id / remove_external_id, which already owned this.
# The behaviors below are the ones that were implicit in the old inline code
# and are now the called module's contract — pinned here because nothing else
# in packages/admin/tests covers podAdmins at all.


def _post_pod_admins(app, op: str, channel: str, ext_id: str):
    return app.test_client().post("/api/config", json={
        "action": "podAdmins",
        "podAdmins": {"op": op, "channel": channel, "external_id": ext_id},
    }, headers={"X-Requester-Identity": "telegram:999"},
        environ_overrides=_SOCKET_ENV)


def test_pod_admins_add_is_idempotent_and_normalizes_channel_case(gate_app):
    """Re-adding an existing id does not duplicate it, and a mixed-case
    channel lands on the canonical lower-cased key (external_ids._clean_channel
    strips + lowers, matching what the old inline `channel.strip().lower()`
    did)."""
    app, network_path, _baseline = gate_app
    assert _post_pod_admins(app, "add", "Telegram", "999").status_code == 200
    assert _post_pod_admins(app, "add", "telegram", _ATTACKER).status_code == 200
    admins = json.loads(network_path.read_text())["pod"]["admins"]
    assert _external_ids.ids_for(admins, "telegram") == ["999", "555", _ATTACKER]
    assert "Telegram" not in admins["external_ids"]


def test_pod_admins_remove_drops_an_emptied_channel_key(gate_app):
    """An emptied channel key is deleted rather than left as [] — the "no
    empty lists on disk" invariant the tolerant reader relies on.

    Order matters, and not incidentally: the requester IS telegram:999, so
    999 has to go last. See the self-demotion test below.
    """
    app, network_path, _baseline = gate_app
    for ext_id in ("555", "999"):
        assert _post_pod_admins(app, "remove", "telegram", ext_id).status_code == 200
    admins = json.loads(network_path.read_text())["pod"]["admins"]
    assert "telegram" not in admins["external_ids"]


def test_removing_your_own_admin_id_is_allowed_but_one_way(gate_app):
    """Self-demotion succeeds, and is not reversible from the same identity.

    The gate resolves the requester against the state on disk at request time,
    so a pod admin may remove themselves (they still hold the tier when the
    check runs) — but the very next call from that identity is refused,
    including an attempt to add themselves back. Worth pinning: it is the one
    way a legitimate caller can lock themselves out through this route, and it
    is also what makes `remove` a real lockout primitive in an attacker's
    hands rather than a mere annoyance.
    """
    app, network_path, _baseline = gate_app
    assert _post_pod_admins(app, "remove", "telegram", "999").status_code == 200
    assert _external_ids.ids_for(
        json.loads(network_path.read_text())["pod"]["admins"],
        "telegram") == ["555"]
    # Same identity, immediately after: no longer the pod-admin tier.
    retry = _post_pod_admins(app, "add", "telegram", "999")
    assert retry.status_code == 403, retry.get_json()
    assert "not a pod admin" in retry.get_json()["detail"]


def test_pod_admins_remove_of_an_absent_id_is_a_no_op_200(gate_app):
    """A remove that matches nothing succeeds and changes no id.

    This is the ONE place the refactor is not byte-identical to the old inline
    code: the old path always re-wrote (and so re-normalized) the whole
    external_ids map, while remove_external_id returns False and leaves the
    stored shape untouched on a pure miss. Nothing reads the raw shape without
    going through the tolerant reader — external_ids.read_external_ids on the
    Python side, and settings.js renderPodAdminsList explicitly handles the
    bare-string case on the UI side — so the only loss is an opportunistic
    normalization that used to ride along on failed removes. Pinned so the
    difference is a recorded decision rather than a silent drift.
    """
    app, network_path, _baseline = gate_app
    assert _post_pod_admins(app, "remove", "telegram", "nobody").status_code == 200
    admins = json.loads(network_path.read_text())["pod"]["admins"]
    assert _external_ids.ids_for(admins, "telegram") == ["999", "555"]


def test_pod_admins_remove_evicts_the_resolved_names_cache_entry(gate_app):
    """The one bit of the branch that is NOT external_ids' job, and so had to
    survive the refactor by hand: dropping the resolved_names entry so a later
    re-add doesn't render a stale display name."""
    app, network_path, _baseline = gate_app
    net = json.loads(network_path.read_text())
    net["pod"]["admins"]["resolved_names"] = {"telegram:555": "Sam Sample"}
    network_path.write_text(json.dumps(net, indent=2))

    assert _post_pod_admins(app, "remove", "telegram", "555").status_code == 200
    admins = json.loads(network_path.read_text())["pod"]["admins"]
    assert "telegram:555" not in admins["resolved_names"]


def test_pod_admins_tolerates_a_legacy_scalar_external_ids_shape(gate_app):
    """A hand-edited pod block may hold a bare string instead of a list. The
    add path reads it tolerantly and writes the list shape back (M1-B2)."""
    app, network_path, _baseline = gate_app
    net = json.loads(network_path.read_text())
    net["pod"]["admins"]["external_ids"] = {"telegram": "999"}   # scalar
    network_path.write_text(json.dumps(net, indent=2))

    assert _post_pod_admins(app, "add", "telegram", _ATTACKER).status_code == 200
    admins = json.loads(network_path.read_text())["pod"]["admins"]
    assert admins["external_ids"]["telegram"] == ["999", _ATTACKER]


# ── The escalation chain the gate exists to break ──────────────────────────


def test_promoted_admin_resolves_to_admin_role_on_every_bot(gate_app):
    """Second half of the PoC: why a single podAdmins add is pod-wide.

    Once the id is in pod.admins.external_ids, roster_overlay.resolve_role
    returns "admin" on EVERY bot (the pod-admin branch is bot-INDEPENDENT),
    and "admin" maps to ["*"] — so it grants bot.roster.mutate and every
    other gate #3647 / #3643 / #3642 install.
    """
    from evolve_admin import capabilities as _cap
    from evolve_admin import roster_overlay as _ro

    app, network_path, _baseline = gate_app
    with app.test_client() as c:
        assert c.post("/api/config", json=_GATED[0][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV).status_code == 200
    net = json.loads(network_path.read_text())
    shared = Path(net["sharedDir"])
    for bot_id in net["bots"]:
        overlay = _ro.load_overlay(shared, bot_id)
        role = _ro.resolve_role(overlay, net, bot_id, "telegram", _ATTACKER)
        assert role == "admin", f"{bot_id}: {role}"
        granted = _cap.resolve_role_capabilities(
            role, overlay_role_bindings=overlay.get("role_bindings") or {})
        assert "bot.roster.mutate" in granted


def test_pod_passphrase_write_is_the_claim_admin_pivot(gate_app):
    """Proves the ONE-HOP PIVOT rather than asserting it.

    Writing pod.admin_passphrase through action="pod" makes
    evo.identity.matches_admin_passphrase return True for the attacker's
    chosen word — and that predicate is the only thing standing between a DM
    `evo claim <word>` and evo/dispatch.py's IN-PROCESS claim_admin, which
    has no capability check of its own. So gating podAdmins without gating
    the passphrase write would have left the escalation reachable in two
    hops instead of one.
    """
    from evolve_admin.config import load_network
    from evolve_admin.evo import identity as _identity

    app, network_path, _baseline = gate_app
    before = load_network(network_path)
    assert not _identity.matches_admin_passphrase(before, "hunter2")

    with app.test_client() as c:
        assert c.post("/api/config", json=_GATED[2][1],
                      headers={"X-Requester-Identity": "telegram:999"},
                      environ_overrides=_SOCKET_ENV).status_code == 200

    after = load_network(network_path)
    assert _identity.matches_admin_passphrase(after, "hunter2"), (
        "the pivot this gate exists to close is not reproducible — the deny "
        "tests above would be asserting the absence of a write that never "
        "happens anyway")


# ── Ordering + bootstrap ───────────────────────────────────────────────────


def test_gate_runs_after_body_validation(gate_app):
    """Validation-before-403 parity with every sibling handler: a malformed
    body answers 400 even for a caller the gate would deny.

    Asserted as a PAIR from the SAME denied caller. The 400 alone would pass
    on pre-gate code too (it 400s for everyone), proving nothing about
    ordering; it is the contrast — same caller, same transport, only the body
    differs → 400 vs 403 — that pins the gate as sitting after validation
    rather than before it or nowhere at all.
    """
    app, _network_path, _baseline = gate_app
    with app.test_client() as c:
        bad_body = c.post("/api/config", json={
            "action": "podAdmins",
            "podAdmins": {"op": "sudo", "channel": "telegram",
                          "external_id": _ATTACKER},
        }, environ_overrides=_SOCKET_ENV)
        good_body = c.post("/api/config", json=_GATED[0][1],
                           environ_overrides=_SOCKET_ENV)
    assert bad_body.status_code == 400
    assert "op (add|remove)" in bad_body.get_json()["error"]
    assert good_body.status_code == 403


def test_unknown_action_still_400_not_403(gate_app):
    """The unknown-action 400 is not shadowed by the gate — an action that
    reaches no gated branch must still answer 400, so the error a caller
    sees still names the real problem."""
    app, _network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json={"action": "nope"},
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 400
    assert "unknown action" in resp.get_json()["error"]


def test_fresh_pod_bootstrap_still_works_from_the_ui(tmp_path):
    """The bootstrap case a pod-scoped gate could plausibly have bricked: a
    FRESH pod with ZERO pod admins, where "requester must already be a pod
    admin" has nobody to admit.

    It still works, because the admin UI sends no X-Requester-Identity and is
    trusted by the same header-absent rule _check_capability applies. (The
    real first-admin paths — the DM `evo claim <passphrase>` flow in
    evo.dispatch and the setup wizard in evo.wizard.engine — call
    evo.identity.claim_admin IN-PROCESS behind the pod admin passphrase and
    never touch this route at all; and `evolve-admin` sets the passphrases
    through cli.py's in-process write, not over HTTP.)
    """
    network_path = _seed_network(tmp_path, admins={})
    app = create_app(network_path)
    app.testing = True
    mp = pytest.MonkeyPatch()
    try:
        resp = _as_authenticated_ui(app, {
            "action": "podAdmins",
            "podAdmins": {"op": "add", "channel": "telegram",
                          "external_id": "1111"},
        }, mp)
        assert resp.status_code == 200, resp.get_json()
    finally:
        mp.undo()
    net = json.loads(network_path.read_text())
    assert "1111" in _external_ids.ids_for(net["pod"]["admins"], "telegram")


def test_fresh_pod_bootstrap_still_denied_from_the_socket(tmp_path):
    """The other half of the bootstrap story: a fresh pod does NOT become an
    open door. With zero pod admins, a header-less socket caller is still
    denied — "no admins yet" must not degrade to "anyone may claim"."""
    network_path = _seed_network(tmp_path, admins={})
    app = create_app(network_path)
    app.testing = True
    with app.test_client() as c:
        resp = c.post("/api/config", json=_GATED[0][1],
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 403
    net = json.loads(network_path.read_text())
    assert _external_ids.ids_for(net["pod"]["admins"], "telegram") == []


# ── Explicitly NOT gated (reported, not fixed — see the PR body) ───────────


@pytest.mark.parametrize("body", [
    {"action": "timezone", "timezone": "America/New_York"},
    {"action": "heal", "heal": {"windowHours": 3}},
    {"action": "alerts", "alerts": {"enabled": True}},
])
def test_other_config_actions_remain_ungated(gate_app, body):
    """Scope boundary: the other api_config action branches (alerts,
    appTesting, backup, heal, classifiers, security, timezone) are
    deliberately left ungated by this change — see the PR body. A
    handler-level gate over all of them was considered and rejected without
    a full per-action caller audit, because it could brick the setup wizard
    and install flow.

    Asserted so a future edit that silently widens THIS change's scope has to
    do so deliberately. It is a statement about today's boundary, not a claim
    that leaving them ungated is correct forever.
    """
    app, _network_path, _baseline = gate_app
    with app.test_client() as c:
        resp = c.post("/api/config", json=body,
                      environ_overrides=_SOCKET_ENV)
    assert resp.status_code == 200, resp.get_json()

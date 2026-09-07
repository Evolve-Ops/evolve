"""tests/test_evo_identity.py — primary-user identity claim + read.

Spec: internal/spec-evo-wizard-2026-05-05.md.

Exercises the writer (``evo.identity.claim_primary``), the admin endpoints
(``/api/evo/identity/<bot>`` and ``/api/evo/identity/<bot>/claim``), and the
CLI subcommands (``evolve-admin evo claim-primary`` and
``evolve-admin evo show-identity``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Writer
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_creates_primary_block():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    block = claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    assert block == {"external_ids": {"slack": ["U123ABC"]}}
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U123ABC"]


def test_claim_idempotent_for_same_value():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    # Re-claim the same channel + value: no error
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U123ABC"]


def test_claim_multiple_channels_accumulate():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    claim_primary(network, "team_bot_a", channel="telegram", external_id="12345")
    ids = network["bots"]["team_bot_a"]["primary_user"]["external_ids"]
    assert ids == {"slack": ["U123ABC"], "telegram": ["12345"]}


def test_claim_conflict_without_force_raises():
    from evolve_admin.evo.identity import claim_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    with pytest.raises(ClaimError) as ei:
        claim_primary(network, "team_bot_a", channel="slack", external_id="UDIFFERENT")
    assert "already recorded" in str(ei.value)


def test_claim_force_overwrites():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    claim_primary(
        network, "team_bot_a", channel="slack", external_id="UDIFFERENT", force=True
    )
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["UDIFFERENT"]


def test_claim_rejects_non_member():
    from evolve_admin.evo.identity import claim_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    with pytest.raises(ClaimError) as ei:
        claim_primary(network, "unknown", channel="slack", external_id="X")
    assert "not a pod member" in str(ei.value)


def test_claim_accepts_primary_bot_not_listed_in_members():
    """`primary` is a sibling of `members` — the primary never appears in
    the members list on current-schema pods, yet it is the bot most
    installs claim FIRST. A members-only guard made it unclaimable
    (fresh-install bug: "bot 'evo' is not a pod member — claim aborted")."""
    from evolve_admin.evo.identity import claim_primary

    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    block = claim_primary(network, "evo", channel="telegram", external_id="4567890")
    assert block["external_ids"]["telegram"] == ["4567890"]


def test_passphrase_override_accepts_primary_bot():
    from evolve_admin.evo.identity import set_bot_primary_passphrase

    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    set_bot_primary_passphrase(network, "evo", "newton")
    assert network["bots"]["evo"]["primary_passphrase"] == "newton"


def test_claim_rejection_message_names_pod_bots():
    """The error must not read like the pod is misconfigured — it lists the
    full set of claimable bots (primary included), not just members."""
    from evolve_admin.evo.identity import claim_primary, ClaimError

    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    with pytest.raises(ClaimError) as ei:
        claim_primary(network, "ghost", channel="slack", external_id="X")
    msg = str(ei.value)
    assert "evo" in msg and "schoolassistant" in msg


def test_claim_rejects_empty_inputs():
    from evolve_admin.evo.identity import claim_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    with pytest.raises(ClaimError):
        claim_primary(network, "team_bot_a", channel="", external_id="X")
    with pytest.raises(ClaimError):
        claim_primary(network, "team_bot_a", channel="slack", external_id="")
    with pytest.raises(ClaimError):
        claim_primary(network, "team_bot_a", channel="slack", external_id="   ")


def test_claim_lowercases_channel():
    """Channel names are normalized to lowercase so 'Slack' and 'slack' don't
    create two entries."""
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="SLACK", external_id="U123ABC")
    assert "slack" in network["bots"]["team_bot_a"]["primary_user"]["external_ids"]
    assert "SLACK" not in network["bots"]["team_bot_a"]["primary_user"]["external_ids"]


def test_claim_pod_user_persisted_when_provided():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(
        network, "team_bot_a", channel="slack", external_id="U1",
        pod_user="pod_admin_user",
    )
    assert network["bots"]["team_bot_a"]["primary_user"]["pod_user"] == "pod_admin_user"


def test_resolve_role_round_trip():
    """After claim, resolve_role correctly distinguishes primary vs secondary."""
    from evolve_admin.evo.identity import claim_primary, resolve_role

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")

    assert resolve_role(network, "team_bot_a", "slack", "U123ABC") == "primary"
    assert resolve_role(network, "team_bot_a", "slack", "U999XYZ") == "secondary"
    # Unrecorded channel — falls back to primary (v1 fallback)
    assert resolve_role(network, "team_bot_a", "telegram", "anyone") == "primary"


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    """Mount evo_routes against a tmp network.json. Patches save_network so
    the test doesn't try to sudo / chown / chmod the file."""
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {"members": ["team_bot_a", "admin_bot"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(evo_routes, "save_network", _atomic_save)
    monkeypatch.setattr(_cfg, "save_network", _atomic_save)

    app = Flask(__name__)
    app.config["TESTING"] = True
    evo_routes.register_evo_routes(app, network_path)
    return app, network_path


def test_route_get_identity_empty(evo_app):
    app, _ = evo_app
    client = app.test_client()
    r = client.get("/api/evo/identity/team_bot_a")
    assert r.status_code == 200
    data = r.get_json()
    assert data["bot_id"] == "team_bot_a"
    assert data["primary_user"]["external_ids"] == {}
    assert data["primary_user"]["pod_user"] is None
    assert data["is_member"] is True


def test_route_claim_succeeds(evo_app):
    app, network_path = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/identity/team_bot_a/claim",
        json={"channel": "slack", "external_id": "U123ABC"},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["primary_user"]["external_ids"]["slack"] == ["U123ABC"]

    # Verify on disk
    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U123ABC"]


def test_route_claim_conflict_returns_409(evo_app):
    app, _ = evo_app
    client = app.test_client()
    client.post(
        "/api/evo/identity/team_bot_a/claim",
        json={"channel": "slack", "external_id": "U123ABC"},
    )
    r = client.post(
        "/api/evo/identity/team_bot_a/claim",
        json={"channel": "slack", "external_id": "UDIFFERENT"},
    )
    assert r.status_code == 409
    assert "already recorded" in r.get_json()["error"]


def test_route_claim_force_overwrites(evo_app):
    app, _ = evo_app
    client = app.test_client()
    client.post(
        "/api/evo/identity/team_bot_a/claim",
        json={"channel": "slack", "external_id": "U123ABC"},
    )
    r = client.post(
        "/api/evo/identity/team_bot_a/claim",
        json={"channel": "slack", "external_id": "UDIFFERENT", "force": True},
    )
    assert r.status_code == 200


def test_route_claim_non_member_returns_400(evo_app):
    app, _ = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/identity/unknown/claim",
        json={"channel": "slack", "external_id": "U1"},
    )
    assert r.status_code == 400
    assert "not a pod member" in r.get_json()["error"]


def test_route_claim_missing_fields_returns_400(evo_app):
    app, _ = evo_app
    client = app.test_client()
    r = client.post("/api/evo/identity/team_bot_a/claim", json={})
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner_with_network(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import config as _cfg

    network = {"members": ["team_bot_a", "admin_bot"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_cfg, "save_network", _atomic_save)
    return CliRunner(), network_path


def test_cli_claim_primary(cli_runner_with_network):
    runner, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "team_bot_a",
        "--channel", "slack", "--external-id", "U123ABC",
    ])
    assert r.exit_code == 0, r.output
    assert "claimed" in r.output
    assert "U123ABC" in r.output

    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U123ABC"]


def test_cli_claim_conflict_exits_nonzero(cli_runner_with_network):
    runner, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "team_bot_a",
        "--channel", "slack", "--external-id", "U123ABC",
    ])
    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "team_bot_a",
        "--channel", "slack", "--external-id", "UDIFFERENT",
    ])
    assert r.exit_code != 0
    assert "already recorded" in r.output


def test_cli_show_identity_all_bots(cli_runner_with_network):
    runner, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "team_bot_a",
        "--channel", "slack", "--external-id", "U123ABC",
    ])
    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "show-identity",
    ])
    assert r.exit_code == 0, r.output
    assert "team_bot_a" in r.output
    assert "U123ABC" in r.output
    assert "admin_bot" in r.output
    assert "no primary recorded" in r.output


def test_cli_show_identity_single_bot(cli_runner_with_network):
    runner, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "team_bot_a",
        "--channel", "slack", "--external-id", "U123ABC",
    ])
    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "show-identity", "team_bot_a",
    ])
    assert r.exit_code == 0, r.output
    assert "U123ABC" in r.output
    assert "admin_bot" not in r.output


# ─────────────────────────────────────────────────────────────────────────────
# Capability gate on the evo identity-MUTATION routes (CBR-1.2 class)
# ─────────────────────────────────────────────────────────────────────────────
#
# ``/claim`` and ``/reassign`` write ``bots.<id>.primary_user`` — a per-bot
# ownership change — and had ZERO capability checks. Neither path is in
# server's ``_AUTH_EXEMPT_PATHS``, so an untrusted TCP peer 401s at the device
# gate, but the trusted evo peer uid on the admin unix socket is exempted from
# that gate by ``peer_auth.device_gate_trusted_peer()`` on EVERY pod, and on an
# auth-DISABLED pod ``_enforce_device_auth`` returns None for everyone.
#
# ``reassign``'s ``from`` check is NOT an authorization check: it proves the
# caller knows the current primary, which is readable from
# ``GET /api/evo/identity/<bot_id>`` on the same transports.
#
# Every deny test asserts the write did NOT happen as well as the 403.

_EVO_SOCKET_ENV = {"REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": 0}
_EVO_ATTACKER = "U_ATTACKER"


@pytest.fixture
def evo_gate_app(tmp_path, monkeypatch):
    """evo routes over a network with a pod admin (telegram 999) and a bot
    whose primary is already recorded (so ``reassign`` has a real ``from``)."""
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {
        "members": ["team_bot_a", "admin_bot"],
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a": {
                "primary_user": {"external_ids": {"slack": ["U_OWNER"]}},
            },
            "admin_bot": {},
        },
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(evo_routes, "save_network", _atomic_save)
    monkeypatch.setattr(_cfg, "save_network", _atomic_save)

    app = Flask(__name__)
    app.config["TESTING"] = True
    evo_routes.register_evo_routes(app, network_path)
    return app, network_path


# name, path, body
_EVO_GATED = [
    ("claim", "/api/evo/identity/admin_bot/claim",
     {"channel": "slack", "external_id": _EVO_ATTACKER}),
    ("reassign", "/api/evo/identity/team_bot_a/reassign",
     {"channel": "slack", "from": "U_OWNER", "to": _EVO_ATTACKER}),
]
_EVO_GATED_IDS = [r[0] for r in _EVO_GATED]


def _assert_no_evo_write(network_path) -> None:
    """A denied evo identity mutation must leave primary_user alone.

    Read through the tolerant reader, never a raw ``.get(channel)``:
    external_ids tolerates a legacy SCALAR shape, and ``x not in "U_OWNERX"``
    would be substring containment rather than membership.
    """
    from evolve_admin import external_ids as _ext

    after = json.loads(network_path.read_text())
    for bot_id, cfg in (after.get("bots") or {}).items():
        assert _EVO_ATTACKER not in _ext.ids_for(
            cfg.get("primary_user"), "slack"), (
            f"attacker landed in {bot_id}.primary_user on a denied request")
    # The legitimate owner is still recorded — reassign did not transfer.
    assert _ext.ids_for(
        after["bots"]["team_bot_a"].get("primary_user"), "slack") == [
        "U_OWNER"]


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_header_absent_over_socket_denied(
        evo_gate_app, _name, path, body):
    """The evo peer on the admin unix socket omitting X-Requester-Identity is
    not the trusted UI — 403, and no ownership change happened."""
    app, network_path = evo_gate_app
    r = app.test_client().post(path, json=body,
                               environ_overrides=_EVO_SOCKET_ENV)
    assert r.status_code == 403, r.get_json()
    assert r.get_json()["error"] == "forbidden"
    assert "no requester identity" in r.get_json()["detail"]
    _assert_no_evo_write(network_path)


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_header_absent_unauthenticated_tcp_denied(
        evo_gate_app, _name, path, body):
    """Header-absent over TCP with device-auth ENFORCED and no valid cookie →
    403 (conftest disables auth globally, so patch the module attributes)."""
    app, network_path = evo_gate_app
    import evolve_admin.web.admin_auth as _aa
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: False)
    try:
        r = app.test_client().post(path, json=body)
        assert r.status_code == 403, r.get_json()
        assert "no requester identity" in r.get_json()["detail"]
        _assert_no_evo_write(network_path)
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_malformed_header_denied(
        evo_gate_app, _name, path, body):
    """A PRESENT but unparseable header is denied on EVERY transport,
    including the authenticated-UI one (WO-H1-2)."""
    app, network_path = evo_gate_app
    r = app.test_client().post(
        path, json=body,
        headers={"X-Requester-Identity": "not-a-valid-identity"})
    assert r.status_code == 403, r.get_json()
    assert "malformed" in r.get_json()["detail"]
    _assert_no_evo_write(network_path)


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_participant_identity_denied(
        evo_gate_app, _name, path, body):
    """A well-formed participant identity resolves to ``participant``, which
    grants no bot.* built-ins — denied."""
    app, network_path = evo_gate_app
    r = app.test_client().post(
        path, json=body,
        headers={"X-Requester-Identity": "telegram:333"},
        environ_overrides=_EVO_SOCKET_ENV)
    assert r.status_code == 403, r.get_json()
    assert "bot.roster.mutate" in r.get_json()["detail"]
    _assert_no_evo_write(network_path)


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_header_absent_authenticated_ui_allowed(
        evo_gate_app, _name, path, body):
    """Working path preserved: header-absent over the authenticated admin-UI
    HTTP transport (device-auth enforced, VALID device cookie) → 200."""
    app, _ = evo_gate_app
    import evolve_admin.web.admin_auth as _aa
    mp = pytest.MonkeyPatch()
    mp.setattr(_aa, "is_auth_enabled", lambda _shared: True)
    mp.setattr(_aa, "verify_device_token", lambda _shared, _tok: True)
    try:
        r = app.test_client().post(path, json=body)
        assert r.status_code == 200, r.get_json()
    finally:
        mp.undo()


@pytest.mark.parametrize("_name,path,body", _EVO_GATED, ids=_EVO_GATED_IDS)
def test_evo_identity_pod_admin_identity_over_socket_allowed(
        evo_gate_app, _name, path, body):
    """Working path preserved: a VALID pod-admin identity over the socket is
    still allowed — the socket is not blanket-denied."""
    app, _ = evo_gate_app
    r = app.test_client().post(
        path, json=body,
        headers={"X-Requester-Identity": "telegram:999"},
        environ_overrides=_EVO_SOCKET_ENV)
    assert r.status_code == 200, r.get_json()


def test_evo_claim_allowed_actually_writes(evo_gate_app):
    """Non-vacuity proof: the write the deny tests rule out is real."""
    app, network_path = evo_gate_app
    from evolve_admin import external_ids as _ext
    r = app.test_client().post(
        "/api/evo/identity/admin_bot/claim",
        json={"channel": "slack", "external_id": _EVO_ATTACKER},
        headers={"X-Requester-Identity": "telegram:999"},
        environ_overrides=_EVO_SOCKET_ENV)
    assert r.status_code == 200, r.get_json()
    after = json.loads(network_path.read_text())
    assert _ext.ids_for(
        after["bots"]["admin_bot"].get("primary_user"), "slack") == [
        _EVO_ATTACKER]


def test_evo_reassign_allowed_actually_transfers(evo_gate_app):
    """Non-vacuity proof for reassign's ownership transfer."""
    app, network_path = evo_gate_app
    from evolve_admin import external_ids as _ext
    r = app.test_client().post(
        "/api/evo/identity/team_bot_a/reassign",
        json={"channel": "slack", "from": "U_OWNER", "to": _EVO_ATTACKER},
        headers={"X-Requester-Identity": "telegram:999"},
        environ_overrides=_EVO_SOCKET_ENV)
    assert r.status_code == 200, r.get_json()
    after = json.loads(network_path.read_text())
    assert _ext.ids_for(
        after["bots"]["team_bot_a"].get("primary_user"), "slack") == [
        _EVO_ATTACKER]


def test_evo_identity_reads_remain_ungated(evo_gate_app):
    """Scope boundary: the GET routes are READS and are deliberately left
    ungated by this change (reported, not fixed — see the PR body)."""
    app, _ = evo_gate_app
    c = app.test_client()
    assert c.get("/api/evo/identity/team_bot_a",
                 environ_overrides=_EVO_SOCKET_ENV).status_code == 200
    assert c.get("/api/evo/identity/audit",
                 environ_overrides=_EVO_SOCKET_ENV).status_code == 200

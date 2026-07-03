"""tests/test_evo_identity.py — primary-user identity claim + read.

Spec: docs/spec-evo-wizard-2026-05-05.md.

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
    assert block == {"external_ids": {"slack": "U123ABC"}}
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U123ABC"


def test_claim_idempotent_for_same_value():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    # Re-claim the same channel + value: no error
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U123ABC"


def test_claim_multiple_channels_accumulate():
    from evolve_admin.evo.identity import claim_primary

    network = {"members": ["team_bot_a"], "bots": {}}
    claim_primary(network, "team_bot_a", channel="slack", external_id="U123ABC")
    claim_primary(network, "team_bot_a", channel="telegram", external_id="12345")
    ids = network["bots"]["team_bot_a"]["primary_user"]["external_ids"]
    assert ids == {"slack": "U123ABC", "telegram": "12345"}


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
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "UDIFFERENT"


def test_claim_rejects_non_member():
    from evolve_admin.evo.identity import claim_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    with pytest.raises(ClaimError) as ei:
        claim_primary(network, "unknown", channel="slack", external_id="X")
    assert "not a pod member" in str(ei.value)


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
    assert data["primary_user"]["external_ids"]["slack"] == "U123ABC"

    # Verify on disk
    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U123ABC"


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
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U123ABC"


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

"""tests/test_evo_claim.py — passphrase-based admin/primary self-claim.

Spec: internal/spec-evo-wizard-2026-05-05.md §3.2.2.

Exercises:
  * 3-state ``resolve_role`` priority (admin > primary > secondary)
  * ``claim_admin`` writer (idempotent, multi-channel, multi-admin)
  * ``matches_admin_passphrase`` / ``matches_primary_passphrase`` (case-insensitive)
  * ``evo claim`` dispatch handler — token parsing, single/double tokens,
    order independence, admin-only / primary-only / both, conflict refusal,
    network_dirty signaling, audit log writes
  * Admin role bypasses ``available_to`` so admins can run primary-only commands
  * CLI: ``evo set-passphrase`` and ``evo show-admins``
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
# Identity — 3-state resolution + claim_admin writer
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_role_returns_admin_first():
    from evolve_admin.evo.identity import resolve_role

    network = {
        "members": ["team_bot_a"],
        "pod": {
            "admins": {"external_ids": {"telegram": ["999"]}, "pod_users": ["pod_admin"]},
        },
        "bots": {
            "team_bot_a": {"primary_user": {"external_ids": {"telegram": "12345"}}}
        },
    }
    # Admin wins over primary even if external_id also matches primary
    assert resolve_role(network, "team_bot_a", "telegram", "999") == "admin"
    # Recorded primary
    assert resolve_role(network, "team_bot_a", "telegram", "12345") == "primary"
    # Neither admin nor recorded primary
    assert resolve_role(network, "team_bot_a", "telegram", "77777") == "secondary"


def test_resolve_role_secondary_only_when_primary_recorded():
    """v1 fallback: an unconfigured bot returns primary, not secondary."""
    from evolve_admin.evo.identity import resolve_role

    assert resolve_role({"members": ["team_bot_a"]}, "team_bot_a", "telegram", "anyone") == "primary"


def test_claim_admin_idempotent_and_multi_channel():
    from evolve_admin.evo.identity import claim_admin

    network = {"members": ["team_bot_a"]}
    claim_admin(network, channel="telegram", external_id="999")
    claim_admin(network, channel="telegram", external_id="999")  # idempotent
    claim_admin(network, channel="slack", external_id="U-POD_ADMIN")
    claim_admin(network, channel="telegram", external_id="888")  # 2nd admin

    admins = network["pod"]["admins"]["external_ids"]
    assert admins["telegram"] == ["999", "888"]
    assert admins["slack"] == ["U-POD_ADMIN"]


def test_claim_admin_records_pod_user():
    from evolve_admin.evo.identity import claim_admin

    network = {"members": ["team_bot_a"]}
    claim_admin(network, channel="telegram", external_id="999", pod_user="pod_admin")
    assert network["pod"]["admins"]["pod_users"] == ["pod_admin"]
    # Re-claiming with same pod_user is idempotent
    claim_admin(network, channel="telegram", external_id="999", pod_user="pod_admin")
    assert network["pod"]["admins"]["pod_users"] == ["pod_admin"]


def test_passphrase_match_case_insensitive():
    from evolve_admin.evo.identity import (
        matches_admin_passphrase, matches_primary_passphrase,
    )

    network = {"pod": {"admin_passphrase": "Charles", "primary_passphrase": "DARWIN"}}
    assert matches_admin_passphrase(network, "charles")
    assert matches_admin_passphrase(network, "CHARLES")
    assert matches_admin_passphrase(network, "  charles  ")
    assert matches_primary_passphrase(network, "darwin")
    assert not matches_admin_passphrase(network, "darwin")
    assert not matches_primary_passphrase(network, "")


def test_passphrase_match_with_no_pod_config():
    """Defensive: if pod block is missing entirely, no passphrase matches."""
    from evolve_admin.evo.identity import matches_admin_passphrase

    assert matches_admin_passphrase({}, "anything") is False


def test_load_network_backfills_passphrases(tmp_path):
    """Existing installs predate the passphrase fields; load_network
    backfills the defaults so `evo claim` and the wizard challenge can
    match the values doc'd in `_default_network()`."""
    from evolve_admin import config as _cfg

    pre_existing = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {"admins": {"external_ids": {}, "pod_users": []}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(pre_existing))

    data = _cfg.load_network(network_path)
    assert data["pod"]["admin_passphrase"] == "charles"
    assert data["pod"]["primary_passphrase"] == "darwin"
    # Existing admins block preserved
    assert data["pod"]["admins"] == {"external_ids": {}, "pod_users": []}


def test_load_network_preserves_custom_passphrases(tmp_path):
    """Backfill must not stomp values an operator has already set."""
    from evolve_admin import config as _cfg

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "members": ["team_bot_a"],
        "pod": {"admin_passphrase": "newton", "primary_passphrase": "tesla"},
    }))
    data = _cfg.load_network(network_path)
    assert data["pod"]["admin_passphrase"] == "newton"
    assert data["pod"]["primary_passphrase"] == "tesla"


def test_load_network_backfills_when_pod_block_missing(tmp_path):
    from evolve_admin import config as _cfg

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"members": ["team_bot_a"]}))
    data = _cfg.load_network(network_path)
    assert data["pod"]["admin_passphrase"] == "charles"
    assert data["pod"]["primary_passphrase"] == "darwin"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch — `evo claim`
# ─────────────────────────────────────────────────────────────────────────────


def _base_network(tmp_path):
    return {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {},
    }


def _claim(network, *, sender_external_id, raw_text):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id=sender_external_id, raw_text=raw_text,
    )


def test_claim_bare_returns_usage(tmp_path):
    r = _claim(_base_network(tmp_path), sender_external_id="999",
               raw_text="evo claim")
    assert "Usage" in (r.system_append or "")
    assert r.network_dirty is False


def test_claim_bare_already_admin_short_circuits(tmp_path):
    """When the sender is already a pod admin, bare `evo claim` should
    tell them so rather than dumping the passphrase usage — the prior
    behavior left admins confused about what passphrase to type."""
    network = _base_network(tmp_path)
    # Pre-record this sender as admin
    _claim(network, sender_external_id="999", raw_text="evo claim charles")
    r = _claim(network, sender_external_id="999", raw_text="evo claim")
    body = r.system_append or ""
    assert "already" in body.lower() and "admin" in body.lower()
    assert "Usage" not in body
    assert r.network_dirty is False


def test_claim_bare_already_primary_short_circuits(tmp_path):
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345", raw_text="evo claim darwin")
    r = _claim(network, sender_external_id="12345", raw_text="evo claim")
    body = r.system_append or ""
    assert "already" in body.lower() and "primary" in body.lower()
    assert "Usage" not in body


def test_claim_bare_already_both_short_circuits(tmp_path):
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345",
           raw_text="evo claim charles darwin")
    r = _claim(network, sender_external_id="12345", raw_text="evo claim")
    body = (r.system_append or "").lower()
    assert "already" in body
    assert "admin" in body and "primary" in body


def test_claim_too_many_tokens(tmp_path):
    r = _claim(_base_network(tmp_path), sender_external_id="999",
               raw_text="evo claim a b c")
    assert "Too many tokens" in (r.system_append or "")


def test_claim_unrecognized_passphrase(tmp_path):
    r = _claim(_base_network(tmp_path), sender_external_id="999",
               raw_text="evo claim wrong")
    assert "don't recognize" in (r.system_append or "")
    assert r.network_dirty is False


def test_claim_admin_only(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id="999", raw_text="evo claim charles")
    assert r.network_dirty is True
    assert "admin: recorded" in (r.system_append or "")
    assert network["pod"]["admins"]["external_ids"]["telegram"] == ["999"]
    # Primary block untouched — bot stays unowned
    assert network["bots"].get("team_bot_a", {}).get("primary_user") is None


def test_claim_primary_only(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id="12345", raw_text="evo claim darwin")
    assert r.network_dirty is True
    assert "primary: recorded" in (r.system_append or "")
    assert (
        network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"]
        == ["12345"]
    )
    assert network["pod"].get("admins", {}).get("external_ids", {}) == {}


def test_claim_both_in_one_message(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id="12345",
               raw_text="evo claim charles darwin")
    body = r.system_append or ""
    assert "admin: recorded" in body
    assert "primary: recorded" in body
    assert r.network_dirty is True
    assert network["pod"]["admins"]["external_ids"]["telegram"] == ["12345"]
    assert (
        network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"]
        == ["12345"]
    )


def test_claim_order_independent(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id="12345",
               raw_text="evo claim darwin charles")
    body = r.system_append or ""
    assert "admin: recorded" in body
    assert "primary: recorded" in body


def test_claim_idempotent(tmp_path):
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345",
           raw_text="evo claim charles darwin")
    r = _claim(network, sender_external_id="12345",
               raw_text="evo claim charles darwin")
    body = (r.system_append or "").lower()
    assert "already" in body
    assert r.network_dirty is False


def test_claim_primary_conflict_when_someone_else_holds_it(tmp_path):
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345", raw_text="evo claim darwin")
    # Different sender tries to claim primary
    r = _claim(network, sender_external_id="77777", raw_text="evo claim darwin")
    assert "another user is already recorded" in (r.system_append or "")
    assert r.network_dirty is False
    # Existing primary unchanged
    assert (
        network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"]
        == ["12345"]
    )


def test_claim_case_insensitive(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id="999",
               raw_text="evo claim CHARLES")
    assert "admin: recorded" in (r.system_append or "")


def test_claim_anonymous_caller_handled(tmp_path):
    network = _base_network(tmp_path)
    r = _claim(network, sender_external_id=None, raw_text="evo claim charles")
    assert "can't tell who you are" in (r.system_append or "")
    assert r.network_dirty is False


def test_claim_writes_audit_log(tmp_path):
    from evolve_admin.evo import audit
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345",
           raw_text="evo claim charles darwin")
    events = audit.read_events(tmp_path)
    actions = [e["action"] for e in events]
    assert "claim_admin" in actions
    assert "claim" in actions  # primary claim
    admin_event = next(e for e in events if e["action"] == "claim_admin")
    assert admin_event["bot_id"] == "<pod>"
    assert admin_event["channel"] == "telegram"
    assert admin_event["to"] == "12345"


def test_admin_bypasses_available_to_on_primary_commands(tmp_path):
    """An admin should be able to run `evo better` even though it's
    listed as PRIMARY-only — admin is a strict superset.

    ``evo better`` routes to the rec_pending dispatch (mode=speak).
    The point is admin reaches that branch at all instead of getting
    a "not available" rejection."""
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="999", raw_text="evo claim charles")
    r = _claim(network, sender_external_id="999", raw_text="evo better")
    assert r.mode == "speak"
    assert "isn't available" not in (r.system_append or "")


def test_secondary_blocked_from_primary_commands(tmp_path):
    """Confirms admin-bypass is admin-only, not generally permissive."""
    network = _base_network(tmp_path)
    _claim(network, sender_external_id="12345", raw_text="evo claim darwin")
    # Now 77777 is neither admin nor recorded primary → secondary
    r = _claim(network, sender_external_id="77777", raw_text="evo better")
    assert r.mode == "speak"
    assert "isn't available" in (r.system_append or "")


# ─────────────────────────────────────────────────────────────────────────────
# Flask route — confirm dispatch.network_dirty triggers save_network
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {},
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


def test_route_dispatch_claim_persists_to_disk(evo_app):
    app, network_path = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a", "channel": "telegram",
            "sender_external_id": "12345", "raw_text": "evo claim charles darwin",
        },
    )
    assert r.status_code == 200, r.get_json()
    on_disk = json.loads(network_path.read_text())
    assert on_disk["pod"]["admins"]["external_ids"]["telegram"] == ["12345"]
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"] == ["12345"]


def test_route_dispatch_unrecognized_passphrase_does_not_persist(evo_app):
    app, network_path = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a", "channel": "telegram",
            "sender_external_id": "12345", "raw_text": "evo claim wrong",
        },
    )
    assert r.status_code == 200
    on_disk = json.loads(network_path.read_text())
    # No admin block written
    assert "admins" not in on_disk.get("pod", {}) or \
           on_disk["pod"]["admins"].get("external_ids") in ({}, None)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner_with_network(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import config as _cfg

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_cfg, "save_network", _atomic_save)
    return CliRunner(), tmp_path, network_path


def test_cli_set_passphrase_admin_and_primary(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-passphrase",
        "--admin", "newton", "--primary", "tesla",
    ])
    assert r.exit_code == 0, r.output
    on_disk = json.loads(network_path.read_text())
    assert on_disk["pod"]["admin_passphrase"] == "newton"
    assert on_disk["pod"]["primary_passphrase"] == "tesla"


def test_cli_set_passphrase_partial_update(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-passphrase", "--admin", "newton",
    ])
    assert r.exit_code == 0
    on_disk = json.loads(network_path.read_text())
    assert on_disk["pod"]["admin_passphrase"] == "newton"
    assert on_disk["pod"]["primary_passphrase"] == "darwin"  # untouched


def test_cli_set_passphrase_warns_when_same(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "set-passphrase",
        "--admin", "darwin", "--primary", "darwin",
    ])
    assert r.exit_code == 0
    assert "same" in r.output.lower()


def test_cli_set_passphrase_no_args_errors(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "set-passphrase",
    ])
    assert r.exit_code != 0


def test_cli_show_admins_empty(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "show-admins",
    ])
    assert r.exit_code == 0
    assert "no pod admins recorded" in r.output


def test_cli_show_admins_after_claim(cli_runner_with_network):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main
    from evolve_admin.evo.identity import claim_admin
    from evolve_admin.config import save_network, load_network

    network = load_network(network_path)
    claim_admin(network, channel="telegram", external_id="999", pod_user="pod_admin")
    save_network(network, network_path)

    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "show-admins",
    ])
    assert r.exit_code == 0
    assert "999" in r.output
    assert "telegram" in r.output
    assert "pod_admin" in r.output

"""tests/test_evo_reassign.py — primary-user reassignment + audit log.

Spec: internal/spec-evo-wizard-2026-05-05.md.

Exercises the writer (``evo.identity.reassign_primary``), the audit log
(``evo.audit``), the admin endpoints (``/api/evo/identity/<bot>/reassign``
and the audit GET), and the CLI subcommands (``evolve-admin evo
reassign-primary`` and ``evolve-admin evo show-audit``).
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
# reassign_primary writer
# ─────────────────────────────────────────────────────────────────────────────


def _seeded_network():
    return {
        "members": ["team_bot_a", "admin_bot"],
        "bots": {
            "team_bot_a": {
                "primary_user": {"external_ids": {"slack": "U123ABC"}}
            }
        },
    }


def test_reassign_succeeds_when_from_matches():
    from evolve_admin.evo.identity import reassign_primary

    network = _seeded_network()
    block = reassign_primary(
        network, "team_bot_a", channel="slack",
        from_external_id="U123ABC", to_external_id="U456DEF",
    )
    assert block["external_ids"]["slack"] == ["U456DEF"]
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U456DEF"]


def test_reassign_rejects_mismatched_from():
    from evolve_admin.evo.identity import reassign_primary, ClaimError

    network = _seeded_network()
    with pytest.raises(ClaimError) as ei:
        reassign_primary(
            network, "team_bot_a", channel="slack",
            from_external_id="WRONG", to_external_id="U456DEF",
        )
    msg = str(ei.value)
    assert "U123ABC" in msg
    assert "WRONG" in msg
    # Original value untouched — including its legacy scalar shape, since a
    # rejected reassign must not rewrite the file at all (M1-B2: writers
    # normalize opportunistically, never as a side effect of a failed call).
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == "U123ABC"


def test_reassign_rejects_when_no_primary_recorded():
    from evolve_admin.evo.identity import reassign_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    with pytest.raises(ClaimError) as ei:
        reassign_primary(
            network, "team_bot_a", channel="slack",
            from_external_id="X", to_external_id="Y",
        )
    assert "no primary" in str(ei.value).lower()


def test_reassign_rejects_non_member():
    from evolve_admin.evo.identity import reassign_primary, ClaimError

    network = {"members": ["team_bot_a"], "bots": {}}
    with pytest.raises(ClaimError) as ei:
        reassign_primary(
            network, "unknown", channel="slack",
            from_external_id="X", to_external_id="Y",
        )
    assert "not a pod member" in str(ei.value)


def test_reassign_accepts_primary_bot_not_listed_in_members():
    """The primary lives in the sibling `primary` key, not `members` —
    reassign must accept it like claim does."""
    from evolve_admin.evo.identity import claim_primary, reassign_primary

    network = {"primary": "evo", "members": ["schoolassistant"], "bots": {}}
    claim_primary(network, "evo", channel="telegram", external_id="111")
    block = reassign_primary(
        network, "evo", channel="telegram",
        from_external_id="111", to_external_id="222",
    )
    assert block["external_ids"]["telegram"] == ["222"]


def test_reassign_rejects_empty_inputs():
    from evolve_admin.evo.identity import reassign_primary, ClaimError

    network = _seeded_network()
    with pytest.raises(ClaimError):
        reassign_primary(
            network, "team_bot_a", channel="slack",
            from_external_id="", to_external_id="Y",
        )
    with pytest.raises(ClaimError):
        reassign_primary(
            network, "team_bot_a", channel="slack",
            from_external_id="U123ABC", to_external_id="",
        )


def test_reassign_normalizes_channel_case():
    from evolve_admin.evo.identity import reassign_primary

    network = _seeded_network()
    reassign_primary(
        network, "team_bot_a", channel="SLACK",
        from_external_id="U123ABC", to_external_id="U456DEF",
    )
    assert network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U456DEF"]


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_append_creates_dir_and_writes_jsonl(tmp_path):
    from evolve_admin.evo import audit

    audit.append_event(
        tmp_path,
        actor="cli:pod_admin_user",
        action="claim",
        bot_id="team_bot_a",
        channel="slack",
        from_external_id=None,
        to_external_id="U123ABC",
    )
    audit_file = tmp_path / "audit" / "evo_identity.jsonl"
    assert audit_file.exists()
    raw = audit_file.read_text().strip()
    record = json.loads(raw)
    assert record["actor"] == "cli:pod_admin_user"
    assert record["action"] == "claim"
    assert record["bot_id"] == "team_bot_a"
    assert record["channel"] == "slack"
    assert record["from"] is None
    assert record["to"] == "U123ABC"
    assert record["force"] is False
    assert record["reason"] is None
    assert "ts" in record


def test_audit_append_multiple_events_appends(tmp_path):
    from evolve_admin.evo import audit

    audit.append_event(
        tmp_path, actor="cli:a", action="claim", bot_id="team_bot_a",
        channel="slack", to_external_id="U1",
    )
    audit.append_event(
        tmp_path, actor="cli:b", action="reassign", bot_id="team_bot_a",
        channel="slack", from_external_id="U1", to_external_id="U2",
        reason="handoff",
    )
    events = audit.read_events(tmp_path)
    assert len(events) == 2
    assert events[0]["actor"] == "cli:a"
    assert events[1]["actor"] == "cli:b"
    assert events[1]["reason"] == "handoff"


def test_audit_read_returns_empty_when_no_log(tmp_path):
    from evolve_admin.evo import audit
    assert audit.read_events(tmp_path) == []


def test_audit_read_skips_corrupt_lines(tmp_path):
    from evolve_admin.evo import audit

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "evo_identity.jsonl").write_text(
        '{"ts":"2026-05-06","actor":"cli:a","action":"claim",'
        '"bot_id":"team_bot_a","channel":"slack","to":"U1","from":null,'
        '"force":false,"reason":null}\n'
        'not-json-at-all\n'
        '{"ts":"2026-05-06","actor":"cli:b","action":"claim",'
        '"bot_id":"admin_bot","channel":"telegram","to":"99","from":null,'
        '"force":false,"reason":null}\n'
    )
    events = audit.read_events(tmp_path)
    assert len(events) == 2
    assert events[0]["bot_id"] == "team_bot_a"
    assert events[1]["bot_id"] == "admin_bot"


def test_cli_actor_prefers_sudo_user(monkeypatch):
    from evolve_admin.evo import audit

    monkeypatch.setenv("SUDO_USER", "pod_admin_user")
    monkeypatch.setenv("USER", "root")
    assert audit.cli_actor() == "cli:pod_admin_user"


def test_cli_actor_falls_back_to_user(monkeypatch):
    from evolve_admin.evo import audit

    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv("USER", "alice")
    assert audit.cli_actor() == "cli:alice"


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {
        "members": ["team_bot_a", "admin_bot"],
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a": {
                "primary_user": {"external_ids": {"slack": "U123ABC"}}
            }
        },
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
    return app, tmp_path, network_path


def test_route_reassign_succeeds(evo_app):
    app, shared_dir, network_path = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/identity/team_bot_a/reassign",
        json={"channel": "slack", "from": "U123ABC", "to": "U456DEF",
              "reason": "handoff"},
    )
    assert r.status_code == 200, r.get_json()
    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U456DEF"]

    # Audit log written
    audit_lines = (shared_dir / "audit" / "evo_identity.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["action"] == "reassign"
    assert record["from"] == "U123ABC"
    assert record["to"] == "U456DEF"
    assert record["actor"] == "api"
    assert record["reason"] == "handoff"


def test_route_reassign_rejects_mismatched_from(evo_app):
    app, shared_dir, _ = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/identity/team_bot_a/reassign",
        json={"channel": "slack", "from": "WRONG", "to": "U456DEF"},
    )
    assert r.status_code == 400
    assert "U123ABC" in r.get_json()["error"]
    # No audit on failed reassign
    assert not (shared_dir / "audit" / "evo_identity.jsonl").exists()


def test_route_claim_writes_audit(evo_app):
    app, shared_dir, _ = evo_app
    client = app.test_client()
    r = client.post(
        "/api/evo/identity/admin_bot/claim",
        json={"channel": "telegram", "external_id": "12345"},
    )
    assert r.status_code == 200
    audit_lines = (shared_dir / "audit" / "evo_identity.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["action"] == "claim"
    assert record["from"] is None
    assert record["to"] == "12345"


def test_route_claim_idempotent_skips_audit(evo_app):
    """Claiming the same value twice writes one audit entry, not two."""
    app, shared_dir, _ = evo_app
    client = app.test_client()
    client.post(
        "/api/evo/identity/admin_bot/claim",
        json={"channel": "telegram", "external_id": "12345"},
    )
    client.post(
        "/api/evo/identity/admin_bot/claim",
        json={"channel": "telegram", "external_id": "12345"},
    )
    audit_lines = (shared_dir / "audit" / "evo_identity.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1


def test_route_audit_get_filters(evo_app):
    app, _, _ = evo_app
    client = app.test_client()
    client.post(
        "/api/evo/identity/admin_bot/claim",
        json={"channel": "telegram", "external_id": "12345"},
    )
    client.post(
        "/api/evo/identity/team_bot_a/reassign",
        json={"channel": "slack", "from": "U123ABC", "to": "U456DEF"},
    )

    # No filter — both events
    r = client.get("/api/evo/identity/audit")
    assert r.status_code == 200
    assert len(r.get_json()["events"]) == 2

    # Filter by bot
    r = client.get("/api/evo/identity/audit?bot_id=admin_bot")
    assert len(r.get_json()["events"]) == 1
    assert r.get_json()["events"][0]["bot_id"] == "admin_bot"

    # Filter by channel
    r = client.get("/api/evo/identity/audit?channel=slack")
    assert len(r.get_json()["events"]) == 1
    assert r.get_json()["events"][0]["channel"] == "slack"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner_with_network(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import config as _cfg

    network = {
        "members": ["team_bot_a", "admin_bot"],
        "sharedDir": str(tmp_path),
        "bots": {
            "team_bot_a": {
                "primary_user": {"external_ids": {"slack": "U123ABC"}}
            }
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_cfg, "save_network", _atomic_save)
    monkeypatch.setenv("SUDO_USER", "pod_admin_user")  # stable actor across hosts
    return CliRunner(), tmp_path, network_path


def test_cli_reassign_primary(cli_runner_with_network):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "reassign-primary", "team_bot_a",
        "--channel", "slack",
        "--from", "U123ABC", "--to", "U456DEF",
        "--reason", "handoff to Bob",
    ])
    assert r.exit_code == 0, r.output
    assert "reassigned" in r.output
    assert "U456DEF" in r.output

    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["team_bot_a"]["primary_user"]["external_ids"]["slack"] == ["U456DEF"]

    audit_lines = (shared_dir / "audit" / "evo_identity.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["action"] == "reassign"
    assert record["actor"] == "cli:pod_admin_user"
    assert record["reason"] == "handoff to Bob"


def test_cli_reassign_mismatched_from_exits_nonzero(cli_runner_with_network):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "reassign-primary", "team_bot_a",
        "--channel", "slack",
        "--from", "WRONG", "--to", "U456DEF",
    ])
    assert r.exit_code != 0
    assert "U123ABC" in r.output
    # No audit on failed reassign
    assert not (shared_dir / "audit" / "evo_identity.jsonl").exists()


def test_cli_claim_writes_audit(cli_runner_with_network):
    runner, shared_dir, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "admin_bot",
        "--channel", "telegram", "--external-id", "12345",
        "--reason", "first time",
    ])
    assert r.exit_code == 0, r.output

    audit_lines = (shared_dir / "audit" / "evo_identity.jsonl").read_text().strip().splitlines()
    assert len(audit_lines) == 1
    record = json.loads(audit_lines[0])
    assert record["action"] == "claim"
    assert record["actor"] == "cli:pod_admin_user"
    assert record["reason"] == "first time"


def test_cli_show_audit(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "admin_bot",
        "--channel", "telegram", "--external-id", "12345",
    ])
    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "reassign-primary", "team_bot_a",
        "--channel", "slack", "--from", "U123ABC", "--to", "U999XYZ",
    ])

    r = runner.invoke(main, [
        "--network", str(network_path), "evo", "show-audit",
    ])
    assert r.exit_code == 0, r.output
    assert "claim" in r.output
    assert "reassign" in r.output
    assert "admin_bot" in r.output
    assert "team_bot_a" in r.output


def test_cli_show_audit_filter_by_bot(cli_runner_with_network):
    runner, _, network_path = cli_runner_with_network
    from evolve_admin.cli import main

    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "claim-primary", "admin_bot",
        "--channel", "telegram", "--external-id", "12345",
    ])
    runner.invoke(main, [
        "--network", str(network_path),
        "evo", "reassign-primary", "team_bot_a",
        "--channel", "slack", "--from", "U123ABC", "--to", "U999XYZ",
    ])

    r = runner.invoke(main, [
        "--network", str(network_path),
        "evo", "show-audit", "--bot", "admin_bot",
    ])
    assert r.exit_code == 0
    assert "admin_bot" in r.output
    assert "team_bot_a" not in r.output

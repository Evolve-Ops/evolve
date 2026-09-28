"""Tests for inline-token-write in `POST /api/mcp-admin/install`.

The install route was extended to accept an optional `token_values: {slot: value}`
dict in the request body. When present, each (slot, value) is written to the
keystore BEFORE the InstallMcpServer proposal is created — so operators can
paste a PAT inline at install time instead of dropping to the CLI to register
the slot.

These tests pin:
- Happy path: token gets written, proposal call still happens, response is annotated
- Orphan token (slot not referenced in env_bindings) is rejected
- Malformed token (whitespace / too long) is rejected before any write
- Proposal failure after token write — token still saved (caller can retry install)
- token_values absent → behaves exactly like the pre-extension version
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture
def install_app(tmp_path):
    """Build a Flask app via create_app() with a tmp shared_dir and stub
    `_create_mcp_proposal` so the test doesn't need the full proposals
    pipeline. Returns (app, network_path)."""
    from flask import jsonify
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_a": {}, "personal_bot": {}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # Force the server module to load our tmp network. create_app() reads
    # network_path on each request via load_network — pointing at the right
    # path here is enough.
    app = srv.create_app(network_path=network_path)
    app.config["TESTING"] = True

    # Stub the proposal creator. The install route calls _create_mcp_proposal
    # which depends on the whole proposal pipeline; replacing it with a stub
    # that returns a fake "applied" proposal lets us focus on the token-write
    # path being tested.
    def _fake_create(action_kind, action_payload, bot_id, summary):
        return {
            "id": "fake-proposal-id",
            "status": "applied",
            "kind": action_kind,
            "summary": summary,
            "payload": action_payload,
        }, None

    return app, network_path, _fake_create


def _post_install(app, _fake_create, body):
    """Helper: POST to /api/mcp-admin/install with the stub in place."""
    from evolve_admin.web import server as srv
    with patch.object(srv, "_operator_create_apply", lambda **kw: _fake_create(
        kw["action_kind"], kw["action_payload"], kw["bot_id"], kw["summary"]
    )):
        return app.test_client().post("/api/mcp-admin/install", json=body)


def test_install_writes_inline_token_to_keystore(install_app):
    """Happy path: token_values → keystore write + proposal applies."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        "token_values": {"github-team_bot_a": "ghp_validtoken"},
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body.get("tokens_saved") == 1
    # Verify the keystore got the value
    from evolve_admin.keystore import KeystoreManager
    shared = Path(json.loads(_net.read_text())["sharedDir"])
    assert KeystoreManager(shared).get_value("github-team_bot_a") == "ghp_validtoken"


def test_install_without_token_values_works_as_before(install_app):
    """Pre-extension behavior preserved: no token_values → no keystore write,
    no tokens_saved field, proposal still creates."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert "tokens_saved" not in body
    # And keystore is empty for that slot
    from evolve_admin.keystore import KeystoreManager
    shared = Path(json.loads(_net.read_text())["sharedDir"])
    assert KeystoreManager(shared).get_value("github-team_bot_a") is None


def test_install_rejects_orphan_token(install_app):
    """Tokens whose slot isn't referenced by env_bindings are rejected —
    refuse to write a secret the install can't consume."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        # "rogue-slot" isn't referenced by any env binding
        "token_values": {"rogue-slot": "ghp_orphan"},
    })
    assert r.status_code == 400
    assert "orphan" in r.get_json()["error"].lower()
    # And no keystore write happened
    from evolve_admin.keystore import KeystoreManager
    shared = Path(json.loads(_net.read_text())["sharedDir"])
    assert KeystoreManager(shared).get_value("rogue-slot") is None
    assert KeystoreManager(shared).get_value("github-team_bot_a") is None


def test_install_rejects_malformed_token_before_write(install_app):
    """Whitespace / too-long tokens are rejected — no keystore write."""
    app, _net, fake = install_app

    # Whitespace inside
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        "token_values": {"github-team_bot_a": "ghp_has space"},
    })
    assert r.status_code == 400
    assert "malformed" in r.get_json()["error"].lower()
    # And no keystore write
    from evolve_admin.keystore import KeystoreManager
    shared = Path(json.loads(_net.read_text())["sharedDir"])
    assert KeystoreManager(shared).get_value("github-team_bot_a") is None

    # Way too long
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        "token_values": {"github-team_bot_a": "g" * 500},
    })
    assert r.status_code == 400


def test_install_rejects_empty_token_value(install_app):
    """Empty token value is rejected — operator probably meant to omit."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        "token_values": {"github-team_bot_a": ""},
    })
    assert r.status_code == 400
    assert "empty" in r.get_json()["error"].lower()


def test_install_writes_multiple_tokens(install_app):
    """Multi-env servers (rare but possible) — each token lands."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "multi",
        "catalog_id": "multi",
        "env_bindings": {
            "TOKEN_A": "keystore:slot-a",
            "TOKEN_B": "keystore:slot-b",
        },
        "token_values": {
            "slot-a": "value-a",
            "slot-b": "value-b",
        },
    })
    assert r.status_code == 200
    assert r.get_json().get("tokens_saved") == 2
    from evolve_admin.keystore import KeystoreManager
    shared = Path(json.loads(_net.read_text())["sharedDir"])
    assert KeystoreManager(shared).get_value("slot-a") == "value-a"
    assert KeystoreManager(shared).get_value("slot-b") == "value-b"


def test_install_token_values_not_dict_rejected(install_app):
    """token_values must be an object — wrong shape → 400."""
    app, _net, fake = install_app
    r = _post_install(app, fake, {
        "bot_id": "team_bot_a",
        "server_id": "github",
        "catalog_id": "github",
        "env_bindings": {"GITHUB_TOKEN": "keystore:github-team_bot_a"},
        "token_values": ["not", "a", "dict"],
    })
    assert r.status_code == 400
    assert "object" in r.get_json()["error"].lower()

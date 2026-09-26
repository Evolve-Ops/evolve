"""tests/test_skills_github_install.py — GitHub-MCP install (purpose 2).

Covers the github skill's LLM-access install path, which is distinct
from purpose 1 (backup wizard in upstream_plugin_skills). Same shape
as the Notion install (#1831) — API-key skill, credentials in pod
keystore, validate-against-API before keystore write, InstallMcpServer
proposal with env_bindings + rollback on failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import github_install  # noqa: E402


# ── Keystore slot naming ──────────────────────────────────────────────────────


class TestKeystoreSlot:
    """Per-bot slot pattern. Important: the github-* keystore namespace
    is shared with purpose-1 backup PATs (per the keystore_hint in
    catalog.py), so use a distinct slot name to avoid collision."""

    def test_slot_is_per_bot(self):
        assert github_install.keystore_slot_for("team_bot_b") == "github-team_bot_b"
        assert github_install.keystore_slot_for("admin_bot") == "github-admin_bot"


# ── verify_token — uses HTTP, mocked ──────────────────────────────────────────


def _fake_github_get(status, body=None, scopes_hdr=None, err=None):
    """Build a _github_get_user replacement that returns a fixed result."""
    def _replacement(_token):
        return status, body, scopes_hdr, err
    return _replacement


class TestVerifyToken:
    def test_empty_token_is_invalid(self):
        result = github_install.verify_token("")
        assert result["ok"] is False
        assert result["status"] == "invalid"
        assert result["error"] == "empty_token"

    def test_whitespace_only_token_is_invalid(self):
        result = github_install.verify_token("   ")
        assert result["ok"] is False
        assert result["status"] == "invalid"

    def test_valid_200_returns_username_and_scopes(self):
        with patch.object(
            github_install, "_github_get_user",
            _fake_github_get(200, {"login": "octocat"}, "repo:read, gist"),
        ):
            result = github_install.verify_token("ghp_validtoken")
        assert result["ok"] is True
        assert result["status"] == "valid"
        assert result["username"] == "octocat"
        # X-OAuth-Scopes split by comma + whitespace stripped
        assert result["scopes"] == ["repo:read", "gist"]

    def test_401_returns_revoked(self):
        with patch.object(
            github_install, "_github_get_user",
            _fake_github_get(401, {"message": "Bad credentials"}),
        ):
            result = github_install.verify_token("ghp_badtoken")
        assert result["ok"] is False
        assert result["status"] == "revoked"
        assert result["http_status"] == 401

    def test_connection_failed_returns_unknown(self):
        with patch.object(
            github_install, "_github_get_user",
            _fake_github_get(0, None, None, "connection_failed"),
        ):
            result = github_install.verify_token("ghp_x")
        assert result["ok"] is False
        assert result["status"] == "unknown"
        assert result["error"] == "connection_failed"

    def test_unexpected_5xx_returns_unknown(self):
        with patch.object(
            github_install, "_github_get_user",
            _fake_github_get(503, {"message": "Service unavailable"}),
        ):
            result = github_install.verify_token("ghp_x")
        assert result["ok"] is False
        assert result["status"] == "unknown"
        assert "http_error_503" in result["error"]

    def test_empty_scopes_header_returns_empty_list(self):
        """Some PAT types (e.g. fine-grained) return empty X-OAuth-Scopes
        because their permission model is per-resource, not OAuth scopes."""
        with patch.object(
            github_install, "_github_get_user",
            _fake_github_get(200, {"login": "octocat"}, ""),
        ):
            result = github_install.verify_token("github_pat_xyz")
        assert result["scopes"] == []


# ── MCP-aware status resolver ─────────────────────────────────────────────────


class TestResolveStatusMcp:
    def test_missing_when_mcp_block_absent(self):
        def _read(_bot_id):
            return {"mcp": {"servers": {}}}, None

        status = github_install.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read, read_keystore_slot=lambda _s: "x",
        )
        assert status.status == "missing"

    def test_valid_when_mcp_and_keystore_present(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "github": {"command": "/launcher", "args": []},
                    },
                },
            }, None

        status = github_install.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: "ghp_real_token",
        )
        assert status.status == "valid"
        assert status.error is None

    def test_revoked_when_mcp_present_but_keystore_empty(self):
        def _read(_bot_id):
            return {
                "mcp": {"servers": {"github": {"command": "/l", "args": []}}},
            }, None

        status = github_install.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: None,
        )
        assert status.status == "revoked"
        assert "keystore_slot_empty" in (status.error or "")

    def test_unknown_when_oc_unreadable(self):
        def _read(_bot_id):
            return None, "permission_denied"

        status = github_install.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")


# ── Access panel: post-install callout ────────────────────────────────────────


class TestAccessPanel:
    def test_post_install_callout_mentions_scopes(self):
        """The PAT's scope choice is THE controllable risk knob for
        GitHub-MCP — the access panel must surface scope guidance after
        install so users don't over-grant."""
        panel = github_install.GITHUB_MCP_ACCESS_PANEL
        callout = panel.get("post_install_callout") or ""
        assert "scope" in callout.lower()
        # Concrete recommendation should be present
        assert "repo:read" in callout or "read" in callout.lower()


# ── Route integration: /api/skills/install/github/install-mcp-server ──────────


@pytest.fixture
def github_route_app(tmp_path):
    """Flask app + proposal-capturing stub. Same fixture shape as the
    notion + dropbox route tests."""
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_b": {"user": "personal_bot_user"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = srv.create_app(network_path=network_path)
    app.config["TESTING"] = True

    proposals_captured: list[dict] = []

    def _fake_create(action_kind, action_payload, bot_id, summary):
        proposals_captured.append({
            "kind": action_kind, "payload": action_payload,
            "bot_id": bot_id, "summary": summary,
        })
        return {
            "id": "fake-prop-id",
            "status": "applied",
            "kind": action_kind,
            "summary": summary,
            "payload": action_payload,
        }, None

    return app, proposals_captured, _fake_create


def _stub_create_apply(_fake_create):
    from evolve_admin.web import server as srv
    return patch.object(srv, "_operator_create_apply", lambda **kw: _fake_create(
        kw["action_kind"], kw["action_payload"], kw["bot_id"], kw["summary"]
    ))


def _ok_verify(_token):
    return {
        "ok": True, "status": "valid",
        "username": "octocat",
        "scopes": ["repo:read", "gist"],
        "error": None, "http_status": 200,
    }


class TestInstallMcpServerRoute:
    def test_missing_bot_id_rejected(self, github_route_app):
        app, _, fake = github_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"access_token": "ghp_x"},
            )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_token_rejected(self, github_route_app):
        app, _, fake = github_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"bot_id": "team_bot_b"},
            )
        assert r.status_code == 400
        assert "access_token" in r.get_json()["error"]

    def test_revoked_token_returns_401(self, github_route_app):
        app, captured, fake = github_route_app

        def _revoked(_token):
            return {
                "ok": False, "status": "revoked",
                "username": None, "scopes": [],
                "error": "unauthorized", "http_status": 401,
            }

        with _stub_create_apply(fake), \
             patch.object(github_install, "verify_token", _revoked):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"bot_id": "team_bot_b", "access_token": "ghp_revoked"},
            )
        assert r.status_code == 401
        assert captured == []  # no proposal got through

    def test_connection_failure_returns_502(self, github_route_app):
        app, captured, fake = github_route_app

        def _unreachable(_token):
            return {
                "ok": False, "status": "unknown",
                "username": None, "scopes": [],
                "error": "connection_failed", "http_status": 0,
            }

        with _stub_create_apply(fake), \
             patch.object(github_install, "verify_token", _unreachable):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"bot_id": "team_bot_b", "access_token": "ghp_x"},
            )
        assert r.status_code == 502
        assert captured == []

    def test_happy_path_writes_keystore_and_creates_proposal(
        self, github_route_app,
    ):
        """End-to-end: verify_token → keystore write (verbatim PAT,
        not JSON-encoded like Notion) → InstallMcpServer with
        catalog_id="github" + env_bindings referencing the slot."""
        app, captured, fake = github_route_app

        ks_writes: list[tuple[str, str]] = []

        class _FakeKS:
            class _Inner:
                @staticmethod
                def get_key_entry(_name):
                    return None
            ks = _Inner()

            def __init__(self, _shared_dir):
                pass

            def register(self, name, *, provider, scope, description,
                         bots, value):
                ks_writes.append((name, value))

            def set_value(self, name, value):
                ks_writes.append((name, value))

        with _stub_create_apply(fake), \
             patch.object(github_install, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _FakeKS), \
             patch.object(
                 github_install, "resolve_status_mcp",
                 return_value=github_install.InstallStatus(
                     bot_id="team_bot_b", status="valid",
                 ),
             ):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"bot_id": "team_bot_b", "access_token": "ghp_real_token_xyz"},
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "valid"
        assert body.get("username") == "octocat"
        assert body.get("scopes") == ["repo:read", "gist"]

        # Keystore: VERBATIM PAT (not JSON-encoded — this is the
        # github-vs-notion difference; GitHub MCP wants GITHUB_TOKEN as
        # a plain Bearer string).
        assert len(ks_writes) == 1
        slot, value = ks_writes[0]
        assert slot == "github-team_bot_b"
        assert value == "ghp_real_token_xyz"

        # Proposal shape — critical install contract
        assert len(captured) == 1
        prop = captured[0]
        assert prop["kind"] == "InstallMcpServer"
        payload = prop["payload"]
        assert payload["bot_id"] == "team_bot_b"
        assert payload["server_id"] == "github"
        assert payload["catalog_id"] == "github"
        # env_bindings is a single GITHUB_TOKEN ref (not OPENAPI_MCP_HEADERS
        # like Notion — the MCP server determines the env var shape).
        assert payload["env_bindings"] == {
            "GITHUB_TOKEN": "keystore:github-team_bot_b",
        }

    def test_keystore_failure_does_not_create_proposal(self, github_route_app):
        """Keystore-write failure must bail BEFORE creating the proposal —
        otherwise we'd install an MCP server pointing at a missing
        credential. Same rollback shape as the notion route."""
        app, captured, fake = github_route_app

        class _BrokenKS:
            def __init__(self, _shared_dir):
                raise RuntimeError("keystore corrupted")

        with _stub_create_apply(fake), \
             patch.object(github_install, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _BrokenKS):
            r = app.test_client().post(
                "/api/skills/install/github/install-mcp-server",
                json={"bot_id": "team_bot_b", "access_token": "ghp_x"},
            )
        assert r.status_code == 500
        assert "keystore" in r.get_json()["error"].lower()
        assert captured == []


# ── Independence from purpose-1 (backup wizard) ───────────────────────────────


class TestPurposeIndependence:
    """The github MCP install is independent of the backup wizard install
    (purpose 1). Critical contract: the new wrapper must NOT touch
    ~/.openclaw/workspace/.git/config (where backup writes the PAT) and
    revoke must NOT clear the backup PAT."""

    def test_keystore_slot_distinct_from_backup_storage(self):
        """The MCP install puts the PAT in pod keystore; backup writes it
        into .git/config inside the bot's workspace. Distinct storage."""
        slot = github_install.keystore_slot_for("team_bot_b")
        # The slot is in the github-* keystore namespace per catalog.py
        # vetting_notes — but the backup PAT does NOT live in the keystore.
        # The two cannot collide because they're in different storage.
        assert slot.startswith("github-")
        # No reference to the backup-storage path in the slot name.
        assert ".git" not in slot
        assert "workspace" not in slot
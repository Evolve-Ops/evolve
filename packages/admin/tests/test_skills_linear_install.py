"""Tests for evolve_admin.skills.linear_install.

Mirrors the Notion test layout: token format validation, the verify_token
state mapping (with the Linear GraphQL POST stubbed), the resolve_status
state machine, install plan shape, and Flask route behaviour. The Linear
API is never contacted — _linear_post_graphql is stubbed in every test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from evolve_admin.skills import linear_install as linear


# ── Token format validation ───────────────────────────────────────────────────

class TestTokenFormat:
    def test_accepts_lin_api_prefix(self):
        assert linear._token_looks_valid("lin_api_" + "a" * 40) is True

    def test_accepts_legacy_long_tokens_without_prefix(self):
        """Pre-2023 Linear tokens didn't always carry lin_api_ prefix; we let
        Linear's API be the authoritative judge rather than rejecting client-
        side and frustrating users with legacy keys still in active use."""
        assert linear._token_looks_valid("a" * 50) is True

    def test_rejects_short_token(self):
        assert linear._token_looks_valid("lin_api_short") is False

    def test_rejects_empty(self):
        assert linear._token_looks_valid("") is False
        assert linear._token_looks_valid(None) is False


# ── verify_token state mapping ────────────────────────────────────────────────

class TestVerifyToken:
    GOOD_TOKEN = "lin_api_" + "a" * 40

    def test_valid_on_graphql_200_with_data(self):
        body = {
            "data": {
                "viewer": {"id": "u1", "name": "Carla", "email": "c@x.com"},
                "organization": {"id": "o1", "name": "Carla Design Studio"},
            }
        }
        with patch.object(linear, "_linear_post_graphql", return_value=(200, body, None)):
            r = linear.verify_token(self.GOOD_TOKEN)
        assert r["ok"] is True
        assert r["status"] == "valid"
        assert r["workspace_name"] == "Carla Design Studio"
        assert r["viewer_name"] == "Carla"

    def test_revoked_on_401(self):
        with patch.object(linear, "_linear_post_graphql", return_value=(401, None, None)):
            r = linear.verify_token(self.GOOD_TOKEN)
        assert r["status"] == "revoked"

    def test_revoked_on_graphql_auth_error_in_200_body(self):
        """Linear surfaces some auth failures as 200 + {errors:[{...AUTHENTICATION_ERROR...}]}.
        Must classify those as revoked, not unknown."""
        body = {
            "errors": [{
                "message": "Authentication required, not authenticated",
                "extensions": {"code": "AUTHENTICATION_ERROR"},
            }]
        }
        with patch.object(linear, "_linear_post_graphql", return_value=(200, body, None)):
            r = linear.verify_token(self.GOOD_TOKEN)
        assert r["status"] == "revoked"

    def test_unknown_on_graphql_non_auth_error(self):
        body = {"errors": [{"message": "Internal server error"}]}
        with patch.object(linear, "_linear_post_graphql", return_value=(200, body, None)):
            r = linear.verify_token(self.GOOD_TOKEN)
        assert r["status"] == "unknown"

    def test_invalid_format_short_circuits_http(self):
        """Format check must run before _linear_post_graphql so we don't
        waste a network round-trip on obviously bad input."""
        with patch.object(linear, "_linear_post_graphql") as m:
            r = linear.verify_token("garbage")
            assert r["status"] == "invalid"
            m.assert_not_called()

    def test_unknown_on_connection_failed(self):
        with patch.object(linear, "_linear_post_graphql",
                          return_value=(0, None, "connection_failed")):
            r = linear.verify_token(self.GOOD_TOKEN)
        assert r["status"] == "unknown"
        assert r["error"] == "connection_failed"


# ── resolve_status ────────────────────────────────────────────────────────────

class TestResolveStatus:
    GOOD_TOKEN = "lin_api_" + "a" * 40

    def test_missing_when_no_config(self):
        st = linear.resolve_status("admin_bot",
                                    read_cfg=lambda b: None,
                                    check_token=lambda t: {"ok": True})
        assert st.status == "missing"

    def test_valid_when_check_passes(self):
        st = linear.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": self.GOOD_TOKEN},
                                    check_token=lambda t: {
                                        "ok": True, "status": "valid",
                                        "workspace_name": "Carla", "viewer_name": "C",
                                    })
        assert st.status == "valid"
        assert st.workspace_name == "Carla"

    def test_revoked_preserves_stored_workspace_for_ui(self):
        """Show 'Carla Design Studio rejected the key' rather than just 'rejected'
        on revoke — which side of the chain to fix is the load-bearing info."""
        st = linear.resolve_status("admin_bot",
                                    read_cfg=lambda b: {
                                        "access_token": self.GOOD_TOKEN,
                                        "workspace_name": "Carla Design Studio",
                                    },
                                    check_token=lambda t: {
                                        "ok": False, "status": "revoked",
                                        "error": "unauthorized",
                                    })
        assert st.status == "revoked"
        assert st.workspace_name == "Carla Design Studio"

    def test_invalid_when_stored_token_garbage(self):
        st = linear.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": "junk"},
                                    check_token=lambda t: {"ok": True})
        assert st.status == "invalid"


# ── Install plan ──────────────────────────────────────────────────────────────

class TestInstallPlan:
    def test_valid_yields_no_steps(self):
        st = linear.InstallStatus(bot_id="admin_bot", status="valid")
        assert linear.build_install_plan(st) == []

    def test_missing_yields_set_config_then_confirm(self):
        st = linear.InstallStatus(bot_id="admin_bot", status="missing")
        steps = linear.build_install_plan(st)
        assert [s.id for s in steps] == ["set_config", "confirm"]
        names = [f["name"] for f in steps[0].fields]
        assert names == ["access_token"]
        assert steps[0].fields[0]["type"] == "password"

    def test_set_token_endpoint_matches_route(self):
        st = linear.InstallStatus(bot_id="admin_bot", status="missing")
        steps = linear.build_install_plan(st)
        assert steps[0].endpoint.endswith("/linear/set-token")


# ── HTTP layer: header shape contract ─────────────────────────────────────────

class TestHttpHeaderShape:
    """The personal-API-key wire-protocol detail: Authorization header must
    NOT have a 'Bearer' prefix (that's only for OAuth). Pin this so a
    well-intentioned refactor doesn't accidentally add it and revoke every
    user's key in one go."""

    def test_personal_api_key_uses_bare_authorization_header(self):
        import urllib.request
        captured = {}

        class FakeResp:
            status = 200
            def read(self): return b'{"data":{"viewer":{"name":"x"},"organization":{"name":"X"}}}'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            linear._linear_post_graphql("lin_api_abc123", "{viewer{id}}")

        # Header names are title-cased by urllib.
        assert captured["headers"].get("Authorization") == "lin_api_abc123"
        # Explicit anti-regression: no Bearer prefix.
        assert not captured["headers"]["Authorization"].startswith("Bearer ")


# ── Flask route smoke tests ───────────────────────────────────────────────────


# ── MCP-install helpers (2026-05-30 rewire) ──────────────────────────────────


class TestKeystoreSlot:
    """The per-bot slot pattern (linear-<bot>) mirrors Notion's and
    Telegram's per-bot token model — each bot can connect to a different
    Linear workspace by minting the API key under a different user."""

    def test_slot_is_per_bot(self):
        assert linear.keystore_slot_for("team_bot_b") == "linear-team_bot_b"
        assert linear.keystore_slot_for("admin_bot") == "linear-admin_bot"

    def test_two_bots_get_distinct_slots(self):
        assert linear.keystore_slot_for("team_bot_b") != linear.keystore_slot_for("admin_bot")


# ── MCP-aware status resolver ─────────────────────────────────────────────────


class TestResolveStatusMcp:
    """resolve_status_mcp reads openclaw.json::mcp.servers.linear + the
    per-bot keystore slot. Mirrors Notion's state machine exactly because
    the credential-shape difference (verbatim PAT vs JSON headers blob)
    doesn't affect the loader-side signal."""

    def test_missing_when_mcp_block_absent(self):
        def _read(_bot_id):
            return {"mcp": {"servers": {}}}, None

        status = linear.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read, read_keystore_slot=lambda _s: "anything",
        )
        assert status.status == "missing"

    def test_valid_when_mcp_block_and_keystore_present(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "linear": {"command": "/launcher", "args": []},
                    },
                },
            }, None

        status = linear.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: "lin_api_xyz_filled",
        )
        assert status.status == "valid"
        assert status.error is None

    def test_revoked_when_mcp_block_present_but_keystore_empty(self):
        """Operator wiped the keystore slot manually but didn't uninstall
        the MCP server — surface as ``revoked`` so the UI prompts for
        re-paste rather than claiming everything's fine."""
        def _read(_bot_id):
            return {
                "mcp": {"servers": {"linear": {"command": "/l", "args": []}}},
            }, None

        status = linear.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: None,  # slot exists but empty
        )
        assert status.status == "revoked"
        assert "keystore_slot_empty" in (status.error or "")

    def test_valid_without_keystore_check_when_reader_not_injected(self):
        """When read_keystore_slot is None, the resolver trusts the
        mcp.servers entry alone — same defensive default as Notion."""
        def _read(_bot_id):
            return {
                "mcp": {"servers": {"linear": {"command": "/l", "args": []}}},
            }, None

        status = linear.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
        )
        assert status.status == "valid"

    def test_unknown_when_oc_unreadable(self):
        def _read(_bot_id):
            return None, "permission_denied"

        status = linear.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")


# ── Access panel: post-install identity callout ──────────────────────────────


class TestAccessPanelPostInstallCallout:
    """Linear's identity model — the bot acts as the API key holder — is
    the most common surprise after install. The access panel must carry
    a post_install_callout the UI surfaces on the confirmation screen."""

    def test_post_install_callout_present(self):
        callout = linear.LINEAR_ACCESS_PANEL.get("post_install_callout")
        assert callout, "Linear access panel must have post_install_callout"

    def test_post_install_callout_mentions_identity_caveat(self):
        callout = linear.LINEAR_ACCESS_PANEL.get("post_install_callout") or ""
        # Don't assert exact wording — the operator-facing copy may evolve.
        # Do assert the two load-bearing pieces of information are present.
        assert "act" in callout.lower() or "identity" in callout.lower()
        assert "user" in callout.lower() or "bot user account" in callout.lower()


# ── Flask route tests (2026-05-30 rewire) ─────────────────────────────────────


import json as _json
import pytest as _pytest


@_pytest.fixture
def linear_route_app(tmp_path):
    """Flask app + stubs for the linear install route. Mirrors the
    fixture in test_skills_notion_install.py."""
    from evolve_admin.web import server as srv

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evo",
        "bots": {"team_bot_b": {"user": "personal_bot_user"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(_json.dumps(network))
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
        "workspace_name": "TeamBotB's Workspace",
        "viewer_name": "TeamBotB Bot",
        "error": None, "http_status": 200,
    }


class TestSetTokenRoute:
    def test_missing_bot_id_rejected(self, linear_route_app):
        app, _, fake = linear_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"access_token": "lin_api_x"},
            )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_token_rejected(self, linear_route_app):
        app, _, fake = linear_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"bot_id": "team_bot_b"},
            )
        assert r.status_code == 400
        assert "access_token" in r.get_json()["error"]

    def test_invalid_token_format_rejected_before_keystore_or_proposal(
        self, linear_route_app,
    ):
        """Token-format check should bail before we touch the keystore or
        create the proposal — otherwise a typo would land bad data."""
        app, captured, fake = linear_route_app

        def _bad_format(_token):
            return {
                "ok": False, "status": "invalid",
                "workspace_name": None, "viewer_name": None,
                "error": "invalid_token_format", "http_status": 0,
            }

        with _stub_create_apply(fake), \
             patch.object(linear, "verify_token", _bad_format):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"bot_id": "team_bot_b", "access_token": "not-a-real-token"},
            )
        assert r.status_code == 400
        assert captured == []  # no proposal got through

    def test_revoked_token_returns_401(self, linear_route_app):
        app, captured, fake = linear_route_app

        def _revoked(_token):
            return {
                "ok": False, "status": "revoked",
                "workspace_name": None, "viewer_name": None,
                "error": "unauthorized", "http_status": 401,
            }

        with _stub_create_apply(fake), \
             patch.object(linear, "verify_token", _revoked):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"bot_id": "team_bot_b", "access_token": "lin_api_revoked"},
            )
        assert r.status_code == 401
        assert captured == []

    def test_happy_path_writes_keystore_and_creates_install_proposal(
        self, linear_route_app,
    ):
        """The critical end-to-end shape: verify_token → keystore write
        (verbatim PAT, NOT JSON like Notion) → InstallMcpServer proposal
        with catalog_id="linear" + env_bindings.LINEAR_API_KEY referencing
        the slot."""
        app, captured, fake = linear_route_app

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
             patch.object(linear, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _FakeKS), \
             patch.object(linear, "resolve_status_mcp",
                          return_value=linear.InstallStatus(
                              bot_id="team_bot_b", status="valid",
                              workspace_name="TeamBotB's Workspace",
                              viewer_name="TeamBotB Bot",
                          )):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"bot_id": "team_bot_b", "access_token": "lin_api_real_secret_abc_xxxxxxxxxxxxxxxxxxxx"},
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "valid"
        assert body.get("workspace_name") == "TeamBotB's Workspace"
        assert body.get("viewer_name") == "TeamBotB Bot"

        # Keystore: one write to linear-team_bot_b with the VERBATIM secret —
        # critically NOT JSON-encoded (that's the Notion shape).
        assert len(ks_writes) == 1
        slot, value = ks_writes[0]
        assert slot == "linear-team_bot_b"
        assert value == "lin_api_real_secret_abc_xxxxxxxxxxxxxxxxxxxx"
        # Sanity: this should NOT parse as JSON — that would be the Notion shape.
        try:
            parsed = _json.loads(value)
            # If it does parse, make sure it's not a dict (avoid false positives
            # like an all-digit token). The Linear keystore value must be the
            # raw PAT string, never a JSON headers blob.
            assert not isinstance(parsed, dict), (
                f"Linear keystore value should be raw PAT, not JSON dict: {value!r}"
            )
        except (ValueError, _json.JSONDecodeError):
            pass  # expected — verbatim PAT is not valid JSON

        # Proposal shape — the critical install contract
        assert len(captured) == 1
        prop = captured[0]
        assert prop["kind"] == "InstallMcpServer"
        payload = prop["payload"]
        assert payload["bot_id"] == "team_bot_b"
        assert payload["server_id"] == "linear"
        assert payload["catalog_id"] == "linear"
        # env_bindings: LINEAR_API_KEY (NOT OPENAPI_MCP_HEADERS — that's Notion)
        assert payload["env_bindings"] == {
            "LINEAR_API_KEY": "keystore:linear-team_bot_b",
        }

    def test_keystore_failure_does_not_create_proposal(self, linear_route_app):
        """If the keystore write fails, no InstallMcpServer proposal should
        be created — otherwise we'd ship an MCP install with a missing
        credential."""
        app, captured, fake = linear_route_app

        class _BrokenKS:
            def __init__(self, _shared_dir):
                raise RuntimeError("keystore corrupted")

        with _stub_create_apply(fake), \
             patch.object(linear, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _BrokenKS):
            r = app.test_client().post(
                "/api/skills/install/linear/set-token",
                json={"bot_id": "team_bot_b", "access_token": "lin_api_x_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
            )
        assert r.status_code == 500
        assert "keystore" in r.get_json()["error"].lower()
        assert captured == []

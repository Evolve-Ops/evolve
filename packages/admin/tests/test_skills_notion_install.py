"""Tests for evolve_admin.skills.notion_install.

Covers token format validation, the verify_token state mapping (with the
Notion HTTP layer stubbed), the resolve_status state machine, install-plan
shape, and Flask route behaviour including error code mapping.
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

from evolve_admin.skills import notion_install as notion


# ── Token format validation ───────────────────────────────────────────────────

class TestTokenFormat:
    def test_accepts_legacy_secret_prefix(self):
        assert notion._token_looks_valid("secret_" + "a" * 40) is True

    def test_accepts_ntn_prefix(self):
        """May-2024+ Notion tokens use ntn_ rather than secret_."""
        assert notion._token_looks_valid("ntn_" + "a" * 40) is True

    def test_rejects_short(self):
        assert notion._token_looks_valid("secret_short") is False

    def test_rejects_unknown_prefix(self):
        """Tokens that don't start with secret_ or ntn_ should bounce
        before we waste a network round-trip."""
        assert notion._token_looks_valid("xoxb-" + "a" * 40) is False

    def test_rejects_empty(self):
        assert notion._token_looks_valid("") is False
        assert notion._token_looks_valid(None) is False


# ── verify_token state mapping ────────────────────────────────────────────────

class TestVerifyToken:
    def test_valid_on_200_with_bot(self):
        good = "secret_" + "a" * 40
        body = {
            "object": "user",
            "name": "Evolve Bot Integration",
            "bot": {"owner": {"workspace_name": "Carla's Workspace"}},
        }
        with patch.object(notion, "_notion_get_json", return_value=(200, body, None)):
            r = notion.verify_token(good)
        assert r["ok"] is True
        assert r["status"] == "valid"
        assert r["workspace_name"] == "Carla's Workspace"
        assert r["integration_name"] == "Evolve Bot Integration"

    def test_revoked_on_401(self):
        good = "secret_" + "a" * 40
        with patch.object(notion, "_notion_get_json", return_value=(401, None, None)):
            r = notion.verify_token(good)
        assert r["status"] == "revoked"
        assert r["ok"] is False

    def test_invalid_on_bad_format_no_http_call(self):
        """Format check must short-circuit before _notion_get_json runs."""
        with patch.object(notion, "_notion_get_json") as m:
            r = notion.verify_token("not-a-real-token")
            assert r["status"] == "invalid"
            m.assert_not_called()

    def test_unknown_on_connection_failed(self):
        good = "secret_" + "a" * 40
        with patch.object(notion, "_notion_get_json",
                          return_value=(0, None, "connection_failed")):
            r = notion.verify_token(good)
        assert r["status"] == "unknown"
        assert r["error"] == "connection_failed"

    def test_unknown_on_unexpected_http(self):
        good = "secret_" + "a" * 40
        with patch.object(notion, "_notion_get_json",
                          return_value=(503, None, None)):
            r = notion.verify_token(good)
        assert r["status"] == "unknown"


# ── resolve_status ────────────────────────────────────────────────────────────

class TestResolveStatus:
    def test_missing_when_no_config(self):
        st = notion.resolve_status("admin_bot",
                                    read_cfg=lambda b: None,
                                    check_token=lambda t: {"ok": True})
        assert st.status == "missing"

    def test_missing_when_config_has_no_token(self):
        st = notion.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"workspace_name": "X"},
                                    check_token=lambda t: {"ok": True})
        assert st.status == "missing"

    def test_invalid_when_stored_token_garbage(self):
        st = notion.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": "garbage"},
                                    check_token=lambda t: {"ok": True})
        assert st.status == "invalid"

    def test_valid_propagates_workspace_from_check(self):
        good = "secret_" + "a" * 40
        st = notion.resolve_status("admin_bot",
                                    read_cfg=lambda b: {"access_token": good},
                                    check_token=lambda t: {
                                        "ok": True, "status": "valid",
                                        "workspace_name": "X",
                                        "integration_name": "Y",
                                    })
        assert st.status == "valid"
        assert st.workspace_name == "X"
        assert st.integration_name == "Y"

    def test_revoked_carries_stored_workspace_for_ui(self):
        """When a token is revoked we still want to tell the user 'X workspace
        rejected the key' rather than just 'rejected' — so we preserve the
        stored workspace name on the InstallStatus."""
        good = "secret_" + "a" * 40
        st = notion.resolve_status("admin_bot",
                                    read_cfg=lambda b: {
                                        "access_token": good,
                                        "workspace_name": "Carla's Workspace",
                                    },
                                    check_token=lambda t: {
                                        "ok": False, "status": "revoked",
                                        "error": "unauthorized",
                                    })
        assert st.status == "revoked"
        assert st.workspace_name == "Carla's Workspace"


# ── Install plan ──────────────────────────────────────────────────────────────

class TestInstallPlan:
    def test_valid_yields_no_steps(self):
        st = notion.InstallStatus(bot_id="admin_bot", status="valid")
        assert notion.build_install_plan(st) == []

    def test_missing_yields_set_config_then_confirm(self):
        st = notion.InstallStatus(bot_id="admin_bot", status="missing")
        steps = notion.build_install_plan(st)
        assert [s.id for s in steps] == ["set_config", "confirm"]
        # Notion is single-field: just access_token (no URL like HA).
        names = [f["name"] for f in steps[0].fields]
        assert names == ["access_token"]
        assert steps[0].fields[0]["type"] == "password"

    def test_set_config_endpoint_is_set_token(self):
        """The generic modal posts to step.endpoint — must match the
        Flask route at /api/skills/install/notion/set-token."""
        st = notion.InstallStatus(bot_id="admin_bot", status="missing")
        steps = notion.build_install_plan(st)
        assert steps[0].endpoint.endswith("/notion/set-token")


# ── Flask route smoke tests ───────────────────────────────────────────────────


# Unit tests above (token format, verify_token, resolve_status,
# build_install_plan) stay because the helpers are still in use by the
# rewired MCP-backed install path (third revival landed late 2026-05-30,
# same day as the original withdrawal). New tests for the MCP-install
# helpers + the wrapper route follow.


import json as _json
from unittest.mock import patch


# ── Headers JSON encoding (Notion-specific to the MCP server's auth shape) ──


class TestBuildHeadersJson:
    """build_headers_json constructs the OPENAPI_MCP_HEADERS value that
    @notionhq/notion-mcp-server expects — a JSON string with the Bearer
    auth header AND the Notion-Version header. The wrapper route hides
    this from operators; they paste a plain Internal Integration Secret."""

    def test_includes_bearer_authorization(self):
        result = notion.build_headers_json("ntn_test_secret_xyz")
        parsed = _json.loads(result)
        assert parsed["Authorization"] == "Bearer ntn_test_secret_xyz"

    def test_includes_notion_version(self):
        result = notion.build_headers_json("secret_legacy_token")
        parsed = _json.loads(result)
        assert parsed["Notion-Version"] == notion.NOTION_API_VERSION

    def test_strips_whitespace_from_token(self):
        result = notion.build_headers_json("   ntn_padded   ")
        parsed = _json.loads(result)
        assert parsed["Authorization"] == "Bearer ntn_padded"

    def test_empty_token_raises(self):
        import pytest
        with pytest.raises(ValueError, match="token is required"):
            notion.build_headers_json("")

    def test_whitespace_only_token_raises(self):
        import pytest
        with pytest.raises(ValueError, match="token is required"):
            notion.build_headers_json("   ")

    def test_compact_json_no_spaces(self):
        """Compact encoding matters because the keystore value is
        substituted verbatim into shell env at MCP-server exec time —
        extra whitespace doesn't break things but smaller blobs are
        cheaper to pass through subprocess env."""
        result = notion.build_headers_json("ntn_x")
        assert ": " not in result  # JSON separator is ":"  not ": "
        assert ", " not in result  # Same for ","


# ── Keystore slot naming ──────────────────────────────────────────────────────


class TestKeystoreSlot:
    """The per-bot slot pattern (notion-<bot>) mirrors Telegram's
    per-bot token model — each bot can connect to a different Notion
    workspace."""

    def test_slot_is_per_bot(self):
        assert notion.keystore_slot_for("team_bot_b") == "notion-team_bot_b"
        assert notion.keystore_slot_for("admin_bot") == "notion-admin_bot"

    def test_two_bots_get_distinct_slots(self):
        assert notion.keystore_slot_for("team_bot_b") != notion.keystore_slot_for("admin_bot")


# ── MCP-aware status resolver ─────────────────────────────────────────────────


class TestResolveStatusMcp:
    def test_missing_when_mcp_block_absent(self):
        def _read(_bot_id):
            return {"mcp": {"servers": {}}}, None

        status = notion.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read, read_keystore_slot=lambda _s: "anything",
        )
        assert status.status == "missing"

    def test_valid_when_mcp_block_and_keystore_present(self):
        def _read(_bot_id):
            return {
                "mcp": {
                    "servers": {
                        "notion": {
                            "command": "/launcher",
                            "args": [],
                        },
                    },
                },
            }, None

        status = notion.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: '{"Authorization":"Bearer x"}',
        )
        assert status.status == "valid"
        assert status.error is None

    def test_revoked_when_mcp_block_present_but_keystore_empty(self):
        """Operator wiped the keystore slot manually but didn't uninstall
        the MCP server — surface as ``revoked`` so the UI prompts for
        re-paste rather than claiming everything's fine."""
        def _read(_bot_id):
            return {
                "mcp": {"servers": {"notion": {"command": "/l", "args": []}}},
            }, None

        status = notion.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            read_keystore_slot=lambda _s: None,  # slot exists but empty
        )
        assert status.status == "revoked"
        assert "keystore_slot_empty" in (status.error or "")

    def test_valid_without_keystore_check_when_reader_not_injected(self):
        """When read_keystore_slot is None, the resolver trusts the
        mcp.servers entry alone. Avoids second-guessing the loader-side
        signal when the keystore reader is unavailable."""
        def _read(_bot_id):
            return {
                "mcp": {"servers": {"notion": {"command": "/l", "args": []}}},
            }, None

        status = notion.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
            # No read_keystore_slot — defaults to skipping the slot check
        )
        assert status.status == "valid"

    def test_unknown_when_oc_unreadable(self):
        def _read(_bot_id):
            return None, "permission_denied"

        status = notion.resolve_status_mcp(
            "team_bot_b", read_oc_config=_read,
        )
        assert status.status == "unknown"
        assert "permission_denied" in (status.error or "")


# ── Access panel: post-install page-sharing callout ───────────────────────────


class TestAccessPanelPostInstallCallout:
    """Notion's permission model is per-page-share, which is the most
    common cause of "I installed it but the bot can't see my pages"
    confusion. The access panel must carry a post_install_callout the
    UI renders prominently on the confirmation screen."""

    def test_post_install_callout_present(self):
        callout = notion.NOTION_ACCESS_PANEL.get("post_install_callout")
        assert callout, "Notion access panel must have post_install_callout"
        # Must mention page-sharing in user-visible terms
        assert "share" in callout.lower() or "sharing" in callout.lower()
        # Must mention the Integration concept (matches Notion's UI)
        assert "integration" in callout.lower()


# ── Route integration: /api/skills/install/notion/set-token ───────────────────


import pytest as _pytest


@_pytest.fixture
def notion_route_app(tmp_path):
    """Flask app + stubs for the notion install route. Mirrors the
    fixture in test_skills_dropbox_install.py."""
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
        "integration_name": "Evolve Bot",
        "error": None, "http_status": 200,
    }


class TestSetTokenRoute:
    def test_missing_bot_id_rejected(self, notion_route_app):
        app, _, fake = notion_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"access_token": "ntn_x"},
            )
        assert r.status_code == 400
        assert "bot_id" in r.get_json()["error"]

    def test_missing_token_rejected(self, notion_route_app):
        app, _, fake = notion_route_app
        with _stub_create_apply(fake):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"bot_id": "team_bot_b"},
            )
        assert r.status_code == 400
        assert "access_token" in r.get_json()["error"]

    def test_invalid_token_format_rejected_before_keystore_or_proposal(
        self, notion_route_app,
    ):
        """Token-format check should bail before we touch the keystore or
        create the proposal — otherwise a typo would land bad data."""
        app, captured, fake = notion_route_app

        def _bad_format(_token):
            return {
                "ok": False, "status": "invalid",
                "workspace_name": None, "integration_name": None,
                "error": "invalid_token_format", "http_status": 0,
            }

        with _stub_create_apply(fake), \
             patch.object(notion, "verify_token", _bad_format):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"bot_id": "team_bot_b", "access_token": "not-a-real-token"},
            )
        assert r.status_code == 400
        assert captured == []  # no proposal got through

    def test_revoked_token_returns_401(self, notion_route_app):
        app, captured, fake = notion_route_app

        def _revoked(_token):
            return {
                "ok": False, "status": "revoked",
                "workspace_name": None, "integration_name": None,
                "error": "unauthorized", "http_status": 401,
            }

        with _stub_create_apply(fake), \
             patch.object(notion, "verify_token", _revoked):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"bot_id": "team_bot_b", "access_token": "ntn_revoked"},
            )
        assert r.status_code == 401
        assert captured == []

    def test_happy_path_writes_keystore_and_creates_install_proposal(
        self, notion_route_app,
    ):
        """The critical end-to-end shape: verify_token → keystore write
        (JSON headers blob) → InstallMcpServer proposal with
        catalog_id="notion" + env_bindings referencing the slot."""
        app, captured, fake = notion_route_app

        # Track keystore writes via a fake KeystoreManager
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
             patch.object(notion, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _FakeKS), \
             patch.object(notion, "resolve_status_mcp",
                          return_value=notion.InstallStatus(
                              bot_id="team_bot_b", status="valid",
                              workspace_name="TeamBotB's Workspace",
                              integration_name="Evolve Bot",
                          )):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"bot_id": "team_bot_b", "access_token": "ntn_real_secret_abc"},
            )

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["status"] == "valid"
        assert body.get("workspace_name") == "TeamBotB's Workspace"

        # Keystore: one write to notion-team_bot_b with the JSON headers blob
        assert len(ks_writes) == 1
        slot, value = ks_writes[0]
        assert slot == "notion-team_bot_b"
        # The stored value is the JSON headers — confirm the bearer is there
        parsed = _json.loads(value)
        assert parsed["Authorization"] == "Bearer ntn_real_secret_abc"
        assert parsed["Notion-Version"] == notion.NOTION_API_VERSION

        # Proposal shape — the critical install contract
        assert len(captured) == 1
        prop = captured[0]
        assert prop["kind"] == "InstallMcpServer"
        payload = prop["payload"]
        assert payload["bot_id"] == "team_bot_b"
        assert payload["server_id"] == "notion"
        # catalog_id is "notion" (NOT "filesystem" — Notion uses its own
        # MCP server, unlike Obsidian/Dropbox which share the filesystem one)
        assert payload["catalog_id"] == "notion"
        # env_bindings references the keystore slot we just wrote
        assert payload["env_bindings"] == {
            "OPENAPI_MCP_HEADERS": "keystore:notion-team_bot_b",
        }

    def test_keystore_failure_does_not_create_proposal(self, notion_route_app):
        """If the keystore write fails, no InstallMcpServer proposal should
        be created — otherwise we'd ship an MCP install with a missing
        credential. Better to surface the keystore error and let the user
        retry."""
        app, captured, fake = notion_route_app

        class _BrokenKS:
            def __init__(self, _shared_dir):
                raise RuntimeError("keystore corrupted")

        with _stub_create_apply(fake), \
             patch.object(notion, "verify_token", _ok_verify), \
             patch("evolve_admin.keystore.KeystoreManager", _BrokenKS):
            r = app.test_client().post(
                "/api/skills/install/notion/set-token",
                json={"bot_id": "team_bot_b", "access_token": "ntn_x"},
            )
        assert r.status_code == 500
        assert "keystore" in r.get_json()["error"].lower()
        assert captured == []

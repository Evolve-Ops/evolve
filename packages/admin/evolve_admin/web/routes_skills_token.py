"""HTTP routes for the token-based skill installs — Notion / Runway / Linear.

``/api/skills/install/notion/{set-token,revoke}``
``/api/skills/install/runway/{set-token,revoke}``
``/api/skills/install/linear/{set-token,revoke}``

The least-entangled slice of the ``/api/skills/install/*`` region: each of
these integrations installs by storing a single operator-pasted secret
(verify → keystore/auth-profile write → MCP-or-bundled-plugin install →
kickstart) and uninstalls by clearing it. **No OAuth-callback dance**
(Slack/Discord) and **no device-pairing poll** (WhatsApp/Signal/Telegram) —
those clusters move in later sub-PRs.

Split out of ``routes_admin.py``'s ``register_admin_routes`` closure — 4.1b
Increment 2a (skills token-install cluster) — per the strategy memo
``docs/design-routes-admin-decomposition-2026-06-12.md`` (Option A: a sibling
``register_*_routes(app, network_path)`` module mirroring the ~12 that already
exist; NOT Blueprints, NOT a ctx object). Pure code-motion: no route
added/removed/renamed/re-pathed/re-method-ed; no request/response shape,
validation, error-handling, sudo, or keystore behavior change.

Privileged surface (doctrine auditor-grade bar):
  * Secrets touched: per-bot Notion ``OPENAPI_MCP_HEADERS`` and Linear
    ``LINEAR_API_KEY`` live in the keystore at slots ``notion-<bot>`` /
    ``linear-<bot>`` (``scope=shared``); the Runway API key lives in the bot's
    ``auth-profiles.json`` at ``profiles["runway:default"]``.
  * Keystore writes go through ``evolve_admin.keystore.KeystoreManager``;
    auth-profile / openclaw.json writes go through ``runway_install``'s
    sudo-aware helpers (per CLAUDE.md /tmp-staging + ``sudo /bin/cp``).
  * Proposal create/apply is the only privileged side effect besides the
    keystore — it runs via the late-bound ``server._operator_create_apply``
    shim (see §1.3 below), never a direct store write.

§1.3 monkeypatch-at-call-time invariant (memo): handlers reach patchable
server helpers through ``_module._NAME`` (``_module = sys.modules[
"evolve_admin.web.server"]``) at call time, so test monkeypatches on
``server._NAME`` are honored. The helpers this surface touches that way are
``server._audit_log_entry`` and ``server._operator_create_apply`` — NOT
imported as module-level names here (that would shadow the patch).

The three ``_*_resolve_status`` shims are recreated verbatim here (they are
also called by the generic ``/api/skills/install/<skill_id>{,/status}``
dispatchers that stay in ``routes_admin.py``); recreating the thin wrapper —
rather than importing it — keeps the moved call sites byte-identical, the same
shape Increment 1 used for ``_resolve_user``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_skills_token")


def register_skills_token_routes(app: Flask, network_path: Path) -> None:
    # Late-bound server-module handle (memo §1.3): handlers reach patchable
    # helpers via ``_module._NAME`` at call time so test monkeypatches on
    # ``server._NAME`` are respected. Derived inside the function (mirroring
    # ``routes_admin.register_admin_routes``) so importing this module never
    # requires ``server`` to be in ``sys.modules`` yet.
    _module = sys.modules["evolve_admin.web.server"]

    from ..skills import notion_install as _notion
    from ..skills import runway_install as _runway
    from ..skills import linear_install as _linear

    # ── Notion skill helpers (MCP-backed) ─────────────────────────────────────
    # Notion uses catalog_id="notion" (not "filesystem") and the JSON-encoded
    # OPENAPI_MCP_HEADERS env var via env_bindings. The keystore slot is per-bot
    # (mirrors Telegram's bot-token-per-bot pattern). The status resolver
    # mirrors Obsidian/Dropbox; the helpers know nothing about ACLs because
    # Notion's permission model is enforced by Notion itself (per-page sharing).

    def _notion_resolve_status(bot_id: str) -> "_notion.InstallStatus":
        """Resolve the Notion install status via openclaw.json + keystore.

        Reads ``mcp.servers.notion`` for the loader-side signal and also
        checks the per-bot keystore slot — if the slot was wiped without
        also removing mcp.servers.notion, we report ``revoked`` instead of
        ``valid`` so the UI can prompt for re-paste.
        """
        from ..skills import _oc_install_common as _oc_common

        def _read_slot(slot: str) -> str | None:
            try:
                from evolve_admin.keystore import KeystoreManager
                mgr = KeystoreManager(Path(
                    load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
                ))
                return mgr.get_value(slot)
            except Exception:
                # Keystore unreachable → don't second-guess the mcp.servers
                # entry; report valid based on the openclaw.json signal alone.
                # (Returning None would flip the status to revoked, which is
                # the wrong answer when the keystore itself is broken.)
                return "<keystore_unreachable_assume_present>"

        return _notion.resolve_status_mcp(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
            read_keystore_slot=_read_slot,
        )

    def _create_notion_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Same shape as the obsidian/dropbox helpers — third near-identical
        closure. When a fourth MCP skill ships, extracting these into a
        shared ``_create_mcp_skill_proposal(action_kind, payload, bot_id,
        summary, *, technique="operator_ui_install", dimension="operational_health")``
        helper is worth the small refactor.
        """
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["bot_config"],
        )
        return _module._operator_create_apply(
            action_kind=action_kind,
            action_payload=action_payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_install",
            dimension="operational_health",
            risk=risk,
            shared_dir=Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ),
        )

    # POST /api/skills/install/notion/set-token
    #   body: {bot_id, access_token}
    #
    # Mirrors Obsidian/Dropbox structurally but differs in storage shape:
    #   1. Validate the token format and verify against Notion's API.
    #   2. Build the OPENAPI_MCP_HEADERS JSON string from the plain token
    #      (operator never writes JSON — wrapper hides the encoding).
    #   3. Save the JSON to the keystore at slot ``notion-<bot>``.
    #   4. Install mcp.servers.notion via the InstallMcpServer applier with
    #      env_bindings={"OPENAPI_MCP_HEADERS": "keystore:notion-<bot>"}.
    #
    # If the InstallMcpServer proposal fails, the keystore slot is rolled
    # back so the system stays consistent (same rollback shape as the
    # Obsidian set-vault-path route's ACL grant rollback).

    @app.post("/api/skills/install/notion/set-token")
    def api_skills_notion_set_token() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        access_token = (body.get("access_token") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not access_token:
            return jsonify({"ok": False, "error": "access_token required"}), 400

        # 1. Verify the token works against Notion's /v1/users/me. Catches
        # bad format, revoked tokens, and connection errors before we
        # touch the keystore.
        verify_result = _notion.verify_token(access_token)
        if not verify_result.get("ok"):
            status_code = {
                "invalid": 400,
                "revoked": 401,
                "unknown": 502,
            }.get(verify_result.get("status") or "", 400)
            return jsonify({
                "ok": False,
                "status": verify_result.get("status"),
                "error": verify_result.get("error") or "verification_failed",
            }), status_code

        # 2. Build the OPENAPI_MCP_HEADERS JSON — operator gives us a plain
        # secret; the MCP server wants a JSON-encoded headers blob.
        try:
            headers_json = _notion.build_headers_json(access_token)
        except ValueError as exc:
            return jsonify({
                "ok": False, "error": f"headers_build_failed: {exc}",
            }), 400

        # 3. Save to the keystore. If a slot already exists (re-install
        # with a new token), set_value updates in place.
        slot = _notion.keystore_slot_for(bot_id)
        shared_dir = Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(shared_dir)
            existing = mgr.ks.get_key_entry(slot)
            if existing:
                mgr.set_value(slot, headers_json)
            else:
                mgr.register(
                    slot,
                    provider="notion",
                    scope="shared",
                    description=(
                        f"Notion OPENAPI_MCP_HEADERS for bot {bot_id} — "
                        f"workspace {verify_result.get('workspace_name') or '?'}"
                    ),
                    bots=None,
                    value=headers_json,
                )
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"keystore_write_failed: {exc.__class__.__name__}: {exc}",
            }), 500

        # 4. Install the MCP server entry. catalog_id="notion" + env_bindings
        # referencing the slot we just wrote.
        summary = (
            f"Install Notion MCP for {bot_id} "
            f"(workspace={verify_result.get('workspace_name') or '?'!r})"
        )
        action_payload = {
            "bot_id": bot_id,
            "server_id": _notion.NOTION_MCP_SERVER_ID,
            "catalog_id": "notion",
            "env_bindings": {
                "OPENAPI_MCP_HEADERS": f"keystore:{slot}",
            },
        }
        proposal, err = _create_notion_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        if err:
            # Roll back the keystore write — the install didn't take.
            try:
                mgr.set_value(slot, "")  # blank out the value
            except Exception:
                pass
            _module._audit_log_entry("skill.notion.set_token", bot_id, {
                "ok": False,
                "workspace": verify_result.get("workspace_name"),
                "error": f"mcp_install_create_failed: {err}",
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_create_failed: {err}",
            }), 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            # Applier refused — same rollback as the create-error path.
            try:
                mgr.set_value(slot, "")
            except Exception:
                pass
            _module._audit_log_entry("skill.notion.set_token", bot_id, {
                "ok": False,
                "workspace": verify_result.get("workspace_name"),
                "proposal_status": prop_status,
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_applier_returned_{prop_status}",
                "proposal": proposal,
            }), 500

        _module._audit_log_entry("skill.notion.set_token", bot_id, {
            "ok": True,
            "workspace": verify_result.get("workspace_name"),
            "integration_name": verify_result.get("integration_name"),
            "mcp_proposal_status": prop_status,
        })

        status = _notion_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            **status.to_dict(),
            "workspace_name": verify_result.get("workspace_name"),
            "integration_name": verify_result.get("integration_name"),
        })

    @app.post("/api/skills/install/notion/revoke")
    def api_skills_notion_revoke() -> Response:
        """Remove the Notion MCP server + clear the keystore slot.

        The integration object itself remains in the user's Notion workspace
        until they delete it from notion.so → My Integrations — Notion's
        API doesn't expose a token-holder-side revoke. Local revoke clears
        what we control.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        # Remove the MCP server entry first — load-bearing for "bot can no
        # longer reach Notion on next gateway boot".
        proposal, err = _create_notion_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": _notion.NOTION_MCP_SERVER_ID},
            bot_id=bot_id,
            summary=f"Remove Notion MCP from {bot_id}",
        )

        # Blank the keystore slot (best-effort). We don't fully delete the
        # slot entry — keeping the registration around makes a future
        # re-install slightly faster and the empty value is enough to
        # disable the credential.
        slot_cleared = False
        slot_err: str | None = None
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ))
            slot = _notion.keystore_slot_for(bot_id)
            if mgr.ks.get_key_entry(slot):
                mgr.set_value(slot, "")
                slot_cleared = True
        except Exception as exc:
            slot_err = str(exc)

        _module._audit_log_entry("skill.notion.revoke", bot_id, {
            "ok": err is None,
            "proposal_status": (proposal or {}).get("status"),
            "keystore_slot_cleared": slot_cleared,
            "keystore_err": slot_err,
            "create_error": err,
        })
        if err:
            return jsonify({
                "ok": False,
                "error": f"mcp_remove_create_failed: {err}",
            }), 500
        return jsonify({
            "ok": True,
            "proposal": proposal,
            "keystore_slot_cleared": slot_cleared,
            "note": (
                "Local credential cleared. To fully revoke access, also "
                "delete the integration from notion.so → My Integrations "
                "(or unshare the pages from it)."
            ),
        })

    # ── Runway skill helpers (bundled-plugin, not MCP) ────────────────────────
    # Runway uses OC's bundled @openclaw/runway-provider — no MCP server is
    # involved. Install writes auth-profiles.json + openclaw.json
    # videoGenerationModel.primary, then kickstarts the gateway. The
    # status resolver checks both signals (model-default + auth profile)
    # so we can distinguish valid vs revoked vs invalid vs missing.

    def _runway_resolve_status(bot_id: str) -> "_runway.InstallStatus":
        """Resolve Runway install status via openclaw.json + auth-profiles.json.

        Uses the shared _oc_install_common.read_oc_config helper (same as
        Obsidian / Dropbox / Notion). The auth-profiles reader defaults
        to runway_install.read_auth_profiles (direct read + sudo /bin/cat
        fallback per CLAUDE.md). Both readers are injectable for testing.
        """
        from ..skills import _oc_install_common as _oc_common
        return _runway.resolve_status_bundled(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
        )

    # POST /api/skills/install/runway/set-token
    #   body: {bot_id, access_token}
    #
    # Distinct from the MCP install pattern (Obsidian / Dropbox / Notion /
    # GitHub-MCP / Linear). Runway uses OC's bundled @openclaw/runway-provider —
    # no MCP server, no env_bindings layer. The install:
    #   1. Verify the API key against Runway's /v1/organization.
    #   2. Write profiles["runway:default"] into auth-profiles.json
    #      (mirrors how Google OAuth profiles are stored).
    #   3. Set agents.defaults.videoGenerationModel.primary in openclaw.json.
    #   4. Kickstart the gateway so OC re-reads both files.
    # Same pattern will serve any future bundled OC provider (Google
    # Veo, Synthesia if it ships, etc).

    @app.post("/api/skills/install/runway/set-token")
    def api_skills_runway_set_token() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        access_token = (body.get("access_token") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not access_token:
            return jsonify({"ok": False, "error": "access_token required"}), 400

        # 1. Verify against /v1/organization. Bails 400/401/502 before
        # any filesystem writes.
        verify_result = _runway.verify_token(access_token)
        if not verify_result.get("ok"):
            status_code = {
                "invalid": 400,
                "revoked": 401,
                "unknown": 502,
            }.get(verify_result.get("status") or "", 400)
            return jsonify({
                "ok": False,
                "status": verify_result.get("status"),
                "error": verify_result.get("error") or "verification_failed",
            }), status_code

        # 2. Write auth-profiles.json profile. Preserves other providers.
        auth_ok, auth_err = _runway.write_runway_auth_profile(bot_id, access_token)
        if not auth_ok:
            _module._audit_log_entry("skill.runway.set_token", bot_id, {
                "ok": False,
                "organization_name": verify_result.get("organization_name"),
                "error": f"auth_profile_write_failed: {auth_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"auth_profile_write_failed: {auth_err}",
                "hint": (
                    "Could not write auth-profiles.json. Check that the "
                    "bot's ~/.openclaw/agents/main/agent/ directory is "
                    "writable and sudoers grants the evolve user."
                ),
            }), 500

        # 3. Set videoGenerationModel.primary in openclaw.json.
        oc_ok, oc_err = _runway.enable_runway_in_oc_config(bot_id)
        if not oc_ok:
            # Rollback the auth-profiles.json write so we don't leave a
            # half-installed state. If the rollback itself fails, the
            # error gets surfaced via the audit log; the user can retry.
            _runway.delete_runway_auth_profile(bot_id)
            _module._audit_log_entry("skill.runway.set_token", bot_id, {
                "ok": False,
                "organization_name": verify_result.get("organization_name"),
                "error": f"oc_config_write_failed: {oc_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"oc_config_write_failed: {oc_err}",
                "hint": "Could not update openclaw.json with the runway model default.",
            }), 500

        # 4. Kickstart the gateway so OC re-reads both files.
        from ..skills import _oc_install_common as _oc_common
        kick_ok, kick_err = _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.runway.set_token", bot_id, {
            "ok": True,
            "organization_name": verify_result.get("organization_name"),
            "organization_tier": verify_result.get("organization_tier"),
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })

        status = _runway_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            **status.to_dict(),
            "organization_name": verify_result.get("organization_name"),
            "organization_tier": verify_result.get("organization_tier"),
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })

    @app.post("/api/skills/install/runway/revoke")
    def api_skills_runway_revoke() -> Response:
        """Remove the runway:default auth profile + unset
        videoGenerationModel.primary + kickstart the gateway.

        Does NOT delete the API key from Runway's side — Runway doesn't
        expose a token-holder-side revoke. Operator must also delete the
        key at app.runwayml.com → API Keys for full revocation.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        # Best-effort: clear both signals. If either fails, surface in
        # audit log but keep going so we don't leave a partial revoke.
        auth_ok, auth_err = _runway.delete_runway_auth_profile(bot_id)
        oc_ok, oc_err = _runway.disable_runway_in_oc_config(bot_id)
        from ..skills import _oc_install_common as _oc_common
        kick_ok, kick_err = _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.runway.revoke", bot_id, {
            "auth_profile_removed": auth_ok,
            "auth_profile_err": auth_err,
            "oc_config_cleared": oc_ok,
            "oc_config_err": oc_err,
            "gateway_kickstarted": kick_ok,
        })

        # 200 even on partial failure — the user can retry; nothing is
        # in a worse state than they started.
        return jsonify({
            "ok": True,
            "auth_profile_removed": auth_ok,
            "oc_config_cleared": oc_ok,
            "gateway_kickstarted": kick_ok,
            "note": (
                "Local credential cleared and openclaw.json updated. To "
                "fully revoke, also delete the key in the Runway "
                "dashboard → API Keys (Runway doesn't expose a "
                "token-holder-side revoke)."
            ),
        })

    # ── Linear skill helpers (MCP-backed) ─────────────────────────────────────
    # Linear uses catalog_id="linear" + env_bindings with the verbatim
    # LINEAR_API_KEY (linear-mcp reads a plain env var, no JSON encoding
    # like Notion). Per-bot keystore slot ``linear-<bot>``.

    def _linear_resolve_status(bot_id: str) -> "_linear.InstallStatus":
        """Resolve the Linear install status via openclaw.json + keystore.

        Mirrors _notion_resolve_status — same loader-side signal check
        plus keystore-presence verification. If the slot was wiped without
        also removing mcp.servers.linear, we report ``revoked`` instead of
        ``valid`` so the UI prompts for re-paste.
        """
        from ..skills import _oc_install_common as _oc_common

        def _read_slot(slot: str) -> str | None:
            try:
                from evolve_admin.keystore import KeystoreManager
                mgr = KeystoreManager(Path(
                    load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
                ))
                return mgr.get_value(slot)
            except Exception:
                # Same keystore-unreachable handling as Notion — don't
                # let a broken keystore flip status to revoked.
                return "<keystore_unreachable_assume_present>"

        return _linear.resolve_status_mcp(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
            read_keystore_slot=_read_slot,
        )

    def _create_linear_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Sixth near-identical closure (after obsidian/dropbox/notion/github-mcp).

        At this point the recurrence is strong enough that a shared
        ``_create_mcp_skill_proposal(action_kind, payload, bot_id, summary)``
        helper would be cleaner — left in this shape on purpose so the
        end-of-roadmap audit pass can do the refactor in one place rather
        than chasing six call sites scattered through the file.
        """
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["bot_config"],
        )
        return _module._operator_create_apply(
            action_kind=action_kind,
            action_payload=action_payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_install",
            dimension="operational_health",
            risk=risk,
            shared_dir=Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ),
        )

    # POST /api/skills/install/linear/set-token
    #   body: {bot_id, access_token}
    #
    # Mirrors notion/set-token structurally; differs in storage shape:
    #   1. Validate the token format + verify against Linear's GraphQL viewer query.
    #   2. Save the verbatim PAT to the keystore at slot ``linear-<bot>``
    #      (linear-mcp reads LINEAR_API_KEY directly — no JSON encoding).
    #   3. Install mcp.servers.linear via InstallMcpServer with
    #      env_bindings={"LINEAR_API_KEY": "keystore:linear-<bot>"}.
    # Rollback semantics mirror Notion: if the InstallMcpServer applier
    # refuses, blank the keystore slot so the system stays consistent.

    @app.post("/api/skills/install/linear/set-token")
    def api_skills_linear_set_token() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        access_token = (body.get("access_token") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not access_token:
            return jsonify({"ok": False, "error": "access_token required"}), 400

        # 1. Verify the token works against Linear's GraphQL. Catches
        # bad format, revoked keys, and connection errors before we
        # touch the keystore.
        verify_result = _linear.verify_token(access_token)
        if not verify_result.get("ok"):
            status_code = {
                "invalid": 400,
                "revoked": 401,
                "unknown": 502,
            }.get(verify_result.get("status") or "", 400)
            return jsonify({
                "ok": False,
                "status": verify_result.get("status"),
                "error": verify_result.get("error") or "verification_failed",
            }), status_code

        # 2. Save the verbatim PAT to the keystore. If a slot already
        # exists (re-install with a new key), set_value updates in place.
        slot = _linear.keystore_slot_for(bot_id)
        shared_dir = Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(shared_dir)
            existing = mgr.ks.get_key_entry(slot)
            if existing:
                mgr.set_value(slot, access_token)
            else:
                mgr.register(
                    slot,
                    provider="linear",
                    scope="shared",
                    description=(
                        f"Linear API key for bot {bot_id} — "
                        f"workspace {verify_result.get('workspace_name') or '?'} "
                        f"(viewer {verify_result.get('viewer_name') or '?'})"
                    ),
                    bots=None,
                    value=access_token,
                )
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"keystore_write_failed: {exc.__class__.__name__}: {exc}",
            }), 500

        # 3. Install the MCP server entry. catalog_id="linear" + env_bindings
        # referencing the slot we just wrote.
        summary = (
            f"Install Linear MCP for {bot_id} "
            f"(workspace={verify_result.get('workspace_name') or '?'!r})"
        )
        action_payload = {
            "bot_id": bot_id,
            "server_id": _linear.LINEAR_MCP_SERVER_ID,
            "catalog_id": "linear",
            "env_bindings": {
                "LINEAR_API_KEY": f"keystore:{slot}",
            },
        }
        proposal, err = _create_linear_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        if err:
            # Roll back the keystore write — the install didn't take.
            try:
                mgr.set_value(slot, "")
            except Exception:
                pass
            _module._audit_log_entry("skill.linear.set_token", bot_id, {
                "ok": False,
                "workspace": verify_result.get("workspace_name"),
                "error": f"mcp_install_create_failed: {err}",
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_create_failed: {err}",
            }), 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            try:
                mgr.set_value(slot, "")
            except Exception:
                pass
            _module._audit_log_entry("skill.linear.set_token", bot_id, {
                "ok": False,
                "workspace": verify_result.get("workspace_name"),
                "proposal_status": prop_status,
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_applier_returned_{prop_status}",
                "proposal": proposal,
            }), 500

        _module._audit_log_entry("skill.linear.set_token", bot_id, {
            "ok": True,
            "workspace": verify_result.get("workspace_name"),
            "viewer": verify_result.get("viewer_name"),
            "mcp_proposal_status": prop_status,
        })

        status = _linear_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            **status.to_dict(),
            "workspace_name": verify_result.get("workspace_name"),
            "viewer_name": verify_result.get("viewer_name"),
        })

    @app.post("/api/skills/install/linear/revoke")
    def api_skills_linear_revoke() -> Response:
        """Remove the Linear MCP server + clear the keystore slot.

        The API key itself remains on the user's Linear account until they
        delete it from linear.app → Settings → API → Personal API keys —
        Linear's API doesn't expose a token-holder-side revoke. Local
        revoke clears what we control.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        proposal, err = _create_linear_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": _linear.LINEAR_MCP_SERVER_ID},
            bot_id=bot_id,
            summary=f"Remove Linear MCP from {bot_id}",
        )

        slot_cleared = False
        slot_err: str | None = None
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ))
            slot = _linear.keystore_slot_for(bot_id)
            if mgr.ks.get_key_entry(slot):
                mgr.set_value(slot, "")
                slot_cleared = True
        except Exception as exc:
            slot_err = str(exc)

        _module._audit_log_entry("skill.linear.revoke", bot_id, {
            "ok": err is None,
            "proposal_status": (proposal or {}).get("status"),
            "keystore_slot_cleared": slot_cleared,
            "keystore_err": slot_err,
            "create_error": err,
        })
        if err:
            return jsonify({
                "ok": False,
                "error": f"mcp_remove_create_failed: {err}",
            }), 500
        return jsonify({
            "ok": True,
            "proposal": proposal,
            "keystore_slot_cleared": slot_cleared,
            "note": (
                "Local credential cleared. To fully revoke access, also "
                "delete the personal API key from linear.app → Settings "
                "→ API → Personal API keys."
            ),
        })

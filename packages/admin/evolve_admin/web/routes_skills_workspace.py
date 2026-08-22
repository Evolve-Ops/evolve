"""HTTP routes for the remaining skills installs — generic dispatchers +
Obsidian / Dropbox / Google-Workspace / GitHub-MCP / iMessage.

This is the LAST cluster of the ``/api/skills/install/*`` region (4.1b
Increment 2d) — it completes the dissolution of ``register_admin_routes``'s
skills-install section started by 2a (token), 2b (OAuth) and 2c (pairing).
After this move, ``routes_admin.py`` holds ZERO ``/api/skills/install/``
``@app`` handlers. What lands here:

  * The generic per-skill dispatchers every skill routes through:
    ``GET /api/skills/install/<skill_id>/status``,
    ``POST /api/skills/install/<skill_id>`` (install-plan), and
    ``POST /api/skills/install/<skill_id>/enable-plugin`` (gog only).
  * The filesystem-MCP install handlers — Obsidian
    (``set-vault-path``/``revoke``/``set-mode``) and Dropbox
    (``set-folder-path``/``revoke``/``set-mode``).
  * The Google-Workspace install handlers — unified ``google`` and legacy
    ``google_workspace_write`` (``complete``/``revoke``).
  * The GitHub-MCP install handlers (``install-mcp-server``/``revoke-mcp-server``).
  * The iMessage install handlers (``set-handle``/``check-tcc``/``revoke``).

Split out of ``routes_admin.py``'s ``register_admin_routes`` closure per the
strategy memo ``docs/design-routes-admin-decomposition-2026-06-12.md``
(Option A: a sibling ``register_*_routes(app, network_path)`` module
mirroring the ~13 that already exist; NOT Blueprints, NOT a ctx object).
PURE code-motion: no route added/removed/renamed/re-pathed/re-method-ed; no
request/response shape, validation, error-handling, sudo, OAuth/token-secret,
or keystore behavior change.

Privileged surface (doctrine auditor-grade bar):
  * Secrets touched: the Google-Workspace ``complete``/``revoke`` handlers
    read/refresh the per-bot Google OAuth profile + client (auth-profiles
    + Evolve credential store), write/blank 3 keystore slots, write/wipe the
    token-shim credentials dir, and hit Google's revoke endpoint. The
    GitHub-MCP handler verifies a PAT against ``/user`` and writes it to the
    per-bot keystore slot (or binds the pod-wide slot). Obsidian/Dropbox
    grant/revoke macOS ACLs on the vault/folder and install/remove the
    filesystem MCP server entry. iMessage writes ``channels.imessage`` to the
    bot's openclaw.json + kickstarts.
  * All openclaw.json / keystore / credential writes go through the same
    ``_module._operator_create_apply`` proposal→applier pipeline and the
    per-skill install modules' sudo-aware helpers — unchanged by the move.

§1.3 monkeypatch-at-call-time invariant (memo): handlers reach the patchable
server helpers (``_audit_log_entry``, ``_operator_create_apply``,
``_resolve_bot_user``, ``_read_oc_json``) through ``_module._NAME``
(``_module = sys.modules["evolve_admin.web.server"]``) at call time, so test
monkeypatches on ``server._NAME`` are honored. They are NOT imported as
module-level names here (that would shadow the patch). The four STABLE server
names this surface uses as bare names (``_operator_proposal_response``,
``_google_http_form_post``, ``GOOGLE_REVOKE_URL``, ``_google_oauth_profile_id``)
are not patched by any test (verified) — imported at module top exactly as
``routes_admin.py`` does, so the moved call sites stay byte-identical.

Shared Google-OAuth + auth-profiles helpers are imported from
``routes_admin_shared.py`` (Inc 0 lift + the 4.1b-prep auth-profiles lift),
NOT duplicated; the thin network_path-binding shims below mirror the closure
shims that remain in ``routes_admin.py`` for the Google-onboard region.

Dedup note for the coordinator (separate item, NOT this PR): the
``_*_resolve_status`` shims for telegram/whatsapp/signal, slack/discord and
notion/runway/linear now exist here AND in the pairing/oauth/token cluster
modules (the established 2a/2b/2c relocation — net duplication unchanged, the
routes_admin.py copies are deleted). The thin Google-OAuth binding shims +
``_write_google_oauth_profile`` also exist in ``routes_admin.py`` (kept there
for the Google-onboard region until Inc 4). A future lift could hoist the
resolve-status shims to a shared skills-status module and the Google-OAuth
binding shims to a factory.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore
from ..config import load_network
from ..telemetry import get_logger
from .server import (  # noqa: E402  (stable, non-patched server names — see docstring)
    GOOGLE_REVOKE_URL,
    _google_http_form_post,
    _google_oauth_profile_id,
    _operator_proposal_response,
)
from .routes_admin_shared import (
    _read_auth_profiles as _shared_read_auth_profiles,
    _write_auth_profiles as _shared_write_auth_profiles,
    _read_google_oauth_client as _shared_read_google_oauth_client,
    _read_google_oauth_profile as _shared_read_google_oauth_profile,
    _delete_google_oauth_profile as _shared_delete_google_oauth_profile,
    _ensure_fresh_google_access_token as _shared_ensure_fresh_google_access_token,
)

_log = get_logger("web.routes_skills_workspace")


def register_skills_workspace_routes(app: Flask, network_path: Path) -> None:
    # Late-bound server-module handle (memo §1.3): handlers reach the patchable
    # server helpers via ``_module._NAME`` at call time so test monkeypatches on
    # ``server._NAME`` are respected.
    _module = sys.modules["evolve_admin.web.server"]

    from ..skills import gog_install as _gog
    from ..skills import slack_install as _slack
    from ..skills import imessage_install as _imessage
    from ..skills import supported_on_host as _skill_supported_on_host
    from ..skills import whatsapp_install as _whatsapp
    from ..skills import signal_install as _signal
    from ..skills import discord_install as _discord
    from ..skills import telegram_install as _telegram
    from ..skills import upstream_plugin_skills as _upstream
    from ..skills import autocad_install as _autocad
    from ..skills import obsidian_install as _obsidian
    from ..skills import dropbox_install as _dropbox
    from ..skills import notion_install as _notion
    from ..skills import github_install as _github_mcp
    from ..skills import runway_install as _runway
    from ..skills import linear_install as _linear
    from ..skills import google_workspace_write_install as _gws_write
    from ..skills import google_workspace_token_shim as _gws_shim
    from ..skills import google_install as _google

    # ── Auth-profiles + Google-OAuth binding shims ────────────────────────────
    # Thin shims binding this app's ``network_path`` (and the late-bound
    # auth-profiles readers) to the module-level helpers in
    # ``routes_admin_shared``. Mirrors the closure shims in ``routes_admin.py``
    # so the moved call sites stay byte-identical; the shared helpers hold the
    # one canonical copy of the logic (no duplication).
    def _read_auth_profiles(bot_id: str) -> dict:
        return _shared_read_auth_profiles(bot_id, network_path=network_path)

    def _write_auth_profiles(bot_id: str, data: dict) -> bool:
        return _shared_write_auth_profiles(bot_id, data, network_path=network_path)

    def _read_google_oauth_client(bot_id: str | None = None) -> dict | None:
        return _shared_read_google_oauth_client(
            bot_id, network_path=network_path, read_auth_profiles=_read_auth_profiles,
        )

    def _read_google_oauth_profile(bot_id: str) -> dict | None:
        return _shared_read_google_oauth_profile(
            bot_id, read_auth_profiles=_read_auth_profiles,
        )

    def _write_google_oauth_profile(bot_id: str, profile: dict) -> bool:
        """Save the bot's Google OAuth profile (mirrors routes_admin.py)."""
        auth = _read_auth_profiles(bot_id) or {"profiles": {}}
        profiles = auth.setdefault("profiles", {})
        profiles[_google_oauth_profile_id(bot_id)] = profile
        return _write_auth_profiles(bot_id, auth)

    def _delete_google_oauth_profile(bot_id: str) -> bool:
        return _shared_delete_google_oauth_profile(
            bot_id,
            read_auth_profiles=_read_auth_profiles,
            write_auth_profiles=_write_auth_profiles,
        )

    def _ensure_fresh_google_access_token(bot_id: str) -> tuple[str | None, str | None]:
        return _shared_ensure_fresh_google_access_token(
            bot_id,
            network_path=network_path,
            read_auth_profiles=_read_auth_profiles,
            write_google_oauth_profile=_write_google_oauth_profile,
        )

    def _gog_plugin_enabled(bot_id: str) -> bool | None:
        """Live-read whether the bot's OpenClaw ``google`` plugin entry is enabled.

        Returns True/False if the inventory could be read; None on read error
        (callers treat None as "unknown", not as "disabled"). Re-uses
        :func:`plugins.inventory.read_inventory` — exactly one place parses
        ``plugins.entries`` from openclaw.json across the dashboard.
        """
        try:
            from plugins import inventory as _inv
        except ImportError:
            return None
        try:
            inv = _inv.read_inventory(bot_id)
        except Exception:
            return None
        if inv.read_error:
            return None
        for entry in inv.entries:
            if entry.name == _gog.GOG_PLUGIN_NAME:
                return bool(entry.enabled)
        return False  # plugin entry simply not present → not enabled

    def _gog_oauth_profile(bot_id: str) -> dict | None:
        """Adapter: read the bot's Google OAuth profile via the closure helper."""
        return _read_google_oauth_profile(bot_id)

    def _gog_oauth_client_configured() -> bool:
        return _read_google_oauth_client() is not None

    # ── Obsidian skill helpers (MCP-backed, rewired 2026-05-30) ───────────────
    # The skill is "active" when ``mcp.servers.obsidian`` is present in the
    # bot's openclaw.json (installed by the InstallMcpServer applier on the
    # filesystem catalog entry, with vault_path passed via extra_args). The
    # mode marker at ~/.openclaw/skills/obsidian_vault.json records read vs
    # read_write for the admin UI; absence is treated as legacy/unknown.

    def _obsidian_resolve_status(bot_id: str) -> "_obsidian.InstallStatus":
        """Resolve Obsidian install status — reads openclaw.json + mode marker.

        Uses the same shared read_oc_config helper that telegram/slack use
        for their install flows, so the read path / sudo-fallback behaviour
        stays consistent across skills.
        """
        from ..skills import _oc_install_common as _oc_common
        return _obsidian.resolve_status_mcp(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
        )

    def _create_obsidian_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Inline copy of _create_mcp_proposal from _register_mcp_admin_routes.

        The MCP-admin helper lives inside its own route-register closure
        and isn't visible from here. Duplicating the 8-line wrapper is
        cleaner than threading a module-level helper through both
        register-routes functions; the only contract is "create + auto-apply
        an operator-originated MCP install/remove proposal with bot-blast,
        auto-reversibility risk tag" which both skills (Obsidian today;
        future paste-token rewires per the design doc) will need.
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

    # ── Dropbox skill helpers (MCP-backed, second of the paste-token rewires) ─
    # Mirrors the Obsidian helpers exactly — folder_path replaces vault_path
    # in the payload, `dropbox` is the server_id in mcp.servers, and the mode
    # toggle is enforced via the same OS-ACL mechanism. See dropbox_install.py
    # for the full design rationale and the differences from Obsidian
    # (auto-detect from ~/.dropbox/info.json, looser path validation).

    def _dropbox_resolve_status(bot_id: str) -> "_dropbox.InstallStatus":
        """Resolve Dropbox install status via the shared read_oc_config helper.

        The resolver also fills in ``suggested_path`` by reading
        ~/.dropbox/info.json — done at the closure layer (not in the pure
        module) so the resolver stays unit-testable without sudo access.
        """
        from ..skills import _oc_install_common as _oc_common
        from ..config import bot_home as _bot_home

        status = _dropbox.resolve_status_mcp(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
        )
        # Only suggest a path when the bot has no install yet (active /
        # unknown statuses already have their folder_path or shouldn't be
        # nudged toward a different folder).
        if status.status == "no_folder_configured":
            try:
                home = _bot_home(bot_id)
                status.suggested_path = _dropbox.find_dropbox_folder(home)
            except Exception:
                pass
        return status

    def _create_dropbox_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Same inline-helper shape as _create_obsidian_mcp_proposal.

        Kept as a separate closure for symmetry — when the third
        filesystem skill arrives, extracting both into a shared
        ``_create_filesystem_skill_proposal`` helper becomes worth it.
        Until then, two near-identical closures are cleaner than one
        with conditional branching.
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

    # ── Notion skill helpers (MCP-backed, first non-filesystem rewire) ────────
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

    # ── GitHub-MCP skill helpers (purpose 2: LLM access) ─────────────────────
    # Same shape as Notion — API-key skill, credentials in keystore at
    # github-<bot>, validate against /user before install. Purpose 1
    # (backup) lives in upstream_plugin_skills and is unaffected.

    def _github_mcp_resolve_status(bot_id: str) -> "_github_mcp.InstallStatus":
        """Resolve the GitHub-MCP install status via openclaw.json + keystore."""
        from ..skills import _oc_install_common as _oc_common

        def _read_slot(slot: str) -> str | None:
            try:
                from evolve_admin.keystore import KeystoreManager
                mgr = KeystoreManager(Path(
                    load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
                ))
                return mgr.get_value(slot)
            except Exception:
                return "<keystore_unreachable_assume_present>"

        return _github_mcp.resolve_status_mcp(
            bot_id,
            read_oc_config=_oc_common.read_oc_config,
            read_keystore_slot=_read_slot,
        )

    def _create_github_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Same inline-helper shape as the obsidian / dropbox / notion ones.

        This is the fourth near-identical closure — the refactor threshold
        is now reasonable. A follow-up can extract them all into a single
        ``_create_mcp_skill_proposal(action_kind, payload, bot_id, summary)``
        that takes the standard RiskTag(bot, auto, ['bot_config']) and the
        operator_ui_install technique. Until then, four closures.
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

    # ── Runway skill helpers (bundled-plugin pattern — FIRST non-MCP rewire) ─
    # Runway uses OC's bundled @openclaw/runway-provider — no MCP server
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

    # ── Linear skill helpers (MCP-backed, fifth rewire) ──────────────────────
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

    # ── iMessage skill helpers — RE-ADDED 2026-06-04 (bundled-plugin rewire) ──
    # The 2026-05-30 withdrawal was correct about the Evolve implementation
    # being broken end-to-end, but missed that OC ships @openclaw/imessage at
    # dist/extensions/imessage/. The 2026-06-04 coverage audit caught the
    # gap. This rewire wires the bundled plugin via channels.imessage in
    # the bot's openclaw.json + plugins.entries.imessage.enabled, then
    # kickstarts. The poller / send-helper from V2.1-6 are now dead code
    # (deprecation queued for follow-on cleanup PR).
    #
    # Closure pattern mirrors _telegram_resolve_status — no overrides in
    # the production path; the resolver knows how to wire TCC + OC probe
    # defaults itself.

    def _imessage_resolve_status(bot_id: str) -> "_imessage.InstallStatus":
        """Resolve the iMessage install status for bot_id.

        resolve_status() handles all default callable wiring internally
        (TCC checks, OC config read, live probe). Tests pass stubs.
        """
        return _imessage.resolve_status(bot_id)

    # ── WhatsApp skill helpers (2026-06-04 bundled-plugin install) ────────────
    # WhatsApp is wired through OC's officially-shipped @openclaw/whatsapp
    # plugin (defaultChoice: clawhub). Pairing is via Baileys QR device-link
    # — operator scans on phone, Baileys writes authDir under the bot home.
    # Routes live further down the file; this closure mirrors the iMessage
    # pattern for the catalog / status dispatchers above.

    def _whatsapp_resolve_status(bot_id: str) -> "_whatsapp.InstallStatus":
        """Resolve the WhatsApp install status for bot_id.

        resolve_status() handles defaults internally (openclaw.json read,
        authDir probe, live OC probe). Tests pass stubs.
        """
        return _whatsapp.resolve_status(bot_id)

    # ── Signal skill helpers (2026-06-04 bundled-plugin install) ──────────────
    # **LICENSING REVIEW REQUIRED BEFORE MERGE** — see signal_install module
    # docstring. Signal is wired through OC's officially-shipped
    # @openclaw/signal plugin which transitively downloads signal-cli.
    # Pairing flow has one extra step vs WhatsApp because signal-cli needs
    # the E.164 phone number captured up-front (Baileys infers it from the
    # scanning device; signal-cli does not).

    def _signal_resolve_status(bot_id: str) -> "_signal.InstallStatus":
        """Resolve the Signal install status for bot_id.

        resolve_status() handles defaults internally (openclaw.json read,
        configDir probe, live OC probe). Tests pass stubs.
        """
        return _signal.resolve_status(bot_id)

    # ── Telegram skill helpers ────────────────────────────────────────────────
    # Telegram is a token skill: no OAuth dance, BotFather token stored at
    # ~/.openclaw/skills/telegram.json on each bot's home dir.
    # Token validation calls Telegram's getMe API. Writes via /tmp + sudo /bin/cp.

    def _telegram_resolve_status(bot_id: str) -> "_telegram.InstallStatus":
        """Resolve the Telegram install status for bot_id.

        Wires the real token read + getMe check as the default callables.
        resolve_status() handles defaults internally; we call without overrides.
        """
        return _telegram.resolve_status(bot_id)

    @app.get("/api/skills/install/<skill_id>/status")
    def api_skills_install_status(skill_id: str) -> Response:
        """Return the bot's current install status for ``skill_id``.

        Query: ?bot_id=<bot>. Status shape varies by skill kind:
        - gog: oauth_client_missing | plugin_disabled | oauth_pending | active | unknown
        - obsidian: no_vault_configured | vault_not_found | vault_not_readable | active | unknown

        Callers (the UI auto-poll, the Morning Briefing template installer)
        treat ``status == "active"`` as the completion signal.
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        if skill_id == _gog.GOG_SKILL_ID:
            status = _gog.resolve_status(
                bot_id,
                read_plugin_enabled=_gog_plugin_enabled,
                read_oauth_profile=_gog_oauth_profile,
                read_oauth_client_configured=_gog_oauth_client_configured,
            )
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _slack.SLACK_SKILL_ID:
            status = _slack_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _imessage.IMESSAGE_SKILL_ID:
            status = _imessage_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _whatsapp.WHATSAPP_SKILL_ID:
            status = _whatsapp_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _signal.SIGNAL_SKILL_ID:
            status = _signal_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _discord.DISCORD_SKILL_ID:
            status = _discord_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _telegram.TELEGRAM_SKILL_ID:
            status = _telegram_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        _up = _upstream.get_skill(skill_id)
        if _up is not None:
            status = _upstream.resolve_status(_up, bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        # apple_local — withdrawn 2026-05-30; falls through to 404.

        if skill_id == _autocad.AUTOCAD_SKILL_ID:
            status = _autocad.resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _obsidian.OBSIDIAN_SKILL_ID:
            # MCP-backed status: reads the bot's openclaw.json and reports
            # active when mcp.servers.obsidian is present, missing otherwise.
            # The mode marker fills in the read vs read_write info for the UI.
            status = _obsidian_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _dropbox.DROPBOX_SKILL_ID:
            # MCP-backed status — same shape as Obsidian. The closure also
            # auto-suggests a folder_path by reading ~/.dropbox/info.json
            # when the bot has no install yet, so the UI's install modal
            # pre-fills the Dropbox sync folder.
            status = _dropbox_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _notion.NOTION_SKILL_ID:
            # MCP-backed status — reads openclaw.json::mcp.servers.notion
            # PLUS checks the per-bot keystore slot. If the slot was wiped
            # manually but mcp.servers.notion remains, the resolver returns
            # ``revoked`` so the UI prompts for re-paste.
            status = _notion_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _runway.RUNWAY_SKILL_ID:
            # Bundled-plugin status — reads BOTH openclaw.json (model
            # default) AND auth-profiles.json (api key). Returns
            # valid / revoked / invalid / missing / unknown depending
            # on which signals are present. See runway_install for the
            # state machine.
            status = _runway_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _linear.LINEAR_SKILL_ID:
            # MCP-backed status — same shape as Notion (mcp.servers.linear
            # + per-bot keystore slot ``linear-<bot>``). The credential
            # shape is verbatim PAT, not JSON headers like Notion.
            status = _linear_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _google.GOOGLE_SKILL_ID:
            # Unified Google skill (PR #2155). Status carries
            # granted_capabilities (chip's label) + capability_summary.
            # Legacy gog-installed bots are detected here automatically.
            status = _google_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        if skill_id == _gws_write.GOOGLE_WORKSPACE_WRITE_SKILL_ID:
            # Four-stage status resolver — see spec §9.6. Returns
            # oauth_client_missing / oauth_pending / mcp_not_installed /
            # consumer_unreachable / active / unknown. NEVER reports
            # ``active`` from credential-presence alone (F3 mandate).
            status = _gws_write_resolve_status(bot_id)
            return jsonify({"ok": True, **status.to_dict()})

        # NOTE: ``google_workspace_read`` was removed as vestigial — see
        # the catalog-detail comment above. Falls through to the 404 below.

        # Still withdrawn 2026-05-30: home_assistant (pending vetting +
        # scope-toggle design). All other paste-token rewires (obsidian /
        # dropbox / notion / linear / runway / github-mcp) shipped
        # same-day. See docs/design/skills-install-roadmap-2026-05-30.md.

        return jsonify({
            "ok": False,
            "error": f"unknown skill {skill_id!r}",
            "hint": (
                "Known skills: 'gog', 'gmail', 'calendar', 'slack', "
                "'discord', 'telegram', 'imessage', 'whatsapp', 'signal', 'brave', 'github', "
                "'dropbox', 'notion', 'linear', 'runway', 'autocad', "
                "'google', 'obsidian_vault'."
            ),
        }), 404

    @app.post("/api/skills/install/<skill_id>")
    def api_skills_install(skill_id: str) -> Response:
        """Compute the install plan for ``skill_id`` on the given bot.

        Body: {bot_id: str}.

        V2.1-4: OAuth skills go through the orchestrator first.  If the skill
        requires OAuth that is not yet configured, the response uses the
        awaiting_oauth shape (same as Gallery install) rather than the older
        step-plan shape.  The UI helper ``_renderAwaitingOauthInstallPlan``
        handles this shape on both the Skills page and the Gallery page.

        Non-OAuth skills (Obsidian) are unaffected — they continue to return
        the step-plan shape because filesystem setup has no OAuth state to wait for.

        OAuth-requiring skills (GOG + future providers):
            If not satisfied →
                {ok: true, status: "awaiting_oauth", missing: [...], next: "..."}
            If already satisfied →
                {ok: true, status: "already_active", message: "..."}

        Obsidian / non-OAuth skills (unchanged):
            {ok, status: <InstallStatus dict>,
             steps: [<InstallStep dict>...],
             skill: {id, display_name, summary, access_panel}}

        Returns 404 for unknown skill ids; 400 if bot_id is missing.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        # V2.4-4: emit standardised plan_requested event for all skills via this
        # endpoint.  Fires once per POST regardless of which per-skill branch runs.
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(skill_id, bot_id, "plan_requested", {"skill_id": skill_id})
        except Exception:
            pass

        # ── V2.1-4: orchestrator prereq evaluation for OAuth skills ──────────
        # Call evaluate_install_prerequisites({integrations: [{id: skill_id}]})
        # for skills that have a registered provider.  If satisfied → short-circuit
        # to success.  If not → return the awaiting_oauth shape without creating a
        # forge job (option b: skills are lighter than gallery apps; no build phase).
        # Non-OAuth skills (Obsidian) have no registered provider so find_provider
        # returns None → fall through to the existing per-skill dispatch below.
        try:
            from ..oauth.providers import find_provider as _find_provider
            from ..oauth.orchestrator import evaluate_install_prerequisites as _eval_prereqs
            _provider = _find_provider(skill_id)
        except Exception:
            _provider = None
            _eval_prereqs = None

        if _provider is not None and _eval_prereqs is not None:
            try:
                _prereq = _eval_prereqs(
                    bot_id,
                    {"integrations": [{"id": skill_id}]},
                    shared_dir=Path(
                        load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
                    ),
                    # Do NOT forward GOG-specific readers here: we let the provider
                    # use its own default closures (make_plugin_enabled_reader etc.)
                    # so that the skills-page path is provider-agnostic and new
                    # providers (Slack, Calendar) need no changes here.
                    # The V2-4 backward-compat reader path in the orchestrator is
                    # reserved for the gallery install handler only.
                )
            except Exception as _prereq_exc:
                import logging as _log_mod
                _log_mod.getLogger(__name__).warning(
                    "api_skills_install: orchestrator check failed for %s/%s: %s",
                    bot_id, skill_id, _prereq_exc,
                )
                _prereq = {"satisfied": True, "missing": []}

            if _prereq["satisfied"]:
                # Already active — short-circuit to success (no steps to drive)
                meta = _gog.get_skill(skill_id) or {}
                _module._audit_log_entry("skill.install.plan", bot_id, {
                    "skill_id": skill_id,
                    "current_status": "already_active",
                    "step_count": 0,
                })
                return jsonify({
                    "ok": True,
                    "status": "already_active",
                    "message": (
                        f"This bot is already connected to "
                        f"{meta.get('display_name', skill_id)}."
                    ),
                })

            # Not satisfied — return awaiting_oauth shape (no forge job)
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": "awaiting_oauth",
                "missing_count": len(_prereq["missing"]),
            })
            return jsonify({
                "ok": True,
                "status": "awaiting_oauth",
                "missing": _prereq["missing"],
                "next": (
                    "Complete the integration setup shown above. "
                    "Once done, the skill will be ready to use."
                ),
            }), 202

        # ── Per-skill install dispatch (non-OAuth or fallback) ───────────────

        if skill_id == _gog.GOG_SKILL_ID:
            status = _gog.resolve_status(
                bot_id,
                read_plugin_enabled=_gog_plugin_enabled,
                read_oauth_profile=_gog_oauth_profile,
                read_oauth_client_configured=_gog_oauth_client_configured,
            )
            steps = _gog.build_install_plan(status)
            meta = _gog.get_skill(skill_id) or {}
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": meta.get("id"),
                    "display_name": meta.get("display_name"),
                    "summary": meta.get("summary"),
                    "access_panel": dict(meta.get("access_panel") or {}),
                },
            })

        if skill_id == _slack.SLACK_SKILL_ID:
            status = _slack_resolve_status(bot_id)
            steps = _slack.build_install_plan(status)
            reg = _slack.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.token_state,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _imessage.IMESSAGE_SKILL_ID:
            status = _imessage_resolve_status(bot_id)
            steps = _imessage.build_install_plan(status)
            reg = _imessage.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _whatsapp.WHATSAPP_SKILL_ID:
            status = _whatsapp_resolve_status(bot_id)
            steps = _whatsapp.build_install_plan(status)
            reg = _whatsapp.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _signal.SIGNAL_SKILL_ID:
            status = _signal_resolve_status(bot_id)
            steps = _signal.build_install_plan(status)
            reg = _signal.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _discord.DISCORD_SKILL_ID:
            status = _discord_resolve_status(bot_id)
            steps = _discord.build_install_plan(status)
            reg = _discord.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.token_state,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _telegram.TELEGRAM_SKILL_ID:
            status = _telegram_resolve_status(bot_id)
            steps = _telegram.build_install_plan(status)
            reg = _telegram.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.bot_token_state,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        _up = _upstream.get_skill(skill_id)
        if _up is not None:
            status = _upstream.resolve_status(_up, bot_id)
            if status.status == "active":
                _module._audit_log_entry("skill.install.plan", bot_id, {
                    "skill_id": skill_id,
                    "current_status": "already_active",
                    "step_count": 0,
                })
                return jsonify({
                    "ok": True,
                    "status": "already_active",
                    "message": (
                        f"This bot is already connected to {_up.display_name}."
                    ),
                })
            steps = _upstream.build_install_plan(_up, status)
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": _up.id,
                    "display_name": _up.display_name,
                    "summary": _up.summary,
                    "access_panel": dict(_up.access_panel),
                },
            })

        # apple_local — withdrawn 2026-05-30; falls through to 404.

        if skill_id == _autocad.AUTOCAD_SKILL_ID:
            status = _autocad.resolve_status(bot_id)
            steps = _autocad.build_install_plan(status)
            reg = _autocad.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _obsidian.OBSIDIAN_SKILL_ID:
            status = _obsidian_resolve_status(bot_id)
            # The install plan is computed against the MCP-aware status:
            # active → empty (already installed), otherwise the legacy
            # set_vault_path + confirm pair pointing at the wrapper route
            # below. The set_vault_path step still carries the access panel
            # — but the access panel now exposes ``mode_choices`` so the UI
            # can render the read/read_write radio at the same step.
            steps = _obsidian.build_install_plan(status)
            reg = _obsidian.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _dropbox.DROPBOX_SKILL_ID:
            # Same plan shape as Obsidian — set_folder_path step (with the
            # auto-detected suggested_path from ~/.dropbox/info.json) +
            # confirm. The access panel exposes mode_choices for the radio.
            status = _dropbox_resolve_status(bot_id)
            steps = _dropbox.build_install_plan(status)
            reg = _dropbox.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _notion.NOTION_SKILL_ID:
            # API-key skill — the install plan is set_token (Internal Integration
            # Secret paste) + confirm. The access panel carries the
            # post_install_callout reminding users to share pages with the
            # integration in Notion's UI before expecting the bot to see them.
            status = _notion_resolve_status(bot_id)
            steps = _notion.build_install_plan(status)
            reg = _notion.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _runway.RUNWAY_SKILL_ID:
            # Bundled-plugin install — set_token step + confirm. The
            # build_install_plan helper handles the state machine
            # (returns empty for ``valid``, the set-token-then-confirm
            # pair otherwise). Access panel has the cost callout.
            status = _runway_resolve_status(bot_id)
            steps = _runway.build_install_plan(status)
            reg = _runway.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _linear.LINEAR_SKILL_ID:
            # API-key skill — set_token (personal API key paste from
            # linear.app → Settings → API) + confirm. The access panel
            # carries the post_install_callout warning about identity (the
            # bot acts as the API key holder).
            status = _linear_resolve_status(bot_id)
            steps = _linear.build_install_plan(status)
            reg = _linear.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _google.GOOGLE_SKILL_ID:
            # Unified Google skill (PR #2155). Plan: pick_capabilities
            # (checkbox sheet) → oauth → complete. The frontend driver
            # captures the picker's output and uses it to override the
            # oauth step's services payload and the complete step's
            # capabilities payload.
            status = _google_resolve_status(bot_id)
            steps = _google.build_install_plan(status)
            reg = _google.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
                "granted_capabilities": status.granted_capabilities,
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "default_capabilities": list(reg.get("default_capabilities", [])),
                    "capabilities_catalog": list(reg.get("capabilities_catalog", [])),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        if skill_id == _gws_write.GOOGLE_WORKSPACE_WRITE_SKILL_ID:
            # OAuth skill — three-step wizard (account_type → capability_review
            # → oauth) + complete provisioning step. Reuses the existing
            # /api/admin/onboard/google/begin OAuth wizard; this module
            # owns only the post-OAuth provisioning sequence (preflight +
            # keystore + token shim + InstallMcpServer + kickstart).
            status = _gws_write_resolve_status(bot_id)
            steps = _gws_write.build_install_plan(status)
            reg = _gws_write.SKILL_REGISTRY_ENTRY
            _module._audit_log_entry("skill.install.plan", bot_id, {
                "skill_id": skill_id,
                "current_status": status.status,
                "step_count": len(steps),
            })
            return jsonify({
                "ok": True,
                "status": status.to_dict(),
                "steps": [s.to_dict() for s in steps],
                "skill": {
                    "id": reg.get("id"),
                    "display_name": reg.get("display_name"),
                    "summary": reg.get("summary"),
                    "access_panel": dict(reg.get("access_panel") or {}),
                },
            })

        # NOTE: ``google_workspace_read`` plan dispatch was removed as
        # vestigial — see the catalog-detail comment above. Falls through
        # to the 404 hint below.

        return jsonify({
            "ok": False,
            "error": f"unknown skill {skill_id!r}",
            "hint": (
                "Known skills: 'gog', 'gmail', 'calendar', 'slack', "
                "'discord', 'telegram', 'imessage', 'whatsapp', 'signal', 'brave', 'github', "
                "'dropbox', 'notion', 'linear', 'runway', 'autocad', "
                "'google', 'obsidian_vault'."
            ),
        }), 404

    # ── Obsidian wrapper route (MCP-backed install with mode toggle) ──────────
    #
    # POST /api/skills/install/obsidian/set-vault-path
    #   body: {bot_id, vault_path, mode in {"read", "read_write"}}
    #
    # Validates vault_path → grants the matching ACL to the bot user →
    # installs ``mcp.servers.obsidian`` via the existing InstallMcpServer
    # applier with catalog_id="filesystem" + extra_args=[vault_path] → writes
    # the mode marker. The mode is enforced at the OS file-permission layer:
    # in read mode the bot user has no write ACE on the vault, so even if
    # the filesystem MCP exposes write_file the kernel returns EACCES.

    @app.post("/api/skills/install/obsidian/set-vault-path")
    def api_skills_obsidian_set_vault_path() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        vault_path_raw = (body.get("vault_path") or "").strip()
        mode = (body.get("mode") or "read").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not vault_path_raw:
            return jsonify({"ok": False, "error": "vault_path required"}), 400
        if mode not in ("read", "read_write"):
            return jsonify({
                "ok": False,
                "error": "mode must be 'read' or 'read_write'",
            }), 400

        # Path validation: reject /tmp, ~/.ssh, system dirs, etc. Existing
        # blacklist from the pre-withdrawal skill module.
        from pathlib import Path as _Path
        vault_path = str(_Path(vault_path_raw).expanduser())
        ok, err = _obsidian.validate_vault_path(vault_path)
        if not ok:
            return jsonify({
                "ok": False,
                "error": "vault_path_invalid",
                "detail": err or "unknown validation error",
            }), 400

        # Resolve bot user (for the ACL grant target) — bot_id can differ
        # from the macOS account name (e.g. a team-bot bot_id whose macOS
        # account is the operator's personal account name).
        try:
            from ..config import get_bot_user as _get_bot_user
            bot_user = _get_bot_user(bot_id)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"could not resolve bot user for {bot_id!r}: {exc}",
            }), 500

        # Revoke any pre-existing ACEs for this bot user on the vault so a
        # re-install with a different mode starts clean. Idempotent: missing
        # ACEs are treated as success.
        rev_ok, rev_err = _obsidian.revoke_vault_acl(vault_path, bot_user)
        if not rev_ok:
            _log.warning(
                "obsidian: pre-install ACL revoke had warnings (continuing): %s",
                rev_err,
            )

        # Grant the right ACL for the requested mode. macOS ACLs propagate
        # via file_inherit + directory_inherit so files created AFTER the
        # install (by either the user or the bot in read_write mode) pick
        # up the same access.
        grant_ok, grant_err = _obsidian.grant_vault_acl(vault_path, bot_user, mode)
        if not grant_ok:
            _module._audit_log_entry("skill.obsidian.set_vault_path", bot_id, {
                "ok": False,
                "vault_path": vault_path,
                "mode": mode,
                "error": f"acl_grant_failed: {grant_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"acl_grant_failed: {grant_err}",
                "hint": (
                    "Could not grant the bot user filesystem access to the "
                    "vault. Check that the vault path exists and that "
                    "sudoers grants the evolve user `chmod +a`."
                ),
            }), 500

        # Install the MCP server entry. catalog_id="filesystem" is the vetted
        # @modelcontextprotocol/server-filesystem; extra_args=[vault_path]
        # scopes it to the vault (the wrapper script's trailing "$@" passes
        # this through to the real binary).
        summary = (
            f"Install Obsidian vault MCP for {bot_id} "
            f"({vault_path!r}, mode={mode})"
        )
        action_payload = {
            "bot_id": bot_id,
            "server_id": "obsidian",
            "catalog_id": "filesystem",
            "env_bindings": {},
            "extra_args": [vault_path],
        }
        proposal, err = _create_obsidian_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        if err:
            # ACL was already granted — revoke to keep the system consistent.
            _obsidian.revoke_vault_acl(vault_path, bot_user)
            _module._audit_log_entry("skill.obsidian.set_vault_path", bot_id, {
                "ok": False,
                "vault_path": vault_path,
                "mode": mode,
                "error": f"mcp_install_create_failed: {err}",
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_create_failed: {err}",
            }), 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            # Applier refused (e.g. catalog missing, openclaw.json unreadable).
            # Roll back the ACL grant so a retry can start clean.
            _obsidian.revoke_vault_acl(vault_path, bot_user)
            _module._audit_log_entry("skill.obsidian.set_vault_path", bot_id, {
                "ok": False,
                "vault_path": vault_path,
                "mode": mode,
                "proposal_status": prop_status,
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_applier_returned_{prop_status}",
                "proposal": proposal,
            }), 500

        # Persist the mode marker now that the install landed. Best-effort —
        # a marker write failure doesn't undo the install (status resolver
        # falls back to mode=None for legacy bots).
        marker_ok, marker_err = _obsidian.write_mode_marker(bot_id, vault_path, mode)
        if not marker_ok:
            _log.warning(
                "obsidian: mode marker write failed (install otherwise complete): %s",
                marker_err,
            )

        _module._audit_log_entry("skill.obsidian.set_vault_path", bot_id, {
            "ok": True,
            "vault_path": vault_path,
            "mode": mode,
            "mcp_proposal_status": prop_status,
            "marker_written": marker_ok,
        })

        status = _obsidian_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/obsidian/revoke")
    def api_skills_obsidian_revoke() -> Response:
        """Remove the Obsidian MCP server + revoke ACL + clear mode marker."""
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        # Read the mode marker so we know which vault to revoke ACL on.
        marker = _obsidian.read_mode_marker(bot_id) or {}
        vault_path = marker.get("vault_path")

        # Remove the MCP server entry (this is the load-bearing revoke — the
        # bot's gateway stops loading the filesystem MCP on next boot).
        proposal, err = _create_obsidian_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": "obsidian"},
            bot_id=bot_id,
            summary=f"Remove Obsidian vault MCP from {bot_id}",
        )

        # Revoke the ACL (best-effort) and clear the marker.
        acl_ok = True
        acl_err: str | None = None
        if vault_path:
            try:
                from ..config import get_bot_user as _get_bot_user
                bot_user = _get_bot_user(bot_id)
                acl_ok, acl_err = _obsidian.revoke_vault_acl(vault_path, bot_user)
            except Exception as exc:
                acl_ok, acl_err = False, str(exc)
        marker_cleared = _obsidian.delete_mode_marker(bot_id)

        _module._audit_log_entry("skill.obsidian.revoke", bot_id, {
            "ok": err is None,
            "proposal_status": (proposal or {}).get("status"),
            "acl_revoked": acl_ok,
            "acl_error": acl_err,
            "marker_cleared": marker_cleared,
            "vault_path": vault_path,
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
            "acl_revoked": acl_ok,
            "marker_cleared": marker_cleared,
        })

    # POST /api/skills/install/obsidian/set-mode
    #   body: {bot_id, mode in {"read", "read_write"}}
    #
    # Flip an already-installed vault between read and read+write. The MCP
    # server entry in openclaw.json is unchanged — only the OS ACL on the
    # vault directory swaps. No gateway kickstart needed; the kernel
    # enforces the new ACL on the next syscall. Reference: P3 in
    # docs/design/skills-install-roadmap-2026-05-30.md.

    @app.post("/api/skills/install/obsidian/set-mode")
    def api_skills_obsidian_set_mode() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        mode = (body.get("mode") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if mode not in ("read", "read_write"):
            return jsonify({
                "ok": False,
                "error": "mode must be 'read' or 'read_write'",
            }), 400

        # Resolve the current install from the mode marker — absence means
        # the skill isn't installed for this bot.
        marker = _obsidian.read_mode_marker(bot_id) or {}
        vault_path = marker.get("vault_path")
        current_mode = marker.get("mode")
        if not vault_path:
            return jsonify({
                "ok": False,
                "error": "skill_not_installed",
                "detail": (
                    "no obsidian mode marker for this bot; "
                    "install the skill via /set-vault-path first"
                ),
            }), 404

        # Cross-check the marker against openclaw.json::mcp.servers.obsidian.
        # If they disagree, the marker is stale or the MCP entry was
        # rewritten out-of-band; surface as 409 rather than silently
        # operating on a drifted install.
        status = _obsidian_resolve_status(bot_id)
        installed_path = status.vault_path
        if installed_path and installed_path != vault_path:
            return jsonify({
                "ok": False,
                "error": "mode_marker_drift",
                "detail": (
                    f"marker says {vault_path!r} but openclaw.json says "
                    f"{installed_path!r}; reinstall to reconcile"
                ),
            }), 409

        # No-op when the mode is already what was requested.
        if current_mode == mode:
            return jsonify({
                "ok": True,
                **status.to_dict(),
                "status": "unchanged",
            })

        # Resolve bot user (bot_id ≠ macOS account name for some bots).
        try:
            from ..config import get_bot_user as _get_bot_user
            bot_user = _get_bot_user(bot_id)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"could not resolve bot user for {bot_id!r}: {exc}",
            }), 500

        # Idempotent revoke of both modes before granting the new one.
        rev_ok, rev_err = _obsidian.revoke_vault_acl(vault_path, bot_user)
        if not rev_ok:
            _log.warning(
                "obsidian.set_mode: pre-grant ACL revoke had warnings "
                "(continuing): %s", rev_err,
            )

        # Apply the new mode. On failure, re-grant the previous mode so
        # the bot is not left without any ACL on the vault.
        grant_ok, grant_err = _obsidian.grant_vault_acl(vault_path, bot_user, mode)
        if not grant_ok:
            rollback_ok, rollback_err = (True, None)
            if current_mode in ("read", "read_write"):
                rollback_ok, rollback_err = _obsidian.grant_vault_acl(
                    vault_path, bot_user, current_mode,
                )
            _module._audit_log_entry("skill.obsidian.set_mode", bot_id, {
                "ok": False,
                "vault_path": vault_path,
                "from_mode": current_mode,
                "to_mode": mode,
                "error": f"acl_grant_failed: {grant_err}",
                "rollback_ok": rollback_ok,
                "rollback_error": rollback_err,
            })
            return jsonify({
                "ok": False,
                "error": f"acl_grant_failed: {grant_err}",
                "rolled_back_to": current_mode if rollback_ok else None,
                "rollback_error": rollback_err,
            }), 500

        # Persist the new mode marker. Marker-write failures don't undo
        # the ACL flip (status resolver tolerates a missing marker).
        marker_ok, marker_err = _obsidian.write_mode_marker(bot_id, vault_path, mode)
        if not marker_ok:
            _log.warning(
                "obsidian.set_mode: mode marker write failed (ACL already "
                "swapped): %s", marker_err,
            )

        _module._audit_log_entry("skill.obsidian.set_mode", bot_id, {
            "ok": True,
            "vault_path": vault_path,
            "from_mode": current_mode,
            "to_mode": mode,
            "marker_written": marker_ok,
        })

        updated = _obsidian_resolve_status(bot_id)
        return jsonify({"ok": True, **updated.to_dict()})

    # ── Dropbox wrapper route (MCP-backed install with mode toggle) ───────────
    #
    # POST /api/skills/install/dropbox/set-folder-path
    #   body: {bot_id, folder_path, mode in {"read", "read_write"}}
    #
    # Identical shape to Obsidian's set-vault-path. The only differences:
    #   - folder_path replaces vault_path in the payload
    #   - server_id is "dropbox" (creating mcp.servers.dropbox)
    #   - validate / grant / revoke helpers come from dropbox_install.py
    # See docs/design/skills-install-roadmap-2026-05-30.md for the per-skill
    # plan and dropbox_install.py for the design rationale.

    @app.post("/api/skills/install/dropbox/set-folder-path")
    def api_skills_dropbox_set_folder_path() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        folder_path_raw = (body.get("folder_path") or "").strip()
        mode = (body.get("mode") or "read").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not folder_path_raw:
            return jsonify({"ok": False, "error": "folder_path required"}), 400
        if mode not in ("read", "read_write"):
            return jsonify({
                "ok": False,
                "error": "mode must be 'read' or 'read_write'",
            }), 400

        # Path validation: same reserved-location blacklist as Obsidian.
        from pathlib import Path as _Path
        folder_path = str(_Path(folder_path_raw).expanduser())
        ok, err = _dropbox.validate_dropbox_path(folder_path)
        if not ok:
            return jsonify({
                "ok": False,
                "error": "folder_path_invalid",
                "detail": err or "unknown validation error",
            }), 400

        try:
            from ..config import get_bot_user as _get_bot_user
            bot_user = _get_bot_user(bot_id)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"could not resolve bot user for {bot_id!r}: {exc}",
            }), 500

        # Idempotent revoke of any existing ACEs before granting the new
        # one — handles re-install with a different mode cleanly.
        rev_ok, rev_err = _dropbox.revoke_dropbox_acl(folder_path, bot_user)
        if not rev_ok:
            _log.warning(
                "dropbox: pre-install ACL revoke had warnings (continuing): %s",
                rev_err,
            )

        grant_ok, grant_err = _dropbox.grant_dropbox_acl(folder_path, bot_user, mode)
        if not grant_ok:
            _module._audit_log_entry("skill.dropbox.set_folder_path", bot_id, {
                "ok": False,
                "folder_path": folder_path,
                "mode": mode,
                "error": f"acl_grant_failed: {grant_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"acl_grant_failed: {grant_err}",
                "hint": (
                    "Could not grant the bot user filesystem access to the "
                    "Dropbox folder. Check that the folder exists, isn't on "
                    "a read-only mount, and that sudoers grants the evolve "
                    "user `chmod +a`."
                ),
            }), 500

        # Install the MCP server entry via the same catalog as Obsidian:
        # @modelcontextprotocol/server-filesystem scoped to folder_path.
        summary = (
            f"Install Dropbox folder MCP for {bot_id} "
            f"({folder_path!r}, mode={mode})"
        )
        action_payload = {
            "bot_id": bot_id,
            "server_id": "dropbox",
            "catalog_id": "filesystem",
            "env_bindings": {},
            "extra_args": [folder_path],
        }
        proposal, err = _create_dropbox_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        if err:
            _dropbox.revoke_dropbox_acl(folder_path, bot_user)
            _module._audit_log_entry("skill.dropbox.set_folder_path", bot_id, {
                "ok": False,
                "folder_path": folder_path,
                "mode": mode,
                "error": f"mcp_install_create_failed: {err}",
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_create_failed: {err}",
            }), 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            _dropbox.revoke_dropbox_acl(folder_path, bot_user)
            _module._audit_log_entry("skill.dropbox.set_folder_path", bot_id, {
                "ok": False,
                "folder_path": folder_path,
                "mode": mode,
                "proposal_status": prop_status,
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_applier_returned_{prop_status}",
                "proposal": proposal,
            }), 500

        marker_ok, marker_err = _dropbox.write_mode_marker(bot_id, folder_path, mode)
        if not marker_ok:
            _log.warning(
                "dropbox: mode marker write failed (install otherwise complete): %s",
                marker_err,
            )

        _module._audit_log_entry("skill.dropbox.set_folder_path", bot_id, {
            "ok": True,
            "folder_path": folder_path,
            "mode": mode,
            "mcp_proposal_status": prop_status,
            "marker_written": marker_ok,
        })

        status = _dropbox_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/dropbox/revoke")
    def api_skills_dropbox_revoke() -> Response:
        """Remove the Dropbox MCP server + revoke ACL + clear mode marker."""
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        marker = _dropbox.read_mode_marker(bot_id) or {}
        folder_path = marker.get("folder_path")

        proposal, err = _create_dropbox_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": "dropbox"},
            bot_id=bot_id,
            summary=f"Remove Dropbox folder MCP from {bot_id}",
        )

        acl_ok = True
        acl_err: str | None = None
        if folder_path:
            try:
                from ..config import get_bot_user as _get_bot_user
                bot_user = _get_bot_user(bot_id)
                acl_ok, acl_err = _dropbox.revoke_dropbox_acl(folder_path, bot_user)
            except Exception as exc:
                acl_ok, acl_err = False, str(exc)
        marker_cleared = _dropbox.delete_mode_marker(bot_id)

        _module._audit_log_entry("skill.dropbox.revoke", bot_id, {
            "ok": err is None,
            "proposal_status": (proposal or {}).get("status"),
            "acl_revoked": acl_ok,
            "acl_error": acl_err,
            "marker_cleared": marker_cleared,
            "folder_path": folder_path,
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
            "acl_revoked": acl_ok,
            "marker_cleared": marker_cleared,
        })

    # ── Dropbox /set-mode (P3 from skills roadmap) ────────────────────────────
    # POST /api/skills/install/dropbox/set-mode
    #   body: {bot_id, mode in {"read", "read_write"}}
    #
    # Mirrors /api/skills/install/obsidian/set-mode. Re-grants the bot's
    # ACL on the Dropbox folder for the requested mode and updates the
    # mode marker. The mcp.servers.dropbox entry is untouched. Reference:
    # P3 in docs/design/skills-install-roadmap-2026-05-30.md.

    @app.post("/api/skills/install/dropbox/set-mode")
    def api_skills_dropbox_set_mode() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        mode = (body.get("mode") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if mode not in ("read", "read_write"):
            return jsonify({
                "ok": False,
                "error": "mode must be 'read' or 'read_write'",
            }), 400

        marker = _dropbox.read_mode_marker(bot_id) or {}
        folder_path = marker.get("folder_path")
        current_mode = marker.get("mode")
        if not folder_path:
            return jsonify({
                "ok": False,
                "error": "skill_not_installed",
                "detail": (
                    "no dropbox mode marker for this bot; "
                    "install the skill via /set-folder-path first"
                ),
            }), 404

        status = _dropbox_resolve_status(bot_id)
        installed_path = status.folder_path
        if installed_path and installed_path != folder_path:
            return jsonify({
                "ok": False,
                "error": "mode_marker_drift",
                "detail": (
                    f"marker says {folder_path!r} but openclaw.json says "
                    f"{installed_path!r}; reinstall to reconcile"
                ),
            }), 409

        if current_mode == mode:
            return jsonify({
                "ok": True,
                **status.to_dict(),
                "status": "unchanged",
            })

        try:
            from ..config import get_bot_user as _get_bot_user
            bot_user = _get_bot_user(bot_id)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"could not resolve bot user for {bot_id!r}: {exc}",
            }), 500

        rev_ok, rev_err = _dropbox.revoke_dropbox_acl(folder_path, bot_user)
        if not rev_ok:
            _log.warning(
                "dropbox.set_mode: pre-grant ACL revoke had warnings "
                "(continuing): %s", rev_err,
            )

        grant_ok, grant_err = _dropbox.grant_dropbox_acl(folder_path, bot_user, mode)
        if not grant_ok:
            rollback_ok, rollback_err = (True, None)
            if current_mode in ("read", "read_write"):
                rollback_ok, rollback_err = _dropbox.grant_dropbox_acl(
                    folder_path, bot_user, current_mode,
                )
            _module._audit_log_entry("skill.dropbox.set_mode", bot_id, {
                "ok": False,
                "folder_path": folder_path,
                "from_mode": current_mode,
                "to_mode": mode,
                "error": f"acl_grant_failed: {grant_err}",
                "rollback_ok": rollback_ok,
                "rollback_error": rollback_err,
            })
            return jsonify({
                "ok": False,
                "error": f"acl_grant_failed: {grant_err}",
                "rolled_back_to": current_mode if rollback_ok else None,
                "rollback_error": rollback_err,
            }), 500

        marker_ok, marker_err = _dropbox.write_mode_marker(bot_id, folder_path, mode)
        if not marker_ok:
            _log.warning(
                "dropbox.set_mode: mode marker write failed (ACL already "
                "swapped): %s", marker_err,
            )

        _module._audit_log_entry("skill.dropbox.set_mode", bot_id, {
            "ok": True,
            "folder_path": folder_path,
            "from_mode": current_mode,
            "to_mode": mode,
            "marker_written": marker_ok,
        })

        updated = _dropbox_resolve_status(bot_id)
        return jsonify({"ok": True, **updated.to_dict()})

    # ── Google Workspace (Write) wrapper routes ─────────────────────────────
    #
    # Closes the runtime-consumer gap (F4 from the deep audit) that drove the
    # withdrawn `gog` family. The wizard flow:
    #
    #   1. Status   → GET  /api/skills/install/google_workspace_write/status
    #   2. Plan     → POST /api/skills/install/google_workspace_write
    #   3. OAuth    → POST /api/admin/onboard/google/begin  (existing wizard)
    #                 GET  /api/admin/onboard/google/callback (existing)
    #   4. Complete → POST /api/skills/install/google_workspace_write/complete
    #      - Pre-flight: Gmail.getProfile + Calendar.list + Drive.about all-200
    #      - Write 3 keystore slots (client_id, client_secret, creds_dir)
    #      - Token shim writes credentials.json
    #      - InstallMcpServer proposal (catalog_id=google_workspace,
    #        server_id=google_workspace, extra_args=WRITE_MCP_EXTRA_ARGS)
    #      - Kickstart gateway
    #   5. Revoke   → POST /api/skills/install/google_workspace_write/revoke
    #      - RemoveMcpServer proposal
    #      - Blank keystore slots
    #      - Token shim removes credentials dir
    #      - Hit Google's revoke endpoint (best-effort)
    #      - Clear OAuth profile + kickstart
    #
    # See:
    #   * docs/spec-google-workspace-suite-2026-06-04.md
    #   * docs/vetting-workspace-mcp-2026-06-04.md
    #   * skills/google_workspace_write_install.py (the module these routes call)

    def _gws_keystore_reader(slot: str) -> str | None:
        """Shared keystore reader for both Workspace skills. Returns the
        ``<unreachable_assume_present>`` sentinel on keystore failure so
        status doesn't flap to ``consumer_unreachable`` on transient errors."""
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ))
            return mgr.get_value(slot)
        except Exception:
            return "<keystore_unreachable_assume_present>"

    def _gws_write_resolve_status(bot_id: str) -> "_gws_write.InstallStatus":
        """Bind real readers to the Workspace-Write status resolver."""
        from ..skills import _oc_install_common as _oc_common
        return _gws_write.resolve_status(
            bot_id,
            read_oauth_profile=_read_google_oauth_profile,
            read_oauth_client=_read_google_oauth_client,
            read_oc_config=_oc_common.read_oc_config,
            read_keystore_slot=_gws_keystore_reader,
        )

    # _gws_read_resolve_status removed with the rest of the _read
    # vestigial layer (post-PR-#2231 cleanup).

    def _google_resolve_status(bot_id: str) -> "_google.InstallStatus":
        """Bind real readers to the unified Google status resolver.

        Returns ``granted_capabilities`` (derived from profile.scopes)
        and ``capability_summary`` (the chip's label). Legacy gog-
        installed bots are auto-detected — their gmail.readonly +
        calendar.readonly profile maps to [gmail_read, calendar_read]
        and is reported active (assuming MCP entry + keystore healthy).
        """
        from ..skills import _oc_install_common as _oc_common
        return _google.resolve_status(
            bot_id,
            read_oauth_profile=_read_google_oauth_profile,
            read_oauth_client=_read_google_oauth_client,
            read_oc_config=_oc_common.read_oc_config,
            read_keystore_slot=_gws_keystore_reader,
        )

    def _create_gws_mcp_proposal(
        action_kind: str, action_payload: dict, bot_id: str, summary: str,
    ):
        """Inline helper mirroring _create_notion_mcp_proposal /
        _create_github_mcp_proposal. Fifth near-identical closure; the
        refactor to a shared `_create_mcp_skill_proposal` is now well
        past the threshold (see notion docstring) but stays out of
        scope for this PR."""
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

    # NOTE: status (GET /api/skills/install/google_workspace_write/status)
    # and plan (POST /api/skills/install/google_workspace_write) are
    # served by the generic dispatchers above, which call
    # _gws_write_resolve_status and _gws_write.build_install_plan.
    # The dedicated /complete and /revoke routes live here because they
    # encapsulate skill-specific provisioning + teardown logic.
    #
    # The unified ``google`` skill (post-PR-#2231) and the legacy
    # ``google_workspace_write`` route share this /complete + /revoke
    # implementation. The per-skill route is a thin wrapper that
    # supplies the module (and thus extra_args + skill_id for audit) to
    # the implementation. For the unified Google skill, an inline
    # _DynamicMod adapter parameterises extra_args by the operator's
    # capability picks; see api_skills_google_complete below.

    def _gws_complete_install_impl(
        bot_id: str,
        gws_mod,          # google_workspace_write_install OR _DynamicMod adapter
        skill_id: str,    # for audit logging
    ) -> tuple[dict, int]:
        """Shared post-OAuth provisioning for Google Workspace skills.

        Five steps; each must succeed for ``ok=True``. Earlier failures
        short-circuit and roll back what landed before. The wizard's
        diagnostic banner renders the per-step pass/fail breakdown.
        Returns ``(response_dict, http_status)``.

        The ``gws_mod`` parameter supplies the InstallMcpServer extra_args
        via its ``build_install_mcp_action_payload(bot_id)`` slot — either
        ``_gws_write`` (static WRITE_MCP_EXTRA_ARGS) or the inline
        ``_DynamicMod`` adapter the unified Google route builds with
        runtime-picked capabilities. ``skill_id`` is the audit log
        namespace.
        """
        result = _gws_write.CompletionResult(bot_id=bot_id, ok=False)
        audit_key = f"skill.{skill_id}.complete"

        # Step 1 — pre-flight Gmail + Calendar + Drive.
        access_token, fresh_err = _ensure_fresh_google_access_token(bot_id)
        if not access_token:
            result.preflight_error = fresh_err or "no_access_token"
            return {"ok": False, **result.to_dict()}, 400
        preflight = _gws_write.preflight_check(access_token)
        if not preflight.get("ok"):
            result.preflight_error = preflight.get("error") or "preflight_failed"
            _module._audit_log_entry(audit_key, bot_id, {
                "ok": False, "stage": "preflight", **result.to_dict(),
            })
            return {"ok": False, **result.to_dict()}, 400
        result.preflight_done = True
        result.google_account = preflight.get("gmail_email")

        # Step 2 — keystore writes (3 slots). Roll back on partial failure.
        try:
            from evolve_admin.keystore import KeystoreManager
            shared_dir = Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            )
            mgr = KeystoreManager(shared_dir)

            client = _read_google_oauth_client(bot_id)
            if not client or not client.get("client_id") or not client.get("client_secret"):
                result.keystore_error = "client_not_configured"
                return {"ok": False, **result.to_dict()}, 400

            creds_dir = str(_gws_shim.credentials_dir_for_bot(bot_id))
            slot_writes = [
                (_gws_write.keystore_slot_client_id_for(bot_id),
                 client["client_id"], "client_id"),
                (_gws_write.keystore_slot_client_secret_for(bot_id),
                 client["client_secret"], "client_secret"),
                (_gws_write.keystore_slot_credentials_dir_for(bot_id),
                 creds_dir, "credentials_dir"),
            ]
            for slot, value, purpose in slot_writes:
                existing = mgr.ks.get_key_entry(slot)
                if existing:
                    mgr.set_value(slot, value)
                else:
                    mgr.register(
                        slot,
                        provider="google_workspace",
                        scope="shared",
                        description=(
                            f"Google Workspace MCP {purpose} for bot {bot_id}"
                        ),
                        bots=None,
                        value=value,
                    )
        except Exception as exc:
            result.keystore_error = (
                f"keystore_write_failed: {exc.__class__.__name__}: {exc}"
            )
            _module._audit_log_entry(audit_key, bot_id, {
                "ok": False, "stage": "keystore", **result.to_dict(),
            })
            return {"ok": False, **result.to_dict()}, 500
        result.keystore_done = True

        # Step 3 — token shim writes credentials.json.
        ok, shim_err = _gws_shim.write_credentials_for_bot(bot_id)
        if not ok:
            result.token_shim_error = shim_err or "token_shim_failed"
            try:
                for slot, _value, _purpose in slot_writes:
                    mgr.set_value(slot, "")
            except Exception:
                pass
            _module._audit_log_entry(audit_key, bot_id, {
                "ok": False, "stage": "token_shim", **result.to_dict(),
            })
            return {"ok": False, **result.to_dict()}, 500
        result.token_shim_done = True

        # Step 4 — InstallMcpServer proposal (per-skill extra_args).
        action_payload = gws_mod.build_install_mcp_action_payload(bot_id)
        summary = (
            f"Install {skill_id.replace('_', ' ')} for {bot_id} "
            f"(account={result.google_account or '?'!r})"
        )
        proposal, err = _create_gws_mcp_proposal(
            "InstallMcpServer", action_payload, bot_id=bot_id, summary=summary,
        )
        if err:
            result.mcp_install_error = f"mcp_install_create_failed: {err}"
            try:
                for slot, _value, _purpose in slot_writes:
                    mgr.set_value(slot, "")
            except Exception:
                pass
            try:
                _gws_shim.remove_credentials_for_bot(bot_id)
            except Exception:
                pass
            _module._audit_log_entry(audit_key, bot_id, {
                "ok": False, "stage": "mcp_install_create", **result.to_dict(),
            })
            return {"ok": False, **result.to_dict()}, 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            result.mcp_install_error = f"mcp_install_applier_returned_{prop_status}"
            try:
                for slot, _value, _purpose in slot_writes:
                    mgr.set_value(slot, "")
            except Exception:
                pass
            try:
                _gws_shim.remove_credentials_for_bot(bot_id)
            except Exception:
                pass
            _module._audit_log_entry(audit_key, bot_id, {
                "ok": False, "stage": "mcp_install_apply",
                "proposal_status": prop_status, **result.to_dict(),
            })
            return {
                "ok": False, "proposal": proposal, **result.to_dict(),
            }, 500
        result.mcp_install_done = True

        # Step 5 — kickstart so the gateway picks up mcp.servers.google_workspace.
        from ..skills import _oc_install_common as _oc_common
        kick_ok, kick_err = _oc_common.kickstart_gateway(bot_id)
        result.gateway_kickstart_done = bool(kick_ok)
        result.gateway_kickstart_error = None if kick_ok else kick_err

        result.ok = True
        _module._audit_log_entry(audit_key, bot_id, {
            "ok": True, **result.to_dict(),
        })
        return {"ok": True, **result.to_dict()}, 200

    def _gws_revoke_impl(bot_id: str, skill_id: str) -> dict:
        """Shared symmetric uninstall for both Workspace skills (F2 mandate).

        Removes mcp.servers.google_workspace, blanks the 3 keystore slots,
        wipes the credentials dir, hits Google's revoke endpoint (best-
        effort), clears the OAuth profile, kickstarts. All steps run
        regardless of individual failures; the returned dict surfaces what
        landed vs. what didn't.

        Read and Write share this implementation because they share the
        MCP server entry — revoking either tears down the whole shared
        installation.
        """
        audit_key = f"skill.{skill_id}.revoke"

        # 1. RemoveMcpServer proposal (load-bearing: stops the gateway
        # from launching the MCP on next boot).
        proposal, err = _create_gws_mcp_proposal(
            "RemoveMcpServer",
            _gws_write.build_remove_mcp_action_payload(bot_id),
            bot_id=bot_id,
            summary=f"Remove Google Workspace MCP from {bot_id}",
        )
        mcp_removed = err is None and (proposal or {}).get("status") in (
            "applied", "succeeded",
        )

        # 2. Blank the 3 keystore slots.
        slot_results: dict[str, bool] = {}
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ))
            for fn, label in (
                (_gws_write.keystore_slot_client_id_for, "client_id"),
                (_gws_write.keystore_slot_client_secret_for, "client_secret"),
                (_gws_write.keystore_slot_credentials_dir_for, "credentials_dir"),
            ):
                slot = fn(bot_id)
                try:
                    if mgr.ks.get_key_entry(slot):
                        mgr.set_value(slot, "")
                        slot_results[label] = True
                    else:
                        slot_results[label] = False
                except Exception:
                    slot_results[label] = False
        except Exception:
            slot_results = {"client_id": False, "client_secret": False, "credentials_dir": False}

        # 3. Wipe the credentials directory.
        shim_ok, shim_err = _gws_shim.remove_credentials_for_bot(bot_id)

        # 4. Hit Google's revoke endpoint (best-effort).
        google_revoke_attempted = False
        google_revoke_ok = False
        prof = _read_google_oauth_profile(bot_id)
        if prof and prof.get("refresh_token"):
            try:
                google_revoke_attempted = True
                rev_status, _ = _google_http_form_post(
                    GOOGLE_REVOKE_URL, {"token": prof["refresh_token"]},
                )
                google_revoke_ok = rev_status == 200
            except Exception:
                google_revoke_ok = False

        # 5. Clear the OAuth profile + kickstart.
        profile_cleared = _delete_google_oauth_profile(bot_id)
        from ..skills import _oc_install_common as _oc_common
        kick_ok, kick_err = _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry(audit_key, bot_id, {
            "mcp_removed": mcp_removed,
            "create_error": err,
            "proposal_status": (proposal or {}).get("status"),
            "slot_results": slot_results,
            "credentials_dir_wiped": shim_ok,
            "credentials_dir_error": shim_err,
            "google_revoke_attempted": google_revoke_attempted,
            "google_revoke_ok": google_revoke_ok,
            "profile_cleared": profile_cleared,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": kick_err,
        })

        ok_overall = mcp_removed and shim_ok
        return {
            "ok": ok_overall,
            "proposal": proposal,
            "mcp_removed": mcp_removed,
            "keystore_slots": slot_results,
            "credentials_dir_wiped": shim_ok,
            "credentials_dir_error": shim_err,
            "google_revoke_attempted": google_revoke_attempted,
            "google_revoke_ok": google_revoke_ok,
            "profile_cleared": profile_cleared,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": kick_err,
            "note": (
                "Local credentials cleared. To fully revoke Google's "
                "side, also visit https://myaccount.google.com/permissions "
                "and remove Evolve from the list."
            ),
        }

    def _google_complete_install_impl(
        bot_id: str, capabilities: list[str],
    ) -> tuple[dict, int]:
        """Unified-skill /complete impl. Forks from _gws_complete_install_impl
        only in step 4 — the InstallMcpServer payload's extra_args are
        derived from the picked capabilities, not from a static module
        constant.

        Body of steps 1-3 + 5 is identical to the helper used by the
        legacy _read / _write routes; rather than duplicate ~150 LOC we
        construct a tiny stand-in module for the dispatcher.
        """
        class _DynamicMod:
            """Adapter so _gws_complete_install_impl can call its
            ``gws_mod.build_install_mcp_action_payload(bot_id)`` slot
            with the runtime-chosen capability set baked in."""
            @staticmethod
            def build_install_mcp_action_payload(b: str) -> dict[str, Any]:
                return _google.build_install_mcp_action_payload(b, capabilities)

        return _gws_complete_install_impl(
            bot_id, _DynamicMod, _google.GOOGLE_SKILL_ID,
        )

    @app.post("/api/skills/install/google/complete")
    def api_skills_google_complete() -> Response:
        """Unified Google /complete — capabilities-aware.

        Body: ``{bot_id, capabilities: [<capability_id>, ...]}``. The
        capability list determines the workspace-mcp ``--permissions``
        flags (or ``--read-only`` shortcut when only read-* caps were
        picked).
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        capabilities = body.get("capabilities") or []
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        if not isinstance(capabilities, list) or not capabilities:
            return jsonify({
                "ok": False,
                "error": "capabilities required (non-empty list)",
            }), 400
        # Validate every id against the catalog; reject unknowns so the
        # MCP doesn't end up with garbage --permissions args.
        unknown = [c for c in capabilities if _google.capability_by_id(c) is None]
        if unknown:
            return jsonify({
                "ok": False,
                "error": f"unknown_capabilities:{','.join(unknown)}",
            }), 400
        response, http_status = _google_complete_install_impl(
            bot_id, capabilities,
        )
        return jsonify(response), http_status

    @app.post("/api/skills/install/google/revoke")
    def api_skills_google_revoke() -> Response:
        """Unified Google revoke — same impl as the split skills'
        revokes (they all share one MCP entry and one OAuth profile)."""
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        return jsonify(_gws_revoke_impl(bot_id, _google.GOOGLE_SKILL_ID))

    @app.post("/api/skills/install/google_workspace_write/complete")
    def api_skills_gws_write_complete() -> Response:
        """Workspace-Write post-OAuth provisioning."""
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        response, http_status = _gws_complete_install_impl(
            bot_id, _gws_write, _gws_write.GOOGLE_WORKSPACE_WRITE_SKILL_ID,
        )
        return jsonify(response), http_status

    @app.post("/api/skills/install/google_workspace_write/revoke")
    def api_skills_gws_write_revoke() -> Response:
        """Workspace-Write symmetric revoke."""
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        return jsonify(_gws_revoke_impl(
            bot_id, _gws_write.GOOGLE_WORKSPACE_WRITE_SKILL_ID,
        ))

    # ``google_workspace_read`` /complete + /revoke routes removed as
    # vestigial. Operators land on /api/skills/install/google instead.

    # ── GitHub-MCP wrapper routes (purpose 2: LLM access) ────────────────────
    #
    # POST /api/skills/install/github/install-mcp-server
    #   body: {bot_id, access_token?}
    #
    # access_token is OPTIONAL: when absent, the install reads the pod-wide
    # ``github_general_access`` slot and binds the bot's
    # ``env_bindings.GITHUB_TOKEN`` to that slot directly (no per-bot copy).
    # When access_token is supplied, the install writes it to the per-bot
    # ``github-<bot_id>`` override slot and binds env to that. This makes
    # pod-wide-default + per-bot-override a real runtime story rather than
    # just a UI label.
    #
    # Distinct from the github skill's purpose-1 backup wizard
    # (open_github_backup_wizard step in upstream_plugin_skills) — that
    # writes the PAT to ~/.openclaw/workspace/.git/config for nightly
    # backup pushes. This route puts a separate PAT in the keystore for
    # the bot's LLM-driven GitHub access via @modelcontextprotocol/server-github.
    # The two PATs can be the same or different; the two installs are
    # independent and either can exist without the other.

    @app.post("/api/skills/install/github/install-mcp-server")
    def api_skills_github_install_mcp_server() -> Response:
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        access_token = (body.get("access_token") or "").strip()

        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        shared_dir = Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )
        from evolve_admin.keystore import KeystoreManager
        try:
            mgr = KeystoreManager(shared_dir)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"keystore_open_failed: {exc.__class__.__name__}: {exc}",
            }), 500

        # Resolve the install mode:
        #   - explicit access_token in body → override mode, write per-bot slot
        #   - empty access_token AND pod-wide slot has a value → pod mode,
        #     bind env directly to the pod slot, no per-bot write
        #   - empty access_token AND pod-wide slot empty → 400, operator
        #     needs to either paste a token or set the pod-wide PAT first
        slot_per_bot = _github_mcp.keystore_slot_for(bot_id)
        pod_slot = _github_mcp.POD_KEYSTORE_SLOT

        if access_token:
            install_mode = "override"
            slot_for_binding = slot_per_bot
            token_to_verify = access_token
        else:
            try:
                pod_token = mgr.ks.get_value(pod_slot) if mgr.ks.get_key_entry(pod_slot) else None
            except Exception:
                pod_token = None
            if not pod_token:
                return jsonify({
                    "ok": False,
                    "error": (
                        "no access_token supplied and pod-wide "
                        f"{pod_slot} slot is empty — either paste a "
                        "per-bot PAT or set the pod-wide PAT in Plugins → "
                        "POD → Credentials first"
                    ),
                }), 400
            install_mode = "pod"
            slot_for_binding = pod_slot
            token_to_verify = pod_token

        # 1. Verify the PAT against GitHub /user. Bails on revoked / bad
        # format / connection-error before touching the keystore.
        verify_result = _github_mcp.verify_token(token_to_verify)
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

        # 2. Save token to keystore (override mode only). Pod-mode installs
        # don't touch any slot — the operator already set the pod-wide PAT
        # via the Credentials surface and we're just binding to it.
        if install_mode == "override":
            try:
                existing = mgr.ks.get_key_entry(slot_per_bot)
                if existing:
                    mgr.set_value(slot_per_bot, access_token)
                else:
                    username = verify_result.get("username") or "?"
                    scopes = ",".join(verify_result.get("scopes") or []) or "?"
                    mgr.register(
                        slot_per_bot,
                        provider="github",
                        scope="shared",
                        description=(
                            f"GitHub PAT (MCP override) for bot {bot_id} — "
                            f"user={username} scopes={scopes}"
                        ),
                        bots=None,
                        value=access_token,
                    )
            except Exception as exc:
                return jsonify({
                    "ok": False,
                    "error": f"keystore_write_failed: {exc.__class__.__name__}: {exc}",
                }), 500

        # 3. Install the MCP server entry. catalog_id="github" + env_bindings
        # referencing whichever slot we resolved above.
        summary = (
            f"Install GitHub MCP for {bot_id} "
            f"(user={verify_result.get('username') or '?'!r}, mode={install_mode})"
        )
        action_payload = {
            "bot_id": bot_id,
            "server_id": _github_mcp.GITHUB_MCP_SERVER_ID,
            "catalog_id": "github",
            "env_bindings": {
                "GITHUB_TOKEN": f"keystore:{slot_for_binding}",
            },
        }
        proposal, err = _create_github_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        # Rollback only the per-bot slot we wrote in override mode. In pod
        # mode we wrote nothing, so there's nothing to blank — and blanking
        # the pod-wide slot would break every OTHER bot bound to it.
        def _rollback_per_bot_slot() -> None:
            if install_mode != "override":
                return
            try:
                mgr.set_value(slot_per_bot, "")
            except Exception:
                pass

        if err:
            _rollback_per_bot_slot()
            _module._audit_log_entry("skill.github.install_mcp", bot_id, {
                "ok": False,
                "username": verify_result.get("username"),
                "error": f"mcp_install_create_failed: {err}",
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_create_failed: {err}",
            }), 500

        prop_status = (proposal or {}).get("status")
        if prop_status not in ("applied", "succeeded"):
            _rollback_per_bot_slot()
            _module._audit_log_entry("skill.github.install_mcp", bot_id, {
                "ok": False,
                "username": verify_result.get("username"),
                "proposal_status": prop_status,
            })
            return jsonify({
                "ok": False,
                "error": f"mcp_install_applier_returned_{prop_status}",
                "proposal": proposal,
            }), 500

        _module._audit_log_entry("skill.github.install_mcp", bot_id, {
            "ok": True,
            "username": verify_result.get("username"),
            "scopes": verify_result.get("scopes"),
            "mcp_proposal_status": prop_status,
        })

        status = _github_mcp_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            **status.to_dict(),
            "username": verify_result.get("username"),
            "scopes": verify_result.get("scopes"),
        })

    @app.post("/api/skills/install/github/revoke-mcp-server")
    def api_skills_github_revoke_mcp_server() -> Response:
        """Remove the github MCP server entry + blank the keystore slot.

        Does NOT touch the backup PAT in ~/.openclaw/workspace/.git/config
        — that's purpose 1 (backup), managed via the Backup page.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        proposal, err = _create_github_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": _github_mcp.GITHUB_MCP_SERVER_ID},
            bot_id=bot_id,
            summary=f"Remove GitHub MCP from {bot_id}",
        )

        slot_cleared = False
        slot_err: str | None = None
        try:
            from evolve_admin.keystore import KeystoreManager
            mgr = KeystoreManager(Path(
                load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
            ))
            slot = _github_mcp.keystore_slot_for(bot_id)
            if mgr.ks.get_key_entry(slot):
                mgr.set_value(slot, "")
                slot_cleared = True
        except Exception as exc:
            slot_err = str(exc)

        _module._audit_log_entry("skill.github.revoke_mcp", bot_id, {
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
                "GitHub MCP removed and keystore slot blanked. The "
                "backup PAT in ~/.openclaw/workspace/.git/config is "
                "independent — manage from the Backup page if you "
                "also want to disable backups."
            ),
        })

    # ── Withdrawn skill endpoints (2026-05-30) ────────────────────────────────
    #
    # The paste-token endpoints that used to live here have been removed:
    #   /api/skills/install/home_assistant/set-config
    #   /api/skills/install/home_assistant/revoke
    #
    # (obsidian/set-vault-path, dropbox/set-folder-path, notion/set-token,
    # linear/set-token, and runway-via-bundled-plugin were ALL withdrawn
    # at the same time but rewired same-day as real installs — see the
    # wrapper routes above. home_assistant remains withdrawn pending
    # MCP-server vetting + scope-toggle design per the roadmap.)
    #
    # They wrote a credential file the inventory then detected as
    # "configured" — but no code anywhere in the codebase consumed the file
    # at runtime. The bot couldn't actually use the skill. See
    # docs/design/skills-install-roadmap-2026-05-30.md for the per-skill plan.
    #
    # The skill modules themselves (notion_install.py / linear_install.py
    # / runway_install.py / home_assistant_install.py) are kept under
    # packages/admin/evolve_admin/skills/ — the verify_token helpers are
    # still useful for the install patterns that landed (and for future
    # ones).

    # ── iMessage skill routes ─────────────────────────────────────────────────
    # The /set-handle, /check-tcc, and /revoke endpoints live further up
    # (immediately before /api/skills/install/<skill_id>/enable-plugin)
    # alongside the bundled-plugin wiring that calls
    # _imessage.enable_channel_in_oc_config + kickstart_gateway. This block
    # is intentionally empty — kept as a navigation anchor so future
    # maintainers searching for "iMessage skill routes" land here and then
    # follow the comment back up to the actual definitions.

    # ── iMessage skill routes (2026-06-04 bundled-plugin rewire) ─────────────
    # The /set-handle route is the load-bearing one: it validates the handle,
    # writes channels.imessage + plugins.entries.imessage to the bot's
    # openclaw.json, and kickstarts the gateway so OC's bundled
    # @openclaw/imessage plugin loads. This closes the audit's three
    # load-bearing failures by handing the runtime to OC.

    @app.post("/api/skills/install/imessage/set-handle")
    def api_skills_imessage_set_handle() -> Response:
        """Set the bot's iMessage handle and wire the OC channel plugin.

        Body: ``{bot_id: str, handle: str, allowed_senders?: [str]}``.

        On success: writes channels.imessage + plugins.entries.imessage in
        the bot's openclaw.json, kickstarts the gateway, and re-runs
        ``resolve_status`` so the response carries the live state (which
        will typically be ``oc_probe_failed`` for a few seconds while the
        gateway settles, then transitions to ``active``).

        Returns 400 if handle is missing or malformed; 409 on a pod host
        platform that can't run the channel; 500 if config write or
        kickstart fails; 200 with the new status otherwise.
        """
        # Platform gate (design-linux-port §8: never show-and-fail). The
        # catalog hides iMessage on non-macOS hosts; refuse direct API
        # calls too, before any config write, so nothing half-wires a
        # channel upstream can't run on this host.
        if not _skill_supported_on_host(_imessage.SKILL_REGISTRY_ENTRY):
            return jsonify({
                "ok": False,
                "error": "skill_unavailable_on_platform",
                "detail": (
                    "iMessage requires a macOS pod host (upstream "
                    "OpenClaw constraint); this pod's host platform "
                    "cannot run the channel."
                ),
            }), 409

        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        handle = (body.get("handle") or "").strip()
        if not handle:
            return jsonify({"ok": False, "error": "handle required"}), 400

        # Basic handle validation: must look like a phone (+1…) or email
        import re as _re
        _phone_re = _re.compile(r"^\+\d{7,15}$")
        _email_re = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        if not (_phone_re.match(handle) or _email_re.match(handle)):
            return jsonify({
                "ok": False,
                "error": "handle_invalid",
                "detail": (
                    "The iMessage address must be either a phone number "
                    "(starting with +) or an email address."
                ),
            }), 400

        allowed_senders: list[str] = []
        raw_senders = body.get("allowed_senders")
        if isinstance(raw_senders, list):
            allowed_senders = [str(s).strip() for s in raw_senders if str(s).strip()]

        # Write channels.imessage + plugins.entries.imessage to the bot's
        # openclaw.json (the load-bearing wiring step).
        ok, err = _imessage.enable_channel_in_oc_config(
            bot_id, handle, allowed_senders=allowed_senders,
        )
        if not ok:
            _module._audit_log_entry("skill.imessage.set_handle.error", bot_id, {
                "handle": handle,
                "error": err,
            })
            return jsonify({
                "ok": False,
                "error": "oc_config_write_failed",
                "detail": err or "unknown write error",
            }), 500

        # Kickstart the bot's gateway so OC re-reads openclaw.json and
        # loads the @openclaw/imessage plugin. Without this, the plugin
        # doesn't activate even though the credential is on disk.
        ok2, err2 = _imessage._oc_common.kickstart_gateway(bot_id)
        if not ok2:
            _module._audit_log_entry("skill.imessage.set_handle.kickstart_error", bot_id, {
                "handle": handle, "error": err2,
            })
            # The config write succeeded; surface the kickstart problem
            # but don't fail outright — re-probe will pick up state
            return jsonify({
                "ok": False,
                "error": "kickstart_failed",
                "detail": err2 or "kickstart failed; bot may need manual restart",
            }), 500

        _module._audit_log_entry("skill.imessage.set_handle", bot_id, {
            "handle": handle,
            "allowed_senders_count": len(allowed_senders),
        })
        status = _imessage_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/imessage/check-tcc")
    def api_skills_imessage_check_tcc() -> Response:
        """Re-check TCC permissions after the operator toggles them in
        System Settings.

        Body: ``{bot_id: str}``. Returns the new status; useful when the
        UI's polling cadence is slower than the operator's click rate.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        status = _imessage_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/imessage/revoke")
    def api_skills_imessage_revoke() -> Response:
        """Tear down iMessage wiring for a bot.

        Body: ``{bot_id: str}``. Clears channels.imessage +
        plugins.entries.imessage from openclaw.json, deletes the legacy
        marker file if present, and kickstarts so OC unloads the plugin.
        TCC grants stay in place (they're pod-wide on the evolve user).
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        ok, err = _imessage.revoke_account(bot_id)
        _module._audit_log_entry("skill.imessage.revoke", bot_id, {
            "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "revoke_failed",
                "detail": err or "unknown revoke error",
            }), 500
        return jsonify({"ok": True, "cleared": True})

    @app.post("/api/skills/install/<skill_id>/enable-plugin")
    def api_skills_enable_plugin(skill_id: str) -> Response:
        """Enable the underlying OpenClaw plugin entry for the bot.

        Body: {bot_id: str}. Goes through the same operator-UI inline
        proposal pipeline (``EnablePluginEntry`` → applier → ``succeeded``)
        that the Plugins admin tab uses. We do NOT bypass security_warden —
        refusals land as ``failed_flagged`` and the response's
        ``proposal.status`` shows why so the UI can surface the reason.

        On success the OpenClaw gateway needs to pick up the new plugin entry
        before OAuth tokens can be exercised. The applier emits a restart
        hint where appropriate; this route does not force a restart itself.

        Note: filesystem skills (obsidian) do not use this route — they have
        no OpenClaw plugin entry to enable.
        """
        if skill_id != _gog.GOG_SKILL_ID:
            return jsonify({
                "ok": False,
                "error": f"unknown skill {skill_id!r}",
                "hint": "Only 'gog' uses the enable-plugin flow.",
            }), 404
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"schema import failed: {exc}"}), 500
        # Resolve the shared dir the same way the plugins-admin routes do.
        shared_dir = Path(
            load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR)
        )
        risk = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["bot_config"],
        )
        proposal, err = _module._operator_create_apply(
            action_kind="EnablePluginEntry",
            action_payload={
                "bot_id": bot_id,
                "plugin_name": _gog.GOG_PLUGIN_NAME,
            },
            bot_id=bot_id,
            summary=(
                f"Enable {_gog.GOG_PLUGIN_NAME!r} plugin on {bot_id} "
                f"as part of {_gog.GOG_SKILL_ID!r} skill install"
            ),
            technique="operator_ui_skills",
            dimension="operational_health",
            risk=risk,
            shared_dir=shared_dir,
        )
        _module._audit_log_entry("skill.install.enable_plugin", bot_id, {
            "skill_id": skill_id,
            "plugin_name": _gog.GOG_PLUGIN_NAME,
            "proposal_status": (proposal or {}).get("status"),
            "creation_error": err,
        })
        return _operator_proposal_response(proposal, err)

    def _slack_shared_dir() -> Path:
        """Resolve shared dir from network.json, same as other routes."""
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _slack_resolve_status(bot_id: str) -> "_slack.InstallStatus":
        """Resolve Slack install status with live credential + token reads."""
        return _slack.resolve_status(
            bot_id,
            shared_dir=_slack_shared_dir(),
        )

    def _discord_shared_dir() -> Path:
        """Resolve shared dir from network.json, same as other routes."""
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _discord_resolve_status(bot_id: str) -> "_discord.InstallStatus":
        """Resolve Discord install status with live credential + token reads."""
        return _discord.resolve_status(
            bot_id,
            shared_dir=_discord_shared_dir(),
        )

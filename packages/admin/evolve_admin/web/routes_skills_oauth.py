"""HTTP routes for the OAuth-based skill installs — Slack / Discord.

``/api/skills/install/slack/{status,start-oauth,oauth-callback,poll,revoke}``
  plus the plan endpoint ``POST /api/skills/install/slack``
``/api/skills/install/discord/{status,set-token,start-oauth,confirm,poll,revoke}``
  plus the plan endpoint ``POST /api/skills/install/discord``

The OAuth-dance slice of the ``/api/skills/install/*`` region: each install
runs an authorization flow (Slack: begin → Slack consent → ``oauth-callback``
code-for-token exchange → poll; Discord: begin → guild-invite URL → ``confirm``
→ poll) rather than a single operator-pasted secret (the Notion/Runway/Linear
token cluster moved in Increment 2a) or a device-pairing poll (the
WhatsApp/Signal/Telegram pairing cluster moves in a later sub-PR). Discord's
``set-token`` paste path (used by the add-bot wizard) lives with the rest of the
Discord family here.

Split out of ``routes_admin.py``'s ``register_admin_routes`` closure — 4.1b
Increment 2b (skills OAuth-install cluster) — per the strategy memo
``internal/design-routes-admin-decomposition-2026-06-12.md`` (Option A: a sibling
``register_*_routes(app, network_path)`` module mirroring the ~12 that already
exist; NOT Blueprints, NOT a ctx object). Pure code-motion: no route
added/removed/renamed/re-pathed/re-method-ed; no request/response shape,
validation, error-handling, OAuth-secret, sudo, or keystore behavior change.

Privileged surface (doctrine auditor-grade bar):
  * OAuth secrets touched: the pod-level Slack ``client_id``/``client_secret``
    and the pod-level Discord ``client_id``/``client_secret``/``bot_token`` live
    in the shared keystore (read via ``slack_install.read_slack_credentials`` /
    ``discord_install.read_discord_credentials``). Per-bot grants — the Slack
    workspace bot token obtained from the ``oauth.v2.access`` exchange and the
    Discord per-bot activation record — are written to
    ``~/.openclaw/skills/{slack,discord}.json`` via the install modules'
    sudo-aware ``write_token_config`` helpers (per CLAUDE.md /tmp-staging +
    ``sudo /bin/cp``).
  * Channel wiring (``channels.{slack,discord}`` + ``plugins.entries.*.enabled``)
    is written to ``openclaw.json`` via the install modules'
    ``enable_channel_in_oc_config`` / shared ``_oc_install_common``
    ``disable_channel_in_oc_config``; the gateway is kickstarted so OC re-reads.
  * The OAuth ``state`` token store (CSRF + replay guard for the callback) lives
    inside the install modules (``slack_state_*`` / ``discord_state_*``), not
    here.

§1.3 monkeypatch-at-call-time invariant (memo): handlers reach the patchable
server helper through ``_module._NAME`` (``_module = sys.modules[
"evolve_admin.web.server"]``) at call time, so test monkeypatches on
``server._NAME`` are honored. The only such helper this surface touches is
``server._audit_log_entry`` — NOT imported as a module-level name here (that
would shadow the patch).

The ``_slack_resolve_status`` / ``_discord_resolve_status`` shims (and their
``_slack_shared_dir`` / ``_discord_shared_dir`` deps) are recreated verbatim
here AND kept in ``routes_admin.py``: the generic
``/api/skills/install/<skill_id>{,/status}`` dispatchers that stay in
``routes_admin.py`` still call them. Recreating the thin wrappers — rather than
importing them — keeps the moved call sites byte-identical, the same shape
Increment 2a used.
"""

from __future__ import annotations

import json as _json  # alias used by the _*_close_tab_html helpers
import sys
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR  # type: ignore

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_skills_oauth")


def register_skills_oauth_routes(app: Flask, network_path: Path) -> None:
    # Late-bound server-module handle (memo §1.3): handlers reach the
    # patchable ``_audit_log_entry`` via ``_module._NAME`` at call time so
    # test monkeypatches on ``server._NAME`` are respected. Derived inside
    # the function (mirroring ``routes_admin.register_admin_routes``) so
    # importing this module never requires ``server`` to be in
    # ``sys.modules`` yet.
    _module = sys.modules["evolve_admin.web.server"]

    from ..skills import slack_install as _slack
    from ..skills import discord_install as _discord
    # Shared install/revoke mechanics — used by the slack/discord revoke
    # routes for symmetric channel tear-down (closes deep-audit 2026-05-30 F2).
    from ..skills import _oc_install_common as _oc_common

    # ── Slack skill install routes (V2.1-2) ───────────────────────────────────
    # Mirror the GOG install flow for Slack. Credentials (Client ID + Secret)
    # are pod-level, stored in the shared keystore. Per-bot tokens are stored
    # at ~/.openclaw/skills/slack.json via /tmp staging + sudo /bin/cp.
    #
    # Trust-chain notes:
    #   - Bot tokens only — we never store user (xoxp) tokens.
    #   - Token storage is per-bot, never centralized.
    #   - Scopes are conservative: chat:write, channels:read, im:read, im:write.
    #   - Pod admin must register a Slack app once (see docs/skills/slack-setup.md).

    def _slack_shared_dir() -> Path:
        """Resolve shared dir from network.json, same as other routes."""
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _slack_read_credentials() -> tuple[str | None, str | None]:
        """Read pod-level Slack credentials from keystore."""
        return _slack.read_slack_credentials(_slack_shared_dir())

    def _slack_resolve_status(bot_id: str) -> "_slack.InstallStatus":
        """Resolve Slack install status with live credential + token reads."""
        return _slack.resolve_status(
            bot_id,
            shared_dir=_slack_shared_dir(),
        )

    def _slack_close_tab_html(message: str, ok: bool) -> str:
        """Tiny self-closing HTML returned to the Slack OAuth popup.
        Posts a message to the opener and closes the tab. Same pattern as GOG.
        """
        color = "#34a853" if ok else "#ea4335"
        icon = "✅" if ok else "❌"
        msg_json = _json.dumps(message)
        ok_json = _json.dumps(ok)
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Slack authorization</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
            "background:#0a0a0a;color:#eee;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0}"
            ".box{text-align:center;padding:32px;border-radius:8px;border:1px solid #333}"
            ".icon{font-size:48px;margin-bottom:12px}"
            f".msg{{color:{color}}}"
            "</style></head><body>"
            f"<div class='box'><div class='icon'>{icon}</div>"
            f"<div class='msg'>{message}</div>"
            "<div style='font-size:0.78rem;color:#888;margin-top:12px'>"
            "You can close this tab.</div></div>"
            "<script>"
            "try{if(window.opener){window.opener.postMessage("
            f"{{type:'slack-oauth',ok:{ok_json},message:{msg_json}}},'*');}}"
            "}catch(e){}"
            "setTimeout(function(){try{window.close();}catch(e){}},800);"
            "</script></body></html>"
        )

    @app.get("/api/skills/install/slack/status")
    def api_skills_slack_status() -> Response:
        """Return the bot's current Slack install status.

        Query: ?bot_id=<bot>. Status values:
          credentials_missing | missing | valid | revoked | unknown

        ``status == "valid"`` is the completion signal for the UI auto-poll.
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        status = _slack_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/slack")
    def api_skills_slack_install_plan() -> Response:
        """Compute the Slack install plan for the given bot.

        Body: {bot_id: str}. Returns:
            {ok, status: <InstallStatus dict>,
             steps: [<InstallStep dict>...],
             skill: {id, display_name, summary, access_panel}}

        The UI walks ``steps`` in order. The OAuth step carries the
        plain-language access panel so the user sees it before consenting.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        status = _slack_resolve_status(bot_id)
        steps = _slack.build_install_plan(status)
        reg = _slack.SKILL_REGISTRY_ENTRY
        _module._audit_log_entry("skill.install.plan", bot_id, {
            "skill_id": _slack.SLACK_SKILL_ID,
            "current_status": status.token_state,
            "step_count": len(steps),
        })
        # V2.4-4: standardised plan_requested (dedicated Slack plan endpoint)
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_slack.SLACK_SKILL_ID, bot_id, "plan_requested", {
                "skill_id": _slack.SLACK_SKILL_ID,
            })
        except Exception:
            pass
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

    @app.post("/api/skills/install/slack/start-oauth")
    def api_skills_slack_start_oauth() -> Response:
        """Build a Slack authorization URL for the given bot.

        Body: {bot_id: str}. Returns {authorize_url, state}.
        Returns 412 if pod Slack credentials are not configured.

        The authorize URL directs the user to Slack's OAuth consent screen.
        After approval, Slack redirects to /api/skills/install/slack/oauth-callback.
        """
        import urllib.parse as _up

        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        client_id, client_secret = _slack_read_credentials()
        if not client_id or not client_secret:
            return jsonify({
                "ok": False,
                "error": "slack_credentials_not_configured",
                "hint": (
                    "Pod admin needs to register a Slack app and add credentials "
                    "to the keystore. See docs/skills/slack-setup.md."
                ),
            }), 412

        host = request.headers.get("Host") or "localhost:5050"
        scheme = request.headers.get("X-Forwarded-Proto") or (
            "https" if request.is_secure else "http"
        )
        redirect_uri = f"{scheme}://{host}/api/skills/install/slack/oauth-callback"
        state = _slack.slack_state_create(bot_id, redirect_uri)

        params = {
            "client_id": client_id,
            "scope": ",".join(_slack.SLACK_DEFAULT_SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        authorize_url = f"{_slack.SLACK_AUTHORIZE_URL}?{_up.urlencode(params)}"

        _module._audit_log_entry("skill.slack.start_oauth", bot_id, {
            "scopes": list(_slack.SLACK_DEFAULT_SCOPES),
        })
        # V2.4-4: standardised oauth_started event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_slack.SLACK_SKILL_ID, bot_id, "oauth_started", {
                "scopes": list(_slack.SLACK_DEFAULT_SCOPES),
            })
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "authorize_url": authorize_url,
            "state": state,
            "redirect_uri": redirect_uri,
        })

    @app.get("/api/skills/install/slack/oauth-callback")
    def api_skills_slack_oauth_callback() -> Response:
        """OAuth redirect target for Slack.

        Receives ``code`` + ``state`` from Slack, exchanges for a workspace
        bot token via ``oauth.v2.access``, stores token + workspace metadata
        in ``~/<bot_home>/.openclaw/skills/slack.json``.

        Always returns a small HTML page that closes the popup tab. Result is
        delivered to the UI via the /poll endpoint or postMessage.
        """
        state = request.args.get("state") or ""
        code = request.args.get("code") or ""
        error = request.args.get("error") or ""

        if not state:
            return Response(
                _slack_close_tab_html("Missing state", False),
                mimetype="text/html",
            ), 400

        entry = _slack.slack_state_get(state)
        if not entry:
            return Response(
                _slack_close_tab_html("Unknown or expired state", False),
                mimetype="text/html",
            ), 400

        bot_id = entry["bot_id"]
        redirect_uri = entry.get("redirect_uri") or ""

        if error:
            _slack.slack_state_set_result(state, {
                "status": "denied" if error == "access_denied" else "error",
                "error": error,
            })
            _module._audit_log_entry("skill.slack.oauth_callback", bot_id, {
                "ok": False, "error": error,
            })
            human = (
                "Authorization denied"
                if error == "access_denied"
                else f"Slack error: {error}"
            )
            return Response(
                _slack_close_tab_html(human, False),
                mimetype="text/html",
            ), 200

        if not code:
            _slack.slack_state_set_result(state, {"status": "error", "error": "no_code"})
            return Response(
                _slack_close_tab_html("No code returned", False),
                mimetype="text/html",
            ), 400

        client_id, client_secret = _slack_read_credentials()
        if not client_id or not client_secret:
            _slack.slack_state_set_result(state, {
                "status": "error", "error": "credentials_not_configured",
            })
            return Response(
                _slack_close_tab_html("Slack credentials not configured", False),
                mimetype="text/html",
            ), 500

        ok, token_body, err_msg = _slack.exchange_code_for_token(
            code, client_id, client_secret, redirect_uri,
        )
        if not ok or not isinstance(token_body, dict):
            _slack.slack_state_set_result(state, {
                "status": "error",
                "error": f"token_exchange_failed: {err_msg}",
            })
            return Response(
                _slack_close_tab_html(f"Token exchange failed ({err_msg})", False),
                mimetype="text/html",
            ), 200

        # Slack v2 access token response shape:
        # {ok, access_token, token_type, scope, bot_user_id, team: {id, name}, ...}
        bot_token = token_body.get("access_token") or ""
        workspace_id = (token_body.get("team") or {}).get("id") or ""
        workspace_name = (token_body.get("team") or {}).get("name") or ""
        bot_user_id = token_body.get("bot_user_id") or ""
        scopes = [s.strip() for s in (token_body.get("scope") or "").split(",") if s.strip()]

        if not bot_token:
            _slack.slack_state_set_result(state, {
                "status": "error", "error": "no_access_token",
            })
            return Response(
                _slack_close_tab_html("No access token returned", False),
                mimetype="text/html",
            ), 200

        import time as _time_mod
        config = {
            "bot_token": bot_token,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "bot_user_id": bot_user_id,
            "scopes": scopes,
            "issued_at": _time_mod.time(),
        }

        wrote_ok, write_err = _slack.write_token_config(bot_id, config)
        if not wrote_ok:
            _slack.slack_state_set_result(state, {
                "status": "error", "error": f"token_write_failed: {write_err}",
            })
            _module._audit_log_entry("skill.slack.oauth_callback", bot_id, {
                "ok": False, "error": f"write_failed: {write_err}",
            })
            return Response(
                _slack_close_tab_html("Could not save credentials", False),
                mimetype="text/html",
            ), 500

        # Wire the channel into openclaw.json. Same shape as the Telegram fix
        # (#1757): without channels.slack + plugins.entries.slack.enabled, the
        # gateway never loads the Slack channel plugin and inbound messages
        # silently drop. Slack-specific wrinkle: socket mode also needs an
        # appToken (xapp-) which the OAuth flow doesn't deliver; helpers seed
        # the safe-default policy fields and idempotently preserve any
        # operator-set appToken / mode on a redo.
        oc_ok, oc_err = _slack.enable_channel_in_oc_config(bot_id, bot_token)
        if not oc_ok:
            _slack.slack_state_set_result(state, {
                "status": "error", "error": f"oc_config_write_failed: {oc_err}",
            })
            _module._audit_log_entry("skill.slack.oauth_callback", bot_id, {
                "ok": False, "error": f"oc_write_failed: {oc_err}",
            })
            return Response(
                _slack_close_tab_html(
                    f"Connected to Slack but could not update openclaw.json ({oc_err})",
                    False,
                ),
                mimetype="text/html",
            ), 500

        # Kickstart the gateway so it picks up the new channels + plugin entry.
        # Best-effort: surface the failure but keep the install otherwise
        # complete (credential + openclaw.json are already on disk).
        kick_ok, kick_err = _slack.kickstart_gateway(bot_id)

        _slack.slack_state_set_result(state, {
            "status": "success",
            "bot_id": bot_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "bot_user_id": bot_user_id,
            "scopes": scopes,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        _module._audit_log_entry("skill.slack.oauth_callback", bot_id, {
            "ok": True,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "bot_user_id": bot_user_id,
            "oc_config_applied": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        # V2.4-4: standardised activated event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_slack.SLACK_SKILL_ID, bot_id, "activated", {
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "bot_user_id": bot_user_id,
                "scopes": scopes,
            })
        except Exception:
            pass
        workspace_label = workspace_name or workspace_id or bot_id
        return Response(
            _slack_close_tab_html(
                f"Connected to Slack workspace: {workspace_label}",
                True,
            ),
            mimetype="text/html",
        )

    @app.post("/api/skills/install/slack/poll")
    def api_skills_slack_poll() -> Response:
        """Poll for Slack OAuth callback completion by state token.

        Body: {state}. Returns {pending: true} or the result dict.
        Successful / terminal results consume the state (prevent replay).
        """
        body = request.get_json(silent=True) or {}
        state = (body.get("state") or "").strip()
        if not state:
            return jsonify({"error": "state required"}), 400
        entry = _slack.slack_state_get(state)
        if not entry:
            return jsonify({"status": "expired"}), 410
        result = entry.get("result") or {"status": "pending"}
        if result.get("status") == "pending":
            return jsonify({"pending": True})
        # Terminal — consume and return
        _slack.slack_state_consume(state)
        return jsonify(result)

    @app.post("/api/skills/install/slack/revoke")
    def api_skills_slack_revoke() -> Response:
        """Revoke the bot's Slack token and clear local config.

        Body: {bot_id: str}. Returns {ok, cleared, cleared_oc_config, kickstarted}.

        Symmetric revoke per deep-audit 2026-05-30 F2: delete the marker file,
        remove ``channels.slack`` + ``plugins.entries.slack`` from openclaw.json,
        kickstart the gateway so OC drops the channel. Slack tokens can also
        be revoked at the source from the Slack app-management page at
        api.slack.com/apps.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        cleared = _slack.delete_token_config(bot_id)
        oc_ok, oc_err = _oc_common.disable_channel_in_oc_config(bot_id, "slack")
        ks_ok, ks_err = (False, "skipped") if not oc_ok else _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.slack.revoke", bot_id, {
            "local_cleared": cleared,
            "cleared_oc_config": oc_ok, "oc_config_error": oc_err,
            "kickstarted": ks_ok, "kickstart_error": ks_err,
        })
        # V2.4-4: standardised revoked event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_slack.SLACK_SKILL_ID, bot_id, "revoked", {
                "local_cleared": cleared,
                "cleared_oc_config": oc_ok,
                "kickstarted": ks_ok,
            })
        except Exception:
            pass
        ok = cleared or oc_ok
        return jsonify({
            "ok": ok, "cleared": cleared,
            "cleared_oc_config": oc_ok,
            "kickstarted": ks_ok,
        })

    # ── Discord skill install routes (V2.3-2) ──────────────────────────────────
    # Mirror the Slack install flow for Discord. Credentials (Client ID + Client
    # Secret + Bot Token) are pod-level, stored in the shared keystore. Per-bot
    # activation config is stored at ~/.openclaw/skills/discord.json via /tmp
    # staging + sudo /bin/cp.
    #
    # Discord vs. Slack flow differences:
    #   - The bot token is a single pod-level credential (not per-workspace).
    #   - Guild membership comes from a bot invite URL, not a per-user OAuth flow.
    #   - The /start-oauth route generates a guild-invite URL and returns a state
    #     token. The operator opens the URL in a browser to add the bot to guilds.
    #   - After inviting, the operator calls /confirm to verify the token is valid.
    #   - No code → token exchange: the bot token is already in the keystore.
    #
    # Trust-chain notes:
    #   - Bot token only — we never store user (OAuth) tokens.
    #   - Token storage is per-bot config, pod-level token, never centralized.
    #   - Default permissions: VIEW_CHANNEL + SEND_MESSAGES + READ_MESSAGE_HISTORY
    #     + USE_APPLICATION_COMMANDS. No Administrator permission.
    #   - Pod admin must register a Discord app once (see docs/skills/discord-setup.md).

    def _discord_shared_dir() -> Path:
        """Resolve shared dir from network.json, same as other routes."""
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _discord_read_credentials() -> tuple[str | None, str | None, str | None]:
        """Read pod-level Discord credentials from keystore."""
        return _discord.read_discord_credentials(_discord_shared_dir())

    def _discord_resolve_status(bot_id: str) -> "_discord.InstallStatus":
        """Resolve Discord install status with live credential + token reads."""
        return _discord.resolve_status(
            bot_id,
            shared_dir=_discord_shared_dir(),
        )

    def _discord_close_tab_html(message: str, ok: bool) -> str:
        """Tiny self-closing HTML returned to the Discord invite popup.
        Posts a message to the opener and closes the tab. Same pattern as Slack.
        """
        color = "#5865f2" if ok else "#ed4245"  # Discord blurple / red
        icon = "✅" if ok else "❌"
        msg_json = _json.dumps(message)
        ok_json = _json.dumps(ok)
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Discord authorization</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
            "background:#0a0a0a;color:#eee;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0}"
            ".box{text-align:center;padding:32px;border-radius:8px;border:1px solid #333}"
            ".icon{font-size:48px;margin-bottom:12px}"
            f".msg{{color:{color}}}"
            "</style></head><body>"
            f"<div class='box'><div class='icon'>{icon}</div>"
            f"<div class='msg'>{message}</div>"
            "<div style='font-size:0.78rem;color:#888;margin-top:12px'>"
            "You can close this tab.</div></div>"
            "<script>"
            "try{if(window.opener){window.opener.postMessage("
            f"{{type:'discord-oauth',ok:{ok_json},message:{msg_json}}},'*');}}"
            "}catch(e){}"
            "setTimeout(function(){try{window.close();}catch(e){}},800);"
            "</script></body></html>"
        )

    @app.get("/api/skills/install/discord/status")
    def api_skills_discord_status() -> Response:
        """Return the bot's current Discord install status.

        Query: ?bot_id=<bot>. Status values:
          credentials_missing | missing | valid | revoked | unknown

        ``status == "valid"`` is the completion signal for the UI auto-poll.
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        status = _discord_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/discord/set-token")
    def api_skills_discord_set_token() -> Response:
        """Accept a Discord bot token, verify via /users/@me, store on success.

        Body: {bot_id: str, bot_token: str}

        Parallel to the Telegram set-token endpoint — used by the add-bot
        wizard's Screen 4 messaging-channel step, where the operator pastes
        the bot token from the Discord Developer Portal directly rather than
        going through the pod-level keystore + OAuth invite flow used by the
        Skills page. Both flows converge on the same per-bot state:
        ``~/.openclaw/skills/discord.json`` plus ``channels.discord`` and
        ``plugins.entries.discord.enabled`` in openclaw.json.

        Returns 400 for missing inputs, 422 if Discord rejects the token,
        500 on disk write failure, 200 + updated status on success.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        bot_token = (body.get("bot_token") or "").strip()
        if not bot_token:
            return jsonify({"ok": False, "error": "bot_token required"}), 400

        # Same cheap sanity check as Telegram before hitting the API.
        if len(bot_token) > 512:
            return jsonify({
                "ok": False,
                "error": "token_too_long",
                "detail": "A Discord bot token cannot be longer than 512 characters.",
            }), 400

        ok, err, user_info = _discord.verify_token(bot_token)
        if not ok or not user_info:
            _module._audit_log_entry("skill.discord.set_token.invalid", bot_id, {
                "error": err or "unauthorized",
            })
            return jsonify({
                "ok": False,
                "error": "token_invalid",
                "detail": (
                    f"Discord rejected the token: {err or 'unauthorized'}. "
                    "Check that you copied the bot token from the Discord "
                    "Developer Portal → Bot section (not the Client Secret)."
                ),
            }), 422

        import time as _time_mod
        bot_user_id = user_info.get("id") or ""
        bot_username = user_info.get("username") or ""
        config = {
            "bot_user_id": bot_user_id,
            "bot_username": bot_username,
            "invited_guilds": [],
            "activated_at": _time_mod.time(),
        }

        wrote_ok, write_err = _discord.write_token_config(bot_id, config)
        if not wrote_ok:
            _module._audit_log_entry("skill.discord.set_token.error", bot_id, {
                "error": write_err,
            })
            return jsonify({
                "ok": False,
                "error": "config_write_failed",
                "detail": write_err or "unknown write error",
            }), 500

        # Wire the channel into openclaw.json. Without channels.discord +
        # plugins.entries.discord.enabled the gateway never loads the Discord
        # plugin and inbound messages are silently dropped — same dead-end
        # the Telegram fix (#1757) and the Discord /confirm route guard against.
        oc_ok, oc_err = _discord.enable_channel_in_oc_config(bot_id, bot_token)
        if not oc_ok:
            _module._audit_log_entry("skill.discord.set_token.oc_write_failed", bot_id, {
                "error": oc_err,
            })
            return jsonify({
                "ok": False,
                "error": "oc_config_write_failed",
                "detail": oc_err or "unknown openclaw.json write error",
            }), 500

        # Best-effort kickstart so the gateway picks up the new wiring.
        kick_ok, kick_err = _discord.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.discord.set_token", bot_id, {
            "bot_user_id": bot_user_id,
            "bot_username": bot_username,
            "oc_config_applied": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "set_token", {
                "bot_user_id": bot_user_id,
                "bot_username": bot_username,
            })
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "activated", {
                "bot_user_id": bot_user_id,
                "bot_username": bot_username,
            })
        except Exception:
            pass
        status = _discord_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
            **status.to_dict(),
        })

    @app.post("/api/skills/install/discord")
    def api_skills_discord_install_plan() -> Response:
        """Compute the Discord install plan for the given bot.

        Body: {bot_id: str}. Returns:
            {ok, status: <InstallStatus dict>,
             steps: [<InstallStep dict>...],
             skill: {id, display_name, summary, access_panel}}

        The UI walks ``steps`` in order. The invite step carries the
        plain-language access panel so the user sees it before consenting.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        status = _discord_resolve_status(bot_id)
        steps = _discord.build_install_plan(status)
        reg = _discord.SKILL_REGISTRY_ENTRY
        _module._audit_log_entry("skill.install.plan", bot_id, {
            "skill_id": _discord.DISCORD_SKILL_ID,
            "current_status": status.token_state,
            "step_count": len(steps),
        })
        # V2.4-4: standardised plan_requested (dedicated Discord plan endpoint)
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "plan_requested", {
                "skill_id": _discord.DISCORD_SKILL_ID,
            })
        except Exception:
            pass
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

    @app.post("/api/skills/install/discord/start-oauth")
    def api_skills_discord_start_oauth() -> Response:
        """Generate the Discord bot invite URL for the given bot.

        Body: {bot_id: str}. Returns {invite_url, state}.
        Returns 412 if pod Discord credentials are not configured.

        The invite URL directs the operator to Discord's server-selection screen.
        After the operator selects a server and confirms, the bot is added to
        that guild. The operator then calls /confirm to validate the token.

        Unlike Slack, there is no code → token exchange in the callback.
        The bot token is already in the keystore. The OAuth "flow" here is
        purely the guild-invite step.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        client_id, _client_secret, bot_token = _discord_read_credentials()
        if not client_id or not bot_token:
            return jsonify({
                "ok": False,
                "error": "discord_credentials_not_configured",
                "hint": (
                    "Pod admin needs to register a Discord app and add credentials "
                    "to the keystore. See docs/skills/discord-setup.md."
                ),
            }), 412

        invite_url = _discord.build_invite_url(client_id)
        state = _discord.discord_state_create(bot_id, invite_url)

        _module._audit_log_entry("skill.discord.start_oauth", bot_id, {
            "scopes": list(_discord.DISCORD_DEFAULT_SCOPES),
            "permissions": _discord.DISCORD_DEFAULT_PERMISSIONS,
        })
        # V2.4-4: standardised oauth_started event (Discord = guild invite flow)
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "oauth_started", {
                "scopes": list(_discord.DISCORD_DEFAULT_SCOPES),
                "permissions": _discord.DISCORD_DEFAULT_PERMISSIONS,
            })
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "invite_url": invite_url,
            "state": state,
        })

    @app.post("/api/skills/install/discord/confirm")
    def api_skills_discord_confirm() -> Response:
        """Confirm the Discord bot token is valid and write per-bot config.

        Body: {bot_id: str}. Validates the keystore bot token against Discord's
        /users/@me endpoint and writes ~/.openclaw/skills/discord.json.

        This replaces the OAuth callback step used by Slack: since Discord's
        bot token is pod-level (not obtained via per-user OAuth), we confirm by
        verifying the token and saving the per-bot activation record.

        Returns {ok, status: "valid", bot_user_id, bot_username} on success.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        _client_id, _client_secret, bot_token = _discord_read_credentials()
        if not bot_token:
            return jsonify({
                "ok": False,
                "error": "discord_bot_token_not_configured",
                "hint": (
                    "Pod admin needs to add the bot token to the keystore. "
                    "See docs/skills/discord-setup.md."
                ),
            }), 412

        ok, err, user_info = _discord.verify_token(bot_token)
        if not ok or not user_info:
            _module._audit_log_entry("skill.discord.confirm", bot_id, {
                "ok": False, "error": err or "token_invalid",
            })
            return jsonify({
                "ok": False,
                "error": err or "token_invalid",
                "hint": (
                    "The Discord bot token appears to be invalid. "
                    "Check the token in the keystore and the Developer Portal."
                ),
            }), 400

        import time as _time_mod
        bot_user_id = user_info.get("id") or ""
        bot_username = user_info.get("username") or ""
        config = {
            "bot_user_id": bot_user_id,
            "bot_username": bot_username,
            "invited_guilds": [],
            "activated_at": _time_mod.time(),
        }

        wrote_ok, write_err = _discord.write_token_config(bot_id, config)
        if not wrote_ok:
            _module._audit_log_entry("skill.discord.confirm", bot_id, {
                "ok": False, "error": f"write_failed: {write_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"config_write_failed: {write_err}",
            }), 500

        # Wire the channel into openclaw.json. Same shape as the Telegram fix
        # (#1757) and Slack: without channels.discord + plugins.entries.discord.
        # enabled, the gateway never loads the Discord channel plugin and
        # inbound messages silently drop even though skills/discord.json is
        # on disk. Pre-fix, the install returned ok=true but the bot's gateway
        # remained unwired -- exactly the dead-end pattern documented in
        # internal/audit-skills-install-flows-2026-05-30.md.
        oc_ok, oc_err = _discord.enable_channel_in_oc_config(bot_id, bot_token)
        if not oc_ok:
            _module._audit_log_entry("skill.discord.confirm", bot_id, {
                "ok": False, "error": f"oc_write_failed: {oc_err}",
            })
            return jsonify({
                "ok": False,
                "error": f"oc_config_write_failed: {oc_err}",
                "hint": (
                    "Discord credential saved, but could not update "
                    "openclaw.json to enable the channel."
                ),
            }), 500

        # Kickstart the gateway so it picks up the new channels + plugin entry.
        # Best-effort: surface the failure but keep the install otherwise
        # complete (credential + openclaw.json are already on disk).
        kick_ok, kick_err = _discord.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.discord.confirm", bot_id, {
            "ok": True,
            "bot_user_id": bot_user_id,
            "bot_username": bot_username,
            "oc_config_applied": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        # V2.4-4: standardised activated event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "activated", {
                "bot_user_id": bot_user_id,
                "bot_username": bot_username,
            })
        except Exception:
            pass
        return jsonify({
            "ok": True,
            "status": "valid",
            "bot_user_id": bot_user_id,
            "bot_username": bot_username,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })

    @app.post("/api/skills/install/discord/poll")
    def api_skills_discord_poll() -> Response:
        """Poll for Discord invite flow completion by state token.

        Body: {state}. Returns {pending: true} or the result dict.
        Successful / terminal results consume the state (prevent replay).

        The Discord flow is manual (operator opens invite URL in browser),
        so this endpoint lets the UI detect when the operator returns.
        """
        body = request.get_json(silent=True) or {}
        state = (body.get("state") or "").strip()
        if not state:
            return jsonify({"error": "state required"}), 400
        entry = _discord.discord_state_get(state)
        if not entry:
            return jsonify({"status": "expired"}), 410
        result = entry.get("result") or {"status": "pending"}
        if result.get("status") == "pending":
            return jsonify({"pending": True})
        # Terminal — consume and return
        _discord.discord_state_consume(state)
        return jsonify(result)

    @app.post("/api/skills/install/discord/revoke")
    def api_skills_discord_revoke() -> Response:
        """Revoke the bot's Discord config and clear local config.

        Body: {bot_id: str}. Returns {ok, cleared, cleared_oc_config, kickstarted}.

        Symmetric revoke per deep-audit 2026-05-30 F2: delete the marker file,
        remove ``channels.discord`` + ``plugins.entries.discord`` from
        openclaw.json, kickstart the gateway. The pod-level bot token in the
        keystore is unaffected — revoking here only removes this bot's
        activation record. The bot token itself can be revoked from the
        Discord Developer Portal at discord.com/developers/applications.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        cleared = _discord.delete_token_config(bot_id)
        oc_ok, oc_err = _oc_common.disable_channel_in_oc_config(bot_id, "discord")
        ks_ok, ks_err = (False, "skipped") if not oc_ok else _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.discord.revoke", bot_id, {
            "local_cleared": cleared,
            "cleared_oc_config": oc_ok, "oc_config_error": oc_err,
            "kickstarted": ks_ok, "kickstart_error": ks_err,
        })
        # V2.4-4: standardised revoked event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_discord.DISCORD_SKILL_ID, bot_id, "revoked", {
                "local_cleared": cleared,
                "cleared_oc_config": oc_ok,
                "kickstarted": ks_ok,
            })
        except Exception:
            pass
        ok = cleared or oc_ok
        return jsonify({
            "ok": ok, "cleared": cleared,
            "cleared_oc_config": oc_ok,
            "kickstarted": ks_ok,
        })

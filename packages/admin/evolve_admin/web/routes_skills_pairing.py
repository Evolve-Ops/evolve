"""HTTP routes for the device-pairing skill installs — Telegram / WhatsApp / Signal.

``POST /api/skills/install/telegram`` (install-plan) +
``/api/skills/install/telegram/{status,set-token,revoke}``
``/api/skills/install/whatsapp/{install-plugin,pair/start,pair/<sid>,pair/<sid>/cancel,revoke}``
``/api/skills/install/signal/{install-plugin,set-number,pair/start,pair/<sid>,pair/<sid>/cancel,revoke}``

The device-pairing / link-account slice of the ``/api/skills/install/*``
region: these messaging integrations install by linking the bot to a device
or account rather than by storing a pasted secret (Notion/Runway/Linear →
Increment 2a) or running an OAuth redirect (Slack/Discord → Increment 2b).
WhatsApp and Signal use an interactive QR pairing session (``pair/start`` →
short-poll ``pair/<session_id>`` until ``paired``, which auto-finalises the
openclaw.json channel write + gateway kickstart); Telegram links via a
BotFather bot token verified against ``getMe``.

Split out of ``routes_admin.py``'s ``register_admin_routes`` closure — 4.1b
Increment 2c (skills device-pairing-install cluster) — per the strategy memo
``internal/design-routes-admin-decomposition-2026-06-12.md`` (Option A: a sibling
``register_*_routes(app, network_path)`` module mirroring the ~13 that already
exist; NOT Blueprints, NOT a ctx object). Pure code-motion: no route
added/removed/renamed/re-pathed/re-method-ed; no request/response shape,
validation, error-handling, sudo, or channel/keystore behavior change.

Privileged surface (doctrine auditor-grade bar):
  * Secrets / device links touched: the Telegram BotFather bot token is stored
    per-bot at ``~/.openclaw/skills/telegram.json`` (via /tmp staging + sudo
    /bin/cp per CLAUDE.md) and mirrored into ``channels.telegram`` in the bot's
    openclaw.json. WhatsApp/Signal pairing writes the linked account into
    ``channels.{whatsapp,signal}.accounts.<id>`` and persists the device
    credential under the bot's home (Baileys authDir / signal-cli configDir);
    no token is centralised.
  * Channel/openclaw.json writes + gateway kickstarts go through the
    ``telegram_install`` / ``whatsapp_install`` / ``signal_install`` modules'
    sudo-aware helpers and the shared ``_oc_install_common`` tear-down helpers.
  * The pairing-poll routes are ``POST`` (not ``GET``) on purpose (roadmap
    2.7): the ``paired``-state finalise has a load-bearing side effect (config
    write + kickstart), so it must route through the CSRF/Origin gate and not
    be a forgeable cross-origin GET. Preserved verbatim by the move.

§1.3 monkeypatch-at-call-time invariant (memo): handlers reach the patchable
server helper through ``_module._NAME`` (``_module = sys.modules[
"evolve_admin.web.server"]``) at call time, so test monkeypatches on
``server._NAME`` are honored. The only such helper this surface touches is
``server._audit_log_entry`` — NOT imported as a module-level name here (that
would shadow the patch).

The three ``_*_resolve_status`` shims are recreated verbatim here (they are
also called by the generic ``/api/skills/install/<skill_id>{,/status}``
dispatchers that stay in ``routes_admin.py``); recreating the thin wrapper —
rather than importing it — keeps the moved call sites byte-identical, the same
shape Increments 2a/2b used.

``network_path`` is accepted to match the house
``register_*_routes(app, network_path)`` signature (and the ``create_app`` call
site) but is unused by this cluster: every file path these handlers touch is
resolved inside the ``telegram_install`` / ``whatsapp_install`` /
``signal_install`` modules from the bot's home dir, not from network.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..telemetry import get_logger

_log = get_logger("web.routes_skills_pairing")


def register_skills_pairing_routes(app: Flask, network_path: Path) -> None:
    # Late-bound server-module handle (memo §1.3): handlers reach the patchable
    # ``_audit_log_entry`` via ``_module._NAME`` at call time so test
    # monkeypatches on ``server._NAME`` are respected. Derived inside the
    # function (mirroring ``routes_admin.register_admin_routes``) so importing
    # this module never requires ``server`` to be in ``sys.modules`` yet.
    _module = sys.modules["evolve_admin.web.server"]

    from ..skills import telegram_install as _telegram
    from ..skills import whatsapp_install as _whatsapp
    from ..skills import signal_install as _signal
    # Shared install/revoke mechanics — telegram revoke's symmetric tear-down
    # (closes deep-audit 2026-05-30 F2).
    from ..skills import _oc_install_common as _oc_common

    # ── Recreated _*_resolve_status shims ─────────────────────────────────────
    # Verbatim copies of the closures that ALSO stay in ``routes_admin.py`` for
    # the generic ``/api/skills/install/<skill_id>{,/status}`` dispatchers.
    # Recreating the thin wrapper (rather than importing it) keeps the moved
    # handler call sites byte-identical.
    def _telegram_resolve_status(bot_id: str) -> "_telegram.InstallStatus":
        """Resolve the Telegram install status for bot_id.

        Wires the real token read + getMe check as the default callables.
        resolve_status() handles defaults internally; we call without overrides.
        """
        return _telegram.resolve_status(bot_id)

    def _whatsapp_resolve_status(bot_id: str) -> "_whatsapp.InstallStatus":
        """Resolve the WhatsApp install status for bot_id.

        resolve_status() handles defaults internally (openclaw.json read,
        authDir probe, live OC probe). Tests pass stubs.
        """
        return _whatsapp.resolve_status(bot_id)

    def _signal_resolve_status(bot_id: str) -> "_signal.InstallStatus":
        """Resolve the Signal install status for bot_id.

        resolve_status() handles defaults internally (openclaw.json read,
        configDir probe, live OC probe). Tests pass stubs.
        """
        return _signal.resolve_status(bot_id)

    # ── Telegram skill install routes (V2.3-1) ───────────────────────────────
    # Telegram bots use a static BotFather token — no OAuth dance.
    # Per-bot tokens are stored at ~/.openclaw/skills/telegram.json via
    # /tmp staging + sudo /bin/cp per CLAUDE.md.
    #
    # Trust-chain notes:
    #   - Token is stored per-bot, never centralised.
    #   - Token is verified against Telegram's getMe API before storage.
    #   - No pod-level credentials needed (unlike Slack's OAuth app).

    @app.get("/api/skills/install/telegram/status")
    def api_skills_telegram_status() -> Response:
        """Return the bot's current Telegram install status.

        Query: ?bot_id=<bot>. Status values:
          missing | valid | revoked | invalid | unknown

        ``status == "valid"`` is the completion signal for the UI auto-poll.
        """
        bot_id = (request.args.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        status = _telegram_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/telegram")
    def api_skills_telegram_install_plan() -> Response:
        """Compute the Telegram install plan for the given bot.

        Body: {bot_id: str}. Returns:
            {ok, status: <InstallStatus dict>,
             steps: [<InstallStep dict>...],
             skill: {id, display_name, summary, access_panel}}

        The UI walks ``steps`` in order. The set_token step carries the
        plain-language access panel so the user sees it before submitting.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        status = _telegram_resolve_status(bot_id)
        steps = _telegram.build_install_plan(status)
        reg = _telegram.SKILL_REGISTRY_ENTRY
        _module._audit_log_entry("skill.install.plan", bot_id, {
            "skill_id": _telegram.TELEGRAM_SKILL_ID,
            "current_status": status.bot_token_state,
            "step_count": len(steps),
        })
        # V2.4-4: standardised plan_requested (dedicated Telegram plan endpoint)
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_telegram.TELEGRAM_SKILL_ID, bot_id, "plan_requested", {
                "skill_id": _telegram.TELEGRAM_SKILL_ID,
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

    @app.post("/api/skills/install/telegram/set-token")
    def api_skills_telegram_set_token() -> Response:
        """Accept the BotFather token, verify via getMe, store on success.

        Body: {bot_id: str, bot_token: str}

        Calls Telegram's ``getMe`` API to verify the token before storing it.
        Returns 400 for missing inputs or invalid token format; 422 if the
        token is rejected by Telegram; 200 + updated status on success.

        Token is stored at ``~/<bot_home>/.openclaw/skills/telegram.json``
        via /tmp staging + sudo /bin/cp per CLAUDE.md.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        bot_token = (body.get("bot_token") or "").strip()
        if not bot_token:
            return jsonify({"ok": False, "error": "bot_token required"}), 400

        # Basic length/character sanity check before hitting the API
        if len(bot_token) > 512:
            return jsonify({
                "ok": False,
                "error": "token_too_long",
                "detail": "A BotFather key cannot be longer than 512 characters.",
            }), 400

        # Verify with Telegram's getMe
        verify_result = _telegram.verify_bot_token(bot_token)
        if not verify_result.get("ok"):
            err = verify_result.get("error") or "unauthorized"
            _module._audit_log_entry("skill.telegram.set_token.invalid", bot_id, {
                "error": err,
                "http_status": verify_result.get("http_status", 0),
            })
            return jsonify({
                "ok": False,
                "error": "token_invalid",
                "detail": (
                    f"Telegram rejected the key: {err}. "
                    "Check that you copied the full token from @BotFather."
                ),
            }), 422

        import time as _time_mod
        config = {
            "bot_token": bot_token,
            "bot_username": verify_result.get("bot_username"),
            "bot_first_name": verify_result.get("bot_first_name"),
            "can_join_groups": verify_result.get("can_join_groups"),
            "can_read_all_group_messages": verify_result.get("can_read_all_group_messages"),
            "verified_at": _time_mod.time(),
        }

        wrote_ok, write_err = _telegram.write_token_config(bot_id, config)
        if not wrote_ok:
            _module._audit_log_entry("skill.telegram.set_token.error", bot_id, {
                "error": write_err,
            })
            return jsonify({
                "ok": False,
                "error": "config_write_failed",
                "detail": write_err or "unknown write error",
            }), 500

        # Wire the channel into openclaw.json. Without channels.telegram +
        # plugins.entries.telegram.enabled, the gateway never loads the
        # Telegram plugin and inbound messages are silently dropped — the
        # credential write alone is not enough.
        oc_ok, oc_err = _telegram.enable_channel_in_oc_config(bot_id, bot_token)
        if not oc_ok:
            _module._audit_log_entry("skill.telegram.set_token.oc_write_failed", bot_id, {
                "error": oc_err,
            })
            return jsonify({
                "ok": False,
                "error": "oc_config_write_failed",
                "detail": oc_err or "unknown openclaw.json write error",
            }), 500

        # Kickstart the gateway so it picks up the new channels + plugin entry.
        # Best-effort: surface the failure to the UI but keep the install
        # otherwise complete (token + openclaw.json are already on disk and
        # the operator can restart manually).
        kick_ok, kick_err = _telegram.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.telegram.set_token", bot_id, {
            "bot_username": verify_result.get("bot_username"),
            "bot_first_name": verify_result.get("bot_first_name"),
            "oc_config_applied": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
        })
        # V2.4-4: standardised activated + set_token events
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_telegram.TELEGRAM_SKILL_ID, bot_id, "set_token", {
                "bot_username": verify_result.get("bot_username"),
            })
            _alf(_telegram.TELEGRAM_SKILL_ID, bot_id, "activated", {
                "bot_username": verify_result.get("bot_username"),
            })
        except Exception:
            pass
        status = _telegram_resolve_status(bot_id)
        return jsonify({
            "ok": True,
            "gateway_kickstarted": kick_ok,
            "gateway_kickstart_error": None if kick_ok else kick_err,
            **status.to_dict(),
        })

    @app.post("/api/skills/install/telegram/revoke")
    def api_skills_telegram_revoke() -> Response:
        """Revoke the bot's Telegram token and clear local config.

        Body: {bot_id: str}. Returns {ok, cleared: bool, cleared_oc_config: bool,
        kickstarted: bool}.

        Symmetric revoke per deep-audit 2026-05-30 F2 (closes asymmetric-
        revoke bug):

        1. Delete the marker file at ``~/.openclaw/skills/telegram.json``
        2. Remove ``channels.telegram`` + ``plugins.entries.telegram`` from
           openclaw.json via shared :func:`_oc_install_common.disable_channel_in_oc_config`
        3. Kickstart the gateway so OC unloads the channel plugin (without
           this, the gateway keeps the channel mounted in memory until the
           next deploy)

        Telegram tokens can also be revoked at the source via @BotFather —
        ``/revoke`` in a chat with @BotFather and select your bot.

        Note: Telegram's API does not support remote revocation programmatically;
        the BotFather flow is the only way to invalidate an issued token.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        cleared = _telegram.delete_token_config(bot_id)
        # Symmetric revoke — clear channels.telegram + plugins.entries.telegram
        # then kickstart so OC drops the channel from its in-memory state.
        oc_ok, oc_err = _oc_common.disable_channel_in_oc_config(bot_id, "telegram")
        ks_ok, ks_err = (False, "skipped") if not oc_ok else _oc_common.kickstart_gateway(bot_id)

        _module._audit_log_entry("skill.telegram.revoke", bot_id, {
            "local_cleared": cleared,
            "cleared_oc_config": oc_ok, "oc_config_error": oc_err,
            "kickstarted": ks_ok, "kickstart_error": ks_err,
        })
        # V2.4-4: standardised revoked event
        try:
            from ..oauth import audit_log_provider_event as _alf
            _alf(_telegram.TELEGRAM_SKILL_ID, bot_id, "revoked", {
                "local_cleared": cleared,
                "cleared_oc_config": oc_ok,
                "kickstarted": ks_ok,
            })
        except Exception:
            pass
        # Treat the revoke as successful if EITHER the marker cleared or
        # the OC config did (idempotent: a second call with both already
        # gone returns ok=True). This way the UI doesn't surface false
        # failures when the operator clicks Uninstall twice.
        ok = cleared or oc_ok
        return jsonify({
            "ok": ok, "cleared": cleared,
            "cleared_oc_config": oc_ok,
            "kickstarted": ks_ok,
        })

    # ── WhatsApp skill routes (2026-06-04 Phase 1.2) ────────────────────────
    # Bundled-plugin install via OC's @openclaw/whatsapp. Two flow stages:
    # plugin install (one-time per bot) and QR pairing (per account).
    # Pairing is interactive — UI short-polls /pair/<sid> for QR refreshes
    # until the operator scans and the session reports ``paired``.

    @app.post("/api/skills/install/whatsapp/install-plugin")
    def api_skills_whatsapp_install_plugin() -> Response:
        """Run ``openclaw plugins install clawhub:@openclaw/whatsapp`` as
        the bot user. Idempotent.

        Body: ``{bot_id: str}``. Returns the new status snapshot.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        ok, err = _whatsapp.install_plugin(bot_id)
        _module._audit_log_entry("skill.whatsapp.install_plugin", bot_id, {
            "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "install_failed",
                "detail": err or "unknown install error",
            }), 500
        status = _whatsapp_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/whatsapp/pair/start")
    def api_skills_whatsapp_pair_start() -> Response:
        """Start a QR pairing session for ``bot_id``.

        Body: ``{bot_id: str}``. Returns ``{ok: True, session_id, state,
        qr_png_data_url?, expires_in_s}``. UI then short-polls
        ``GET /pair/<session_id>`` at ~2 s intervals to refresh the QR
        and detect when state transitions to ``paired`` / ``failed`` /
        ``expired``.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        snap = _whatsapp.start_pairing_session(bot_id)
        _module._audit_log_entry("skill.whatsapp.pair_start", bot_id, {
            "session_id": snap.get("session_id"),
            "initial_state": snap.get("state"),
        })
        return jsonify({"ok": True, **snap})

    @app.post("/api/skills/install/whatsapp/pair/<session_id>")
    def api_skills_whatsapp_pair_poll(session_id: str) -> Response:
        """Return the current snapshot of pairing session ``session_id``.

        On state == ``paired`` (or the operator's first poll after
        pairing), the route ALSO finalises: writes channels.whatsapp.
        accounts.<id> to openclaw.json and kickstarts the gateway. Until
        then it's a pure read.

        **POST, not GET** (roadmap 2.7): this poll has a load-bearing
        side effect (config write + gateway kickstart), so it must not be
        reachable as a forgeable cross-origin GET. POST routes it through
        the CSRF/Origin gate; the UI short-poller issues POST.
        """
        snap = _whatsapp.poll_pairing_session(session_id)
        if snap is None:
            return jsonify({"ok": False, "error": "session_not_found"}), 404

        # Auto-finalise when the worker reports ``paired`` and we haven't
        # already written the OC config. This is the load-bearing step that
        # makes the pairing actually visible to OC's runtime.
        if snap.get("state") == "paired":
            bot_id = snap.get("bot_id")
            if bot_id:
                status = _whatsapp.finalize_pairing(bot_id)
                _module._audit_log_entry("skill.whatsapp.pair_finalized", bot_id, {
                    "session_id": session_id,
                    "final_status": status.status,
                })
                return jsonify({
                    "ok": True,
                    **snap,
                    "install_status": status.to_dict(),
                })
        return jsonify({"ok": True, **snap})

    @app.post("/api/skills/install/whatsapp/pair/<session_id>/cancel")
    def api_skills_whatsapp_pair_cancel(session_id: str) -> Response:
        """Cancel an in-flight pairing session. Idempotent."""
        ok = _whatsapp.cancel_pairing_session(session_id)
        _module._audit_log_entry("skill.whatsapp.pair_cancel", None, {
            "session_id": session_id, "found": ok,
        })
        return jsonify({"ok": ok, "session_id": session_id})

    @app.post("/api/skills/install/whatsapp/revoke")
    def api_skills_whatsapp_revoke() -> Response:
        """Tear down a paired WhatsApp account.

        Body: ``{bot_id: str, account_id?: str}``. Logs out remotely
        (best-effort), clears channels.whatsapp.accounts.<id>, wipes the
        authDir, and kickstarts so OC unloads the plugin's account.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        account_id = (body.get("account_id") or _whatsapp.DEFAULT_ACCOUNT_ID).strip()

        ok, err = _whatsapp.revoke_account(bot_id, account_id=account_id)
        _module._audit_log_entry("skill.whatsapp.revoke", bot_id, {
            "account_id": account_id, "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "revoke_failed",
                "detail": err or "unknown revoke error",
            }), 500
        return jsonify({"ok": True, "cleared": True})

    # ── Signal skill routes (2026-06-04 Phase 1.3) ──────────────────────────
    # **LICENSING REVIEW REQUIRED BEFORE MERGE** — see signal_install module
    # docstring for the signal-cli / libsignal copyleft posture. The routes
    # are safe to land alongside the install module (they just dispatch);
    # gating happens at the catalog list level (which we ALSO add in this
    # PR — that's the surface end users discover the skill through). If the
    # review returns FAIL, remove the catalog entry and these route
    # registrations stay inert (no catalog → no UI → no calls).
    #
    # Flow: install-plugin → set-number (E.164) → pair/start (QR session)
    # → pair/<sid> (auto-finalises on `paired`) → revoke (symmetric).

    @app.post("/api/skills/install/signal/install-plugin")
    def api_skills_signal_install_plugin() -> Response:
        """Run ``openclaw plugins install clawhub:@openclaw/signal`` as
        the bot user. Idempotent. OC's installer transitively downloads
        signal-cli into the bot's home dir; first run can take ~minutes.

        Body: ``{bot_id: str}``. Returns the new status snapshot.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        ok, err = _signal.install_plugin(bot_id)
        _module._audit_log_entry("skill.signal.install_plugin", bot_id, {
            "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "install_failed",
                "detail": err or "unknown install error",
            }), 500
        status = _signal_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/signal/set-number")
    def api_skills_signal_set_number() -> Response:
        """Capture the E.164 phone number this bot will use on Signal.

        Body: ``{bot_id: str, number: str}``. Validates E.164 format
        before writing the placeholder ``channels.signal.accounts.<number>``
        block (enabled: false until pair/start completes).
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        number = (body.get("number") or "").strip()
        if not number:
            return jsonify({"ok": False, "error": "number required"}), 400
        if not _signal.is_valid_e164(number):
            return jsonify({
                "ok": False,
                "error": "number_invalid_e164",
                "detail": (
                    "The phone number must be in international format "
                    "(starting with + and country code, like +15551234567)."
                ),
            }), 400

        ok, err = _signal.capture_number(bot_id, number)
        _module._audit_log_entry("skill.signal.set_number", bot_id, {
            "number_digits": len(number) - 1,  # log length only, not the number itself
            "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "set_number_failed",
                "detail": err or "unknown error",
            }), 500
        status = _signal_resolve_status(bot_id)
        return jsonify({"ok": True, **status.to_dict()})

    @app.post("/api/skills/install/signal/pair/start")
    def api_skills_signal_pair_start() -> Response:
        """Start a QR pairing session for ``bot_id``.

        Body: ``{bot_id: str}``. Returns ``{ok: True, session_id, state,
        qr_png_data_url?, expires_in_s}``. UI then short-polls
        ``GET /pair/<session_id>`` at ~2 s intervals.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        snap = _signal.start_pairing_session(bot_id)
        _module._audit_log_entry("skill.signal.pair_start", bot_id, {
            "session_id": snap.get("session_id"),
            "initial_state": snap.get("state"),
        })
        return jsonify({"ok": True, **snap})

    @app.post("/api/skills/install/signal/pair/<session_id>")
    def api_skills_signal_pair_poll(session_id: str) -> Response:
        """Return the current snapshot of pairing session ``session_id``.

        On state == ``paired``, ALSO finalises: writes channels.signal.
        accounts.<number> with enabled:true to openclaw.json and
        kickstarts the gateway. Until then it's a pure read.

        **POST, not GET** (roadmap 2.7): the finalise side effect (config
        write + gateway kickstart) must not be reachable as a forgeable
        cross-origin GET. POST routes it through the CSRF/Origin gate.
        """
        snap = _signal.poll_pairing_session(session_id)
        if snap is None:
            return jsonify({"ok": False, "error": "session_not_found"}), 404

        if snap.get("state") == "paired":
            bot_id = snap.get("bot_id")
            if bot_id:
                status = _signal.finalize_pairing(bot_id)
                _module._audit_log_entry("skill.signal.pair_finalized", bot_id, {
                    "session_id": session_id,
                    "final_status": status.status,
                })
                return jsonify({
                    "ok": True,
                    **snap,
                    "install_status": status.to_dict(),
                })
        return jsonify({"ok": True, **snap})

    @app.post("/api/skills/install/signal/pair/<session_id>/cancel")
    def api_skills_signal_pair_cancel(session_id: str) -> Response:
        """Cancel an in-flight pairing session. Idempotent."""
        ok = _signal.cancel_pairing_session(session_id)
        _module._audit_log_entry("skill.signal.pair_cancel", None, {
            "session_id": session_id, "found": ok,
        })
        return jsonify({"ok": ok, "session_id": session_id})

    @app.post("/api/skills/install/signal/revoke")
    def api_skills_signal_revoke() -> Response:
        """Tear down a paired Signal account.

        Body: ``{bot_id: str, number: str}``. Logs out remotely (best-
        effort), clears channels.signal.accounts.<number>, wipes the
        configDir, and kickstarts so OC unloads the plugin's account.
        """
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400
        number = (body.get("number") or "").strip()
        if not number or not _signal.is_valid_e164(number):
            return jsonify({"ok": False, "error": "number required (E.164)"}), 400

        ok, err = _signal.revoke_account(bot_id, number=number)
        _module._audit_log_entry("skill.signal.revoke", bot_id, {
            "ok": ok, "error": err,
        })
        if not ok:
            return jsonify({
                "ok": False,
                "error": "revoke_failed",
                "detail": err or "unknown revoke error",
            }), 500
        return jsonify({"ok": True, "cleared": True})


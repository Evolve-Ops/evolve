"""evo_routes.py — Flask API routes for the ``evo`` keyword surface.

Spec: docs/spec-evo-wizard-2026-05-05.md (dispatch + identity + guide +
wizard) and docs/spec-primary-bot-interface-2026-05-14.md §4 (help
retrieval) + §6 (intake).

Routes registered:
  POST /api/evo/dispatch                  → resolve role + parse subcommand + run handler
  GET  /api/evo/help                      → render the subcommand registry filtered by role
  GET  /api/evo/identity/<bot_id>         → read primary_user block for a bot
  POST /api/evo/identity/<bot_id>/claim   → record primary's external ID on a channel
  POST /api/evo/identity/<bot_id>/reassign → transfer primary on a channel (with --from check)
  GET  /api/evo/identity/audit            → read the identity-change audit log
  GET  /api/evo/guide/<bot_id>            → read the bot guide (404 if absent)
  PUT  /api/evo/guide/<bot_id>            → write/replace the bot guide
  POST /api/evo/wizard/turn               → process a mid-wizard user message
  GET  /api/evo/wizard/state              → read wizard state for (bot_id, user_key)
  GET  /api/evo/wizard/active             → find active wizard for (bot_id, channel, sender)
  POST /api/evo/intake                    → capture a new bug/feature/question intake
  GET  /api/evo/intake                    → list intakes (optional state/kind filter)
  GET  /api/evo/intake/<id>               → read one intake by id
  POST /api/evo/intake/<id>/promote       → file the intake as a GitHub issue
  POST /api/evo/intake/<id>/dismiss       → move the intake to closed
  POST /api/evo/help/search               → BM25 search over the help index
  POST /api/evo/help/read                 → read a single help doc by id
  GET  /api/credentials/github/status     → three-purposes summary for the
                                            Credentials surface
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from ..config import load_network, save_network
from ..evo import audit as _audit
from ..evo import dispatch as _dispatch
from ..evo import guide as _guide
from ..evo import identity as _identity
from ..evo import subcommands as _subcommands
from ..evo.wizard import engine as _wizard_engine
from ..evo.wizard import state as _wizard_state
from .. import feature_toggle as _feature_toggle
from ..intake import auto_responder as _intake_auto
from ..intake import classifier as _intake_classifier
from ..intake import envelope as _intake_env
from ..intake import policy as _intake_policy
from ..intake import promote as _intake_promote
from ..intake import store as _intake_store
from .. import help_search as _help_search
from ..help_index import build as _help_index_build
from . import peer_auth as _peer_auth


def _shared_dir_from_network(network: dict[str, Any]) -> Path:
    return Path(network.get("sharedDir", "/Users/Shared/evolve"))


# Total wall-clock ceiling for the fan-out of GitHub /user login
# resolutions in api_credentials_github_status. urllib's
# intake.permissions.DEFAULT_TIMEOUT_S (10s) is a *per-socket-op* timeout,
# not a ceiling on the whole call, so a stalled-but-alive connection can
# blow past it — and N tokens resolved serially meant up to N×10s. We fan
# the resolutions out concurrently and wait on them with this single
# overall deadline; any login still outstanding at the deadline is
# reported as unresolved (login=None) rather than blocking the response.
# 6s sits comfortably below the 10s per-op timeout (so one stalled socket
# can't pin the handler) yet leaves ample room for a batch of healthy
# sub-second GitHub calls to finish. Module-level so tests can patch it
# to a small value. See PR "[META:skills] Non-blocking github/status".
_GITHUB_STATUS_RESOLVE_DEADLINE_S = 6.0


def register_evo_routes(app: Flask, network_path: Path) -> None:
    """Register all /api/evo/* routes on the Flask app.

    Loopback-only, unauthenticated — same posture as /api/better/*.
    """

    @app.post("/api/evo/dispatch")
    def api_evo_dispatch() -> "Response | tuple[Response, int]":
        """Dispatch an evo command on behalf of the plugin.

        Body:
          bot_id              required — the bot the user is talking to
          channel             "telegram" | "slack" | "discord" | other
          sender_external_id  the user's ID in that channel (str)
          raw_text            the original message text

        Returns the :class:`DispatchResult` envelope as JSON.
        """
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id") or ""
        channel = body.get("channel") or None
        sender_external_id = body.get("sender_external_id")
        raw_text = body.get("raw_text") or ""

        if not bot_id:
            return jsonify({"error": "bot_id is required"}), 400
        if not raw_text:
            return jsonify({"error": "raw_text is required"}), 400

        # A member-bot socket peer may dispatch ONLY as itself. The body
        # ``bot_id`` is authoritative here — it selects the role and the claim
        # handler below can mutate network.json (identity claims) for that bot —
        # so bind it to the kernel peer-uid identity. Trusted (evo) and
        # device-authed admin-UI callers are unconstrained (returns None); evo's
        # central dispatcher keeps fanning out to any bot.
        _own = _peer_auth.member_peer_bot_id(network_path)
        if _own is not None and bot_id != _own:
            return jsonify({"error": "bot_id does not match caller identity"}), 403

        # sender_external_id is optional; identity resolver tolerates None
        if sender_external_id is not None:
            sender_external_id = str(sender_external_id)

        network = load_network(network_path)
        result = _dispatch.dispatch(
            network,
            bot_id=bot_id,
            channel=channel,
            sender_external_id=sender_external_id,
            raw_text=raw_text,
        )

        # The claim handler mutates ``network`` in-place; persist if so.
        if result.network_dirty:
            try:
                save_network(network, network_path)
            except (PermissionError, OSError, RuntimeError) as e:
                return jsonify({
                    "error": f"failed to persist network.json: {e}",
                }), 500

        return jsonify(result.to_dict())

    @app.get("/api/evo/help")
    def api_evo_help() -> Response:
        """Return the subcommand registry filtered by role.

        Query params:
          bot_id              optional; if provided with sender_external_id +
                              channel, role is resolved against network.json
          channel             optional
          sender_external_id  optional
          role                optional override ("primary"|"secondary") for
                              admin-UI use; ignored if other params resolve

        If no identity is supplied, defaults to ``"primary"`` (most generous;
        same fallback as the dispatcher).
        """
        bot_id = request.args.get("bot_id") or ""
        channel = request.args.get("channel") or None
        sender_external_id = request.args.get("sender_external_id") or None
        role_override = request.args.get("role")

        if bot_id:
            network = load_network(network_path)
            role = _identity.resolve_role(
                network, bot_id, channel, sender_external_id
            )
        elif role_override in ("primary", "secondary"):
            role = role_override
        else:
            role = "primary"

        visible = _subcommands.subcommands_for_role(role)
        return jsonify({
            "role": role,
            "subcommands": [
                {
                    "name": sc.name,
                    "short_help": sc.short_help,
                    "long_help": sc.long_help,
                    "available_to": sorted(sc.available_to),
                }
                for sc in visible
            ],
        })

    # ── Identity (primary_user block) ─────────────────────────────────────────

    @app.get("/api/evo/identity/<bot_id>")
    def api_evo_identity_get(bot_id: str) -> Response:
        """Return the recorded primary_user block for a bot, or an empty
        block if nothing has been claimed yet."""
        network = load_network(network_path)
        bots = network.get("bots") or {}
        bot_cfg = bots.get(bot_id) or {}
        primary_block = bot_cfg.get("primary_user") or {}
        external_ids = primary_block.get("external_ids") or {}
        pod_user = primary_block.get("pod_user") or None
        return jsonify({
            "bot_id": bot_id,
            "primary_user": {
                "external_ids": external_ids if isinstance(external_ids, dict) else {},
                "pod_user": pod_user if isinstance(pod_user, str) else None,
            },
            "is_member": bot_id in (network.get("members") or []),
        })

    @app.post("/api/evo/identity/<bot_id>/claim")
    def api_evo_identity_claim(bot_id: str) -> Response:
        """Record the primary user's external ID on a channel.

        Body:
          channel       required — "telegram" | "slack" | "discord" | …
          external_id   required — the user's stable ID on that channel
          pod_user      optional — pod-level identifier (free-form for v1)
          force         optional — overwrite an existing different value

        Returns 200 with the updated primary_user block on success, 400 on
        validation error, 409 on conflict without force, 500 on persistence
        error.
        """
        body = request.get_json(silent=True) or {}
        channel = body.get("channel") or ""
        external_id = body.get("external_id") or ""
        pod_user = body.get("pod_user") or None
        force = bool(body.get("force"))
        reason = body.get("reason") or None

        network = load_network(network_path)

        # Capture the prior value so the audit record reflects what was
        # actually there before mutation (None on first claim).
        prior = (
            ((network.get("bots") or {}).get(bot_id) or {})
            .get("primary_user", {})
            .get("external_ids", {})
            .get(str(channel).strip().lower())
        )

        try:
            primary_block = _identity.claim_primary(
                network,
                bot_id,
                channel=str(channel),
                external_id=str(external_id),
                pod_user=str(pod_user) if pod_user else None,
                force=force,
            )
        except _identity.ClaimError as e:
            msg = str(e)
            status = 409 if "already recorded" in msg else 400
            return jsonify({"error": msg}), status

        try:
            save_network(network, network_path)
        except (PermissionError, OSError, RuntimeError) as e:
            return jsonify({
                "error": f"failed to persist network.json: {e}",
            }), 500

        # Audit — only when something actually changed (idempotent re-claim
        # of the same value is a no-op and shouldn't generate noise).
        new_value = str(external_id).strip()
        if str(prior or "") != new_value:
            try:
                _audit.append_event(
                    _shared_dir_from_network(network),
                    actor=_audit.api_actor(),
                    action="claim",
                    bot_id=bot_id,
                    channel=str(channel).strip().lower(),
                    from_external_id=(str(prior) if prior is not None else None),
                    to_external_id=new_value,
                    force=force,
                    reason=str(reason) if reason else None,
                )
            except (PermissionError, OSError) as e:
                return jsonify({
                    "error": (
                        f"network.json updated but audit write failed: {e} — "
                        "investigate before further claims"
                    ),
                }), 500

        return jsonify({
            "bot_id": bot_id,
            "primary_user": primary_block,
            "ok": True,
        })

    @app.post("/api/evo/identity/<bot_id>/reassign")
    def api_evo_identity_reassign(bot_id: str) -> Response:
        """Transfer the primary on a channel from one external ID to
        another. Requires ``from`` to match the current recorded value, so
        an operator can't reassign without first knowing who the current
        primary is — this is the whole point of the verb existing alongside
        ``claim``.

        Body:
          channel  required
          from     required — the current external_id (verified)
          to       required — the new external_id
          pod_user optional
          reason   optional — recorded in the audit log

        Returns 200 with the updated primary_user block on success, 400 on
        validation error or mismatched ``from``, 500 on persistence error.
        """
        body = request.get_json(silent=True) or {}
        channel = body.get("channel") or ""
        from_id = body.get("from") or ""
        to_id = body.get("to") or ""
        pod_user = body.get("pod_user") or None
        reason = body.get("reason") or None

        network = load_network(network_path)
        try:
            primary_block = _identity.reassign_primary(
                network,
                bot_id,
                channel=str(channel),
                from_external_id=str(from_id),
                to_external_id=str(to_id),
                pod_user=str(pod_user) if pod_user else None,
            )
        except _identity.ClaimError as e:
            return jsonify({"error": str(e)}), 400

        try:
            save_network(network, network_path)
        except (PermissionError, OSError, RuntimeError) as e:
            return jsonify({
                "error": f"failed to persist network.json: {e}",
            }), 500

        if str(from_id).strip() != str(to_id).strip():
            try:
                _audit.append_event(
                    _shared_dir_from_network(network),
                    actor=_audit.api_actor(),
                    action="reassign",
                    bot_id=bot_id,
                    channel=str(channel).strip().lower(),
                    from_external_id=str(from_id).strip(),
                    to_external_id=str(to_id).strip(),
                    force=False,
                    reason=str(reason) if reason else None,
                )
            except (PermissionError, OSError) as e:
                return jsonify({
                    "error": (
                        f"network.json updated but audit write failed: {e} — "
                        "investigate before further reassigns"
                    ),
                }), 500

        return jsonify({
            "bot_id": bot_id,
            "primary_user": primary_block,
            "ok": True,
        })

    # ── Bot guide ─────────────────────────────────────────────────────────────

    @app.get("/api/evo/guide/<bot_id>")
    def api_evo_guide_get(bot_id: str) -> Response:
        """Read the bot guide for ``bot_id``. Returns 404 if no guide is
        recorded yet; 422 if a file is present but malformed (so the admin
        UI can show a clear error rather than silently treating corruption
        as 'no guide')."""
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        try:
            g = _guide.read_guide(shared_dir, bot_id)
        except _guide.GuideError as e:
            return jsonify({"error": f"guide is malformed: {e}"}), 422
        if g is None:
            return jsonify({
                "bot_id": bot_id,
                "exists": False,
            }), 404
        return jsonify({
            "bot_id": bot_id,
            "exists": True,
            "frontmatter": g.frontmatter,
            "body": g.body,
        })

    @app.put("/api/evo/guide/<bot_id>")
    def api_evo_guide_put(bot_id: str) -> Response:
        """Write (or replace) the bot guide for ``bot_id``.

        Body:
          frontmatter   optional dict of structured fields (audience, tone,
                        do_say, dont_say, …). Unknown keys are preserved.
          body          markdown body string
          authored_by   optional — recorded the first time and on explicit
                        override; preserved across writes otherwise

        ``last_edited_at`` is auto-stamped server-side. ``authored_at`` is
        stamped on first write and preserved thereafter.
        """
        body_data = request.get_json(silent=True) or {}
        frontmatter = body_data.get("frontmatter") or {}
        body_text = body_data.get("body") or ""
        authored_by = body_data.get("authored_by") or None

        if not isinstance(frontmatter, dict):
            return jsonify({"error": "frontmatter must be a JSON object"}), 400
        if not isinstance(body_text, str):
            return jsonify({"error": "body must be a string"}), 400

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        try:
            stored = _guide.write_guide(
                shared_dir,
                bot_id,
                frontmatter=dict(frontmatter),
                body=body_text,
                authored_by=str(authored_by) if authored_by else None,
            )
        except _guide.GuideError as e:
            return jsonify({"error": str(e)}), 400
        except (PermissionError, OSError) as e:
            return jsonify({"error": f"failed to write guide: {e}"}), 500

        return jsonify({
            "bot_id": bot_id,
            "exists": True,
            "frontmatter": stored.frontmatter,
            "body": stored.body,
            "ok": True,
        })

    # ── Wizard ────────────────────────────────────────────────────────────────

    @app.post("/api/evo/wizard/turn")
    def api_evo_wizard_turn() -> "Response | tuple[Response, int]":
        """Process one mid-wizard user message.

        Plugin posts the raw user text plus the wizard session identifier
        it received from a prior ``/api/evo/dispatch`` (currently the
        ``user_key`` itself); we run extraction, advance the phase if its
        exit condition is met, persist state, and return the next
        systemAppend.

        Body:
          bot_id          required
          wizard_session_id  required — the ``user_key`` we returned from dispatch
          user_message    required — the user's text this turn

        Response:
          system_append      string the plugin should inject as systemAppend
          direct_send_message  body for direct-send via channel transport
                             (Telegram Bot API). Set on verbatim phases;
                             null on agenda phases. Plugin direct-sends
                             when set + transport available; falls back
                             to system_append (LLM-echo) otherwise.
          wizard_session_id  same key when more turns expected; null when wrap-up
                             happened on this turn (plugin clears its session)
          phase              current phase name (post-advancement)
          completed          true on the wrap turn, false otherwise
        """
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id") or ""
        wsid = body.get("wizard_session_id") or ""
        user_message = body.get("user_message") or ""
        if not bot_id or not wsid:
            return jsonify({"error": "bot_id and wizard_session_id are required"}), 400
        if not isinstance(user_message, str):
            return jsonify({"error": "user_message must be a string"}), 400

        # Member-bot peers advance ONLY their own bot's wizard — the wizard state
        # is keyed by (bot_id, user_key) and a challenge phase can apply
        # admin/primary claims that mutate network.json, so bind ``bot_id`` to
        # the peer uid (see api_evo_dispatch).
        _own = _peer_auth.member_peer_bot_id(network_path)
        if _own is not None and bot_id != _own:
            return jsonify({"error": "bot_id does not match caller identity"}), 403

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        result = _wizard_engine.process_turn(
            shared_dir,
            bot_id=bot_id,
            user_key=wsid,
            user_message=user_message,
            network=network,
        )
        if result is None:
            # State missing / completed / corrupted — plugin should clear
            # its in-memory session reference and resume normal routing.
            return jsonify({
                "error": "no active wizard for that session",
                "wizard_session_id": None,
            }), 404

        # Challenge phase may have applied admin/primary claims that
        # mutated ``network`` in-place; persist before returning so the
        # next request sees the new role.
        if result.network_dirty:
            try:
                save_network(network, network_path)
            except (PermissionError, OSError, RuntimeError) as e:
                return jsonify({
                    "error": f"failed to persist network.json: {e}",
                }), 500

        # Derive a short subcommand-brief at the wire boundary so the
        # plugin can tell the LLM what wizard the user is in (eliminates
        # speculation work). Sourced from the wizard state's audience
        # field; static map below covers all known audiences. Unknown
        # audience → None, plugin falls back to its generic brief.
        audience_briefs = {
            "primary": "the primary onboarding wizard (collects user profile + goals + initial app recommendations)",
            "secondary": "the secondary-user onboarding tour (introduces a non-primary user to a bot)",
            "guide_drafter": "the team-guide drafting wizard (authors / edits the bot's primary-supplied team guide)",
            "approver": "the conversational-approval flow for a Better Engine recommendation",
            "app_creator": "`evo app create` — drafts an app spec from your description, then iterates conversationally",
            "google_setup": "`evo setup-google` — per-bot Google OAuth client setup wizard (multi-turn, ~4 steps)",
            "add_bot": "`evo add-bot` — plans and builds a new bot conversationally (need → audience → purpose → name → plan → credential → build → final check)",
        }
        wizard_state_obj = _wizard_state.read_state(shared_dir, bot_id, wsid)
        subcommand_brief = None
        if wizard_state_obj is not None:
            subcommand_brief = audience_briefs.get(wizard_state_obj.audience)

        return jsonify({
            "system_append": result.system_append,
            "direct_send_message": result.direct_send_message,
            "wizard_session_id": result.wizard_session_id,
            "phase": result.phase,
            "completed": result.completed,
            "subcommand_brief": subcommand_brief,
        })

    @app.get("/api/evo/wizard/state")
    def api_evo_wizard_state() -> Response:
        """Read the wizard state for ``(bot_id, user_key)`` — used for
        debugging / future admin UI surface. Returns 404 if no state."""
        bot_id = request.args.get("bot_id") or ""
        user_key = request.args.get("user_key") or ""
        if not bot_id or not user_key:
            return jsonify({"error": "bot_id and user_key are required"}), 400
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        st = _wizard_state.read_state(shared_dir, bot_id, user_key)
        if st is None:
            return jsonify({"error": "no wizard state recorded"}), 404
        return jsonify(st.to_dict())

    @app.get("/api/evo/wizard/active")
    def api_evo_wizard_active() -> "Response | tuple[Response, int]":
        """Find the active wizard's ``wizard_session_id`` for the caller
        identified by ``(bot_id, channel, sender_external_id)``.

        Used by the plugin on every user turn to recover from in-memory
        state loss — when the gateway restarts (deploy redeploy, OOM
        kill, plugin reload) the plugin's BetterSessionState map is
        cleared, and the previously-set wizardSessionId for an
        in-flight wizard is gone. Subsequent user replies would hit the
        LLM raw because the plugin doesn't know the wizard is active.

        This endpoint lets the plugin poll cheaply: if there's an active
        wizard for this caller, return its session_id; otherwise return
        ``null``. The plugin re-hydrates ``state.wizardSessionId`` and
        proceeds with the existing wizard-turn handling.

        Response:
          wizard_session_id  string when an active wizard exists for
                             this caller; null otherwise
          phase              current phase name (only when active)

        Always returns 200 — the "no wizard" case is communicated via
        ``wizard_session_id: null`` so the plugin doesn't have to
        distinguish 404 from a transport error.
        """
        bot_id = request.args.get("bot_id") or ""
        channel = request.args.get("channel") or None
        sender = request.args.get("sender_external_id") or None
        if not bot_id:
            return jsonify({"error": "bot_id is required"}), 400
        # Member-bot peers probe ONLY their own bot's active wizard (this can
        # also mark a stale rec_pending session completed → a write), so bind
        # ``bot_id`` to the peer uid (see api_evo_dispatch).
        _own = _peer_auth.member_peer_bot_id(network_path)
        if _own is not None and bot_id != _own:
            return jsonify({"error": "bot_id does not match caller identity"}), 403
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        # The wizard session id IS the user_key — the engine uses
        # user_key as the state file name. derive_user_key handles
        # the role-based pod:/ext:/anon: namespacing identically to
        # how every other wizard entrypoint resolves the caller.
        user_key = _identity.derive_user_key(
            network, bot_id=bot_id, channel=channel, sender_external_id=sender,
        )
        st = _wizard_state.read_state(shared_dir, bot_id, user_key)
        if st is None or not st.is_active():
            return jsonify({"wizard_session_id": None})
        # Don't re-attach to expired rec_pending sessions — otherwise a
        # stale pitch from hours/days ago would hijack the user's next
        # unrelated message into a clarify loop. The expiry inside
        # process_turn would catch it on the next turn, but at the cost
        # of an extra round-trip and a confusing user-facing "all caught
        # up" finalize on a message that had nothing to do with evo.
        if (
            st.audience == "approver"
            and _wizard_engine._rec_pending_expired(shared_dir, st, bot_id)
        ):
            # Mark the state non-active so future probes / process_turn
            # calls also short-circuit. Don't surface the rec yet — the
            # user can re-invoke `evo` to see it again.
            st.status = "completed"
            _wizard_state.write_state(shared_dir, st)
            return jsonify({"wizard_session_id": None})
        return jsonify({
            "wizard_session_id": user_key,
            "phase": st.current_phase,
        })

    @app.get("/api/evo/identity/audit")
    def api_evo_identity_audit() -> Response:
        """Return all recorded identity-change events. Optionally filter by
        ``?bot_id=`` and/or ``?channel=``."""
        network = load_network(network_path)
        events = _audit.read_events(_shared_dir_from_network(network))
        bot_filter = request.args.get("bot_id")
        chan_filter = request.args.get("channel")
        if bot_filter:
            events = [e for e in events if e.get("bot_id") == bot_filter]
        if chan_filter:
            events = [e for e in events if e.get("channel") == chan_filter.lower()]
        return jsonify({"events": events})

    # ── Intake (bug / feature / question capture + promotion) ────────────────

    @app.post("/api/evo/intake")
    def api_evo_intake_create() -> Response:
        """Capture a new intake.

        Body:
          kind                  required — bug | feature | question
          body                  required — free-text description
          submitter_user_key    optional — caller's identity (ext:telegram:…)
          submitter_role        optional — admin | primary | secondary
          submitter_channel     optional — channel id (e.g. "telegram:12345")
          context               optional dict with primary_bot, active_bot,
                                git_commit, evolve_version, recent_turns_excerpt,
                                active_signals

        Returns the created Intake envelope.
        """
        body = request.get_json(silent=True) or {}
        kind = (body.get("kind") or "").strip()
        text = body.get("body")
        if kind not in ("bug", "feature", "question"):
            return jsonify({"error": "kind must be bug|feature|question"}), 400
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "body is required"}), 400

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)

        intake = _intake_env.Intake(
            id=_intake_store.new_intake_id(),
            kind=kind,  # type: ignore[arg-type]
            body=text.strip(),
            submitter_user_key=body.get("submitter_user_key"),
            submitter_role=body.get("submitter_role"),
            submitter_channel=body.get("submitter_channel"),
            context=_intake_env.IntakeContext.from_dict(body.get("context")),
        )
        try:
            _intake_store.write_intake(intake, shared_dir)
        except (PermissionError, OSError) as e:
            return jsonify({"error": f"failed to persist intake: {e}"}), 500
        return jsonify({"intake": intake.to_dict()}), 201

    @app.get("/api/evo/intake")
    def api_evo_intake_list() -> Response:
        """List intakes, optionally filtered by ``?state=`` and/or ``?kind=``.

        Default: every state, every kind, sorted oldest-first by id.
        Caps the response at 200 to keep payloads bounded.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        state = request.args.get("state") or None
        kind = request.args.get("kind") or None
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))

        items = []
        for ix in _intake_store.iter_intakes(
            shared_dir,
            state=state,  # type: ignore[arg-type]
            kind=kind,
        ):
            items.append(ix.to_dict())
            if len(items) >= limit:
                break
        return jsonify({"intakes": items, "count": len(items)})

    @app.get("/api/evo/intake/<intake_id>")
    def api_evo_intake_get(intake_id: str) -> Response:
        """Read one intake by id."""
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located
        return jsonify({"intake": intake.to_dict()})

    @app.post("/api/evo/intake/<intake_id>/promote")
    def api_evo_intake_promote(intake_id: str) -> Response:
        """File ``intake_id`` as a GitHub issue.

        Body:
          include_transcript   optional bool (default False)
          promoted_by          optional user_key for audit
          target               optional named target (e.g. "evolve",
                               "openclaw"); defaults to the configured
                               default target

        Returns 200 with the updated intake on success, 404 if missing,
        409 if already filed, 412 if intake.github is not configured or
        the named target is unknown, 502 if GitHub rejects the request.
        """
        body = request.get_json(silent=True) or {}
        include_transcript = bool(body.get("include_transcript"))
        promoted_by = body.get("promoted_by") or None
        target_name = body.get("target") or None

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located

        cfg = _intake_promote.PromotionConfig.from_network(network)
        if cfg is None:
            return jsonify({
                "error": "intake.github not configured — run `evolve-admin intake configure`",
            }), 412
        try:
            target = cfg.resolve(target_name)
        except _intake_promote.PromotionError as e:
            return jsonify({"error": str(e)}), 412

        from ..keystore import KeystoreManager
        token = KeystoreManager(shared_dir).get_value(target.token_slot)
        if not token:
            return jsonify({
                "error": (
                    f"no token in keystore slot '{target.token_slot}' — "
                    f"run `evolve-admin keys set {target.token_slot}`"
                ),
            }), 412

        try:
            updated = _intake_promote.promote(
                intake,
                network=network,
                shared_dir=shared_dir,
                token=token,
                include_transcript=include_transcript,
                promoted_by=str(promoted_by) if promoted_by else None,
                target_name=target.name,
            )
        except _intake_promote.PromotionError as e:
            msg = str(e)
            status = 409 if "already filed" in msg else 502
            return jsonify({"error": msg}), status

        return jsonify({"intake": updated.to_dict(), "ok": True})

    @app.post("/api/evo/intake/<intake_id>/dismiss")
    def api_evo_intake_dismiss(intake_id: str) -> Response:
        """Move ``intake_id`` to ``closed`` without filing.

        Body:
          actor   optional — user_key for the audit log
          note    optional — short reason recorded with the transition
        """
        body = request.get_json(silent=True) or {}
        actor = body.get("actor") or None
        note = body.get("note") or None

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located
        try:
            updated = _intake_store.transition(
                intake,
                to="closed",
                shared_dir=shared_dir,
                actor=str(actor) if actor else None,
                note=str(note) if note else None,
            )
        except _intake_store.IllegalTransitionError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"intake": updated.to_dict(), "ok": True})

    @app.post("/api/evo/intake/<intake_id>/triage")
    def api_evo_intake_triage(intake_id: str) -> Response:
        """Run the LLM triage classifier against ``intake_id`` and write
        the resulting :class:`TriageRecord` onto the intake.

        Same classifier the inbound watcher uses, but invoked on-demand
        against ANY intake regardless of origin or state. Useful for:

          - Issues the operator filed via the errors page or `evo intake`
            — the inbound watcher skips these (it's gated to inbound only),
            so a "Triage now" affordance is the only way to get a verdict
            on them.
          - Re-triage when the operator wants a fresh verdict (e.g. after
            scope/CONTRIBUTING.md changes that would shift the recommendation).

        Body: empty. The classifier extracts title / body / repo / author
        from the intake itself. Returns the updated intake (including the
        new ``triage`` field) or an error.

        Errors:
          404 — intake_id not found in any state subdir
          500 — write failed
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located

        # Derive title + body from intake.body. The inbound watcher
        # stores body as ``f"{title}\n\n{body_raw}"`` so for inbound
        # intakes the first line is the title. For operator-filed
        # intakes (kind="bug"/"feature"/"question") the body field is
        # the whole report — fall back to a synthesized title from the
        # first line in that case too. Either way, give the classifier
        # something to anchor on.
        body_full = intake.body or ""
        if "\n" in body_full:
            title, body_for_triage = body_full.split("\n", 1)
            title = title.strip() or "(no title)"
            body_for_triage = body_for_triage.lstrip()
        else:
            title = body_full[:200].strip() or "(no title)"
            body_for_triage = ""

        # Derive repo + author from the intake's promotion metadata if
        # it was promoted, else degrade gracefully. The classifier uses
        # repo for duplicate-detection prompts and author for "first-
        # time contributor" hints; both are advisory.
        prom = intake.promotion
        repo = ""
        if prom.github_issue_url:
            import re as _re
            m = _re.match(
                r"https?://github\.com/([^/]+/[^/]+)/issues/\d+",
                prom.github_issue_url,
            )
            if m:
                repo = m.group(1)
        author = prom.promoted_by or "self"

        # Classifier never raises (per docstring). On total failure we
        # still get back a confidence=0 unknown verdict, which is
        # arguably more useful than an HTTP 500 — surface it so the
        # operator sees "tried and failed" rather than "didn't try."
        verdict = _intake_classifier.triage_inbound(
            title=title,
            body=body_for_triage,
            repo=repo,
            author=author,
            context=None,
        )

        body_short_cap = 500
        body_short = body_for_triage[:body_short_cap]
        if len(body_for_triage) > body_short_cap:
            body_short += "…"

        triage = _intake_env.TriageRecord(
            category=verdict.category,
            merit=verdict.merit,
            urgency=verdict.urgency,
            duplicate_of=list(verdict.duplicate_of),
            recommendation=verdict.recommendation,
            draft_reply=verdict.draft_reply,
            draft_labels=list(verdict.draft_labels),
            estimated_effort=verdict.estimated_effort,
            confidence=verdict.confidence,
            reasoning=verdict.reasoning,
            inbound_author=author,
            inbound_title=title,
            inbound_body_short=body_short,
        )
        intake.triage = triage
        intake.updated_at = _intake_env._utc_now_iso()

        try:
            _intake_store.write_intake(intake, shared_dir)
        except (PermissionError, OSError) as e:
            return jsonify({"error": f"write failed: {e}"}), 500
        return jsonify({"intake": intake.to_dict(), "ok": True})

    # ── Inbox (Phase 1 of Issue Inbox spec) ──────────────────────────────────
    #
    # The Inbox view: filed intakes augmented with computed
    # unread_activity_count so the UI can render "needs your reply"
    # badges. Distinct surface from /api/evo/intake (which lists every
    # state) — the Inbox is exclusively about post-filing tracking.

    @app.get("/api/inbox")
    def api_inbox_list() -> Response:
        """List filed intakes for the Inbox UI, plus computed
        ``unread_activity_count`` per row.

        Query params:
          unread_only   when truthy, only return intakes with
                        unread_activity_count > 0 (default: all filed)
          limit         max rows to return (default 200, cap 500)

        Sort: most-recently-active first (by the latest activity
        observed_at, falling back to updated_at). This puts whatever
        needs the operator's attention at the top.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        unread_only = str(request.args.get("unread_only") or "").lower() in (
            "1", "true", "yes",
        )
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))

        rows: list[dict] = []
        for ix in _intake_store.iter_intakes(shared_dir, subdirs=("filed",)):
            # Phase 4: inbound intakes live in the same `filed/` subdir
            # but are surfaced separately via /api/inbox/triage. Filter
            # them out of the outbound /api/inbox list so the existing
            # UI (operator's filed intakes + maintainer reply tracking)
            # stays focused.
            if getattr(ix, "inbound", False):
                continue
            unread = ix.unread_activity_count()
            if unread_only and unread == 0:
                continue
            last_activity_at = (
                ix.activity_log[-1].observed_at if ix.activity_log else ""
            )
            sort_key = last_activity_at or ix.updated_at
            rows.append({
                "intake": ix.to_dict(),
                "unread_activity_count": unread,
                "last_activity_at": last_activity_at,
                "_sort_key": sort_key,
            })

        rows.sort(key=lambda r: r["_sort_key"], reverse=True)
        for r in rows:
            r.pop("_sort_key", None)
        rows = rows[:limit]
        return jsonify({"items": rows, "count": len(rows)})

    @app.get("/api/inbox/triage")
    def api_inbox_triage_list() -> Response:
        """List inbound intakes (Phase 4 of Issue Inbox).

        Returns intakes captured by ``inbound_issues_watcher`` — issues
        filed by OTHERS on tracked repos where the operator has
        maintainer permission. Each row carries its TriageRecord verdict
        so the UI can render category / urgency / recommendation badges
        without joining back to GitHub.

        Query params:
          urgency    optional comma-separated filter (e.g. ?urgency=p0,p1)
          category   optional comma-separated filter
          limit      default 200, cap 500

        Sort: most-recent triaged_at first.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        urgency_filter = {
            u.strip().lower() for u in (request.args.get("urgency") or "").split(",")
            if u.strip()
        }
        category_filter = {
            c.strip().lower() for c in (request.args.get("category") or "").split(",")
            if c.strip()
        }
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))

        rows: list[dict] = []
        for ix in _intake_store.iter_intakes(shared_dir, subdirs=("filed",)):
            if not getattr(ix, "inbound", False):
                continue
            triage = ix.triage
            if triage is None:
                # Inbound capture without triage shouldn't happen post-
                # Phase 4a (the watcher always attaches one), but if a
                # hand-edited intake ends up here, still include it so
                # the operator sees something rather than nothing.
                pass
            else:
                if urgency_filter and triage.urgency not in urgency_filter:
                    continue
                if category_filter and triage.category not in category_filter:
                    continue
            triaged_at = triage.triaged_at if triage else ix.updated_at
            rows.append({
                "intake": ix.to_dict(),
                "triaged_at": triaged_at,
                "_sort_key": triaged_at or ix.updated_at,
            })

        rows.sort(key=lambda r: r["_sort_key"], reverse=True)
        for r in rows:
            r.pop("_sort_key", None)
        rows = rows[:limit]
        return jsonify({"items": rows, "count": len(rows)})

    # ── Phase 5: auto-response policy + per-intake apply / undo ──────────

    @app.get("/api/inbox/triage/policy")
    def api_inbox_triage_policy_get() -> Response:
        """Return the current auto-response policy.

        Always returns 200 — a missing or malformed policy file is
        treated as the default (everything off), not as an error.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        policy = _intake_policy.load_policy(shared_dir)
        return jsonify({"policy": policy.to_dict()})

    @app.post("/api/inbox/triage/policy")
    def api_inbox_triage_policy_set() -> Response:
        """Persist a new auto-response policy.

        Body is the policy dict (any subset of the AutoResponsePolicy
        fields; missing fields default to "off" / the documented floor).
        We don't validate the operator's confidence floors aggressively —
        the auto-responder itself enforces the per-action minimums.
        """
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        # Merge with current policy so the operator can PATCH a single
        # field via the UI without having to resend the whole blob.
        current = _intake_policy.load_policy(shared_dir).to_dict()
        current.update(body)
        new_policy = _intake_policy.AutoResponsePolicy.from_dict(current)
        try:
            _intake_policy.save_policy(shared_dir, new_policy)
        except OSError as e:
            return jsonify({"error": f"save failed: {e}"}), 500
        return jsonify({"policy": new_policy.to_dict(), "ok": True})

    def _resolve_inbound_token(network: dict, shared_dir: Path):
        """Return ``(token, error_response)`` for the auto-responder.

        We use the same token slot that the inbound watcher uses; if the
        operator hasn't configured intake.github we surface the same 412
        the promote endpoint does so the UI can guide them to the fix.
        """
        cfg = _intake_promote.PromotionConfig.from_network(network)
        if cfg is None:
            return None, (jsonify({
                "error": (
                    "intake.github not configured — run "
                    "`evolve-admin intake configure`"
                ),
            }), 412)
        target = cfg.resolve(None)  # default target — its token has write scope
        from ..keystore import KeystoreManager
        token = KeystoreManager(shared_dir).get_value(target.token_slot)
        if not token:
            return None, (jsonify({
                "error": (
                    f"no token in keystore slot '{target.token_slot}' — "
                    f"run `evolve-admin keys set {target.token_slot}`"
                ),
            }), 412)
        return token, None

    @app.post("/api/inbox/triage/<intake_id>/apply")
    def api_inbox_triage_apply(intake_id: str) -> Response:
        """Manually apply the LLM's recommended action to a single intake.

        Bypasses the policy's ``enabled`` flag — this is an operator-
        initiated approval, so the operator has already taken
        responsibility for the action. We still require:
          - intake is inbound
          - triage verdict is present
          - the recommendation has a corresponding action handler
          - no prior auto_action (don't double-fire)

        Body (all optional):
          actor   GH login used as the audit ``actor_login``; defaults to
                  "evo-bot" when omitted.
          kind    override the action kind (one of close_duplicate /
                  reply_clarifying / label_only). Default derives from
                  the verdict's recommendation field.
        """
        body = request.get_json(silent=True) or {}
        actor = str(body.get("actor") or "evo-bot")
        kind_override = body.get("kind")

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located
        if not intake.inbound:
            return jsonify({"error": "not an inbound intake"}), 409
        if intake.auto_action is not None:
            return jsonify({
                "error": "auto_action already recorded — use /undo first",
            }), 409
        if intake.triage is None:
            return jsonify({"error": "no triage verdict on this intake"}), 409

        rec = intake.triage.recommendation
        kind_map = {
            "auto_close_duplicate": "close_duplicate",
            "auto_reply_clarifying": "reply_clarifying",
        }
        kind = str(kind_override) if kind_override else kind_map.get(rec or "", "")
        if kind not in ("close_duplicate", "reply_clarifying", "label_only"):
            return jsonify({
                "error": (
                    f"no auto-action available for recommendation={rec!r}; "
                    f"pass {{'kind': 'close_duplicate'|'reply_clarifying'|'label_only'}}"
                ),
            }), 409

        token, err = _resolve_inbound_token(network, shared_dir)
        if err is not None:
            return err
        try:
            updated = _intake_auto.apply(
                intake, kind,
                token=token,
                transport=_intake_auto.default_transport,
                actor_login=actor,
                reason="manual",
            )
        except _intake_auto.AutoResponseError as e:
            return jsonify({"error": str(e)}), 502
        _intake_store.write_intake(updated, shared_dir)
        return jsonify({"intake": updated.to_dict(), "ok": True})

    @app.post("/api/inbox/triage/<intake_id>/undo")
    def api_inbox_triage_undo(intake_id: str) -> Response:
        """Reverse a previously-applied auto-action.

        Refuses (409) if there's nothing to undo, the undo has already
        happened, or the 24-hour deadline has passed.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located
        if intake.auto_action is None:
            return jsonify({"error": "no auto_action on this intake"}), 409
        token, err = _resolve_inbound_token(network, shared_dir)
        if err is not None:
            return err
        try:
            updated = _intake_auto.undo(
                intake, token=token, transport=_intake_auto.default_transport,
            )
        except _intake_auto.AutoResponseError as e:
            msg = str(e)
            # "expired" / "already undone" / "no auto_action" all surface as 409
            # so the UI can show "this action has hardened" or similar.
            status = 502 if msg.startswith(("PATCH", "POST", "DELETE")) else 409
            return jsonify({"error": msg}), status
        _intake_store.write_intake(updated, shared_dir)
        return jsonify({"intake": updated.to_dict(), "ok": True})

    # ── Phase 4c: developer-feature toggle (inbound watcher + others) ────

    @app.get("/api/features")
    def api_features_list() -> Response:
        """Return the catalog of toggleable power-features and their state.

        The Inbox tab's "Inbound watcher" pill reads this. Other UI
        surfaces can join later — the response shape is generic across
        every feature catalogued in install_profile.PROFILE_DEFAULTS.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        return jsonify(_feature_toggle.list_features(shared_dir=shared_dir))

    @app.get("/api/features/<name>")
    def api_features_get(name: str) -> Response:
        """Return one feature's resolved state.

        Returns 404 for an uncatalogued name — callers must use a name
        from PROFILE_DEFAULTS to avoid ambiguity over "not installed"
        vs. "unknown feature".
        """
        from install_profile import PROFILE_DEFAULTS  # type: ignore
        catalogued = {n for names in PROFILE_DEFAULTS.values() for n in names}
        if name not in catalogued:
            return jsonify({"error": f"unknown feature {name!r}"}), 404
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        return jsonify({
            "status": _feature_toggle.get_feature_status(
                name, shared_dir=shared_dir,
            ),
        })

    @app.post("/api/features/<name>")
    def api_features_set(name: str) -> Response:
        """Flip a feature's enabled override.

        Body: ``{"enabled": true|false}``. On enable, installs the
        feature's launchd job (if it has one) so the watcher starts
        polling immediately. On disable, boots out and removes the
        plist.

        Returns the refreshed status + a ``log`` list with whatever the
        install/uninstall path emitted — surface this in the UI so the
        operator can see the actual actions taken (and any warnings).
        """
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or "enabled" not in body:
            return jsonify({"error": "body must include 'enabled' (bool)"}), 400
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        try:
            out = _feature_toggle.set_feature_enabled(
                name, bool(body["enabled"]),
                shared_dir=shared_dir,
            )
        except _feature_toggle.FeatureToggleError as e:
            msg = str(e)
            status = 404 if "unknown feature" in msg else 500
            return jsonify({"error": msg}), status
        return jsonify(out)

    @app.post("/api/evo/intake/<intake_id>/seen")
    def api_evo_intake_seen(intake_id: str) -> Response:
        """Move the operator's read cursor on ``intake_id`` to now.

        Clears the "unread activity" badge for this intake without
        modifying the activity_log itself. The next time the watcher
        appends a new event, the badge will reappear.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        located = _intake_store.find_intake(shared_dir, intake_id)
        if located is None:
            return jsonify({"error": "not found"}), 404
        intake, _, _ = located
        _intake_store.mark_activity_seen(intake, shared_dir)
        return jsonify({"intake": intake.to_dict(), "ok": True})

    # ── Tracked-repo subscriptions (Phase 3 of Issue Inbox spec) ─────────────
    #
    # The "Tracked repos" card on the Inbox page consumes these:
    #
    #   GET    /api/inbox/repos           list configured targets with
    #                                     computed permission level + the
    #                                     install's identity
    #   POST   /api/inbox/repos {owner,   add a target. Body shape mirrors
    #          repo, name?, token_slot?}  `evolve-admin intake configure`
    #                                     so the two surfaces stay aligned.
    #   DELETE /api/inbox/repos/<name>    remove a target
    #
    # All three mutate network.json::intake.github via save_network; the
    # backwards-compat v1/v2 schema handling lives in PromotionConfig.
    # The UI is purely a thin client over these.

    def _resolve_token_for_target(target, shared_dir) -> str | None:
        """Fetch the configured token for a target from the keystore.

        Same pattern intake.promote uses. Returns None on missing slot
        / unreadable keystore; callers degrade to permission=unknown
        rather than crashing the page."""
        try:
            from ..keystore import KeystoreManager
            return KeystoreManager(shared_dir).get_value(target.token_slot)
        except Exception:  # noqa: BLE001
            return None

    def _parse_github_owner_repo(url: str) -> "tuple[str, str] | None":
        """Extract ``(owner, repo)`` from a github URL, or ``None`` if not parseable.

        Accepts the post-normalize form ``resolve_repo_url`` returns
        (HTTPS, no trailing ``.git``) as well as raw SSH / .git URLs as a
        belt-and-suspenders measure. Non-github URLs return ``None`` so
        the caller can skip the dynamic preset for self-hosted forks.
        """
        if not isinstance(url, str) or not url.strip():
            return None
        url = url.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[: -len(".git")]
        import re
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", url)
        if not m:
            return None
        return m.group(1), m.group(2)

    def _inbox_repo_suggestions(network, existing_targets) -> "list[dict]":
        """Return one-click presets for the empty-state of the Inbox repos card.

        Two suggestions today, both filtered out once their (owner, repo)
        already appears in the configured targets — once a preset is added,
        the chip disappears from the next page render:

        1. ``openclaw/openclaw`` — universal across all Evolve installs;
           the place upstream OC bug-tracking issues are filed via
           ``evo intake promote ... --to openclaw``.

        2. This pod's Evolve repo — derived from ``resolve_repo_url(network)``
           (``network.pod.repo_url`` or ``git remote get-url origin`` on the
           deploy checkout). Marked ``make_default: true`` because the
           pod's own repo is the natural default target for ``evo intake
           promote``. Skipped when the URL doesn't resolve to a github.com
           remote (self-hosted forks degrade gracefully — operator falls
           back to the regular Add-repo modal).
        """
        from ..config import resolve_repo_url

        existing_pairs = {
            (t.owner.lower(), t.repo.lower()) for t in existing_targets
        }
        suggestions: list[dict] = []

        if ("openclaw", "openclaw") not in existing_pairs:
            suggestions.append({
                "owner": "openclaw",
                "repo": "openclaw",
                "name": "openclaw",
                "label": "openclaw/openclaw",
                "hint": "Track activity on issues filed upstream against OpenClaw.",
                "make_default": False,
            })

        parsed = _parse_github_owner_repo(resolve_repo_url(network))
        if parsed:
            owner, repo = parsed
            if (owner.lower(), repo.lower()) not in existing_pairs:
                suggestions.append({
                    "owner": owner,
                    "repo": repo,
                    "name": repo.lower(),
                    "label": f"{owner}/{repo} (this pod)",
                    "hint": "Track activity on issues filed against this pod's Evolve repo.",
                    "make_default": True,
                })

        return suggestions

    @app.get("/api/inbox/repos")
    def api_inbox_repos_list() -> Response:
        """List configured intake targets with per-target permission.

        Response shape:
          {
            "repos": [
              {
                "name": "evolve",
                "owner": "evolve-ops", "repo": "evolve",
                "token_slot": "github_intake",
                "is_default": true,
                "self_login": "cjalden",       # null if token missing
                "permission": "admin",          # one of admin/maintain/triage/
                                                # write/read/none/unknown
                "is_maintainer": true,
                "tier_label": "maintainer"
              },
              ...
            ]
          }

        Identity + permission lookups are cached in
        :mod:`intake.permissions` (5 min TTLs); page refresh hits cache.
        """
        from ..intake import permissions as _perms

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        cfg = _intake_promote.PromotionConfig.from_network(network)
        if cfg is None:
            return jsonify({
                "repos": [],
                "suggestions": _inbox_repo_suggestions(network, []),
            })

        rows: list[dict] = []
        for target in cfg.targets:
            token = _resolve_token_for_target(target, shared_dir)
            login = _perms.get_self_login(token)
            permission = _perms.get_permission(
                owner=target.owner, repo=target.repo,
                login=login or "",
                token=token,
            )
            rows.append({
                "name": target.name,
                "owner": target.owner,
                "repo": target.repo,
                "token_slot": target.token_slot,
                "is_default": target.name == cfg.default_target_name,
                "self_login": login,
                "permission": permission,
                "is_maintainer": _perms.is_maintainer(permission),
                "tier_label": _perms.maintainer_tier_label(permission),
            })
        return jsonify({
            "repos": rows,
            "suggestions": _inbox_repo_suggestions(network, cfg.targets),
        })

    @app.post("/api/inbox/repos")
    def api_inbox_repos_add() -> Response:
        """Add a configured intake target.

        Body:
          owner          required — GitHub org or user
          repo           required — repo name
          name           optional — display name (default: lowercased repo)
          token_slot     optional — keystore slot (default: "github_intake")
          make_default   optional bool — flip the default-target field

        Mirrors `evolve-admin intake configure` so the CLI and the UI
        produce structurally-identical config.

        Returns 200 with the updated list (post-add), 400 on validation,
        409 if a target with that name already exists.
        """
        body = request.get_json(silent=True) or {}
        owner = str(body.get("owner") or "").strip()
        repo = str(body.get("repo") or "").strip()
        if not owner or not repo:
            return jsonify({"error": "owner and repo are required"}), 400

        name = str(body.get("name") or "").strip().lower() or repo.lower()
        token_slot = str(body.get("token_slot") or "").strip() or "github_intake"
        make_default = bool(body.get("make_default"))

        network = load_network(network_path)
        intake_block = network.setdefault("intake", {})
        github_block = intake_block.get("github")
        if not isinstance(github_block, dict):
            github_block = {}

        targets = github_block.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        # v1 → v2 migration when a v1 single-target was present (mirrors
        # the CLI's intake_configure helper).
        if (not targets
                and github_block.get("owner")
                and github_block.get("repo")):
            targets["default"] = {
                "owner": github_block["owner"],
                "repo": github_block["repo"],
                "token_slot": github_block.get("token_slot", "github_intake"),
                "labels": github_block.get("labels", {}),
            }

        if name in targets:
            return jsonify({
                "error": f"a target named '{name}' already exists "
                "(DELETE first to replace)",
            }), 409

        targets[name] = {
            "owner": owner, "repo": repo,
            "token_slot": token_slot,
            "labels": {},  # operator can fill labels via the CLI for now
        }

        existing_default = github_block.get("default")
        if make_default or len(targets) == 1:
            default_name = name
        elif isinstance(existing_default, str) and existing_default in targets:
            default_name = existing_default
        else:
            default_name = next(iter(targets))

        intake_block["github"] = {
            "default": default_name,
            "targets": targets,
        }
        save_network(network, network_path)

        # Invalidate the permission cache for this repo so the new
        # target's perm appears immediately on the next list call.
        try:
            from ..intake import permissions as _perms
            _perms.clear_caches()
        except Exception:  # noqa: BLE001
            pass

        return jsonify({"ok": True, "added": name, "is_default": default_name == name})

    @app.delete("/api/inbox/repos/<target_name>")
    def api_inbox_repos_delete(target_name: str) -> Response:
        """Remove a configured intake target.

        404s on unknown name. If removing the default leaves at least
        one other target, the first remaining target becomes the new
        default. If removing the LAST target, intake.github goes empty.
        """
        network = load_network(network_path)
        intake_block = network.get("intake") or {}
        github_block = intake_block.get("github")
        if not isinstance(github_block, dict):
            return jsonify({"error": "not found"}), 404

        # v2 path is the only one we mutate — if v1 is still in place
        # we don't expose a delete (operator would lose the legacy
        # single-target entry; safer to require migration first).
        targets = github_block.get("targets")
        if not isinstance(targets, dict) or target_name not in targets:
            return jsonify({"error": "not found"}), 404

        del targets[target_name]

        if not targets:
            # Last one — clear the github block entirely.
            intake_block.pop("github", None)
        else:
            current_default = github_block.get("default")
            if current_default == target_name or current_default not in targets:
                new_default = next(iter(targets))
            else:
                new_default = current_default
            intake_block["github"] = {
                "default": new_default,
                "targets": targets,
            }
        save_network(network, network_path)

        try:
            from ..intake import permissions as _perms
            _perms.clear_caches()
        except Exception:  # noqa: BLE001
            pass

        return jsonify({"ok": True, "removed": target_name})

    # ── Pod-wide intake token (writes to keystore) ──────────────────────────
    #
    # The /api/admin/keys/<bot>/<provider> family is per-bot; github_intake
    # (and per-target variants like github_intake_openclaw) is pod-wide. This
    # route handles that case: takes a PAT + slot, validates via GitHub /user,
    # writes via KeystoreManager.set_value. Surfaced from the Inbox card's
    # inline "no token" text — operator clicks → modal → save → list refreshes
    # to show the now-valid login.

    @app.post("/api/inbox/intake-token")
    def api_inbox_intake_token() -> Response:
        """Save a GitHub PAT to the pod-wide keystore slot used by the
        intake watcher.

        Body:
          token   required — the PAT value
          slot    optional — keystore slot (default: "github_intake")

        Validates the token by hitting GitHub /user; rejects on 401/403
        with a friendly error so the operator sees the problem before the
        save. Network failures during validation are non-fatal — the save
        still proceeds and the watcher reports the issue on its next poll.

        Returns 200 with ``{"ok": True, "slot": ..., "login": ...}`` on
        success (login is null when validation couldn't reach GitHub),
        400 on missing/invalid input or rejected token, 500 on save
        failure.
        """
        from ..intake import permissions as _perms

        body = request.get_json(silent=True) or {}
        token = str(body.get("token") or "").strip()
        slot = str(body.get("slot") or "").strip() or "github_intake"
        if not token:
            return jsonify({"error": "token is required"}), 400
        # Belt-and-suspenders: reject obviously-malformed inputs early so
        # we don't waste a GitHub round-trip on a typo. PATs are all
        # ASCII-printable, ≤200 chars, no whitespace.
        if len(token) > 200 or any(c.isspace() for c in token):
            return jsonify({"error": "token looks malformed (whitespace or too long)"}), 400

        # Validate. ``get_self_login`` returns None for any failure — bad
        # token, network error, malformed response. Distinguish the two
        # by attempting the call once with a short transport timeout; on
        # explicit 401/403, surface; on other failures, save anyway.
        validation_error: str | None = None
        login: str | None = None
        try:
            login = _perms.get_self_login(token)
            if login is None:
                # Could be auth failure OR transport failure. Probe once
                # more with a direct transport call so we can distinguish.
                try:
                    status, _resp = _perms._default_transport(
                        "GET",
                        f"{_perms.GITHUB_API_BASE}/user",
                        _perms._gh_headers(token),
                        None,
                    )
                    if status in (401, 403):
                        return jsonify({
                            "error": (
                                f"GitHub rejected the token (HTTP {status}). "
                                "Check that it has not expired and that it has "
                                "at least the public_repo scope."
                            ),
                        }), 400
                    # Anything else: save with no login info; operator will
                    # see the result on the next list refresh.
                    validation_error = f"validation HTTP {status}"
                except Exception as e:  # noqa: BLE001
                    validation_error = f"validation transport error: {e}"
        except Exception as e:  # noqa: BLE001
            validation_error = f"validation crashed: {e}"

        # Save. Mirrors the CLI's intake_configure flow: register if absent,
        # then set the value. KeystoreManager.set_value refuses to write to
        # an unregistered slot, so the register step is load-bearing on
        # first-time setup (which is exactly the common case here — the
        # operator is wiring up github_intake for the first time).
        try:
            from ..keystore import KeystoreManager, _scrub_token_like
            network = load_network(network_path)
            shared_dir = _shared_dir_from_network(network)
            mgr = KeystoreManager(shared_dir)
            existing = mgr.ks.get_key_entry(slot)
            if not existing:
                mgr.register(
                    slot,
                    provider="github",
                    scope="shared",
                    description="GitHub PAT for intake watcher",
                    bots=None,
                    value=token,
                )
            else:
                mgr.set_value(slot, token)
        except Exception as e:  # noqa: BLE001
            # Defense in depth: scrub any PAT-shaped substring from the
            # exception string before it reaches the UI. The keystore
            # layer no longer puts secrets into exception messages (see
            # KeychainUnavailable / _run_security_or_raise in keystore.py),
            # but a stray future regression here would leak the token —
            # so scrub here too. Matches the same patterns as the
            # keystore-side scrub.
            return jsonify({"error": f"failed to save token: {_scrub_token_like(str(e))}"}), 500

        # Invalidate caches so the new token takes effect on the next
        # /api/inbox/repos render (otherwise the list shows the OLD
        # "no token" / "permission unknown" state until TTL expires).
        try:
            _perms.clear_caches()
        except Exception:  # noqa: BLE001
            pass

        out: dict[str, Any] = {"ok": True, "slot": slot, "login": login}
        if validation_error:
            out["validation_warning"] = validation_error
        return jsonify(out)

    # ── GitHub three-purposes status (Credentials surface) ──────────────────
    #
    # Locked 2026-05-27: GitHub integration covers three architecturally
    # distinct purposes — workspace backup (per-bot), issue tracking
    # (pod-wide, developer surface), and general code access (pod-wide
    # default + per-bot override, via MCP). This endpoint exposes a
    # single summary so the Credentials surface can render the unified
    # GitHub row with status badges and deep-links into the existing
    # setup flows.
    @app.get("/api/credentials/github/status")
    def api_credentials_github_status() -> Response:
        """Return a status snapshot covering the three GitHub purposes.

        Response shape:
          {
            "backup": {
              "configured_count": 3,
              "total_bots": 5,
              "bot_status": [{"bot": "team_bot_a", "configured": true}, ...]
            },
            "intake": {
              "configured": bool,
              "login": "cjalden" | null,
              "slot": "github_intake"
            },
            "general_access": {
              "pod": {
                "configured": bool,
                "login": "cjalden" | null,
                "slot": "github_general_access"
              },
              "overrides": [
                {"bot": "atlas", "login": "atlas-bot", "slot": "github-atlas"},
                ...
              ]
            }
          }

        - Backup status comes from ``network.bots.<id>.backupRepoUrl`` —
          the canonical signal used everywhere else (server.py backup
          routes, deploy.py, action_bot.py).
        - Intake status reads the pod-wide ``github_intake`` keystore slot
          and resolves the GitHub login via the cached
          :func:`intake.permissions.get_self_login`. A configured but
          invalid token still reports ``configured=true`` with ``login=None``
          — the UI distinguishes the two via the login field.
        - General-access reports the pod-wide ``github_general_access``
          slot and enumerates per-bot overrides
          (``github-<bot_id>`` slots that hold a value). Resolution at
          read time is pod-wide → per-bot override, but the UI displays
          both so the operator sees exactly what each bot will use.
        """
        from ..intake import permissions as _perms
        from ..skills import github_install as _gh

        network = load_network(network_path)
        bots_cfg = network.get("bots") or {}
        if not isinstance(bots_cfg, dict):
            bots_cfg = {}

        bot_status: list[dict[str, Any]] = []
        configured_count = 0
        for bot_id, cfg in bots_cfg.items():
            if not isinstance(cfg, dict):
                continue
            configured = bool(cfg.get("backupRepoUrl"))
            if configured:
                configured_count += 1
            bot_status.append({"bot": bot_id, "configured": configured})

        intake_slot = "github_intake"
        try:
            from ..keystore import KeystoreManager
            shared_dir = _shared_dir_from_network(network)
            ks = KeystoreManager(shared_dir)
        except Exception:  # noqa: BLE001
            ks = None

        def _read_slot(slot: str) -> str | None:
            if ks is None:
                return None
            try:
                return ks.get_value(slot)
            except Exception:  # noqa: BLE001
                return None

        def _resolve_login(token: str | None) -> str | None:
            if not token:
                return None
            try:
                return _perms.get_self_login(token)
            except Exception:  # noqa: BLE001
                return None

        # Keystore reads are local + fast, so they stay serial; only the
        # network-bound login resolution (a live GET /user per token) is
        # fanned out below.
        intake_token = _read_slot(intake_slot)

        pod_slot = _gh.POD_KEYSTORE_SLOT
        pod_token = _read_slot(pod_slot)

        override_slots: list[tuple[str, str, str]] = []  # (bot_id, slot, token)
        for bot_id in bots_cfg.keys():
            slot = _gh.keystore_slot_for(bot_id)
            tok = _read_slot(slot)
            if not tok:
                continue
            override_slots.append((bot_id, slot, tok))

        # Resolve every configured token's GitHub login concurrently under a
        # single overall deadline. Safe now that the admin server is threaded
        # (#2837): each worker is an independent urllib GET. The ``configured``
        # booleans below derive only from token presence, so the row always
        # renders promptly even when GitHub is slow; login names are
        # best-effort enrichment. Stragglers that finish after the deadline
        # still warm ``_perms._self_login_cache`` (single-key atomic assign),
        # so the next load picks them up. ``_resolve_login`` already folds
        # exceptions to None.
        jobs: list[tuple[Any, str]] = []  # (result_key, token)
        if intake_token:
            jobs.append(("intake", intake_token))
        if pod_token:
            jobs.append(("pod", pod_token))
        for bot_id, _slot, tok in override_slots:
            jobs.append((("bot", bot_id), tok))

        logins: dict[Any, str | None] = {}
        if jobs:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(jobs), 8),
                thread_name_prefix="ghstatus",
            )
            futs = {executor.submit(_resolve_login, tok): key for key, tok in jobs}
            done, _pending = concurrent.futures.wait(
                futs, timeout=_GITHUB_STATUS_RESOLVE_DEADLINE_S,
            )
            for fut, key in futs.items():
                try:
                    logins[key] = fut.result() if fut in done else None
                except Exception:  # noqa: BLE001
                    logins[key] = None
            # Never block the response on stragglers — let any unfinished GET
            # complete in the background (it warms the cache) and return now.
            executor.shutdown(wait=False)

        intake_login = logins.get("intake")
        pod_login = logins.get("pod")

        overrides: list[dict[str, Any]] = []
        for bot_id, slot, _tok in override_slots:
            overrides.append({
                "bot": bot_id,
                "login": logins.get(("bot", bot_id)),
                "slot": slot,
            })

        return jsonify({
            "backup": {
                "configured_count": configured_count,
                "total_bots": len(bot_status),
                "bot_status": bot_status,
            },
            "intake": {
                "configured": bool(intake_token),
                "login": intake_login,
                "slot": intake_slot,
            },
            "general_access": {
                "pod": {
                    "configured": bool(pod_token),
                    "login": pod_login,
                    "slot": pod_slot,
                },
                "overrides": overrides,
            },
        })

    # ── Help retrieval (grounded Q&A for the primary bot) ────────────────────

    @app.post("/api/evo/help/search")
    def api_evo_help_search() -> Response:
        """BM25 search across the help index.

        Body:
          query   required — free-text search string
          k       optional — top-N to return (default 3, max 10)

        Returns 200 with ``{"hits": [...]}`` (possibly empty), or 503 if
        the index file isn't built yet (handler advises running
        ``evolve-admin help-index build``).
        """
        body = request.get_json(silent=True) or {}
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return jsonify({"error": "query is required"}), 400
        try:
            k = int(body.get("k", 3))
        except (TypeError, ValueError):
            k = 3
        k = max(1, min(k, 10))

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        index = _help_index_build.load_index(shared_dir)
        if index is None:
            return jsonify({
                "error": (
                    "help index not built — run `evolve-admin help-index build`"
                ),
            }), 503

        hits = _help_search.search(index, query, k=k)
        return jsonify({"hits": [h.to_dict() for h in hits]})

    @app.post("/api/evo/help/read")
    def api_evo_help_read() -> Response:
        """Read one help doc by id.

        Body:
          doc_id   required — e.g. "cost", "security", "operator-runbook"

        Returns 200 with ``{"doc": {...}}`` on hit, 404 if no such doc,
        503 if the index isn't built. The doc body is the full
        markdown (no streaming, no chunking) capped by spec at 50KB —
        currently every help doc is under that.
        """
        body = request.get_json(silent=True) or {}
        doc_id = body.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            return jsonify({"error": "doc_id is required"}), 400

        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        index = _help_index_build.load_index(shared_dir)
        if index is None:
            return jsonify({
                "error": (
                    "help index not built — run `evolve-admin help-index build`"
                ),
            }), 503

        doc = index.by_id(doc_id.strip())
        if doc is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"doc": doc.to_dict()})

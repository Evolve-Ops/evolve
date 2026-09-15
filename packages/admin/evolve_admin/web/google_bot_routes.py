"""Bot-facing Google tool routes — ``/api/google/*``.

Spec: [internal/spec-google-integration-architecture-2026-06-20.md](../../../internal/spec-google-integration-architecture-2026-06-20.md)
(P1 — §4.1 delivery shape, §7 security).

The Evolve OC plugin (loaded in every bot's gateway, running as the bot's
own macOS user) reaches the curated Google tools through these routes over
the admin daemon's **unix socket** (``{shared}/admin-daemon.sock``). Two
endpoints:

  GET   /api/google/state   → does this bot have Google? + the tool surface
  POST  /api/google/call    → execute one curated tool {tool, args}

THE IDENTITY MODEL (the crux of this PR):

  The calling bot is bound **server-side** from the kernel-reported peer uid
  of the unix-socket connection (``peer_auth.resolve_peer_bot_id``) — NEVER
  from a request field. A bot literally cannot present another bot's uid, so
  bot A can never read bot B's Gmail/Drive/Calendar by any request
  manipulation. Any ``bot`` / ``subject`` in the request body is ignored.
  This is the fix for the evolve-pod bridge's client-supplied-``bot``-arg
  cross-bot data-leak hole (spec §1.2 / §7).

  TCP requests (the admin-UI binding) have no peer credentials and are
  rejected with 403 — a browser session must never act as a bot here.

THE ELIGIBILITY GATE:

  A bot with no configured ``google_integration`` (the wizard is the opt-in)
  gets 403 ``not_configured`` from /call and ``configured: false`` from
  /state. Least-privilege by construction (spec §8 decision 3).

Credentials stay with the ``evolve`` user (the admin daemon); the bot
receives only results. Every call is audited ``(botId, tool, scopes,
outcome)``.
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from .. import google_auth, google_service
from ..config import load_network
from . import peer_auth
from .routes_shared import _audit_log_entry


def _audit(bot_id: str, tool: str, scopes: list, outcome: str, *, write: bool) -> None:
    """Audit one bot-facing Google call: (botId, tool, scopes, outcome).

    Uses the admin audit log (the same trail wizard/skill installs write to).
    Best-effort by contract — ``_audit_log_entry`` swallows its own I/O
    errors, so this never breaks a tool call.
    """
    _audit_log_entry(
        f"google.bot_call.{tool}",
        bot_id,
        {
            "tool": tool,
            "scopes": list(scopes),
            "outcome": outcome,
            "write": write,
            "surface": "bot_plugin",
        },
    )


def register_google_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/google/*`` routes on the Flask app."""

    @app.get("/api/google/state")
    def api_google_bot_state() -> ResponseReturnValue:
        """Report whether the calling bot has Google + the tool surface.

        Bot identity is the unix-socket peer uid (server-trusted). The
        Evolve plugin calls this at session start to decide whether to
        register the curated tools. 403 when the caller can't be bound to a
        bot (TCP, or an unknown peer uid).
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            return jsonify({"error": "caller not a recognized bot"}), 403
        network = load_network(network_path)
        return jsonify(google_service.google_state(bot_id, network))

    @app.post("/api/google/call")
    def api_google_bot_call() -> ResponseReturnValue:
        """Execute one curated Google tool as the *calling* bot.

        Body: ``{"tool": "gmail_list_messages", "args": {...}}``. Any
        ``bot``/``subject`` in the body is IGNORED — the acting bot is the
        unix-socket peer uid, bound server-side.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            # No peer-bound identity (TCP, or unknown uid) → never run a
            # Google call. Fail-closed; this is the cross-bot boundary.
            return jsonify({"error": "caller not a recognized bot"}), 403

        body = request.get_json(silent=True) or {}
        tool = (body.get("tool") or "").strip()
        args = body.get("args")
        if not isinstance(args, dict):
            args = {}
        if not tool:
            _audit(bot_id, "", [], "bad_request", write=False)
            return jsonify({"error": "tool is required"}), 400

        network = load_network(network_path)

        # Eligibility gate: no configured google_integration ⇒ no Google
        # tools for this bot (spec §8 decision 3).
        if not google_service.is_google_configured(bot_id, network):
            _audit(bot_id, tool, [], "not_configured", write=False)
            return jsonify({
                "error": "not_configured",
                "detail": (
                    f"bot {bot_id!r} has no Google integration configured; "
                    f"set one up via the Google wizard on the Skills page"
                ),
            }), 403

        spec = google_service.TOOL_SPECS.get(tool)
        if spec is None:
            _audit(bot_id, tool, [], "unknown_tool", write=False)
            return jsonify({
                "error": "unknown_tool",
                "detail": f"unknown google tool {tool!r}",
            }), 400

        try:
            result = google_service.run_google_tool(
                bot_id, tool, args, network=network,
            )
        except google_auth.InsufficientGrantedScopes as exc:
            _audit(bot_id, tool, exc.missing, "insufficient_scopes", write=spec.write)
            return jsonify({
                "error": "insufficient_scopes",
                "detail": str(exc),
                "missing_scopes": exc.missing,
            }), 403
        except google_auth.RefreshTokenExpired as exc:
            _audit(bot_id, tool, [], "reauth_required", write=spec.write)
            return jsonify({
                "error": "reauth_required",
                "detail": str(exc),
            }), 503
        except google_auth.GoogleIntegrationConfigError as exc:
            _audit(bot_id, tool, [], "config_error", write=spec.write)
            return jsonify({"error": "config_error", "detail": str(exc)}), 400
        except FileNotFoundError as exc:
            # Missing SA JSON / OAuth token record — operator setup gap.
            _audit(bot_id, tool, [], "credentials_missing", write=spec.write)
            return jsonify({"error": "credentials_missing", "detail": str(exc)}), 503
        except ValueError as exc:
            # Tool-level validation (missing/invalid args).
            _audit(bot_id, tool, [], "bad_request", write=spec.write)
            return jsonify({"error": "bad_request", "detail": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 — surface as a 502, audited
            _audit(bot_id, tool, [], f"error:{type(exc).__name__}", write=spec.write)
            return jsonify({
                "error": "tool_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }), 502

        _audit(bot_id, tool, spec_scopes_hint(tool), "ok", write=spec.write)
        return jsonify(result)


def spec_scopes_hint(tool: str) -> list:
    """Best-effort scope label for the audit trail.

    The curated tools encode their scope choice inside the implementation
    (e.g. drive_search → drive.readonly); we surface a coarse per-tool hint
    for the audit log without re-deriving it from the call. Empty list when
    unknown — the audit still records the tool + outcome.
    """
    hints = {
        "gmail_send": google_service.GMAIL_SCOPES_SEND,
        "gmail_list_messages": google_service.GMAIL_SCOPES_READ,
        "gmail_get_message": google_service.GMAIL_SCOPES_READ,
        "gmail_list_labels": google_service.GMAIL_SCOPES_READ,
        "gmail_label_message": google_service.GMAIL_SCOPES_MODIFY,
        "gmail_archive_message": google_service.GMAIL_SCOPES_MODIFY,
        "gmail_delete_message": google_service.GMAIL_SCOPES_MODIFY,
        "gmail_trash_message": google_service.GMAIL_SCOPES_MODIFY,
        "gmail_mark_read": google_service.GMAIL_SCOPES_MODIFY,
        "gmail_mark_unread": google_service.GMAIL_SCOPES_MODIFY,
        "drive_write_file": google_service.DRIVE_SCOPES_FILE,
        "drive_list_files": google_service.DRIVE_SCOPES_FILE,
        "drive_read_file": google_service.DRIVE_SCOPES_FILE,
        "drive_search": google_service.DRIVE_SCOPES_READ,
        "calendar_create_event": google_service.CALENDAR_SCOPES_FULL,
        "calendar_list_events": google_service.CALENDAR_SCOPES_READ,
    }
    return list(hints.get(tool, []))

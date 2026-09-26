"""users_bot_routes — the bot-facing ``/api/users-bot/{whoami,list}`` verbs.

`internal/dispatch/done/users-roster-read-only-surface.md` item 2: ``users.whoami`` /
``users.list``, the app-facing contract rows an app running in a bot's turn calls to learn
who is asking. Mirrors ``directory_bot_routes`` exactly for the identity binding:

  POST /api/users-bot/whoami  body ``{app_id?}``  → ``{user}`` | ``{refused, reason}``
  POST /api/users-bot/list    body ``{app_id?}``  → ``{users:[...]}`` | ``{refused, reason}``

THE CALLING BOT is bound server-side from the unix-socket peer uid
(``peer_auth.resolve_peer_bot_id``) — never a request field, same primitive as
``directory_bot_routes`` / ``google_bot_routes``. TCP or an unrecognized peer gets 403.

THE CALLING PERSON is asserted via the ``X-Requester-Identity: <platform>:<stable_id>``
header — the SAME header + parser ``RosterTools.buildRequesterHeaders`` /
``action_roster._requester_headers_or_refusal`` already send, reused here via
``routes_bot_users._parse_requester_identity`` rather than re-implemented (module-private,
imported the way ``roster_resolver`` already imports ``routes_bot_users`` helpers). A
missing or malformed header refuses — this route answers "who is asking", so an assertion
that failed to parse must never be silently treated as "nobody asking" (which would read
as the header-less trusted-UI case and is not: this route is socket-only, never served to
the admin UI transport at all).

THE APP'S AUDIENCE is named by ``app_id`` in the body (this route trusts the caller's
OWN app_id — the calling bot already knows which app instance made the tool call, exactly
as ``expand_app``'s hard enforcement point resolves app identity from its own call context
per design-app-access §6.1; nothing here re-derives it). Omitted entirely for a plain
bot-conversation call not mediated by any particular app — always allowed, matching
``directory_lookup``'s own no-app-concept precedent.

NO WRITES. Both routes are reads; ``users_roster`` exposes nothing else.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request
from flask.typing import ResponseReturnValue

from .. import users_roster
from ..config import load_network
from . import peer_auth
from .routes_bot_users import _MALFORMED_IDENTITY, _parse_requester_identity
from .routes_shared import _audit_log_entry


def _resolve_app_id(body: dict) -> "tuple[str | None, bool]":
    """``(app_id, ok)``. ``ok=False`` only for a present-but-not-a-string value —
    an app_id must never silently coerce into a different string."""
    raw = body.get("app_id")
    if raw is None:
        return None, True
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), True
    return None, False


def _requester_or_error() -> "tuple[str, str] | Response":
    """``(platform, stable_id)`` from the requester-identity header, or a Flask
    ``Response`` to return as-is. This route is socket-only (never the
    header-less admin-UI transport), so an absent OR malformed header both
    refuse — there is no trusted header-less path here.

    Returns a plain ``Response`` (never a ``(body, status)`` tuple) on the
    error path — a 2-tuple would be indistinguishable from the success
    return's own ``(platform, stable_id)`` tuple, and ``isinstance(x, tuple)``
    at the call site would silently treat the error as a resolved identity.
    """
    req = _parse_requester_identity()
    if req is None or req is _MALFORMED_IDENTITY:
        resp = jsonify({
            "error": "X-Requester-Identity header is required (platform:stable_id)",
        })
        resp.status_code = 400
        return resp
    platform, stable_id, _source_bot = req
    return platform, stable_id


def register_users_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/users-bot/*`` routes on ``app``."""

    @app.post("/api/users-bot/whoami")
    def api_users_bot_whoami() -> ResponseReturnValue:
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            return jsonify({"error": "caller not a recognized bot"}), 403
        requester = _requester_or_error()
        if not isinstance(requester, tuple):
            return requester
        platform, stable_id = requester

        body = request.get_json(silent=True) or {}
        app_id, ok = _resolve_app_id(body)
        if not ok:
            return jsonify({"error": "app_id must be a string"}), 400

        net = load_network(network_path)
        audience = users_roster.app_audience(net, bot_id, app_id)
        result = users_roster.whoami(
            net, bot_id=bot_id, platform=platform, stable_id=stable_id,
            app_audience=audience,
        )
        _audit_log_entry("users.bot_call.whoami", bot_id, {
            "app_id": app_id, "refused": isinstance(result, users_roster.Refusal),
        })
        return jsonify(result.to_dict() if isinstance(
            result, users_roster.Refusal) else {"user": result})

    @app.post("/api/users-bot/list")
    def api_users_bot_list() -> ResponseReturnValue:
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            return jsonify({"error": "caller not a recognized bot"}), 403
        requester = _requester_or_error()
        if not isinstance(requester, tuple):
            return requester
        platform, stable_id = requester

        body = request.get_json(silent=True) or {}
        app_id, ok = _resolve_app_id(body)
        if not ok:
            return jsonify({"error": "app_id must be a string"}), 400

        net = load_network(network_path)
        audience = users_roster.app_audience(net, bot_id, app_id)
        result = users_roster.list_for_caller(
            net, bot_id=bot_id, platform=platform, stable_id=stable_id,
            app_audience=audience,
        )
        _audit_log_entry("users.bot_call.list", bot_id, {
            "app_id": app_id, "refused": isinstance(result, users_roster.Refusal),
        })
        if isinstance(result, users_roster.Refusal):
            return jsonify(result.to_dict())
        return jsonify({"users": result})

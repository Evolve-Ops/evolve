"""HTTP routes for the Connection registry (D-CN2/D-CN8 read path).

GET /api/connections/<bot_id>   this bot's connection rows, health as text

Admin surface (browser, device-token gated by the app-wide auth
``before_request`` — see ``server.py``'s ``_enforce_device_auth``), not the
peer-uid-bound bot-facing family (``google_bot_routes`` etc.). Credentials
never appear in the response — only ``credential_ref``/``credential_kind``.

Spec: internal/design-connections-that-just-work-2026-09-15.md §2, §3 (D-CN4,
D-CS7 — health names its subject and can say "unknown").
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from .. import connections as conn
from ..config import load_network
from .http_errors import error_response
from .routes_shared import _audit_log_entry


def _connection_view(row: dict) -> dict:
    """Public shape for one row — never echoes ``credential_ref`` verbatim
    into anything that looks like a secret (it isn't one; it's a pointer),
    but keeps the response to the fields a caller needs: what this
    connection can do, and whether it's proven to work."""
    return {
        "id": row.get("id"),
        "service": row.get("service"),
        "account": row.get("account"),
        "scope": row.get("scope"),
        "jobs": row.get("jobs", []),
        "capabilities": row.get("capabilities", []),
        "credential_kind": row.get("credential_kind"),
        "expires_at": row.get("expires_at"),
        "health": conn.effective_health(row),
        "health_display": conn.health_display(conn.effective_health(row)),
        "added_at": row.get("added_at"),
        "grant_scope": row.get("grant_scope"),
        "grant_scope_display": conn.grant_scope_display(row),
        "granted_at": row.get("granted_at"),
        "spent_at": row.get("spent_at"),
        "migrated": bool(row.get("migrated")),
    }


def register_connections_routes(app: Flask, network_path: Path) -> None:
    from .imessage_routes import register_imessage_routes
    register_imessage_routes(app, network_path)  # the iMessage wizard (D-IM3)

    @app.get("/api/connections/<bot_id>")
    def api_connections_for_bot(bot_id: str) -> ResponseReturnValue:
        try:
            network = load_network(network_path)
            if bot_id not in (network.get("bots") or {}):
                return jsonify({"error": "unknown bot", "bot_id": bot_id}), 404
            rows = conn.for_bot(bot_id, path=conn.connections_path(network))
            return jsonify({
                "bot_id": bot_id,
                "connections": [_connection_view(r) for r in rows],
            })
        except Exception as e:  # noqa: BLE001 — surfaced via the shared error mapper
            return error_response(e)

    @app.post("/api/connections/<bot_id>/<row_id>/grant")
    def api_connections_set_grant(bot_id: str, row_id: str) -> ResponseReturnValue:
        """Skills-tile "make standing" / "expire now": ``{"action": ...}``."""
        try:
            action = (request.get_json(silent=True) or {}).get("action")
            if action not in ("make_standing", "expire_now"):
                return jsonify({"error": "action must be make_standing or expire_now"}), 400
            network = load_network(network_path)
            path = conn.connections_path(network)
            if not any(r.get("id") == row_id for r in conn.for_bot(bot_id, path=path)):
                return jsonify({"error": "unknown connection", "id": row_id}), 404
            if action == "make_standing":
                conn.set_grant_scope(row_id, "standing", path)
            else:
                conn.expire_now(row_id, path)
            _audit_log_entry(f"connections.grant.{action}", bot_id, {"connection": row_id})
            row = next(r for r in conn.for_bot(bot_id, path=path) if r.get("id") == row_id)
            return jsonify(_connection_view(row))
        except Exception as e:  # noqa: BLE001
            return error_response(e)

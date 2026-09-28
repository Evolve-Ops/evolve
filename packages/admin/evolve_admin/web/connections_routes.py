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

from flask import Flask, jsonify
from flask.typing import ResponseReturnValue

from .. import connections as conn
from ..config import load_network
from .http_errors import error_response


def _connection_view(row: dict) -> dict:
    """Public shape for one row — never echoes ``credential_ref`` verbatim
    into anything that looks like a secret (it isn't one; it's a pointer),
    but keeps the response to the fields a caller needs: what this
    connection can do, and whether it's proven to work."""
    return {
        "id": row.get("id"),
        "service": row.get("service"),
        "account": row.get("account"),
        "jobs": row.get("jobs", []),
        "capabilities": row.get("capabilities", []),
        "credential_kind": row.get("credential_kind"),
        "expires_at": row.get("expires_at"),
        "health": row.get("health"),
        "health_display": conn.health_display(row.get("health")),
        "added_at": row.get("added_at"),
    }


def register_connections_routes(app: Flask, network_path: Path) -> None:
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

"""routes_users — the admin-UI read behind the pod-wide Roster card.

  GET /api/users/roster?bot=<id>&app=<app_id>  — every person the pod serves,
                                                  optionally filtered by bot or app

Admin-UI only (the ordinary ``/api/*`` device-auth gate applies — this route is
deliberately NOT added to ``peer_auth``'s bot-facing exemption list, unlike the
audience-checked bot verbs in ``users_bot_routes.py``). No write route exists here or
anywhere in ``users_roster`` (M1 is read-only roster, D-AP5).
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from .. import users_roster
from ..config import load_network


def register_users_roster_routes(app: Flask, network_path: Path) -> None:
    """Register ``GET /api/users/roster`` on ``app``."""

    @app.get("/api/users/roster")
    def api_users_roster() -> ResponseReturnValue:
        net = load_network(network_path)
        bot_filter = (request.args.get("bot") or "").strip() or None
        app_filter = (request.args.get("app") or "").strip() or None

        users = users_roster.list_users(net)

        if bot_filter:
            users = [
                u for u in users
                if any(b["bot_id"] == bot_filter for b in u["bots"])
            ]
        if app_filter:
            users = [
                u for u in users
                if any(a["app_id"] == app_filter for a in u["apps"])
            ]

        return jsonify({"users": users})

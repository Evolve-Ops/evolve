"""Admin per-bot messaging-CHANNEL routes (M1-B4b).

The HTTP seam over :mod:`evolve_admin.channel_provisioning` — "which channels
can bot B be added to, and add one".

Deliberately NOT part of ``routes_bot_users``: that module answers *who* a
bot's users are (pairing store, roster overlay, roles). This one answers
*where* the bot is reachable. Same page in the UI eventually, orthogonal
state on disk.

Endpoints::

  GET  /api/admin/bots/<bot_id>/channels
        → {bot_id, enabled: [<id>…], available: [{id, label, install,
           plugin_required, enabled, supports_pairing}, …]}

  POST /api/admin/bots/<bot_id>/channels/add
        body {channel, credential?, credential_field?, channel_fields?,
              install_plugin?, restart_gateway?}
        → AddChannelOutcome.to_dict()

``restart_gateway`` defaults to **False**. The response carries
``restart_required``; making the channel live is a second, explicit call.
No route in this module restarts anything by default.

There is no SPA surface yet — see the PR body for what a future Users-page
"Add a channel" control would call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .. import channel_provisioning as cp
from ..channels import enabled_channels_from_config
from ..config import load_network

log = logging.getLogger(__name__)


def _bot_exists(net: dict[str, Any], bot_id: str) -> bool:
    bots = net.get("bots")
    return isinstance(bots, dict) and bot_id in bots


def register_routes(app: Flask, network_path: Path) -> None:
    """Register per-bot channel endpoints on ``app``."""

    @app.get("/api/admin/bots/<bot_id>/channels")
    def bot_channels_list(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404

        from ..skills._oc_install_common import read_oc_config

        cfg, err = read_oc_config(bot_id)
        enabled = enabled_channels_from_config(cfg)
        return jsonify({
            "bot_id": bot_id,
            "config_error": err,
            "enabled": sorted(enabled),
            "available": [
                {
                    "id": spec.id,
                    "label": spec.display_label,
                    "install": spec.install,
                    "plugin_required": cp.channel_needs_plugin_install(spec),
                    "oc_plugin_id": spec.oc_plugin_id,
                    "enabled": spec.id in enabled,
                    "supports_pairing": spec.supports_pairing,
                }
                for spec in cp.provisionable_channels()
            ],
        })

    @app.post("/api/admin/bots/<bot_id>/channels/add")
    def bot_channels_add(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404

        body = request.get_json(silent=True) or {}
        channel = (body.get("channel") or "").strip()
        if not channel:
            return jsonify({"ok": False, "error": "channel required"}), 400

        fields = body.get("channel_fields")
        if fields is not None and not isinstance(fields, dict):
            return jsonify({"ok": False, "error": "channel_fields must be an object"}), 400

        outcome = cp.add_channel_to_bot(
            bot_id,
            channel,
            credential=body.get("credential") or None,
            credential_field=(body.get("credential_field") or "bot_token"),
            channel_fields=fields,
            install_plugin=bool(body.get("install_plugin", True)),
            # Opt-in only — a live pod is never restarted from a default.
            restart_gateway=bool(body.get("restart_gateway", False)),
        )

        # Audit without the credential: the outcome carries no secret, and
        # the request body's credential never reaches the log.
        try:
            from .server import _audit_log_entry

            _audit_log_entry("bot.channel.add", bot_id, {
                "channel": outcome.channel_id,
                "ok": outcome.ok,
                "plugin_state": outcome.plugin_state,
                "config_changed": outcome.config_changed,
                "restart_required": outcome.restart_required,
                "error": outcome.error,
            })
        except Exception:  # noqa: BLE001 - auditing must not fail the call
            log.exception("channel-add audit log failed for %s", bot_id)

        return jsonify(outcome.to_dict()), (200 if outcome.ok else 400)

"""HTTP routes for the iMessage connection wizard (D-IM3).

GET  /api/connections/<bot>/imessage            wizard state: row, sign-in read-back, pre-fill
POST /api/connections/<bot>/imessage/keeper     install the per-user Messages keeper agent
POST /api/connections/<bot>/imessage/connect    step 3: write row + config, restart, probe, first message
POST /api/connections/<bot>/imessage/probe      re-run the probe, persist health
POST /api/connections/<bot>/imessage/disconnect drop the row and the OC channel block

Admin surface behind the app-wide device-token auth, like the read route in
``connections_routes``. The handle is never a request field: it is read back
from Messages (``imessage_channel.Seams.signed_in_handle``).

Spec: internal/design-imessage-channel-2026-09-29.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from .. import connections as conn
from .. import imessage_channel as ic
from ..config import get_bot_user, load_network
from ..skills import imessage_install as _ii
from ..skills import supported_on_host
from .http_errors import error_response
from .routes_shared import _audit_log_entry


class _Ctx(NamedTuple):
    network: dict[str, Any]
    path: Path
    user: str


def register_imessage_routes(app: Flask, network_path: Path) -> None:
    def _ctx(bot_id: str) -> tuple[_Ctx | None, ResponseReturnValue]:
        """``(ctx, _)`` for a known bot on a Mac pod, else ``(None, error)``.
        Callers test ``ctx is None`` so the unpack narrows."""
        network = load_network(network_path)
        if bot_id not in (network.get("bots") or {}):
            return None, (jsonify({"error": "unknown bot", "bot_id": bot_id}), 404)
        if not supported_on_host(_ii.SKILL_REGISTRY_ENTRY):
            return None, (jsonify({
                "ok": False, "error": "skill_unavailable_on_platform",
                "detail": "iMessage needs a Mac pod; this pod's host cannot run the channel.",
            }), 409)
        return _Ctx(network, conn.connections_path(network), get_bot_user(bot_id, network)), ""

    def _macos_user(network: dict, bot_id: str, mode: str) -> str:
        # Shared mode: Messages lives on the pod admin's account, not the bot's.
        if mode == "shared":
            admin = network.get("admin_user")
            if admin:
                return str(admin)
        return get_bot_user(bot_id, network)

    @app.get("/api/connections/<bot_id>/imessage")
    def api_imessage_state(bot_id: str) -> ResponseReturnValue:
        try:
            ctx, err = _ctx(bot_id)
            if ctx is None:
                return err
            network, path, user = ctx
            row = ic.get_row(bot_id, path)
            seams = ic.Seams()
            if row:
                user = row["macos_user"]
            handle = seams.signed_in_handle(user, bot_id)
            cfg, _ = seams.read_config(bot_id)
            return jsonify({
                "bot_id": bot_id,
                "row": ic.public_row(row) if row else None,
                "signin": ic.signin_instruction(user, shared=bool(row and row.get("apple_id_mode") == "shared")),
                "signed_in_handle": handle,
                "prefill_allow_from": ic.prefill_allow_from(network, bot_id),
                "telegram_binding": ic.telegram_binding(cfg),
                "open_policy": ic.open_policy_findings(cfg),
                "apple_id_modes": list(ic.APPLE_ID_MODES),
                "wizard_budget_s": ic.WIZARD_BUDGET_S,
            })
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.post("/api/connections/<bot_id>/imessage/keeper")
    def api_imessage_keeper(bot_id: str) -> ResponseReturnValue:
        try:
            ctx, err = _ctx(bot_id)
            if ctx is None:
                return err
            network, _path, _user = ctx
            body = request.get_json(silent=True) or {}
            mode = body.get("apple_id_mode") or ic.DEFAULT_APPLE_ID_MODE
            ok, detail = ic.install_keeper(_macos_user(network, bot_id, mode))
            _audit_log_entry("connection.imessage.keeper", bot_id, {"ok": ok, "error": detail})
            return jsonify({"ok": ok, "detail": detail}), (200 if ok else 500)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.post("/api/connections/<bot_id>/imessage/connect")
    def api_imessage_connect(bot_id: str) -> ResponseReturnValue:
        try:
            ctx, err = _ctx(bot_id)
            if ctx is None:
                return err
            network, path, _user = ctx
            body = request.get_json(silent=True) or {}
            if "handle" in body:
                return jsonify({"ok": False, "error": "handle_is_read_back",
                                "detail": "The address comes from Messages, not from this form."}), 400
            allow = body.get("allow_from")
            if not isinstance(allow, list):
                return jsonify({"ok": False, "error": "allow_from must be a list"}), 400
            mode = body.get("apple_id_mode") or ic.DEFAULT_APPLE_ID_MODE
            res = ic.connect(
                bot_id=bot_id, macos_user=_macos_user(network, bot_id, mode),
                allow_from=[str(a) for a in allow], connections_file=path,
                apple_id_mode=mode, retire_telegram=body.get("retire_telegram") is True,
            )
            _audit_log_entry("connection.imessage.connect", bot_id, {
                "ok": res.ok, "stage": res.stage, "allow_from_count": len(allow),
                "apple_id_mode": mode, "telegram_retired": res.telegram_retired,
            })
            return jsonify(res.to_dict()), (200 if res.ok else 422)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.post("/api/connections/<bot_id>/imessage/probe")
    def api_imessage_probe(bot_id: str) -> ResponseReturnValue:
        try:
            ctx, err = _ctx(bot_id)
            if ctx is None:
                return err
            _network, path, _user = ctx
            res, row = ic.run_probe(bot_id, path)
            if res is None or row is None:
                return jsonify({"error": "no iMessage connection for this bot"}), 404
            return jsonify({"probe": res.to_dict(), "row": ic.public_row(row)})
        except Exception as e:  # noqa: BLE001
            return error_response(e)

    @app.post("/api/connections/<bot_id>/imessage/disconnect")
    def api_imessage_disconnect(bot_id: str) -> ResponseReturnValue:
        try:
            ctx, err = _ctx(bot_id)
            if ctx is None:
                return err
            _network, path, _user = ctx
            ok, detail = _ii.revoke_account(bot_id)
            row = ic.get_row(bot_id, path)
            if ok and row:
                conn.remove_connection(row["id"], path)
            _audit_log_entry("connection.imessage.disconnect", bot_id, {"ok": ok, "error": detail})
            return jsonify({"ok": ok, "detail": detail}), (200 if ok else 500)
        except Exception as e:  # noqa: BLE001
            return error_response(e)

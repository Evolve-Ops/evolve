"""HTTP routes for the PA-week cost number (brief ``pa-week-cost-is-measured``).

GET  /api/analytics/pa-week-cost          per bot per day rows + week + pod
POST /api/analytics/pa-week-cost/reading  record the operator's pool reading for a day

Thin re-shape of ``analyzer/pa_week_cost.py``; nothing is re-derived here and
nothing here touches which model answers a turn. Registered from
``register_cost_measures_routes`` (server.py is under a no-growth cap).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_pa_week_cost")

_memo: dict[str, Any] = {"at": 0.0, "report": None}
_MEMO_SECONDS = 300


def _import_analyzer(mod: str):
    import importlib
    return importlib.import_module(mod)


def register_pa_week_cost_routes(app: Flask, network_path: Path) -> None:
    def _shared() -> Path:
        from evolve_config import get_shared_dir  # type: ignore[import]

        return get_shared_dir(load_network(network_path))

    @app.get("/api/analytics/pa-week-cost")
    def api_pa_week_cost():
        try:
            pw = _import_analyzer("pa_week_cost")
            eo = _import_analyzer("evolve_overhead")
        except Exception as exc:
            return jsonify({"error": f"pa_week_cost unavailable: {exc}"}), 500
        force = request.args.get("refresh") == "1"
        if not force and _memo["report"] is not None and time.time() - _memo["at"] < _MEMO_SECONDS:
            report = _memo["report"]
        else:
            network = load_network(network_path)
            try:
                report = pw.build_report(_shared(), network, bot_ids=eo.bot_ids_of(network))
            except Exception as exc:  # noqa: BLE001
                _log.warning("pa-week-cost: build failed: %s", exc)
                return jsonify({"error": f"could not build the week's cost rows: {exc}"}), 500
            _memo.update(at=time.time(), report=report)
        return jsonify({**report, "fit_sentence": pw.fit_sentence(report)})

    @app.post("/api/analytics/pa-week-cost/reading")
    def api_pa_week_cost_reading():
        try:
            pw = _import_analyzer("pa_week_cost")
        except Exception as exc:
            return jsonify({"ok": False, "error": f"pa_week_cost unavailable: {exc}"}), 500
        body = request.get_json(silent=True) or {}
        try:
            rec = pw.set_reading(
                _shared(), str(body.get("day") or ""), used_usd=body.get("used_usd"),
                ceiling_usd=body.get("ceiling_usd"), note=body.get("note"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            _log.warning("pa-week-cost: reading write failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500
        _memo.update(at=0.0, report=None)
        return jsonify({"ok": True, "day": body.get("day"), "reading": rec})

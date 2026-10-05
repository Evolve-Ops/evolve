"""HTTP routes for the Evolve overhead ledger (the Cost page's overhead panel).

GET  /api/analytics/evolve-overhead            per-bot + pod overhead, rolling 7 days
POST /api/evolve-overhead/<bot_id>/resume      resume Evolve's machinery, count from now

Thin by design: the numbers, the breaker card and the resume all live in
``analyzer/evolve_overhead.py``; this module re-shapes them for the page and
re-derives nothing. The "reactivate" here is EVOLVE's — it resumes Evolve's
routing/judge/summary calls for one bot. It is not the bot's cost-breaker
reactivation (``/api/breakers/reset``) and touches no cost breaker.

Registered from ``register_cost_measures_routes`` rather than ``server.py``,
which is under a no-growth cap.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_evolve_overhead")

#: A ledger file younger than this is served as-is; older (or absent) is
#: recomputed. The breaker runner refreshes it every 30 minutes, so this only
#: bites when the runner is not running.
_STALE_AFTER_SECONDS = 3600
_memo: dict[str, Any] = {"at": 0.0, "ledger": None}


def _import_analyzer(mod: str):
    import importlib
    return importlib.import_module(mod)


def register_evolve_overhead_routes(app: Flask, network_path: Path) -> None:
    def _shared() -> Path:
        from evolve_config import get_shared_dir  # type: ignore[import]

        return get_shared_dir(load_network(network_path))

    @app.get("/api/analytics/evolve-overhead")
    def api_analytics_evolve_overhead():
        try:
            eo = _import_analyzer("evolve_overhead")
            sc = _import_analyzer("spend_caps")
        except Exception as exc:  # analyzer package missing — say so
            return jsonify({"error": f"evolve_overhead unavailable: {exc}"}), 500
        network = load_network(network_path)
        shared = _shared()
        cfg = eo.OverheadConfig.from_network(network)
        bots = eo.bot_ids_of(network)
        ledger = eo.load_ledger(shared)
        force = request.args.get("refresh") == "1"
        try:
            age = time.time() - eo.ledger_path(shared).stat().st_mtime
        except OSError:
            age = float("inf")
        if force or ledger is None or age > _STALE_AFTER_SECONDS:
            if not force and _memo["ledger"] is not None and time.time() - _memo["at"] < 300:
                ledger = _memo["ledger"]
            else:
                try:
                    ledger = eo.build_ledger(shared, bots, cfg=cfg)
                    _memo.update(at=time.time(), ledger=ledger)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("evolve-overhead: ledger build failed: %s", exc)
                    return jsonify({"error": f"could not build the overhead ledger: {exc}"}), 500
        out: dict[str, Any] = {}
        for bot in bots:
            entry = (ledger.get("bots") or {}).get(bot)
            out[bot] = {
                "measured": entry is not None,
                "d1": (entry or {}).get("d1"),
                "d7": (entry or {}).get("d7"),
                "days": (entry or {}).get("days") or [],
                "footprint_tokens": (entry or {}).get("footprint_tokens"),
                "card": eo.card_for(shared, bot),
                "attribution": sc.read_attribution_snapshot(shared, bot),
            }
        return jsonify({
            "generated_at": ledger.get("generated_at"),
            "config": cfg.as_dict(),
            "bots": out,
            "pod": ledger.get("pod"),
            "unreadable_bots": ledger.get("unreadable_bots") or [],
        })

    @app.post("/api/evolve-overhead/<bot_id>/resume")
    def api_evolve_overhead_resume(bot_id: str):
        try:
            eo = _import_analyzer("evolve_overhead")
        except Exception as exc:
            return jsonify({"ok": False, "error": f"evolve_overhead unavailable: {exc}"}), 500
        if bot_id not in eo.bot_ids_of(load_network(network_path)):
            return jsonify({"ok": False, "error": f"unknown bot {bot_id!r}"}), 404
        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "operator resumed Evolve").strip()
        try:
            accepted = eo.resume(_shared(), bot_id, by="web", reason=reason)
        except Exception as exc:  # noqa: BLE001
            _log.warning("evolve-overhead: resume %s failed: %s", bot_id, exc)
            return jsonify({"ok": False, "error": str(exc)}), 500
        _memo.update(at=0.0, ledger=None)
        return jsonify({"ok": True, "bot_id": bot_id, "accepted": accepted})

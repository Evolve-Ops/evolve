"""HTTP routes for the host-health surface.

GET  /api/host-health           live CPU/memory/disk/load/uptime snapshot
POST /api/host-health/reclaim   privileged server-side disk reclaim

Extracted from server.py to keep it under its no-growth cap.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import DEFAULT_SHARED_DIR, load_network
from .http_errors import error_response


def _register_host_health_routes(app: Flask, network_path: Path | None = None) -> None:
    """Register /api/host-health for live CPU/memory/disk/load/uptime.

    GET /api/host-health → snapshot of the machine the admin server runs on.
    Side effect (Phase 5 of the alerts/signal-store consolidation):
    crit-tier metrics mirror into the Signal store.
    """
    from ..host_health import collect_host_health, emit_signals_from_snapshot

    def _shared_dir() -> Path:
        if network_path is None:
            return DEFAULT_SHARED_DIR
        return Path(load_network(network_path).get("sharedDir", str(DEFAULT_SHARED_DIR)))

    @app.get("/api/host-health")
    def api_host_health() -> Response:
        try:
            snapshot = collect_host_health()
            try:
                emit_signals_from_snapshot(snapshot, _shared_dir())
            except Exception:
                pass
            return jsonify(snapshot)
        except Exception as e:
            return error_response(e, ok=False)

    @app.post("/api/host-health/reclaim")
    def api_host_health_reclaim() -> Response | tuple[Response, int]:
        # Privileged server-side reclaim; safety gate + §3.2 grants in disk_reclaim_apply.
        from ..disk_reclaim_apply import handle_reclaim_request
        try:
            body, status = handle_reclaim_request(request.get_json(silent=True) or {})
            return jsonify(body), status
        except Exception as e:
            return error_response(e, ok=False)

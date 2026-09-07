"""Flask routes for the Remediation execution API.

Two endpoints today:

  POST /api/admin/remediation/execute
       body: {kind, params, signal_id?, actor?}
       returns: {job_id, status: "queued"}

  GET  /api/admin/remediation/job/<job_id>
       returns: full Job.to_dict() OR {error: "not found"} 404

Both are mounted under ``/api/admin/`` to match the existing admin-only
endpoints in server.py. Auth is presumed (admin UI gates access at the
network/session layer).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, request

from .dispatch import dispatch_async
from .handlers import HANDLERS
from .jobs import create_job, iter_recent_jobs, load_job


def register_routes(app: Flask, shared_dir_getter: Callable[[], Path]) -> None:
    """Register the remediation endpoints on ``app``.

    ``shared_dir_getter`` is a zero-arg callable that returns the current
    pod shared_dir Path — passed instead of a fixed path so tests can
    point it at tmp_path without re-wiring the app.
    """

    @app.post("/api/admin/remediation/execute")
    def remediation_execute() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        kind = body.get("kind")
        params = body.get("params") or {}
        signal_id = body.get("signal_id")
        actor = body.get("actor")
        if not isinstance(kind, str) or not kind:
            return jsonify({"error": "missing or invalid 'kind'"}), 400
        if kind not in HANDLERS:
            return jsonify({
                "error": f"unknown kind {kind!r}",
                "available": sorted(HANDLERS),
            }), 400
        if not isinstance(params, dict):
            return jsonify({"error": "'params' must be an object"}), 400
        shared_dir = shared_dir_getter()
        job = create_job(
            shared_dir, kind, params,
            signal_id=signal_id if isinstance(signal_id, str) else None,
            actor=actor if isinstance(actor, str) else None,
        )
        dispatch_async(job, shared_dir)
        return jsonify({"job_id": job.id, "status": job.status}), 202

    @app.get("/api/admin/remediation/job/<job_id>")
    def remediation_job(job_id: str) -> Any:  # type: ignore[no-redef]
        shared_dir = shared_dir_getter()
        job = load_job(shared_dir, job_id)
        if job is None:
            return jsonify({"error": "job not found", "job_id": job_id}), 404
        return jsonify(job.to_dict())

    @app.get("/api/admin/remediation/jobs")
    def remediation_jobs_list() -> Any:  # type: ignore[no-redef]
        """Recent jobs (newest first, capped at 50). Convenience endpoint for
        a future ops view showing what's been run; the UI in PR-2 will key
        off individual signals' embedded job_ids rather than this list."""
        shared_dir = shared_dir_getter()
        jobs = iter_recent_jobs(shared_dir)
        return jsonify({"jobs": [j.to_dict() for j in jobs]})

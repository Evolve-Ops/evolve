"""
oauth_orchestrator.py — Compatibility shim for V2-4 callers.

The OAuth orchestration logic has moved to ``evolve_admin.oauth`` in V2.1-1.
This file re-exports everything callers need so that existing imports and the
13 V2-4 tests continue to work unchanged.

Callers should migrate to importing directly from ``evolve_admin.oauth``, but
there is no urgency — this shim is not scheduled for removal.

New code should import from:
    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites
    from evolve_admin.oauth.sweeper import start_sweeper, check_awaiting_oauth_jobs

The job-lifecycle helpers (start_oauth_wait, get_status, cancel_job,
sweep_awaiting_oauth_jobs) live here because they operate on forge jobs and
are tightly coupled to ``applications/forge_jobs.py``.  They are not part
of the provider-registry abstraction and are not re-routed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ── Re-exports from new oauth/ module ────────────────────────────────────────

from ..oauth.orchestrator import evaluate_install_prerequisites  # noqa: F401
from ..oauth.sweeper import (  # noqa: F401
    OAUTH_ABANDON_MINUTES,
    SWEEPER_INTERVAL_SECONDS,
    check_awaiting_oauth_jobs as sweep_awaiting_oauth_jobs,
    start_sweeper,
)

# ── Job lifecycle helpers — remain here (forge_jobs coupling) ─────────────────
# These are not part of the provider abstraction.  The gallery_routes.py install
# handler and the test suite call them by name from this module.


def start_oauth_wait(
    job_id: str,
    missing: list[dict],
    shared_dir: Path,
) -> None:
    """Transition a forge job to ``awaiting_oauth`` status.

    Called by the gallery install handler immediately after creating the job,
    when prerequisites are not satisfied.  Stores missing-integration metadata
    on the job's context_snapshot so the sweeper and UI can read it without
    re-running the check.
    """
    from .forge_jobs import load_job, save_job
    from .ids import now_iso

    job = load_job(job_id, shared_dir)
    if job is None:
        log.error("oauth_orchestrator.start_oauth_wait: job %s not found", job_id)
        return

    job.status = "awaiting_oauth"
    job.context_snapshot["oauth_missing"] = missing
    job.context_snapshot["oauth_wait_started"] = now_iso()
    save_job(job, shared_dir)
    log.info("oauth_orchestrator: job %s → awaiting_oauth (%d missing)", job_id, len(missing))


def get_status(job_id: str, shared_dir: Path) -> "dict | None":
    """Return a UI-friendly status dict for a forge job in awaiting_oauth state.

    Returns None if the job is not found or not in awaiting_oauth state.
    """
    from .forge_jobs import load_job

    job = load_job(job_id, shared_dir)
    if job is None or job.status != "awaiting_oauth":
        return None

    missing = job.context_snapshot.get("oauth_missing", [])
    return {
        "ok": True,
        "status": "awaiting_oauth",
        "job_id": job_id,
        "bot_id": job.bot_id,
        "missing": [
            {
                "integration": item.get("display_name", item.get("integration_id", "")),
                "reason": item.get("reason", ""),
                "action_url": item.get("action_url"),
                "action_label": item.get("action_label", "Set up integration"),
            }
            for item in missing
        ],
        "next": (
            "After you complete the integration setup, this install will continue "
            "automatically (usually within 30 seconds)."
        ),
    }


def cancel_job(job_id: str, shared_dir: Path, reason: str = "operator_cancelled") -> None:
    """Cancel an awaiting_oauth job, transitioning it to failed."""
    from .forge_jobs import load_job, save_job
    from .ids import now_iso

    job = load_job(job_id, shared_dir)
    if job is None:
        return

    if job.status != "awaiting_oauth":
        log.warning(
            "oauth_orchestrator.cancel_job: job %s is in state %r, not awaiting_oauth",
            job_id, job.status,
        )
        return

    job.status = "failed"
    job.context_snapshot["oauth_cancel_reason"] = reason
    job.last_updated = now_iso()
    save_job(job, shared_dir)
    log.info("oauth_orchestrator: job %s cancelled: %s", job_id, reason)

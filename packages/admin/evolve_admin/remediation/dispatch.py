"""Background-thread dispatch for remediation jobs.

The ``POST /api/admin/remediation/execute`` endpoint creates a Job
(status=queued, persisted) and calls ``dispatch_async``. This module
spawns a daemon thread that runs the handler and updates the persisted
job record through its lifecycle. The endpoint returns immediately with
the job_id; the UI polls ``GET /api/admin/remediation/job/<id>`` for
status.

Persisted jobs survive admin-ui restarts. A job left in ``running``
when the daemon restarts is *not* automatically reaped — the operator
sees it stuck and can re-trigger. Auto-reaping requires a separate
reaper daemon and isn't in PR-1's scope.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

from evolve_util import now_iso_offset as _utc_now_iso

from .handlers import get_handler
from .jobs import Job, load_job, save_job


def _run_job(job_id: str, shared_dir: Path) -> None:
    """Thread body — re-reads the job from disk so we don't trust the
    caller's in-memory copy (the persisted record is the source of truth).
    """
    job = load_job(shared_dir, job_id)
    if job is None:
        return
    if job.status != "queued":
        # Idempotency guard: someone (or a restart-race) already moved this
        # job. Bail without touching the record.
        return

    job.status = "running"
    job.started_at = _utc_now_iso()
    save_job(job, shared_dir)

    try:
        handler = get_handler(job.kind)
        output = handler(dict(job.params), shared_dir)
        job.output = output if isinstance(output, dict) else {"value": output}
        job.status = "succeeded"
    except Exception as exc:  # capture anything the handler raises
        job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        job.status = "failed"
    finally:
        job.finished_at = _utc_now_iso()
        save_job(job, shared_dir)


def dispatch_async(job: Job, shared_dir: Path) -> threading.Thread:
    """Kick off the job in a daemon thread and return the Thread handle.

    Returns the Thread so tests can ``join()`` deterministically; the
    server endpoint discards the handle.
    """
    t = threading.Thread(
        target=_run_job,
        args=(job.id, shared_dir),
        name=f"remediation-{job.kind}-{job.id[:8]}",
        daemon=True,
    )
    t.start()
    return t

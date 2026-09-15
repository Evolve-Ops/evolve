"""arbiter.appliers._forge_kickoff — the shared forge-dispatch tail.

Two appliers hand long-running work to the forge and return immediately:
``build_app`` (generate a new app) and ``install_app`` (install a gallery
package). Everything after "the ForgeJob exists" is identical for both —
run the pipeline with the operator's Act standing in for the forge approval
gate, and, if the runner thread dies before forge records a verdict, mark
the job failed so the sweep can still close the proposal out.

That tail lives here rather than in either applier because the failure it
guards against is silent: a job whose dispatch thread crashed keeps a
non-terminal status forever, and ``arbiter.forge_sweep`` leaves the proposal
in ``applied/`` waiting for a verdict that will never come. One copy, so a
fix to that reasoning cannot land in one applier and miss the other.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def mark_job_failed_after_crash(
    shared_dir: Path, job_id: str, exc_text: str
) -> None:
    """Mark a forge job failed after its dispatch thread crashed.

    Without this, a job that died mid-step keeps its in-flight status
    forever — ``forge_sweep`` watches the job and would never transition
    the proposal out of ``applied``. Marking the job ``failed`` with the
    exception text in the last running step's ``detail`` lets the next
    sweep promote the proposal to ``failed_flagged``.

    Best-effort: if forge_jobs isn't importable or the job has already
    reached a terminal state, this no-ops.
    """
    try:
        from evolve_admin.applications import forge_jobs as _fj  # type: ignore
    except ImportError:
        return

    try:
        job = _fj.load_job(job_id, shared_dir)
    except Exception:
        return
    if job is None:
        return
    if job.status in ("complete", "failed", "rejected"):
        return

    target_step = None
    for step in job.steps:
        if step.status == "running":
            target_step = step
            break
    if target_step is None and job.steps:
        target_step = job.steps[max(0, job.current_step - 1)] if (
            0 <= job.current_step - 1 < len(job.steps)
        ) else job.steps[-1]

    detail = f"runner crashed: {exc_text}"
    try:
        if target_step is not None:
            _fj.mark_step_failed(job, target_step.num, detail, shared_dir)
        else:
            job.status = "failed"
            _fj.save_job(job, shared_dir)
    except Exception:
        # Last resort: try to flip the job-level status so forge_sweep
        # at least sees a terminal state.
        try:
            job.status = "failed"
            _fj.save_job(job, shared_dir)
        except Exception as exc:
            # Nothing further to try — but say so, because the visible
            # consequence is a proposal that sits in applied/ forever.
            logger.warning(
                "forge job %s could not be marked failed (%s); its proposal "
                "will stay in applied/ until the job file is fixed",
                job_id,
                exc,
            )


def run_forge_job_kickoff(
    shared_dir: Path,
    job_id: str,
    bot_id: str,
    *,
    auto_approve_actor: str,
    log_label: str,
) -> None:
    """Run the forge pipeline for ``job_id``, auto-approving the cost gate.

    ``auto_approve_actor`` skips forge's operator gate because the operator
    already approved by clicking Act on the proposal; it names which applier
    did so, so the forge job records who stood in for the gate.

    ``forge_engine`` is imported lazily so the appliers stay importable in
    environments without the forge deps (e.g. trimmed analyzer test envs) —
    and an unimportable engine marks the job failed rather than leaving it
    queued forever.
    """
    try:
        from evolve_admin.applications import forge_engine as _fe  # type: ignore
    except ImportError as exc:
        logger.warning(
            "%s: forge_engine import failed (%s); job %s left in queued state",
            log_label,
            exc,
            job_id,
        )
        mark_job_failed_after_crash(
            shared_dir, job_id, f"forge_engine import failed: {exc}"
        )
        return

    try:
        _fe.run_forge_job(
            job_id=job_id,
            shared_dir=shared_dir,
            bot_id=bot_id,
            auto_approve_actor=auto_approve_actor,
        )
    except Exception as exc:  # noqa: BLE001 — forge errors surface via job status
        logger.warning("%s: forge run raised for job %s: %s", log_label, job_id, exc)
        mark_job_failed_after_crash(shared_dir, job_id, str(exc))

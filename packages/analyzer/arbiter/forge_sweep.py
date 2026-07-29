"""arbiter.forge_sweep — Close out applied BuildApp proposals from forge state.

After a BuildApp proposal is applied, its applier kicks off a forge job
asynchronously and returns immediately with the proposal in the
``applied`` status. Without this sweep, the proposal would sit in
``applied/`` forever (forge has no claim to verify, so the verify daemon
ignores it).

This sweep runs every cycle (called from generator_runner). For each
applied BuildApp proposal:

  1. Look up the forge job_id stored in ``provenance.signals["_apply_details"]``
     (set by ``arbiter.apply`` after successful apply).
  2. Load the ForgeJob.
  3. Read its status:
       - ``complete`` → transition proposal to ``succeeded``
       - ``failed`` / ``rejected`` → transition to ``failed_flagged``
       - anything else (queued / running / awaiting_approval / approved)
         → leave the proposal alone; forge is still in flight.

Job id missing or job file unreadable is logged but not treated as
failure — we'd rather leave the proposal in applied than wrongly mark
it failed.

Test seam: a fake ``job_loader`` callable can be injected so unit tests
don't need the forge_jobs module loaded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from arbiter.state_machine import IllegalTransitionError, transition
from arbiter.store import (
    find_proposal,
    iter_proposals,
    move_proposal,
)
from schema.proposal import BuildApp


logger = logging.getLogger(__name__)


# Forge statuses that indicate the build run has resolved one way or
# another. The daemon won't touch the proposal for any other status.
_FORGE_TERMINAL_OK = "complete"
_FORGE_TERMINAL_FAIL = frozenset({"failed", "rejected"})


JobLoader = Callable[[str, Path], Any]
"""Signature: (job_id, shared_dir) -> ForgeJob | None."""


# Signature: (shared_dir) -> list[str] of rejected job_ids
StaleSweeper = Callable[[Path], list[str]]


def _default_job_loader(job_id: str, shared_dir: Path):
    """Production loader. Lazy import so analyzer test envs that don't
    have the admin package installed can still import this module."""
    try:
        from evolve_admin.applications.forge_jobs import load_job  # type: ignore
    except ImportError as exc:
        logger.warning("forge_sweep: forge_jobs unavailable (%s)", exc)
        return None
    return load_job(job_id, shared_dir)


def _default_stale_sweeper(shared_dir: Path) -> list[str]:
    """Production stale-sweep: auto-reject forge jobs that have sat in
    awaiting_approval past the configured timeout.

    Lazy import for the same reason as ``_default_job_loader``: analyzer
    test envs may not have the admin package installed."""
    try:
        from evolve_admin.applications.forge_jobs import (  # type: ignore
            auto_reject_stale_awaiting_approval,
        )
    except ImportError as exc:
        logger.warning("forge_sweep: forge_jobs.auto_reject unavailable (%s)", exc)
        return []
    return auto_reject_stale_awaiting_approval(shared_dir)


def sweep(
    shared_dir: Path,
    *,
    job_loader: JobLoader | None = None,
    log_fn: Callable[[str], None] | None = None,
    stale_sweeper: StaleSweeper | None = None,
) -> dict[str, int]:
    """Run one sweep over applied BuildApp proposals + stale forge jobs.

    Returns a counter dict (``{succeeded, failed, in_flight, missing,
    auto_rejected}``) so callers can log a single-line summary per cycle.
    """
    log = log_fn or logger.info
    loader = job_loader or _default_job_loader
    stale = stale_sweeper or _default_stale_sweeper
    counts = {
        "succeeded": 0,
        "failed": 0,
        "in_flight": 0,
        "missing": 0,
        "auto_rejected": 0,
    }

    # Stale awaiting_approval cleanup runs before the proposal pass so a
    # job we just auto-rejected is visible to the proposal-side reconcile
    # in the same cycle (forge_sweep promotes the BuildApp proposal to
    # failed_flagged for rejected jobs).
    try:
        rejected = stale(shared_dir)
    except Exception as exc:  # noqa: BLE001 — sweep must not break the cycle
        log(f"[forge_sweep] stale-sweep failed: {exc}")
        rejected = []
    if rejected:
        counts["auto_rejected"] = len(rejected)
        log(
            f"[forge_sweep] auto-rejected {len(rejected)} stale "
            f"awaiting_approval job(s): {', '.join(rejected)}"
        )

    # Orphan-step reconciliation. Multi-cycle retries (or a dispatch
    # thread that died mid-step) can leave a terminal-status job with
    # one or more steps still marked ``running`` and no ``finished_at``.
    # The clone-on-retry path (2026-06-05) closes those out at the
    # moment the operator clicks retry, but this sweep is the safety
    # net for pods running an older admin server, externally-edited
    # job files, or daemon restarts that interrupted the dispatcher.
    # Idempotent — a job whose orphans are already closed is a no-op.
    counts["orphan_steps_reconciled"] = 0
    counts["orphan_jobs_reconciled"] = 0
    try:
        from evolve_admin.applications.forge_jobs import sweep_orphan_steps  # type: ignore
    except ImportError as exc:
        log(f"[forge_sweep] orphan-step sweep unavailable: {exc}")
    else:
        try:
            orphan = sweep_orphan_steps(shared_dir)
            if orphan.get("steps_reconciled"):
                counts["orphan_steps_reconciled"] = orphan["steps_reconciled"]
                counts["orphan_jobs_reconciled"] = orphan["jobs_reconciled"]
                log(
                    f"[forge_sweep] reconciled "
                    f"{orphan['steps_reconciled']} orphan step(s) across "
                    f"{orphan['jobs_reconciled']} job(s)"
                )
        except Exception as exc:  # noqa: BLE001 — sweep must not break the cycle
            log(f"[forge_sweep] orphan-step sweep failed: {exc}")

    # Disk-prune terminal jobs older than 30 days. Forge jobs are operational
    # logs, not compliance audit — the UI window is 7 days, so keeping 30
    # days on disk leaves operators ~3 weeks of headroom to dig into a recent
    # failure that scrolled off the UI.
    counts["pruned"] = 0
    try:
        from evolve_admin.applications.forge_jobs import prune_old_jobs  # type: ignore
    except ImportError as exc:
        log(f"[forge_sweep] prune unavailable (admin pkg not on path): {exc}")
    else:
        try:
            pruned_count = prune_old_jobs(shared_dir, days=30)
            if pruned_count:
                counts["pruned"] = pruned_count
                log(f"[forge_sweep] pruned {pruned_count} terminal job(s) >30 days old")
        except Exception as exc:  # noqa: BLE001 — sweep must not break the cycle
            log(f"[forge_sweep] prune failed: {exc}")

    for proposal in list(iter_proposals(shared_dir, subdirs=("applied",))):
        if not isinstance(proposal.action, BuildApp):
            continue

        details = proposal.provenance.signals.get("_apply_details") or {}
        job_id = details.get("job_id") if isinstance(details, dict) else None
        if not job_id:
            counts["missing"] += 1
            log(
                f"[forge_sweep] proposal {proposal.id} (BuildApp) has no "
                f"job_id in apply details; cannot reconcile"
            )
            continue

        try:
            job = loader(job_id, shared_dir)
        except Exception as exc:  # noqa: BLE001
            log(f"[forge_sweep] failed to load forge job {job_id}: {exc}")
            counts["missing"] += 1
            continue

        if job is None:
            counts["missing"] += 1
            log(
                f"[forge_sweep] forge job {job_id} not found; "
                f"proposal {proposal.id} left in applied/"
            )
            continue

        status = getattr(job, "status", None)
        if status == _FORGE_TERMINAL_OK:
            target_status = "succeeded"
            reason = f"forge job {job_id} completed"
        elif status in _FORGE_TERMINAL_FAIL:
            target_status = "failed_flagged"
            reason = f"forge job {job_id} ended in status {status!r}"
        else:
            counts["in_flight"] += 1
            continue

        located = find_proposal(shared_dir, proposal.id)
        if located is None:
            counts["missing"] += 1
            continue
        _, _, src_subdir = located

        try:
            transition(
                proposal,
                target_status,
                actor="forge_sweep",
                reason=reason,
            )
        except IllegalTransitionError as exc:
            log(
                f"[forge_sweep] illegal transition for {proposal.id} "
                f"({proposal.status} → {target_status}): {exc}"
            )
            continue

        try:
            move_proposal(proposal, shared_dir, from_subdir=src_subdir)
        except OSError as exc:
            log(
                f"[forge_sweep] failed to archive {proposal.id}: {exc}"
            )
            continue

        # Bump the generator's TrackRecord for this terminal transition.
        # Best-effort — bookkeeping must never block the sweep lifecycle,
        # but log at WARNING so silent regressions show up in the runner
        # tail.
        try:
            from arbiter.track_record import bump_for_status_transition

            bump_for_status_transition(
                shared_dir, proposal, to_status=target_status
            )
        except Exception as exc:  # noqa: BLE001
            log(
                f"[forge_sweep] track_record bump failed for {proposal.id} "
                f"(status={target_status}): {exc}"
            )

        if target_status == "succeeded":
            counts["succeeded"] += 1
        else:
            counts["failed"] += 1

    return counts

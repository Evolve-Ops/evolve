"""
evolve_admin.oauth.sweeper — Background sweeper for awaiting_oauth forge jobs.

Extracted from ``applications/oauth_orchestrator.py`` (V2-4).  The sweeper
logic is now generic over provider — it delegates prereq re-checks to
``orchestrator.evaluate_install_prerequisites`` rather than containing any
GOG-specific code.

The daemon-thread infrastructure (``start_sweeper``) stays here and is called
once by ``gallery_routes.py`` at server registration time.  ``forge_engine.py``
does not need to change — the 30-second timer lives here, not there.

All V2-4 injectable-reader arguments are preserved for backward compatibility
with the existing 13 tests.  They are forwarded to ``evaluate_install_prerequisites``
unchanged.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# How long to wait before declaring a job abandoned.  Matches V2-4 constant.
OAUTH_ABANDON_MINUTES = 30
SWEEPER_INTERVAL_SECONDS = 30

_sweeper_started = False
_sweeper_lock = threading.Lock()


def check_awaiting_oauth_jobs(
    shared_dir: Path,
    *,
    dispatch_fn=None,
    now: "datetime | None" = None,
    # Injectable readers forwarded to evaluate_install_prerequisites (V2-4 compat)
    read_plugin_enabled: "Callable[[str], bool | None] | None" = None,
    read_oauth_profile: "Callable[[str], dict | None] | None" = None,
    read_oauth_client_configured: "Callable[[], bool] | None" = None,
) -> list[str]:
    """Check all awaiting_oauth forge jobs and resume or abandon them.

    This is the "Option A" sweeper from the spec: runs every ~30 seconds,
    survives admin-UI restarts, no event bus needed.

    For each awaiting_oauth job:
      - If prereqs are now satisfied → transition to "queued" and dispatch.
      - If waiting > OAUTH_ABANDON_MINUTES → transition to "failed" with
        reason ``oauth_abandoned``.
      - Otherwise → leave as awaiting_oauth.

    Args:
        shared_dir:   Shared evolve data directory.
        dispatch_fn:  Optional callable(job_id, bot_id) to dispatch the resumed
                      job. Defaults to the background-thread forge dispatch.
        now:          Override current UTC time (for testing).
        read_*:       Injectable reader callables for testing; forwarded to
                      evaluate_install_prerequisites().

    Returns:
        List of job_ids that were resumed (transitioned to queued).
    """
    from ..applications.forge_jobs import list_active_jobs, save_job
    from ..applications.ids import now_iso as _now_iso
    from .orchestrator import evaluate_install_prerequisites
    from .providers import find_provider

    cutoff_now = now or datetime.now(timezone.utc)
    abandon_cutoff = cutoff_now - timedelta(minutes=OAUTH_ABANDON_MINUTES)

    resumed_ids: list[str] = []

    for job in list_active_jobs(shared_dir):
        if job.status != "awaiting_oauth":
            continue

        # Parse the wait-start timestamp
        wait_started_str = job.context_snapshot.get("oauth_wait_started", "")
        wait_started = _parse_iso_utc(wait_started_str)

        # ── Abandon check ─────────────────────────────────────────────────────
        if wait_started is not None and wait_started <= abandon_cutoff:
            job.status = "failed"
            job.context_snapshot["oauth_abandon_reason"] = "oauth_abandoned"
            job.last_updated = _now_iso()
            save_job(job, shared_dir)
            log.info(
                "sweeper: job %s abandoned after %d min with no OAuth",
                job.job_id, OAUTH_ABANDON_MINUTES,
            )
            continue

        # ── Prereq re-check ───────────────────────────────────────────────────
        missing_snapshot = job.context_snapshot.get("oauth_missing", [])
        if not missing_snapshot:
            # Nothing left to check — resume immediately
            _resume_job(job, shared_dir, dispatch_fn=dispatch_fn)
            resumed_ids.append(job.job_id)
            continue

        # Re-evaluate only the integrations in the missing snapshot — with the
        # FULL manifest requirement entries when start_oauth_wait stored them.
        # A bare {"id": ...} reconstruction drops check_path / alternatives[] /
        # setup_doc, so a requirement the operator satisfies via a declared
        # alternative (e.g. path-C service-account DwD) during the wait window
        # would never be detected and the job would abandon at 30 min. Jobs
        # created before oauth_requirements existed fall back to bare ids.
        stored_reqs = job.context_snapshot.get("oauth_requirements") or {}
        stored_entries = [
            entry
            for entry in (stored_reqs.get("integrations") or [])
            if isinstance(entry, dict) and entry.get("id")
        ]
        # Missing items and manifest entries live in different id namespaces:
        # a provider-built item carries the provider's SKILL id (e.g.
        # "calendar"), while the manifest entry that routed to it may use an
        # alias (e.g. "google_calendar"). Key each stored entry under every id
        # its provider answers to so the skill-id item still finds its full
        # manifest entry; manifest ids are written last so they win collisions.
        full_by_id: "dict[str, dict]" = {}
        for entry in stored_entries:
            provider = find_provider(entry["id"])
            if provider is not None:
                for alias in {provider.skill_id, *provider.integration_ids}:
                    full_by_id.setdefault(alias, entry)
        for entry in stored_entries:
            full_by_id[entry["id"]] = entry
        reqs = {
            "integrations": [
                full_by_id.get(item["integration_id"], {"id": item["integration_id"]})
                for item in missing_snapshot
            ]
        }
        try:
            result = evaluate_install_prerequisites(
                job.bot_id, reqs,
                shared_dir=shared_dir,
                read_plugin_enabled=read_plugin_enabled,
                read_oauth_profile=read_oauth_profile,
                read_oauth_client_configured=read_oauth_client_configured,
            )
        except Exception as exc:
            log.warning("sweeper: prereq re-check failed for %s: %s", job.job_id, exc)
            continue

        if result["satisfied"]:
            _resume_job(job, shared_dir, dispatch_fn=dispatch_fn)
            resumed_ids.append(job.job_id)

    return resumed_ids


def _resume_job(job, shared_dir: Path, *, dispatch_fn=None) -> None:
    """Transition an awaiting_oauth job to queued and dispatch it."""
    from ..applications.forge_jobs import save_job
    from ..applications.ids import now_iso as _now_iso

    job.status = "queued"
    job.context_snapshot["oauth_resume_reason"] = "oauth_satisfied"
    job.last_updated = _now_iso()
    save_job(job, shared_dir)
    log.info(
        "sweeper: job %s → queued (oauth_satisfied); dispatching",
        job.job_id,
    )

    if dispatch_fn is not None:
        try:
            dispatch_fn(job.job_id, job.bot_id)
        except Exception as exc:
            log.error("sweeper: dispatch_fn failed for job %s: %s", job.job_id, exc)
    else:
        _default_dispatch(job.job_id, job.bot_id, shared_dir)


def _default_dispatch(job_id: str, bot_id: str, shared_dir: Path) -> None:
    """Default dispatch: run forge_engine in a background daemon thread."""
    from ..applications import forge_engine as _fe

    def _run() -> None:
        try:
            _fe.run_forge_job(job_id=job_id, shared_dir=shared_dir, bot_id=bot_id)
        except Exception as exc:
            log.error("sweeper: forge dispatch thread error for %s: %s", job_id, exc)

    t = threading.Thread(target=_run, daemon=True, name=f"forge-oauth-resume-{job_id}")
    t.start()


def _parse_iso_utc(ts: str) -> "datetime | None":
    """Parse a now_iso()-formatted UTC timestamp."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def start_sweeper(shared_dir: Path) -> None:
    """Start the background sweeper thread (idempotent).

    Runs ``check_awaiting_oauth_jobs`` every 30 seconds in a daemon thread.
    Called once by ``gallery_routes.py`` at server registration time.
    """
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    def _sweep_loop() -> None:
        import time
        log.info("sweeper: started (interval=%ds)", SWEEPER_INTERVAL_SECONDS)
        while True:
            time.sleep(SWEEPER_INTERVAL_SECONDS)
            try:
                resumed = check_awaiting_oauth_jobs(shared_dir)
                if resumed:
                    log.info(
                        "sweeper: resumed %d job(s): %s",
                        len(resumed), resumed,
                    )
            except Exception as exc:
                log.warning("sweeper: error: %s", exc)

    t = threading.Thread(target=_sweep_loop, daemon=True, name="oauth-sweeper")
    t.start()
    log.info("sweeper: thread started")

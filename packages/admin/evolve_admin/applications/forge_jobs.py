"""
forge_jobs.py — Forge job lifecycle management for the App Gallery & Unified Forge Engine.

A forge job is a multi-step tracked operation (install, improvement, update, hotfix)
executed by the forge engine.  Jobs are persisted as individual JSON files:

    Active/pending:  {shared_dir}/forge/jobs/{job_id}.json
    Completed:       {shared_dir}/forge/jobs/completed/{job_id}.json

Job types:
    install     — forge run #0, installs a gallery app onto a bot
    improvement — RSI-driven improvement cycle (local or detector-triggered)
    update      — pulls and applies a new gallery version
    hotfix      — targeted single-fix run

Job states:
    queued | running | awaiting_approval | approved | rejected | failed | complete

Step states:
    pending | running | done | failed | waiting | cancelled

``cancelled`` is set by :func:`reconcile_orphan_steps` on a step that was
mid-flight (``started_at`` set, ``finished_at`` unset) when the job
transitioned to a terminal status — typically because a different step
failed in a separate dispatch and left this one orphaned. The janitor
sets ``finished_at`` and writes a short reason into ``detail`` so the
UI can render it truthfully instead of showing the spinner forever.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write

from .ids import new_job_id, run_id_for, now_iso
from .manifest import (
    load_manifest, save_manifest, save_manifest_with_provenance,
    PROVENANCE_FORGE_BUILT,
)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ForgeStep:
    num: int
    label: str
    status: str = "pending"             # pending | running | done | failed | waiting | cancelled
    started_at: str | None = None
    finished_at: str | None = None
    detail: str = ""                    # e.g. "4 issues found", "exit 0"


@dataclass
class ForgeJob:
    job_id: str                         # j- prefixed
    run_id: str                         # r- prefixed (sequential within job, starts at r-00000001)
    job_type: str                       # install | improvement | update | hotfix
    pkg_id: str                         # p- prefixed
    app_id: str                         # manifest slug (bot-local name)
    bot_id: str
    pkg_version_before: str | None      # None for installs
    gallery_version: str | None         # None for non-gallery improvements
    status: str = "queued"             # queued | running | awaiting_approval | approved | rejected | failed | complete
    steps: list[ForgeStep] = field(default_factory=list)
    current_step: int = 0
    created_at: str = ""
    last_updated: str = ""
    # Populated during run
    critique_rounds_done: int = 0
    issues_found: int = 0
    issues_resolved: int = 0
    issues_deferred: int = 0
    test_exit_code: int | None = None
    test_output_summary: str = ""
    shared_file_checks: list[dict] = field(default_factory=list)
    triggered_by_detector: str | None = None  # calibration detector name, or None for manual
    # Context snapshot assembled before the forge run
    context_snapshot: dict = field(default_factory=dict)
    # Rejection metadata. Populated by reject_job when status moves to "rejected"
    # — without these, the Forge Jobs UI had no way to surface why a job was
    # rejected. rejected_by is "forge_sweep" for auto-rejection, "operator" for
    # manual rejection via the API, or a custom actor identifier.
    reject_reason: str = ""
    rejected_by: str = ""
    rejected_at: str = ""
    # Pre-install cost projection (2026-06-03). Populated at job-creation
    # time when the install path computes a projection — captured here so
    # the dispatcher can stamp the forge_sessions annotation with the
    # operator-confirmed subkind, and so the post-completion reconciler
    # has a baseline to compare actual cost against.
    #
    # operator_confirmed True means the operator saw the projected cost
    # band (either inline in the spec review screen or via the
    # gallery install modal) and pressed Approve / Confirm. The
    # downstream effects: forge_sessions annotation carries
    # trigger_subkind="operator_confirmed_install"; cost_events carry
    # the same subkind; spend_alert.load_today_spend exempts those
    # events from the daily_cap_usd computation (when the bot has
    # forge_install_exempt_from_daily_cap=True).
    operator_confirmed: bool = False
    projected_cost_mid_usd: float | None = None
    projected_cost_high_usd: float | None = None
    actual_cost_usd: float | None = None
    # Set True on a job whose dispatch should treat itself as a retry —
    # tells the bot's next build dispatch to wipe ``apps/<app_id>/``
    # before re-building (without this, stale files from a prior attempt
    # at the same app cause sha256 verification failures). The flag is
    # carried into BuildRequest.is_retry by the forge engine (Step 2
    # build dispatch). The clone-on-retry path (clone_for_retry, below)
    # sets this True on every clone so the bot-owned tree is cleaned
    # before the new run starts — the prior_job_id chain shares the
    # bot/app slot even though the job_id is fresh.
    is_retry: bool = False
    # Retry-chain audit linkage (2026-06-05). When an operator retries a
    # failed/cancelled/rejected job we clone it into a NEW job_id rather
    # than mutating the prior in place — this prevents the orphan-step
    # state that the prior in-place reset_to_queued path could produce
    # under concurrent retries or partial failures. The new job carries
    # ``prior_job_id`` pointing at its predecessor; the predecessor gets
    # ``superseded_by_job_id`` written so the Forge Jobs UI can render
    # the chain (older job: "→ retried as j-xxxx"; newer job: "retry of
    # j-yyyy"). See ``clone_for_retry``.
    prior_job_id: str | None = None
    superseded_by_job_id: str | None = None


# ── Step sequence factories ───────────────────────────────────────────────────

def _install_steps() -> list[ForgeStep]:
    """Standard 10-step sequence for install jobs.

    Maps onto the 5-stage UI infographic (Manifest → Build → Critique → Test
    → Ship & Sync) by grouping consecutive steps into stages. Labels reflect
    what bot-driven forge actually does — see _run_bot_dispatch in
    forge_engine.py.
    """
    return [
        ForgeStep(num=1,  label="Manifest: seed from gallery + mark updating"),
        ForgeStep(num=2,  label="Build: bot drafts the code"),
        ForgeStep(num=3,  label="Critique round 1: bot self-reviews"),
        ForgeStep(num=4,  label="Refine round 1: bot fixes critique issues"),
        ForgeStep(num=5,  label="Critique round 2: bot self-reviews"),
        ForgeStep(num=6,  label="Refine round 2: bot fixes critique issues"),
        ForgeStep(num=7,  label="Test: no-op (app-test surface removed 2026-06-08)"),
        ForgeStep(num=8,  label="Ship: mark awaiting operator approval"),
        # Installs auto-ship — no prior version to diff against, the operator's
        # "do I want this app?" decision happened earlier at gallery-install
        # time. The step still runs (state machine requires the
        # awaiting_approval → approved transition) but the label is
        # truthful about there being no real wait. Improvement jobs use
        # the "AWAIT" label below — those genuinely pause for an operator.
        ForgeStep(num=9,  label="Approval (auto-ship for installs)", status="waiting"),
        ForgeStep(num=10, label="Sync: apply manifest + record pkg_version"),
    ]


def _improvement_steps() -> list[ForgeStep]:
    """Standard 10-step sequence for improvement jobs.

    Same stage mapping as install — see _install_steps.
    """
    return [
        ForgeStep(num=1,  label="Manifest: assemble context (RSI, feedback, history)"),
        ForgeStep(num=2,  label="Build: bot drafts the improvement delta"),
        ForgeStep(num=3,  label="Critique round 1: bot self-reviews"),
        ForgeStep(num=4,  label="Refine round 1: bot fixes critique issues"),
        ForgeStep(num=5,  label="Critique round 2: bot self-reviews"),
        ForgeStep(num=6,  label="Refine round 2: bot fixes critique issues"),
        ForgeStep(num=7,  label="Test: no-op (app-test surface removed 2026-06-08)"),
        ForgeStep(num=8,  label="Ship: mark awaiting operator approval"),
        ForgeStep(num=9,  label="AWAIT: operator approval", status="waiting"),
        ForgeStep(num=10, label="Sync: apply manifest + record pkg_version"),
    ]


# ── Storage helpers ───────────────────────────────────────────────────────────

def _active_dir(shared_dir: Path) -> Path:
    return shared_dir / "forge" / "jobs"


def _completed_dir(shared_dir: Path) -> Path:
    return shared_dir / "forge" / "jobs" / "completed"


def _active_path(job_id: str, shared_dir: Path) -> Path:
    return _active_dir(shared_dir) / f"{job_id}.json"


def _completed_path(job_id: str, shared_dir: Path) -> Path:
    return _completed_dir(shared_dir) / f"{job_id}.json"


def _job_to_dict(job: ForgeJob) -> dict:
    """Serialize a ForgeJob to a plain dict, handling nested ForgeStep list."""
    d = asdict(job)
    # asdict already handles nested dataclasses recursively — steps become list[dict]
    return d


def _job_from_dict(data: dict) -> ForgeJob:
    """Deserialize a ForgeJob from a plain dict."""
    steps_raw = data.pop("steps", [])
    steps = []
    step_fields = {f.name for f in dataclasses.fields(ForgeStep)}
    for s in steps_raw:
        if isinstance(s, dict):
            cleaned = {k: v for k, v in s.items() if k in step_fields}
            steps.append(ForgeStep(**cleaned))
    job_fields = {f.name for f in dataclasses.fields(ForgeJob)}
    cleaned_job = {k: v for k, v in data.items() if k in job_fields}
    return ForgeJob(steps=steps, **cleaned_job)


# ── Core CRUD ─────────────────────────────────────────────────────────────────

def save_job(job: ForgeJob, shared_dir: Path) -> None:
    """Atomic write to active jobs dir."""
    job.last_updated = now_iso()
    path = _active_path(job.job_id, shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _job_to_dict(job))


def load_job(job_id: str, shared_dir: Path) -> ForgeJob | None:
    """Load a job from disk.  Checks active dir first, then completed dir."""
    for path in (_active_path(job_id, shared_dir), _completed_path(job_id, shared_dir)):
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return _job_from_dict(data)
            except Exception:
                pass
    return None


def list_active_jobs(shared_dir: Path) -> list[ForgeJob]:
    """Return all jobs in the active dir (queued, running, awaiting_approval)."""
    d = _active_dir(shared_dir)
    if not d.exists():
        return []
    jobs = []
    for p in sorted(d.iterdir()):
        if p.suffix == ".json" and p.is_file():
            try:
                data = json.loads(p.read_text())
                jobs.append(_job_from_dict(data))
            except Exception:
                pass
    return jobs


# Terminal job states that get filtered/pruned by age. ``rejected`` was
# missing from this set previously, so rejected jobs stayed visible in the
# UI forever — surfaced by an operator who still saw week-old rejections
# crowding the Forge Jobs page.
TERMINAL_JOB_STATES: tuple[str, ...] = (
    "complete", "failed", "cancelled", "rejected",
)


def list_recent_jobs(shared_dir: Path, *, days: int = 7) -> list[ForgeJob]:
    """Return active jobs + recent terminal jobs sorted by created_at desc.

    Use this to power the admin "Forge Jobs" page: the UI needs to show both
    in-flight work (queued/running/awaiting_approval, all in the active dir)
    AND the recent terminal outcomes (complete/failed/cancelled/rejected, in
    the completed dir). ``list_active_jobs`` alone hides successful installs —
    they get moved to ``completed/`` and never appear, so the user sees a
    wall of recent failures with no record of the successes that followed.

    Day-based filtering on completed jobs keeps the response bounded even
    when a long-running pod has accumulated thousands of historical jobs.

    Sort order: active jobs first (in-flight work is the priority view), then
    terminal jobs newest-first. Within each group, descending by created_at.
    """
    jobs: list[ForgeJob] = list_active_jobs(shared_dir)

    completed = _completed_dir(shared_dir)
    if completed.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for p in sorted(completed.iterdir()):
            if p.suffix != ".json" or not p.is_file():
                continue
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            # Parse created_at; tolerate missing/malformed and fall back to mtime
            created_raw = data.get("created_at")
            created_dt: datetime | None = None
            if isinstance(created_raw, str):
                try:
                    created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                except ValueError:
                    created_dt = None
            if created_dt is None:
                try:
                    created_dt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
            if created_dt < cutoff:
                continue
            try:
                jobs.append(_job_from_dict(data))
            except Exception:
                pass

    # Sort: active first, then most-recent terminal. Within each group,
    # descending by created_at so the operator sees newest at top.
    def _sort_key(j: ForgeJob) -> tuple[int, str]:
        terminal_bucket = 1 if j.status in TERMINAL_JOB_STATES else 0
        # Reverse string-compare gives desc order for ISO timestamps
        return (terminal_bucket, _negate_iso(j.created_at))

    jobs.sort(key=_sort_key)
    return jobs


def _negate_iso(ts: str) -> str:
    """Helper for descending sort on ISO-8601 strings (lexical comparison)."""
    # Map char-by-char to its inverse so sort-ascending gives desc order
    if not ts:
        return "￿"  # missing timestamps sort last
    return "".join(chr(0xFFFF - ord(c)) for c in ts)


def prune_old_jobs(
    shared_dir: Path,
    *,
    days: int = 30,
    dry_run: bool = False,
) -> int:
    """Delete terminal jobs from disk whose ``created_at`` is older than ``days``.

    Returns the number of files pruned (or that would have been pruned in
    ``dry_run`` mode).

    Forge jobs are operational logs, not compliance audit. 30-day default
    gives operators headroom to review recent failures (the UI window is 7
    days, this is the disk window). Configurable via the caller.

    Active jobs (queued / running / awaiting_approval) live in ``active/``,
    not ``completed/``, and are never touched by this function — only the
    completed/ tree is scanned.

    Idempotent and safe to run periodically. Wired into ``forge_sweep.sweep``
    so the same cron that auto-rejects stale jobs also clears old debris.
    """
    completed = _completed_dir(shared_dir)
    if not completed.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pruned = 0
    for p in completed.iterdir():
        if p.suffix != ".json" or not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        created_raw = data.get("created_at")
        created_dt: datetime | None = None
        if isinstance(created_raw, str):
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created_dt = None
        if created_dt is None:
            try:
                created_dt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if created_dt >= cutoff:
            continue
        if not dry_run:
            try:
                p.unlink()
            except OSError:
                continue
        pruned += 1
    return pruned


def list_jobs_for_app(pkg_id: str, bot_id: str, shared_dir: Path) -> list[ForgeJob]:
    """Return all jobs (active + completed) for a specific app+bot combo."""
    results: list[ForgeJob] = []

    def _load_from_dir(directory: Path) -> None:
        if not directory.exists():
            return
        for p in sorted(directory.iterdir()):
            if p.suffix == ".json" and p.is_file():
                try:
                    data = json.loads(p.read_text())
                    if data.get("pkg_id") == pkg_id and data.get("bot_id") == bot_id:
                        results.append(_job_from_dict(data))
                except Exception:
                    pass

    _load_from_dir(_active_dir(shared_dir))
    _load_from_dir(_completed_dir(shared_dir))
    return results


# ── Job factories ─────────────────────────────────────────────────────────────

def create_install_job(
    pkg_id: str,
    app_id: str,
    bot_id: str,
    gallery_version: str,
    shared_dir: Path,
    *,
    operator_confirmed: bool = False,
    projected_cost_mid_usd: float | None = None,
    projected_cost_high_usd: float | None = None,
) -> ForgeJob:
    """Create and persist a new install job.

    The optional ``operator_confirmed`` / ``projected_cost_*`` kwargs are
    set by the spec_routes approve path and the gallery install path
    once the operator has seen the pre-install cost projection. They
    propagate through to the forge_sessions annotation (so cost_events
    are tagged), the daily-cap exemption (so a confirmed install doesn't
    trip the cap), and the post-completion reconciler (which compares
    actual against projection and emits an overrun Signal when actual
    exceeds projected_cost_high_usd).
    """
    ts = now_iso()
    job = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(1),
        job_type="install",
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        pkg_version_before=None,
        gallery_version=gallery_version,
        steps=_install_steps(),
        created_at=ts,
        last_updated=ts,
        operator_confirmed=operator_confirmed,
        projected_cost_mid_usd=projected_cost_mid_usd,
        projected_cost_high_usd=projected_cost_high_usd,
    )
    save_job(job, shared_dir)
    return job


_RUN_NUMBER_RE = re.compile(r"^r-(\d+)$")


def _max_existing_run_number(pkg_id: str, bot_id: str, shared_dir: Path) -> int:
    """Return the highest run_number across all known jobs for this app+bot, or 0."""
    nums: list[int] = []
    for job in list_jobs_for_app(pkg_id, bot_id, shared_dir):
        m = _RUN_NUMBER_RE.match(job.run_id or "")
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0


def _allocate_next_run_number(pkg_id: str, bot_id: str, shared_dir: Path) -> int:
    """Atomically allocate the next run_number for (pkg_id, bot_id).

    Two concurrent calls race-free thanks to flock on a per-app counter
    file. The counter seeds from the existing max run_number on first use
    so deployments with pre-existing jobs don't collide.
    """
    counter_dir = _active_dir(shared_dir) / "_counters"
    counter_dir.mkdir(parents=True, exist_ok=True)
    safe_pkg = re.sub(r"[^A-Za-z0-9_-]", "_", pkg_id)
    safe_bot = re.sub(r"[^A-Za-z0-9_-]", "_", bot_id)
    counter_path = counter_dir / f"{safe_pkg}__{safe_bot}.counter"

    with open(counter_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            data = fh.read().strip()
            try:
                last = int(data) if data else 0
            except ValueError:
                last = 0
            if last == 0:
                # First use (or corrupt counter) — seed from existing jobs
                # so a counter file appearing in an established deployment
                # doesn't reset the run sequence.
                last = _max_existing_run_number(pkg_id, bot_id, shared_dir)
            next_n = last + 1
            fh.seek(0)
            fh.truncate()
            fh.write(str(next_n))
            fh.flush()
            os.fsync(fh.fileno())
            return next_n
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def create_improvement_job(
    pkg_id: str,
    app_id: str,
    bot_id: str,
    shared_dir: Path,
    triggered_by_detector: str | None = None,
) -> ForgeJob:
    """Create and persist an improvement job."""
    # Atomically allocate the next run_number so concurrent triggers
    # don't both pick the same sequence.
    run_number = _allocate_next_run_number(pkg_id, bot_id, shared_dir)

    ts = now_iso()
    job = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(run_number),
        job_type="improvement",
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        pkg_version_before=None,
        gallery_version=None,
        triggered_by_detector=triggered_by_detector,
        steps=_improvement_steps(),
        created_at=ts,
        last_updated=ts,
    )
    save_job(job, shared_dir)
    return job


# ── Step state transitions ────────────────────────────────────────────────────

def mark_step_running(job: ForgeJob, step_num: int, shared_dir: Path) -> None:
    """Set step status → running, record started_at, set job.current_step."""
    for step in job.steps:
        if step.num == step_num:
            step.status = "running"
            step.started_at = now_iso()
            break
    job.current_step = step_num
    job.status = "running"
    save_job(job, shared_dir)


def mark_step_done(job: ForgeJob, step_num: int, detail: str, shared_dir: Path) -> None:
    """Set step status → done, record finished_at, advance current_step."""
    for step in job.steps:
        if step.num == step_num:
            step.status = "done"
            step.finished_at = now_iso()
            step.detail = detail
            break
    # Advance to the next step number (or stay at step_num if it was the last)
    job.current_step = step_num + 1
    save_job(job, shared_dir)


def mark_step_failed(job: ForgeJob, step_num: int, detail: str, shared_dir: Path) -> None:
    """Set step status → failed, set job.status → failed."""
    for step in job.steps:
        if step.num == step_num:
            step.status = "failed"
            step.finished_at = now_iso()
            step.detail = detail
            break
    job.status = "failed"
    save_job(job, shared_dir)


def reset_to_queued(job: ForgeJob, shared_dir: Path) -> None:
    """Move a failed / cancelled / rejected job back to ``queued`` so it can
    be re-dispatched.

    This is the operator's escape hatch when a forge run died and the job
    file sits there with no way to retry: previously the only path was
    deleting the JSON on disk and re-triggering the install from the
    Gallery (losing all step history). With reset_to_queued, the same
    job_id stays — operators get audit continuity, and the run_id stays
    valid for any external references.

    Concretely:
      - status      → "queued"
      - steps[*]    → "pending" (preserves seed status for the AWAIT step
                      so the UI infographic still looks right)
      - current_step → 0
      - reject_reason / rejected_by / rejected_at → cleared
      - test_exit_code / test_output_summary → cleared
      - issue counts → 0
      - critique_rounds_done → 0
      - timestamps started_at/finished_at on each step → cleared
      - detail on each step → cleared

    What is *preserved* (so retries don't lose context):
      - context_snapshot (the bot's working memory)
      - shared_file_checks
      - triggered_by_detector
      - created_at, last_updated, run_id
      - pkg_id, app_id, bot_id, job_type
      - gallery_version, pkg_version_before
    """
    job.status = "queued"
    job.current_step = 0
    job.reject_reason = ""
    job.rejected_by = ""
    job.rejected_at = ""
    job.test_exit_code = None
    job.test_output_summary = ""
    job.issues_found = 0
    job.issues_resolved = 0
    job.issues_deferred = 0
    job.critique_rounds_done = 0
    job.shared_file_checks = []
    job.actual_cost_usd = None
    # Reset step state but preserve seed "waiting" on the AWAIT step so
    # the UI infographic doesn't show step 9 as plain "pending" — the
    # waiting glyph is intentional for that one row.
    for step in job.steps:
        step.started_at = None
        step.finished_at = None
        step.detail = ""
        if step.label.startswith("AWAIT"):
            step.status = "waiting"
        else:
            step.status = "pending"
    save_job(job, shared_dir)


# Step orphan reconciliation + clone-on-retry (2026-06-05)
# ──────────────────────────────────────────────────────────────────────
# Background: the in-place reset_to_queued path could produce inconsistent
# step state under multi-cycle retry — concurrent dispatch threads racing
# on the same job_id, or a failure that aborted the dispatch before
# mark_step_done could close out a started step. Net result: an active
# job in terminal status with one or more steps showing status="running"
# and started_at set but finished_at unset. The UI spinner kept spinning
# on those step rows even though no work was happening.
#
# The two pieces below close out that hole. `reconcile_orphan_steps`
# rewrites orphaned mid-flight steps to a coherent cancelled state on
# any terminal-status job; `clone_for_retry` makes the new retry path
# produce a fresh job_id instead of mutating the prior — so the prior's
# audit record is frozen the moment the operator clicks retry, and the
# new run starts from a known-clean ForgeJob.


def reconcile_orphan_steps(
    job: ForgeJob,
    shared_dir: Path,
    *,
    reason: str = "abandoned: job ended before this step finished",
    save: bool = True,
) -> list[int]:
    """For a job in a terminal status, reconcile any step that is still
    "in-flight" on disk (status running, started_at set, finished_at
    unset) by marking it cancelled with ``reason`` as its detail.

    Returns the list of step numbers that were reconciled (empty if
    nothing needed fixing).

    No-op for non-terminal jobs: a running job is allowed to have a
    running step. Only terminal-status jobs have the inconsistency we
    care about.

    The ``save`` kwarg lets the janitor batch many reconciles into a
    single write, or callers avoid a redundant disk hit when they're
    about to save_job anyway.
    """
    if job.status not in TERMINAL_JOB_STATES:
        return []

    fixed: list[int] = []
    closed_at = now_iso()
    for step in job.steps:
        # The classic orphan: status says it's still running, but the
        # job is terminal — nothing will ever finish it. Also catch the
        # narrower variant where a step's finished_at is missing even
        # though its status looks live ("running") — same root cause,
        # same fix.
        if step.status == "running" or (
            step.started_at and not step.finished_at and step.status not in (
                "done", "failed", "cancelled"
            )
        ):
            step.status = "cancelled"
            if not step.started_at:
                step.started_at = closed_at
            step.finished_at = closed_at
            # Preserve any pre-existing detail (might carry useful info
            # like "3 issues found") but prefix it with the reconcile
            # reason so it's obvious in the UI.
            if step.detail:
                step.detail = f"{reason} — prior detail: {step.detail}"
            else:
                step.detail = reason
            fixed.append(step.num)

    if fixed and save:
        save_job(job, shared_dir)
    return fixed


def sweep_orphan_steps(shared_dir: Path) -> dict[str, int]:
    """Walk the active jobs dir and reconcile orphaned mid-flight steps
    on any terminal-status job.

    Returns ``{"jobs_reconciled": N, "steps_reconciled": M}`` so callers
    (forge_sweep) can log a single-line summary per cycle. Idempotent:
    running it twice is a no-op the second time.

    Only the active dir is scanned — the completed dir holds only
    ``status == complete`` jobs, which by construction can't have orphan
    steps (every step had to close before complete_job moved them).

    The janitor runs every forge_sweep cycle as a safety net behind the
    clone-on-retry path; if the clone path is bypassed (e.g. an older
    pod running an older admin server, or an externally-written job
    file) the janitor will still close out the orphan steps next sweep.
    """
    jobs_reconciled = 0
    steps_reconciled = 0
    for job in list_active_jobs(shared_dir):
        if job.status not in TERMINAL_JOB_STATES:
            continue
        fixed = reconcile_orphan_steps(job, shared_dir)
        if fixed:
            jobs_reconciled += 1
            steps_reconciled += len(fixed)
    return {
        "jobs_reconciled": jobs_reconciled,
        "steps_reconciled": steps_reconciled,
    }


# Identity fields that carry from a prior job into its retry clone.
# Anything else (run state, step state, issue counts, cost, rejection
# metadata, context_snapshot, …) is dropped — the clone IS a fresh
# attempt and shouldn't inherit ambiguous-or-stale data from the run
# that failed. The prior_job_id field on the clone links the audit
# chain so operators can navigate prior→clone in the UI.
_CLONE_INHERIT_FIELDS: tuple[str, ...] = (
    "job_type",
    "pkg_id",
    "app_id",
    "bot_id",
    "pkg_version_before",
    "gallery_version",
    "triggered_by_detector",
    "operator_confirmed",
    "projected_cost_mid_usd",
    "projected_cost_high_usd",
)


def clone_for_retry(
    prior_job: ForgeJob,
    shared_dir: Path,
    *,
    superseded_reason: str = "superseded by retry clone",
) -> ForgeJob:
    """Create a NEW ForgeJob that retries ``prior_job``.

    The new job gets a fresh ``job_id`` and the next ``run_id`` in the
    (pkg_id, bot_id) sequence. It inherits identity fields (see
    :data:`_CLONE_INHERIT_FIELDS`) and gets a fresh step list seeded
    from ``_install_steps`` / ``_improvement_steps``. Status is
    ``queued``; everything else is at default. ``prior_job_id`` points
    at the predecessor.

    The prior job is mutated in place to:
      - have its ``superseded_by_job_id`` set to the clone's id
      - have any orphan steps closed out via reconcile_orphan_steps

    This is the new retry path, replacing the in-place ``reset_to_queued``
    mutation. The in-place helper is kept for callers that need it
    (CLI flows, tests), but the operator-facing retry endpoint should
    always clone — otherwise multi-cycle retries can produce the
    incoherent step state that motivated this work.

    Raises ValueError if ``prior_job`` is not in a terminal status —
    cloning a job that's still in-flight would race with the active
    dispatch thread.
    """
    if prior_job.status not in TERMINAL_JOB_STATES:
        raise ValueError(
            f"clone_for_retry: cannot clone job {prior_job.job_id!r} "
            f"in non-terminal status {prior_job.status!r}"
        )

    # Step seed by job_type so a clone of an improvement gets improvement
    # labels (the AWAIT step's prose differs).
    if prior_job.job_type == "install":
        steps = _install_steps()
    elif prior_job.job_type == "improvement":
        steps = _improvement_steps()
    else:
        # update / hotfix don't have their own factory yet — fall back to
        # the prior's step labels with state reset. Preserves the seed
        # "waiting" on the AWAIT row.
        steps = []
        for s in prior_job.steps:
            seed_status = "waiting" if s.label.startswith("AWAIT") else "pending"
            steps.append(ForgeStep(num=s.num, label=s.label, status=seed_status))

    inherited = {f: getattr(prior_job, f) for f in _CLONE_INHERIT_FIELDS}

    run_number = _allocate_next_run_number(
        prior_job.pkg_id, prior_job.bot_id, shared_dir,
    )
    ts = now_iso()
    clone = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(run_number),
        pkg_version_before=inherited.pop("pkg_version_before"),
        gallery_version=inherited.pop("gallery_version"),
        triggered_by_detector=inherited.pop("triggered_by_detector"),
        operator_confirmed=inherited.pop("operator_confirmed"),
        projected_cost_mid_usd=inherited.pop("projected_cost_mid_usd"),
        projected_cost_high_usd=inherited.pop("projected_cost_high_usd"),
        job_type=inherited.pop("job_type"),
        pkg_id=inherited.pop("pkg_id"),
        app_id=inherited.pop("app_id"),
        bot_id=inherited.pop("bot_id"),
        steps=steps,
        status="queued",
        created_at=ts,
        last_updated=ts,
        prior_job_id=prior_job.job_id,
        # is_retry=True so the bot's next build dispatch wipes
        # apps/<app_id>/ before re-building. The clone has a fresh
        # job_id, but the (bot_id, app_id) slot is shared with the
        # prior — stale files there would still trip sha256
        # verification. See PR #2210 / bot_forge.build_prompt(is_retry).
        is_retry=True,
    )
    assert not inherited, f"unhandled inherit fields: {sorted(inherited)}"

    # Persist the clone first; then mutate the prior. Either order is
    # safe for correctness, but clone-first means if the second write
    # fails (disk full, ACL issue) the operator still gets a queued
    # new job they can dispatch manually — the prior just doesn't
    # advertise the link.
    save_job(clone, shared_dir)

    prior_job.superseded_by_job_id = clone.job_id
    # Close out any orphan steps on the prior before we freeze it so
    # the UI shows truthful state once the operator navigates back to
    # it. reason mentions the clone id so an operator clicking on a
    # cancelled step sees where the work went.
    reconcile_orphan_steps(
        prior_job, shared_dir,
        reason=f"{superseded_reason} ({clone.job_id})",
        save=False,
    )
    save_job(prior_job, shared_dir)

    return clone


# ── Job state transitions ─────────────────────────────────────────────────────

def mark_awaiting_approval(job: ForgeJob, shared_dir: Path) -> None:
    """Set job.status → awaiting_approval.  Also updates the manifest's install_job field."""
    job.status = "awaiting_approval"
    save_job(job, shared_dir)

    # Update the manifest's install_job reference
    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
    if manifest:
        manifest.install_job = {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "current_step": job.current_step,
            "steps_total": len(job.steps),
            "phase": "awaiting_approval",
            "last_updated": now_iso(),
        }
        save_manifest(manifest, shared_dir)


def approve_job(
    job: ForgeJob,
    shared_dir: Path,
    approved_by: str,
    notes: str = "",
) -> None:
    """
    Set job.status → approved.
    Caller (forge_engine) then runs Phase 5 (apply) and calls complete_job.
    Also routes thumbs_up outcome signal to calibration if detector-triggered.
    """
    job.status = "approved"
    save_job(job, shared_dir)

    # Calibration: per-detector thumbs_up + global approved count
    if job.triggered_by_detector:
        try:
            from calibration import CalibrationLoader
            cal = CalibrationLoader(shared_dir)
            stats = cal.detector(job.triggered_by_detector).get("outcome_stats", {})
            thumbs_up = stats.get("thumbs_up", 0) + 1
            thumbs_down = stats.get("thumbs_down", 0)
            cal.set_detector_outcome_stat(job.triggered_by_detector, "thumbs_up", thumbs_up)
            cal.update_confidence_multiplier(
                job.triggered_by_detector,
                thumbs_up,
                thumbs_down,
                stats.get("expired", 0),
            )
        except Exception:
            pass

    try:
        from calibration import CalibrationLoader
        cal = CalibrationLoader(shared_dir)
        cal.set(
            "forge",
            "forge.outcome_stats.approved",
            cal.get("forge", "forge.outcome_stats.approved", 0) + 1,
        )
    except Exception:
        pass


def reject_job(
    job: ForgeJob,
    shared_dir: Path,
    rejected_by: str,
    reason: str = "",
) -> None:
    """
    Set job.status → rejected and record rejection metadata.

    Previously this function accepted ``reason`` + ``rejected_by`` but only
    flipped ``status`` — the metadata was silently dropped, leaving the
    Forge Jobs UI with rejected rows that had no explanation. Now persists
    all three rejection fields on the job.

    Updates calibration if triggered_by_detector is set.
    """
    job.status = "rejected"
    job.reject_reason = reason or ""
    job.rejected_by = rejected_by or ""
    job.rejected_at = now_iso()
    save_job(job, shared_dir)

    # Calibration: per-detector thumbs_down
    if job.triggered_by_detector:
        try:
            from calibration import CalibrationLoader
            cal = CalibrationLoader(shared_dir)
            stats = cal.detector(job.triggered_by_detector).get("outcome_stats", {})
            thumbs_down = stats.get("thumbs_down", 0) + 1
            thumbs_up = stats.get("thumbs_up", 0)
            cal.set_detector_outcome_stat(job.triggered_by_detector, "thumbs_down", thumbs_down)
            cal.update_confidence_multiplier(
                job.triggered_by_detector,
                thumbs_up,
                thumbs_down,
                stats.get("expired", 0),
            )
        except Exception:
            pass

    try:
        from calibration import CalibrationLoader
        cal = CalibrationLoader(shared_dir)
        cal.set(
            "forge",
            "forge.outcome_stats.rejected",
            cal.get("forge", "forge.outcome_stats.rejected", 0) + 1,
        )
    except Exception:
        pass


def complete_job(job: ForgeJob, shared_dir: Path, manifest) -> None:
    """
    Move job to completed dir.
    Append improvement_history entry to manifest (build_improvement_history_entry).
    Clear manifest.install_job field.
    Save manifest.
    """
    job.status = "complete"
    job.last_updated = now_iso()

    # Write to completed dir and remove from active dir
    _completed_dir(shared_dir).mkdir(parents=True, exist_ok=True)
    _atomic_write(_completed_path(job.job_id, shared_dir), _job_to_dict(job))

    active = _active_path(job.job_id, shared_dir)
    if active.exists():
        try:
            os.unlink(active)
        except OSError:
            pass

    # Update manifest
    entry = build_improvement_history_entry(job)
    if manifest.improvement_history is None:
        manifest.improvement_history = []
    manifest.improvement_history.append(entry)
    manifest.install_job = None
    # v20: stamp forge_built provenance on the build's authored fields.
    # Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md PR 2.
    # On a fresh install (job_type == "install"), forge materialised
    # every authored field from the operator-approved spec — stamp them
    # all. On improvement/update/hotfix runs, forge only re-materialised
    # a subset; we still stamp forge_built across the touched fields
    # because the build itself is the authorship event.
    save_manifest_with_provenance(
        manifest, shared_dir,
        source=PROVENANCE_FORGE_BUILT,
        fields=None,  # forge materialised the whole manifest from the spec
        by=f"forge:{job.job_id}",
        via="forge_approve",
    )


# ── History entry builder ─────────────────────────────────────────────────────

def build_improvement_history_entry(job: ForgeJob) -> dict:
    """
    Build the dict appended to manifest.improvement_history.
    Matches spec section 10.3 schema.
    """
    # Determine approval metadata from job state
    operator_approved = job.status == "complete"
    approved_at = job.last_updated if operator_approved else None

    return {
        "run_id": job.run_id,
        "job_id": job.job_id,
        "type": job.job_type,
        "triggered_by_detector": job.triggered_by_detector,
        "pkg_version_before": job.pkg_version_before,
        "pkg_version_after": None,        # Caller should set this after versioning
        "gallery_version": job.gallery_version,
        "critique_rounds": job.critique_rounds_done,
        "issues_found": job.issues_found,
        "issues_resolved": job.issues_resolved,
        "issues_deferred": job.issues_deferred,
        "test_exit_code": job.test_exit_code,
        "test_output_summary": job.test_output_summary,
        "shared_file_checks": job.shared_file_checks,
        "operator_approved": operator_approved,
        "approved_at": approved_at,
        "approved_by": None,              # Caller should set this from approval signal
        "notes": "",
    }


# ── Stale awaiting_approval auto-reject ───────────────────────────────────────

# Default timeout: 7 days. Picked so a normal week of inattention doesn't
# auto-reject anything an operator would have approved, but abandoned
# jobs don't pin the manifest in `updating` indefinitely.
AWAITING_APPROVAL_TIMEOUT_DAYS = 7


def _parse_iso_utc(ts: str) -> datetime | None:
    """Parse a now_iso()-formatted timestamp back to a tz-aware datetime."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def auto_reject_stale_awaiting_approval(
    shared_dir: Path,
    *,
    threshold_days: int = AWAITING_APPROVAL_TIMEOUT_DAYS,
    now: datetime | None = None,
) -> list[str]:
    """Auto-reject forge jobs that have sat in awaiting_approval too long.

    For each rejected job:
      - Calls :func:`reject_job` with an "auto-rejected" reason.
      - Clears the manifest's ``install_job`` reference.
      - Rolls back ``manifest.status`` so the app isn't pinned in
        ``updating``: improvements roll back to ``active``; never-completed
        installs (the manifest only existed for the in-flight build) have
        their manifest deleted.

    Returns the list of job_ids that were rejected.
    """
    cutoff_now = now or datetime.now(timezone.utc)
    cutoff = cutoff_now - timedelta(days=threshold_days)
    rejected_ids: list[str] = []

    for job in list_active_jobs(shared_dir):
        if job.status != "awaiting_approval":
            continue
        # Use last_updated as the staleness reference — that's the
        # timestamp `mark_awaiting_approval` set when the job entered
        # this state, and it stays put afterwards.
        ts = _parse_iso_utc(job.last_updated) or _parse_iso_utc(job.created_at)
        if ts is None or ts > cutoff:
            continue

        reason = (
            f"auto-rejected: no operator review in {threshold_days} days"
        )
        try:
            reject_job(job, shared_dir, rejected_by="forge_sweep", reason=reason)
        except Exception:
            # Best-effort: bookkeeping failures inside reject_job (e.g.
            # calibration import) shouldn't block the sweep from rolling
            # back the manifest below.
            job.status = "rejected"
            try:
                save_job(job, shared_dir)
            except Exception:
                continue

        # Manifest rollback. Only touched if the manifest still exists
        # and still points at this job — otherwise something else has
        # already moved on and we leave it alone.
        try:
            manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
        except Exception:
            manifest = None
        if manifest is not None:
            current_install_job = manifest.install_job or {}
            still_ours = (
                isinstance(current_install_job, dict)
                and current_install_job.get("job_id") == job.job_id
            )
            if still_ours:
                manifest.install_job = None
                if job.job_type == "install":
                    # Never-completed install: the manifest only exists
                    # because forge created it for this run. Remove it
                    # so the app slot is free.
                    try:
                        from .manifest import delete_manifest as _delete_manifest
                        _delete_manifest(job.app_id, job.bot_id, shared_dir)
                    except Exception:
                        # Fall back to leaving the manifest with status
                        # cleared if delete fails.
                        manifest.status = "draft"
                        try:
                            save_manifest(manifest, shared_dir)
                        except Exception:
                            pass
                else:
                    # improvement / update / hotfix: roll status back to
                    # the canonical pre-job value.
                    manifest.status = "active"
                    try:
                        save_manifest(manifest, shared_dir)
                    except Exception:
                        pass

        rejected_ids.append(job.job_id)

    return rejected_ids


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_pending_approval_job(
    app_id: str,
    bot_id: str,
    shared_dir: Path,
) -> ForgeJob | None:
    """
    Find the active job for this app that is in awaiting_approval status.
    Used by the natural language approval handler.
    """
    for job in list_active_jobs(shared_dir):
        if (
            job.app_id == app_id
            and job.bot_id == bot_id
            and job.status == "awaiting_approval"
        ):
            return job
    return None

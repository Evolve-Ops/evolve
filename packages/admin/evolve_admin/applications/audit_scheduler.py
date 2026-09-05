"""Audit scheduler — periodic infra-audit + per-bot audit-outbox drain.

Renamed + slimmed 2026-06-08 — was the bulk of scheduler.py (the
app-test-scheduler), which got killed along with the rest of the
app-test surface per internal/decision-app-tests-2026-06-08.md. What
survives is the audit-poller half: the hourly tick that

  1. fires the pod-wide infra audit when cadence is due, and
  2. drains every bot's per-bot audit-outbox into the Signal store.

Both responsibilities are independent of test scheduling — they kept
working even when the test scheduler was disabled, because the audit
pipeline was always live.

Runs under the `evolve` user as ``ai.evolve.evolve.audit-scheduler``;
fired hourly from a LaunchDaemon installed by ``install_launchd``.
The CLI entry point is ``evolve-admin audit-scheduler-tick``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..runtime import JobSpec, get_scheduler, render_launchd_plist

log = logging.getLogger(__name__)


SCHEDULER_LABEL = "ai.evolve.evolve.audit-scheduler"
SCHEDULER_PLIST = f"/Library/LaunchDaemons/{SCHEDULER_LABEL}.plist"
SCHEDULER_INTERVAL_SECONDS = 3600  # hourly tick

# Legacy label kept around so install_launchd can bootout the prior
# app-test-scheduler plist if it still exists on a pre-rename pod.
_LEGACY_SCHEDULER_LABEL = "ai.evolve.evolve.app-test-scheduler"
_LEGACY_SCHEDULER_PLIST = f"/Library/LaunchDaemons/{_LEGACY_SCHEDULER_LABEL}.plist"


@dataclass
class TickResult:
    """Summary returned by ``tick`` for logging / telemetry."""

    started_at: str
    finished_at: str
    audit_files_processed: int = 0
    audit_findings_ingested: int = 0
    audit_signals_swept: int = 0
    # Archive-vs-drop accounting for the re-emission cut (so operators can
    # see the reduction): records kept in _ingested/ vs deleted as redundant
    # re-emissions / no-op heartbeats.
    audit_records_archived: int = 0
    audit_records_dropped: int = 0
    audit_dropped_dedup_hit: int = 0
    audit_dropped_heartbeat: int = 0
    infra_audit_files_processed: int = 0
    infra_audit_findings_ingested: int = 0
    cron_jobs_checked: int = 0
    cron_jobs_failing: int = 0
    # Fit Reviewer (Bite 4) drain — candidate outboxes → InstallApp proposals.
    fit_review_files_processed: int = 0
    fit_review_proposals_raised: int = 0
    fit_review_observations_raised: int = 0
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "audit_files_processed": self.audit_files_processed,
            "audit_findings_ingested": self.audit_findings_ingested,
            "audit_signals_swept": self.audit_signals_swept,
            "audit_records_archived": self.audit_records_archived,
            "audit_records_dropped": self.audit_records_dropped,
            "audit_dropped_dedup_hit": self.audit_dropped_dedup_hit,
            "audit_dropped_heartbeat": self.audit_dropped_heartbeat,
            "infra_audit_files_processed": self.infra_audit_files_processed,
            "infra_audit_findings_ingested": self.infra_audit_findings_ingested,
            "cron_jobs_checked": self.cron_jobs_checked,
            "cron_jobs_failing": self.cron_jobs_failing,
            "fit_review_files_processed": self.fit_review_files_processed,
            "fit_review_proposals_raised": self.fit_review_proposals_raised,
            "fit_review_observations_raised": self.fit_review_observations_raised,
            "errors": list(self.errors),
        }


def tick(shared_dir: Path, network: dict | None = None) -> TickResult:
    """One audit-scheduler pass. Idempotent + best-effort."""
    from ..config import load_network

    if network is None:
        network = load_network()

    started_at = datetime.now(timezone.utc).isoformat()
    result = TickResult(started_at=started_at, finished_at=started_at)

    # Phase 0a: pod-wide infra audit (cadence-gated; cheap when nothing's broken).
    _run_infra_audit_if_due(shared_dir, network, result)

    # Phase 0b: drain per-bot + pod-wide audit outboxes into the Signal store.
    _drain_audit_outboxes(shared_dir, network, result)

    # Phase 0c: cron exit-status sweep (PR #2669 follow-up) — convert a
    # persistent non-zero LastExitStatus on a managed calendar job into a
    # Signal. Cheap: one `sudo -n launchctl list` per daily job.
    _sweep_cron_exit_status(shared_dir, network, result)

    # Phase 0d: drain the Fit Reviewer candidate outboxes into InstallApp
    # proposals (spec-fit-reviewer-2026-06-12 §3.1, Bite 4). Same transport as
    # the audit drain; no new daemon.
    _drain_fit_review_outboxes(shared_dir, network, result)

    # Phase 0e: aggregate the per-bot exec-failure ledgers (written by the
    # plugin's message_sending absorber) into dedup'd bot_exec_failures
    # Signals — one per recurring failure shape, sweep-resolved when a
    # shape stops firing. Pure local file reads; cheap.
    # Design: internal/design-exec-failure-hygiene-2026-08-31.md (A2).
    _sweep_exec_failure_ledgers(shared_dir, result)
    _sweep_tool_profile_refusals(shared_dir, result)

    result.finished_at = datetime.now(timezone.utc).isoformat()
    return result


def _drain_fit_review_outboxes(
    shared_dir: Path,
    network: dict,
    result: TickResult,
) -> None:
    """Run the admin-side Fit Reviewer poller; merge counters + errors.

    The bot's fit_review_runner writes capability candidates to
    ``{shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json``; this drain runs
    the deterministic gates (cite-or-don't, gallery-pkg-exists,
    support-reconcile, dedup/cooldown) and mints one InstallApp Proposal per
    surviving candidate via ``arbiter.store``.
    """
    try:
        from .fit_review_poller import tick as fit_review_tick

        fr = fit_review_tick(shared_dir, network=network)
        result.fit_review_files_processed = fr.files_processed
        result.fit_review_proposals_raised = fr.proposals_raised
        result.fit_review_observations_raised = fr.observations_raised
        for err in fr.errors:
            result.errors.append({"stage": "fit_review_poller", "error": err})
    except Exception as exc:
        log.warning("audit_scheduler: fit_review_poller drain failed: %s", exc)
        result.errors.append({"stage": "fit_review_poller", "error": str(exc)})


def _drain_audit_outboxes(
    shared_dir: Path,
    network: dict,
    result: TickResult,
) -> None:
    """Run the admin-side audit poller; merge counters + errors into result.

    The bot's audit_runner writes structural-verifier findings to
    ``<home_root>/<bot>/.openclaw/workspace/evolve/audit_outbox/`` (home_root =
    /Users on macOS, /home on Linux — resolved via platform_profile in the
    poller); this drain turns those into Signals via signals.store.observe +
    sweep_resolve.
    """
    try:
        from .audit_poller import tick as audit_poller_tick
        audit_result = audit_poller_tick(shared_dir, network=network)
        result.audit_files_processed = audit_result.total_files
        result.audit_findings_ingested = audit_result.total_findings
        result.audit_signals_swept = audit_result.total_swept
        result.audit_records_archived = audit_result.total_archived
        result.audit_records_dropped = audit_result.total_dropped
        result.audit_dropped_dedup_hit = audit_result.total_dropped_dedup_hit
        result.audit_dropped_heartbeat = audit_result.total_dropped_heartbeat
        result.infra_audit_findings_ingested = audit_result.total_infra_findings
        result.infra_audit_files_processed = audit_result.infra.files_processed
        for bot_result in audit_result.bots:
            for err in bot_result.errors:
                result.errors.append({
                    "stage": "audit_poller",
                    "bot_id": bot_result.bot_id,
                    "error": err,
                })
        for err in audit_result.infra.errors:
            result.errors.append({
                "stage": "audit_poller_infra",
                "error": err,
            })
    except Exception as exc:
        log.warning("audit_scheduler: audit_poller drain failed: %s", exc)
        result.errors.append({"stage": "audit_poller", "error": str(exc)})


def _sweep_exec_failure_ledgers(shared_dir: Path, result: TickResult) -> None:
    """Mirror recurring absorbed exec-failure shapes into the Signal store.

    The plugin-side ExecFailureAbsorber ledgers every raw
    ``⚠️ 🛠️ Exec failed …`` trailer it sees on a channel-bound payload;
    this step aggregates those ledgers so recurring shapes stay
    operator-visible. See evolve_admin/exec_failure_monitor.py.
    """
    try:
        from ..exec_failure_monitor import emit_exec_failure_signals
        emit_exec_failure_signals(shared_dir)
    except Exception as exc:
        log.warning("audit_scheduler: exec_failure_monitor failed: %s", exc)
        result.errors.append({"stage": "exec_failure_monitor", "error": str(exc)})


def _sweep_tool_profile_refusals(shared_dir: Path, result: TickResult) -> None:
    """Mirror out-of-profile tool-call refusals into the Signal store.

    The plugin-side tool-profile filter trims a background / one-shot session's
    Evolve tool surface and ledgers every refused call; this step aggregates
    those ledgers so a profile that is too narrow becomes visible as evidence
    rather than as an unexplained model retry. See
    evolve_admin/tool_profile_monitor.py.
    """
    try:
        from ..tool_profile_monitor import emit_tool_profile_signals
        emit_tool_profile_signals(shared_dir)
    except Exception as exc:  # noqa: BLE001 — one stage must not kill the tick
        log.warning("audit_scheduler: tool_profile_monitor failed: %s", exc)
        result.errors.append({"stage": "tool_profile_monitor", "error": str(exc)})


def _sweep_cron_exit_status(
    shared_dir: Path,
    network: dict,
    result: TickResult,
) -> None:
    """Mirror failing managed calendar jobs into the Signal store.

    A daily maintenance cron (oc-log-rotate, retention, log-cap, …) that
    exits non-zero leaves only launchd's LastExitStatus behind; this step
    turns that into a cron_exit_monitor Signal and sweep-resolves it when
    the next run succeeds. See evolve_admin/cron_exit_monitor.py.
    """
    try:
        from ..cron_exit_monitor import emit_cron_exit_signals
        summary = emit_cron_exit_signals(shared_dir, network)
        result.cron_jobs_checked = summary.get("checked", 0)
        result.cron_jobs_failing = summary.get("failing", 0)
    except Exception as exc:
        log.warning("audit_scheduler: cron_exit_monitor failed: %s", exc)
        result.errors.append({"stage": "cron_exit_monitor", "error": str(exc)})


def _run_infra_audit_if_due(
    shared_dir: Path,
    network: dict,
    result: TickResult,
) -> None:
    """Fire the admin-side infra audit when it's due by cadence.

    Piggybacks on the hourly tick. The runner is cheap when nothing's
    broken (~5 launchctl calls + a few file stats) and we gate on
    `last successful run ≥ 24h ago` for daily cadence.

    Per spec §4.3 + §5 of the audit-extensions spec: runs in
    calibration mode by default; auto_fix demoted to propose; outputs
    land in the shared infra_audit_outbox/ and get picked up by the
    same poller tick that fires immediately after.
    """
    cfg = (network or {}).get("infra_audit") or {}
    cadence = cfg.get("default_cadence", "daily")
    if cadence in ("off", "manual"):
        return

    try:
        from . import infra_audit
    except Exception as exc:
        log.warning("audit_scheduler: infra_audit import failed: %s", exc)
        result.errors.append({"stage": "infra_audit", "error": str(exc)})
        return

    interval_s = _infra_audit_interval_seconds(cadence)
    last = infra_audit.latest_run_summary(shared_dir=shared_dir)
    if last and interval_s > 0:
        try:
            last_dt = datetime.fromisoformat(
                (last.get("completed_at") or "").replace("Z", "+00:00")
            )
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if age < interval_s:
                return
        except (ValueError, KeyError):
            pass

    try:
        infra_audit.run(
            network=network, shared_dir=shared_dir,
            calibration_mode=bool(cfg.get("calibration_mode", True)),
            requested_by="audit_scheduler",
        )
    except Exception as exc:
        log.warning("audit_scheduler: infra_audit.run failed: %s", exc)
        result.errors.append({"stage": "infra_audit", "error": str(exc)})


def _infra_audit_interval_seconds(cadence: str) -> int:
    """Map cadence name → seconds. 0 means "always due"."""
    return {
        "daily":   24 * 60 * 60,
        "weekly":   7 * 24 * 60 * 60,
        "manual":   0,    # never fires from scheduler — only on demand
        "off":      0,
    }.get(cadence, 24 * 60 * 60)


# ── LaunchDaemon plist + install ──────────────────────────────────────────────


def _scheduler_job_spec(
    evolve_admin_path: str | None = None,
    interval_seconds: int = SCHEDULER_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> JobSpec:
    """The audit-scheduler LaunchDaemon spec.

    Mirrors the repo-puller pattern: evolve user, StartInterval, jitter to
    desync from other hourly daemons, no KeepAlive (short-lived task).

    Paths default to the active platform profile (W10-D): the macOS golden
    (parity test under the conftest MACOS pin) renders /Users/Shared
    byte-identically; a Linux pod renders /var/lib, so the systemd unit body
    carries no /Users leak.
    """
    from platform_profile import get_profile
    _prof = get_profile()
    if evolve_admin_path is None:
        evolve_admin_path = _prof.venv_evolve_admin
    if log_dir is None:
        log_dir = f"{_prof.shared_dir_default}/logs"
    return JobSpec(
        label=SCHEDULER_LABEL,
        program_args=[evolve_admin_path, "audit-scheduler-tick"],
        user="evolve",
        start_interval=interval_seconds,
        run_at_load=False,
        stdout_path=f"{log_dir}/audit-scheduler.log",
        stderr_path=f"{log_dir}/audit-scheduler.err.log",
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        jitter_seconds=120,
    )


def render_plist(
    evolve_admin_path: str | None = None,
    interval_seconds: int = SCHEDULER_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> str:
    """Render the LaunchDaemon plist as a string. Pure — caller writes to disk.

    See :func:`_scheduler_job_spec` for the job semantics (incl. the
    profile-keyed path defaults).
    """
    return render_launchd_plist(_scheduler_job_spec(
        evolve_admin_path, interval_seconds, log_dir,
    ))


def install_launchd() -> bool:
    """Install ai.evolve.evolve.audit-scheduler. Idempotent.

    Also boots-out the legacy ai.evolve.evolve.app-test-scheduler plist
    (and removes the file on disk) so the rename takes effect on the
    first deploy after this change lands.

    Goes through the Scheduler seam. ``install()`` skips the
    bootout+bootstrap bounce when the on-disk plist is byte-identical;
    that's fine for this hourly tick daemon (each tick is a fresh
    process), EXCEPT when the job isn't actually registered with
    launchd (an earlier bootstrap failed after the plist write, or a
    manual bootout). The legacy ritual re-registered unconditionally,
    so compensate with remove+install in that case.
    """
    sched = get_scheduler()

    # Legacy bootout + plist removal — best-effort; remove() is a no-op
    # when the label or file is already gone.
    sched.remove(_LEGACY_SCHEDULER_LABEL)

    res = sched.install(_scheduler_job_spec())
    if res.ok and res.skipped and not sched.status(SCHEDULER_LABEL)["managed"]:
        sched.remove(SCHEDULER_LABEL)
        res = sched.install(_scheduler_job_spec())
    if not res.ok:
        log.warning("audit_scheduler install: %s", res.message)
        return False
    log.info(
        "audit_scheduler install: bootstrapped %s every %ds",
        SCHEDULER_LABEL, SCHEDULER_INTERVAL_SECONDS,
    )
    return True

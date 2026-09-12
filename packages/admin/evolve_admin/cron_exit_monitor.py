"""cron_exit_monitor.py — Signal producer for managed cron jobs that ran and failed.

Gap this closes (PR #2669 follow-up): launchd records ``LastExitStatus``
for every run-to-completion job, ``service.py`` exposes it as ``last_exit``,
and the daemons table renders it — but no Signal producer converted a
persistent non-zero exit on a scheduled maintenance job (oc-log-rotate,
retention, log-cap, overrides-expiry, …) into an Alert. A daily job could
fail every run and nothing fired; that is exactly how the PR #1956 sudoers
revert went unnoticed while gateway logs grew unbounded. ``pod_health``'s
``_check_launchd`` covers *not loaded*; this producer covers *loaded but
failing*.

Scope: calendar-scheduled (StartCalendarInterval) Evolve-owned jobs in
/Library/LaunchDaemons — the ``ai.evolve.*`` / ``ai.openclaw.evolve.*``
namespaces. StartInterval jobs (pod-health 60s, breakers-audit 5min, …)
are deliberately excluded: a single transient failure would fire-and-
resolve within one interval (flap noise), and monitor_coverage already
owns "monitor went silent". KeepAlive daemons are excluded because launchd
restarts them on exit — a past non-zero status is not a steady-state
finding there.

Runs from the hourly audit-scheduler tick (as the ``evolve`` user; the
``evolve ALL=(root) NOPASSWD: /bin/launchctl list *`` sudoers grant makes
the probe non-interactive-safe) and from the Alerts page
``POST /api/signals/refresh`` fan-out.

Tri-state per PR #1579: when ``sudo -n launchctl list`` cannot escalate,
emit ONE ``cron_exit_cannot_escalate`` meta-Signal and skip the per-label
sweep entirely — the per-label state is unknown, and resolving firing
Signals on a tooling failure would mask real breakage.
"""

from __future__ import annotations

import logging
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import LaunchdScheduler, Scheduler, get_scheduler

logger = logging.getLogger(__name__)

PRODUCER = "cron_exit_monitor"
TYPE_FAILED = "cron_job_failed"
TYPE_CANNOT_ESCALATE = "cron_exit_cannot_escalate"


def _default_plist_dir() -> Path:
    """Service-definition dir from the platform profile (8.3 Linux port
    seam) — /Library/LaunchDaemons on the canonical macOS pod."""
    from platform_profile import get_profile
    return Path(get_profile().daemon_dir)


# Evolve-owned launchd namespaces. Scanning the plist dir (rather than
# expected_plist_labels) means an orphaned-but-still-loaded cron from a
# previous version is also watched — if it is failing, alerting is correct.
_MANAGED_PLIST_GLOBS = ("ai.evolve.*.plist", "ai.openclaw.evolve.*.plist")


@dataclass
class CronJob:
    """A calendar-scheduled managed job parsed from its launchd plist."""

    label: str
    user: str | None
    stderr_path: str | None


def list_managed_cron_jobs(plist_dir: Path | None = None) -> list[CronJob]:
    """Enumerate Evolve-owned calendar jobs from the LaunchDaemons dir.

    Filters: StartCalendarInterval present, KeepAlive absent/falsy. An
    unparseable plist is skipped (pod_health's content check owns that
    finding). Missing dir → empty list (non-macOS dev/CI hosts).
    """
    plist_dir = plist_dir or _default_plist_dir()
    jobs: list[CronJob] = []
    for pattern in _MANAGED_PLIST_GLOBS:
        for plist_path in sorted(plist_dir.glob(pattern)):
            try:
                with plist_path.open("rb") as f:
                    data = plistlib.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("KeepAlive"):
                continue
            if not data.get("StartCalendarInterval"):
                continue
            jobs.append(CronJob(
                label=str(data.get("Label") or plist_path.name[: -len(".plist")]),
                user=data.get("UserName"),
                stderr_path=data.get("StandardErrorPath"),
            ))
    return jobs


def _probe_scheduler() -> "Scheduler":
    """The status-probe handle, guarded-derived off the process-wide seam.

    The audit-scheduler daemon calls this every cycle, so it must resolve the
    injected adapter on a Linux pod (SystemdScheduler) rather than a hardcoded
    launchd one. ``status()`` is a portable Protocol verb, so on systemd the
    injected adapter answers directly — truthful instead of a launchctl crash.

    ``sudo_non_interactive=True`` (``sudo -n``) is LOAD-BEARING: this runs in a
    TTY-less daemon context, where a plain ``sudo`` would BLOCK on a password
    prompt instead of failing fast and classifiably (the probe's
    ``status_error`` tri-state then reports ``cannot_escalate`` rather than
    hanging the whole audit cycle). ``get_scheduler()``'s default LaunchdScheduler
    is plain ``sudo`` WITHOUT ``-n``, so a clean swap would silently flip the
    posture and drop the byte-identical argv. We therefore guarded-derive a
    ``sudo_non_interactive=True`` launchd adapter ONLY when the active adapter is
    launchd (reusing the injected fake's runner under test), and on systemd
    return the injected adapter directly — ``sudo -n`` is a launchd argv concept
    SystemdScheduler carries via its own posture set by the platform gate's
    ``set_scheduler()`` injection. 5s per-probe timeout, byte-identical to the
    pre-seam ``LaunchdScheduler(sudo_non_interactive=True, timeout=5.0)``. Same
    guarded-derive shape as mcp_service._scheduler_sudo /
    retire._probe_scheduler. status() carries the tri-state via its
    ``status_error`` field, so no launchd-verbatim escape hatch is needed.
    """
    sched = get_scheduler()
    if not isinstance(sched, LaunchdScheduler):
        return sched
    if sched._custom_runner:
        return LaunchdScheduler(
            sudo_non_interactive=True, runner=sched._runner, timeout=5.0,
        )
    return LaunchdScheduler(sudo_non_interactive=True, timeout=5.0)


def _status_probe(label: str) -> dict:
    return _probe_scheduler().status(label)


def _parse_last_exit(status: dict) -> int:
    """LastExitStatus from a Scheduler ``status()`` dict, 0 when unknown."""
    raw_exit = status.get("last_exit")
    try:
        return int(raw_exit) if raw_exit is not None else 0
    except (TypeError, ValueError):
        return 0


def _describe_wait_status(status: int) -> str:
    """Human description of launchctl's LastExitStatus.

    launchctl reports the raw wait(2) status: a normal exit(N) appears as
    N<<8 (exit 1 → 256), while a signal kill keeps the signal number in
    the low 7 bits (SIGTERM → 15).
    """
    if status & 0x7F:
        return f"terminated by signal {status & 0x7F}"
    return f"exit code {(status >> 8) & 0xFF}"


def _short_name(label: str) -> str:
    return label.replace("ai.openclaw.evolve.", "").replace("ai.evolve.", "")


def emit_cron_exit_signals(
    shared_dir: Path,
    network: dict | None = None,
    *,
    plist_dir: Path | None = None,
    status_fn: Callable[[str], dict] | None = None,
) -> dict[str, Any]:
    """Probe every managed calendar job and mirror failures into Signals.

    Returns ``{"checked": int, "failing": int, "cannot_escalate": bool}``.

    - non-zero LastExitStatus → one ``cron_job_failed`` Signal per label
      (find-or-create by signature; severity inherits the producer default).
    - exit cleared / job retired → sweep_resolve archives the Signal.
    - probe cannot escalate → ONE ``cron_exit_cannot_escalate`` meta-Signal,
      remaining labels unprobed, and NO sweep (per-label state is unknown).
    - job not loaded → skipped; pod_health._check_launchd owns the
      not-loaded finding.
    """
    try:
        from schema.signal import make_signature
        from signals import store as signals_store
    except Exception as exc:
        logger.warning("cron_exit_monitor: signals import failed: %s", exc)
        return {"checked": 0, "failing": 0, "cannot_escalate": False}

    members = set((network or {}).get("members", []))
    probe = status_fn or _status_probe

    checked = 0
    cannot_escalate = False
    kept: set[str] = set()
    failing: list[tuple[CronJob, int]] = []
    for job in list_managed_cron_jobs(plist_dir):
        s = probe(job.label)
        probe_error = s.get("status_error")
        if probe_error == "cannot_escalate":
            # Every subsequent probe would fail the same way; stop here.
            cannot_escalate = True
            break
        if probe_error:
            # Transient per-label probe failure — this label's state is
            # unknown, so protect any firing Signal for it from the sweep.
            kept.add(make_signature(PRODUCER, TYPE_FAILED, job.label))
            continue
        checked += 1
        if not s.get("managed"):
            continue  # not loaded — pod_health._check_launchd's finding
        status = _parse_last_exit(s)
        if status:
            failing.append((job, status))

    if cannot_escalate:
        signals_store.observe(
            shared_dir,
            signature=make_signature(PRODUCER, TYPE_CANNOT_ESCALATE, "pod"),
            producer=PRODUCER,
            type=TYPE_CANNOT_ESCALATE,
            flavor="maintenance",
            scope="pod",
            title="Cron exit-status check cannot run",
            body=(
                "`sudo -n /bin/launchctl list` could not escalate from the "
                "evolve user, so the exit status of scheduled maintenance "
                "jobs (log rotation, retention, …) cannot be checked. The "
                "jobs themselves may be fine."
            ),
            details={
                "missing_grant": "/bin/launchctl list *",
                "what_it_means": (
                    "The monitor that watches whether daily maintenance "
                    "jobs succeed is blind until the sudoers grant for "
                    "launchctl is restored."
                ),
                "fix_steps": "1. sudo evolve-admin refresh-sudoers\n"
                             "2. Re-run the check from the Alerts page Refresh button.",
            },
        )
        # Deliberately no sweep: existing cron_job_failed Signals stay
        # firing — we could not verify they cleared.
        return {"checked": checked, "failing": 0, "cannot_escalate": True}

    for job, status in failing:
        signature = make_signature(PRODUCER, TYPE_FAILED, job.label)
        kept.add(signature)
        bot_id = job.user if job.user in members else None
        log_hint = f" Check its log: {job.stderr_path}" if job.stderr_path else ""
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=PRODUCER,
            type=TYPE_FAILED,
            flavor="maintenance",
            scope="bot" if bot_id else "pod",
            bot_id=bot_id,
            title=f"Scheduled job failing: {_short_name(job.label)}",
            body=(
                f"launchd reports the last run of {job.label} failed "
                f"({_describe_wait_status(status)}). It will not retry until "
                f"its next scheduled window.{log_hint}"
            ),
            details={
                "label": job.label,
                "last_exit_status": status,
                "decoded": _describe_wait_status(status),
                "stderr_path": job.stderr_path,
                "what_it_means": (
                    "A scheduled maintenance job ran and exited with an "
                    "error. Whatever it maintains (log rotation, retention, "
                    "override expiry, …) is not happening until a run "
                    "succeeds."
                ),
                "fix_steps": (
                    f"1. Read the job log{': ' + job.stderr_path if job.stderr_path else ''}\n"
                    f"2. Fix the underlying error it reports\n"
                    f"3. Re-run now: sudo /bin/launchctl kickstart -k system/{job.label}"
                ),
            },
        )

    signals_store.sweep_resolve(
        shared_dir,
        producer=PRODUCER,
        kept_signatures=kept,
        reason="auto-resolve: scheduled job exit status cleared",
    )
    return {"checked": checked, "failing": len(failing), "cannot_escalate": False}

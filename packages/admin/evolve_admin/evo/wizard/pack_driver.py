"""pack_driver.py — queue starter-pack app installs at add-bot finalize (M3).

Spec: docs/spec-add-bot-wizard-build-delta-2026-06-10.md §5 + §6: the
apps the admin accepted in AB_SHAPE (plus the morning briefing when the
default stayed on) queue as forge install jobs the moment the new bot is
built and passes its smoke check — the unchanged 05-28 §7 machinery
(``forge_jobs.create_install_job`` + ``forge_engine.run_forge_job``,
which auto-ships install jobs).

Two deliberate differences from the admin UI's gallery-install endpoint:

  * **Sequential, not concurrent.** Bundle dependencies are declared in
    topological order (foundations before behaviors — the EA Pack
    contract), and the installer honors that by running the jobs one at
    a time in a single daemon thread. Concurrent installs would race a
    behavior app ahead of its data foundation.
  * **Consent seeds ride on the job.** The wizard's consent answers
    populate ``privacy.consent_notice`` (and audience scoping where the
    answers imply it) on every app it installs — schema v24 made the
    blocks first-class, so the conduct prose and the manifests agree
    from day one. The seeds travel in ``job.context_snapshot`` and are
    merged by forge_engine's manifest seeding (gallery-declared blocks
    still win).

Post-wrap honesty (U1 activation fix, 2026-06-11): a job that queues
fine but FAILS mid-build used to die in the job log — the wrap had
already promised the app, and nothing told the operator the promise
broke. The worker now checks every job's final status and pushes a
``system.app_install_failed`` notification through the alerts
dispatcher for anything that didn't complete. Callers activating the
morning briefing (``briefing_activation``) can additionally pass
``briefing_announce`` so a completed briefing install pushes the
``decisions.briefing_activated`` receipt.

Seams (tests substitute instead of patching subprocess/forge):

  * :func:`set_install_runner` — replaces the blocking per-job run.
  * :func:`set_force_sync` — runs the queue inline, no thread.
  * :func:`set_outcome_dispatch` — replaces the alerts-dispatcher send
    used for the post-wrap outcome notifications.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class QueuedApp:
    """One app's queue outcome — job id on success, error otherwise."""

    pkg_id: str
    app_id: str
    name: str
    job_id: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.job_id) and not self.error


# ── Seams ─────────────────────────────────────────────────────────────────────


# Runner: blocking call that executes ONE queued install job. Production
# default runs forge_engine.run_forge_job (auto-ships installs).
InstallRunner = Callable[[Path, str, str], None]  # (shared_dir, job_id, bot_id)
_install_runner: InstallRunner | None = None


def set_install_runner(fn: InstallRunner | None) -> None:
    global _install_runner
    _install_runner = fn


_force_sync = False


def set_force_sync(value: bool) -> None:
    global _force_sync
    _force_sync = value


# Outcome dispatch: keyword passthrough to alerts.dispatcher.send. Tests
# substitute a recorder; production lazily imports the dispatcher so the
# wizard module stays import-light.
OutcomeDispatch = Callable[..., Any]
_outcome_dispatch: OutcomeDispatch | None = None


def set_outcome_dispatch(fn: OutcomeDispatch | None) -> None:
    global _outcome_dispatch
    _outcome_dispatch = fn


def _default_outcome_dispatch(**kwargs: Any) -> Any:
    from ...alerts.dispatcher import send as _send

    return _send(**kwargs)


def _default_install_runner(shared_dir: Path, job_id: str, bot_id: str) -> None:
    from ...applications import forge_engine as _fe

    _fe.run_forge_job(job_id=job_id, shared_dir=shared_dir, bot_id=bot_id)


# ── Outcome notifications ─────────────────────────────────────────────────────


def _plain_failure_reason(
    detail: str, *, bot_id: str, app_name: str, is_briefing: bool,
) -> str:
    """Render a job-failure detail in operator words. The C-A4
    channel-gap refusal gets the honest product sentence (for the
    briefing it has a self-healing follow-up; other apps need a manual
    reinstall); everything else passes through trimmed — technical
    detail beats a vague shrug."""
    d = (detail or "").strip()
    if "messaging output" in d or "C-A4" in d:
        if is_briefing:
            return (
                f"{bot_id} has no place to send messages yet — connect "
                "a channel (Telegram, Slack, and friends on the admin "
                "page) and the briefing sets itself up."
            )
        return (
            f"{bot_id} has no place to send messages yet — connect a "
            f"channel on the admin page, then reinstall {app_name} "
            "from the app gallery."
        )
    if len(d) > 300:
        d = d[:300] + "…"
    return d or "the build stopped without a recorded reason"


def _job_failure_detail(job: Any) -> str:
    """Last failed step's detail, or "" when none is recorded."""
    try:
        steps = list(job.steps or [])
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "could not read steps off job %r for the failure detail",
            getattr(job, "job_id", "?"),
        )
        return ""
    for step in reversed(steps):
        status = getattr(step, "status", None) or (
            step.get("status") if isinstance(step, dict) else None
        )
        if status == "failed":
            return str(
                getattr(step, "detail", None)
                or (step.get("detail") if isinstance(step, dict) else "")
                or ""
            )
    return ""


def notify_outcome(
    shared_dir: Path,
    network: Optional[dict],
    q: QueuedApp,
    bot_id: str,
    *,
    status: str,
    detail: str,
    briefing_announce: Optional[dict],
) -> None:
    """Push the post-wrap outcome for one finished job. Failures are
    always loud; a completed morning-briefing install additionally
    pushes the activation receipt when the caller asked for one.
    Best-effort — a notification failure is logged, never raised, and
    never blocks the rest of the queue."""
    import logging

    dispatch = _outcome_dispatch or _default_outcome_dispatch
    try:
        from ...alerts.dispatcher import Severity
        from . import pack as _pack

        is_briefing = q.pkg_id == _pack.MORNING_BRIEFING_PKG_ID
        if status != "complete":
            dispatch(
                shared_dir=shared_dir,
                network=network,
                source="app_install",
                severity=Severity.WARNING,
                dedup_key=f"app-install/{q.job_id}",
                catalog_event="system.app_install_failed",
                payload={
                    "bot_id": bot_id,
                    "app_name": q.name,
                    "reason": _plain_failure_reason(
                        detail, bot_id=bot_id, app_name=q.name,
                        is_briefing=is_briefing,
                    ),
                },
            )
        elif briefing_announce and q.pkg_id == briefing_announce.get("pkg_id"):
            dispatch(
                shared_dir=shared_dir,
                network=network,
                source="app_install",
                severity=Severity.INFO,
                dedup_key=f"briefing-activated/{bot_id}/{q.job_id}",
                catalog_event="decisions.briefing_activated",
                payload={
                    "bot_id": bot_id,
                    "time": str(briefing_announce.get("time") or "07:00"),
                    "route_note": str(
                        briefing_announce.get("route_note") or "",
                    ),
                },
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "post-wrap install outcome notification failed for %s (%s) on %s",
            q.job_id, q.name, bot_id,
        )


# ── Public operation ──────────────────────────────────────────────────────────


_PLACEHOLDER_RE_TEMPLATE = r"\{%s\}"


def _apply_config_seed(build_spec: str, config_seed: dict) -> str:
    """Append the operator's decided values for any ``{placeholder}``
    the build spec declares (e.g. ``{mission}``, ``{delivery_time}`` in
    the morning-briefing spec). The forge agent uses these instead of
    leaving the placeholders empty — the seam the M4 proof run flagged
    (finding 8b: the wizard captured the mission but never passed it).
    """
    lines = []
    for key, value in config_seed.items():
        v = str(value or "").strip()
        if not v:
            continue
        if re.search(_PLACEHOLDER_RE_TEMPLATE % re.escape(str(key)), build_spec):
            lines.append(f"- {key}: {v}")
    if not lines:
        return build_spec
    return (
        build_spec
        + "\n\n## Operator-provided settings\n\n"
        + "Use these exact values wherever the spec references the "
        + "matching {placeholder}:\n\n"
        + "\n".join(lines)
        + "\n"
    )


def queue_pack_installs(
    shared_dir: Path,
    *,
    bot_id: str,
    apps: list[dict],
    privacy_seed: Optional[dict] = None,
    audience_scoping_seed: Optional[dict] = None,
    network: Optional[dict] = None,
    config_seed: Optional[dict] = None,
    briefing_announce: Optional[dict] = None,
) -> list[QueuedApp]:
    """Create one install job per app (in the given order) and start a
    single worker thread that runs them sequentially.

    ``apps`` is the AB_SHAPE selection: ``[{"pkg_id": …, "name": …}, …]``
    in bundle-declared (topological) order. Returns per-app outcomes —
    job created vs. failed-to-queue — so the success wrap can report
    honestly. Jobs that queue but later fail mid-build push a
    ``system.app_install_failed`` notification from the worker (and
    still carry their status on the Forge Jobs page).

    ``config_seed`` maps build-spec placeholder names to decided values
    (``{"mission": …, "delivery_time": …}``); only placeholders a spec
    actually declares are appended. ``briefing_announce`` (used by
    ``briefing_activation``) is ``{"pkg_id", "time", "route_note"}`` —
    when the matching install completes, the worker pushes the
    ``decisions.briefing_activated`` receipt.
    """
    from ...applications.forge_jobs import create_install_job, save_job
    from ...applications.gallery import load_gallery_package

    queued: list[QueuedApp] = []
    for app in apps:
        pkg_id = str((app or {}).get("pkg_id") or "")
        name = str((app or {}).get("name") or pkg_id)
        if not pkg_id:
            queued.append(QueuedApp(pkg_id="", app_id="", name=name,
                                    error="no package id"))
            continue
        try:
            pkg = load_gallery_package(pkg_id, shared_dir)
            if not isinstance(pkg, dict):
                queued.append(QueuedApp(
                    pkg_id=pkg_id, app_id="", name=name,
                    error="not found in the app gallery",
                ))
                continue
            # Same app_id derivation as the gallery install endpoint —
            # the slug is the manifest filename and the load_manifest key.
            raw_name = str(pkg.get("name") or pkg_id)
            app_id = raw_name.lower().replace(" ", "-").replace("_", "-")
            job = create_install_job(
                pkg_id=pkg_id,
                app_id=app_id,
                bot_id=bot_id,
                gallery_version=str(pkg.get("pkg_version") or ""),
                shared_dir=shared_dir,
                # The admin's explicit yes at AB_SHAPE + the plan confirm
                # is the operator confirmation for these builds.
                operator_confirmed=True,
            )
            build_spec = pkg.get("build_spec", "")
            if build_spec and isinstance(config_seed, dict) and config_seed:
                build_spec = _apply_config_seed(build_spec, config_seed)
            if build_spec:
                job.context_snapshot["build_spec"] = build_spec
            if isinstance(privacy_seed, dict) and privacy_seed:
                job.context_snapshot["privacy_seed"] = dict(privacy_seed)
            if isinstance(audience_scoping_seed, dict) and audience_scoping_seed:
                job.context_snapshot["audience_scoping_seed"] = dict(
                    audience_scoping_seed,
                )
            save_job(job, shared_dir)
            queued.append(QueuedApp(
                pkg_id=pkg_id, app_id=app_id,
                name=str(pkg.get("display_name") or name),
                job_id=job.job_id,
            ))
        except Exception as exc:
            queued.append(QueuedApp(
                pkg_id=pkg_id, app_id="", name=name, error=str(exc),
            ))

    to_run = [q for q in queued if q.ok]
    if to_run:
        runner = _install_runner or _default_install_runner

        def _worker() -> None:
            import logging

            from ...applications.forge_jobs import load_job

            for q in to_run:
                run_error = ""
                try:
                    runner(shared_dir, q.job_id, bot_id)
                except Exception as exc:
                    # A crash before any step leaves the job queued and
                    # retryable on the Forge Jobs page; keep going so
                    # one bad build doesn't strand the rest of the set.
                    run_error = str(exc)
                    logging.getLogger(__name__).error(
                        "add-bot pack install %s (%s) on %s failed: %s",
                        q.job_id, q.name, bot_id, exc,
                    )
                # Post-wrap honesty: report this job's final status —
                # the wrap already promised these apps, so a break in
                # that promise must reach the operator, not a log file.
                try:
                    job = load_job(q.job_id, shared_dir)
                except Exception:
                    job = None
                    logging.getLogger(__name__).warning(
                        "could not load job %s to report its outcome",
                        q.job_id,
                    )
                status = str(getattr(job, "status", "") or "unknown")
                detail = run_error or (_job_failure_detail(job) if job else "")
                notify_outcome(
                    shared_dir, network, q, bot_id,
                    status=status, detail=detail,
                    briefing_announce=briefing_announce,
                )

        if _force_sync:
            _worker()
        else:
            threading.Thread(
                target=_worker, daemon=True,
                name=f"add-bot-pack-{bot_id}",
            ).start()
    return queued

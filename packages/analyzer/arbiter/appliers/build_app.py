"""arbiter.appliers.build_app — Dispatch a BuildApp proposal to the forge.

The proposal carries a fully-formed manifest. This applier:

  1. Persists the manifest to ``{shared_dir}/applications/{bot_id}/{app_id}.json``
  2. Creates a ``ForgeJob`` with a synthetic ``chat-<hex>`` pkg_id, using
     the standard 10-step install sequence
  3. Kicks off the forge pipeline asynchronously (daemon thread) with
     ``auto_approve_actor`` set so the operator gate is skipped — they
     already approved by clicking Act on the proposal
  4. Returns immediately with the ``job_id`` recorded in details so a
     downstream sweep can poll forge for completion

The proposal lands in ``applied`` while forge runs. ``arbiter.forge_sweep``
(invoked from the runner cycle) watches the ForgeJob and transitions the
proposal to ``succeeded`` or ``failed_flagged`` based on the final job
status.

This applier intentionally does NOT register a synchronous "wait for
forge" path. Forge runs can take minutes; blocking the apply call would
both block the admin server and conflate "applier done" with "forge
done", which are different.

Test seam: the kickoff runner is overridable via ``set_kickoff_runner``
(matches the wizard's forge_handlers pattern) so unit tests don't need
the real forge_engine.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Callable

from evolve_config import CANONICAL_SHARED_DIR

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import BuildApp


logger = logging.getLogger(__name__)


_AUTO_APPROVE_ACTOR_DEFAULT = "api:rsi-buildapp"

# Default shared dir; tests / non-default deployments override via
# :func:`set_shared_dir`. Mirrors the pattern in manifest_update and
# throttle_generator appliers.
_SHARED_DIR = CANONICAL_SHARED_DIR


def set_shared_dir(path: Path | str) -> None:
    """Override the shared_dir used by this applier (test seam)."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _shared_dir_path() -> Path:
    return _SHARED_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Test seam — substitute the kickoff runner for unit tests
# ─────────────────────────────────────────────────────────────────────────────


# Signature: (shared_dir, job_id, bot_id) -> None. Should run the forge
# pipeline (synchronously is fine for tests; production runs it async).
KickoffRunner = Callable[[Path, str, str], None]
_kickoff_runner: KickoffRunner | None = None


def set_kickoff_runner(fn: KickoffRunner | None) -> None:
    """Substitute the kickoff runner. Tests use this to avoid pulling in
    the heavy forge_engine module; production leaves it unset and uses
    :func:`_default_kickoff_runner`."""
    global _kickoff_runner
    _kickoff_runner = fn


def _mark_job_failed_after_crash(
    shared_dir: Path, job_id: str, exc_text: str
) -> None:
    """Mark a forge job as failed after the daemon thread crashed.

    Without this, a job that died mid-step keeps its in-flight status
    forever — ``forge_sweep`` watches the job and would never transition
    the BuildApp proposal out of ``applied``. Marking the job ``failed``
    with the exception text in the last running step's ``detail`` lets
    the next sweep promote the proposal to ``failed_flagged``.

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
        except Exception:
            pass


def _default_kickoff_runner(
    shared_dir: Path, job_id: str, bot_id: str
) -> None:
    """Production path: run the existing forge pipeline with auto-approve.

    Imported lazily so this module can be loaded in environments that
    don't have the forge_engine deps installed (e.g. trimmed analyzer
    test envs).
    """
    try:
        from evolve_admin.applications import forge_engine as _fe  # type: ignore
    except ImportError as exc:
        logger.warning(
            "build_app: forge_engine import failed (%s); job %s left in queued state",
            exc,
            job_id,
        )
        # forge_engine isn't importable on this host; the job will sit
        # in queued forever otherwise. Mark it failed so forge_sweep can
        # close out the proposal.
        _mark_job_failed_after_crash(
            shared_dir, job_id, f"forge_engine import failed: {exc}"
        )
        return

    try:
        _fe.run_forge_job(
            job_id=job_id,
            shared_dir=shared_dir,
            bot_id=bot_id,
            auto_approve_actor=_AUTO_APPROVE_ACTOR_DEFAULT,
        )
    except Exception as exc:  # noqa: BLE001 — forge errors surface via job status
        logger.warning(
            "build_app: forge run raised for job %s: %s", job_id, exc
        )
        _mark_job_failed_after_crash(shared_dir, job_id, str(exc))


def _resolve_runner() -> KickoffRunner:
    return _kickoff_runner or _default_kickoff_runner


# ─────────────────────────────────────────────────────────────────────────────
# Manifest persistence
# ─────────────────────────────────────────────────────────────────────────────


def _save_manifest(manifest: dict, *, bot_id: str, app_id: str, shared_dir: Path) -> Path:
    """Atomically write the manifest to the bot's applications dir."""
    target = shared_dir / "applications" / bot_id / f"{app_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{app_id}-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _synthetic_pkg_id() -> str:
    """A ``chat-<8hex>`` pkg_id — same convention the wizard uses for
    messaging-driven builds. Distinguishes RSI-driven and chat-driven
    builds from gallery installs (which use ``p-<8hex>``)."""
    return f"chat-{secrets.token_hex(4)}"


def _create_forge_job(
    *,
    pkg_id: str,
    app_id: str,
    bot_id: str,
    shared_dir: Path,
):
    """Create + persist a ForgeJob with the install step sequence.

    Reuses the same shape ``forge_handlers.create_chat_install_job`` uses
    so the existing forge pipeline runs unchanged.
    """
    from evolve_admin.applications import forge_jobs as _fj  # type: ignore
    from evolve_admin.applications.ids import now_iso  # type: ignore

    ts = now_iso()
    job = _fj.ForgeJob(
        job_id=_fj.new_job_id(),
        run_id=_fj.run_id_for(1),
        job_type="install",
        pkg_id=pkg_id,
        app_id=app_id,
        bot_id=bot_id,
        pkg_version_before=None,
        gallery_version=None,
        steps=_fj._install_steps(),
        created_at=ts,
        last_updated=ts,
    )
    _fj.save_job(job, shared_dir)
    return job


# ─────────────────────────────────────────────────────────────────────────────
# Applier
# ─────────────────────────────────────────────────────────────────────────────


class BuildAppApplier:
    """Applier for BuildApp proposals.

    Apply: persist manifest, create forge job, kick off forge async,
    return immediately with job_id in details.

    Revert: best-effort — mark the forge job rejected (forge_engine
    handles in-flight cancellation) and remove the manifest. Forge work
    that already happened can't be undone, but we restore the operator's
    pre-apply state on disk.
    """

    def capture_snapshot(self, action: BuildApp, bot_id: str) -> dict:
        # Record bot_id and the path we're about to write so revert can
        # remove it. Whether the manifest existed before is captured for
        # safety — production should never overwrite an existing manifest
        # with BuildApp (use ManifestUpdate for that), but the snapshot
        # records prior state if it does happen.
        shared = _shared_dir_path()
        target = shared / "applications" / bot_id / f"{action.app_id}.json"
        prior_exists = target.exists()
        prior_content: dict | None = None
        if prior_exists:
            try:
                prior_content = json.loads(target.read_text())
            except Exception:
                prior_content = None
        return {
            "action_kind": "BuildApp",
            "bot_id": bot_id,
            "app_id": action.app_id,
            "manifest_path": str(target),
            "prior_existed": prior_exists,
            "prior_content": prior_content,
        }

    def apply(self, action: BuildApp, bot_id: str) -> ApplyResult:
        # bot_id from the proposal must match the action's bot_id —
        # otherwise something upstream got confused. Fail closed.
        if action.bot_id and action.bot_id != bot_id:
            return ApplyResult(
                ok=False,
                details={"reason": "bot_id mismatch"},
                message=(
                    f"BuildApp.bot_id ({action.bot_id!r}) does not match "
                    f"proposal.bot_id ({bot_id!r}); refusing to apply"
                ),
            )

        if not isinstance(action.manifest, dict) or not action.manifest:
            return ApplyResult(
                ok=False,
                details={"reason": "empty manifest"},
                message="BuildApp.manifest is empty or not a dict",
            )

        shared = _shared_dir_path()

        # Pre-check: refuse to clobber an existing manifest. BuildApp is
        # for new apps; updates to an existing manifest belong to
        # ManifestUpdate. If a proposal lands here for an app that already
        # exists on this bot, fail loudly so the operator sees it rather
        # than silently replacing the live manifest.
        target = shared / "applications" / bot_id / f"{action.app_id}.json"
        if target.exists():
            return ApplyResult(
                ok=False,
                details={
                    "reason": "manifest_already_exists",
                    "app_id": action.app_id,
                    "bot_id": bot_id,
                    "manifest_path": str(target),
                    "fail_action": "flag",
                },
                message=(
                    f"manifest {action.app_id!r} already exists on bot "
                    f"{bot_id!r}; BuildApp refuses to overwrite. Use "
                    "ManifestUpdate to evolve an existing app."
                ),
            )

        # Step 1: persist the manifest. This is the file forge will read
        # to drive code generation.
        try:
            manifest_path = _save_manifest(
                action.manifest,
                bot_id=bot_id,
                app_id=action.app_id,
                shared_dir=shared,
            )
        except Exception as exc:  # noqa: BLE001 — surface a clean error
            return ApplyResult(
                ok=False,
                details={"reason": f"manifest write failed: {exc}"},
                message=f"failed to persist manifest: {exc}",
            )

        # Step 2: create the forge job.
        try:
            pkg_id = _synthetic_pkg_id()
            job = _create_forge_job(
                pkg_id=pkg_id,
                app_id=action.app_id,
                bot_id=bot_id,
                shared_dir=shared,
            )
        except Exception as exc:  # noqa: BLE001
            return ApplyResult(
                ok=False,
                details={
                    "reason": f"forge job creation failed: {exc}",
                    "manifest_path": str(manifest_path),
                },
                message=f"failed to create forge job: {exc}",
            )

        # Step 3: kick off forge asynchronously. The applier returns now;
        # the forge sweep watches job status and transitions the proposal
        # to succeeded/failed_flagged when forge resolves.
        runner = _resolve_runner()
        thread = threading.Thread(
            target=runner,
            args=(shared, job.job_id, bot_id),
            name=f"build_app-{job.job_id}",
            daemon=True,
        )
        thread.start()

        return ApplyResult(
            ok=True,
            details={
                "job_id": job.job_id,
                "pkg_id": pkg_id,
                "app_id": action.app_id,
                "manifest_path": str(manifest_path),
            },
            message=(
                f"forge job {job.job_id} queued for {action.app_id}; "
                "build status will surface as the proposal resolves"
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        # Best-effort: restore the pre-apply manifest state on disk. Any
        # forge work already in flight can't be undone — the operator
        # would need to manually clean the forge job entry.
        manifest_path = Path(snapshot.get("manifest_path", ""))
        if not manifest_path:
            return RevertResult(
                ok=True, message="no manifest path recorded; nothing to revert"
            )
        if snapshot.get("prior_existed"):
            prior = snapshot.get("prior_content")
            if prior is None:
                return RevertResult(
                    ok=False,
                    message=(
                        "prior manifest existed but content wasn't captured; "
                        "manual cleanup required"
                    ),
                )
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(prior, indent=2))
            except OSError as exc:
                return RevertResult(
                    ok=False,
                    message=f"failed to restore prior manifest: {exc}",
                )
            return RevertResult(
                ok=True, message="restored prior manifest content"
            )
        # No prior file: just remove what we wrote
        try:
            if manifest_path.exists():
                manifest_path.unlink()
        except OSError as exc:
            return RevertResult(
                ok=False,
                message=f"failed to remove manifest: {exc}",
            )
        return RevertResult(ok=True, message="removed manifest written by apply")


register_applier("BuildApp", BuildAppApplier())

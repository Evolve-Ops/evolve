"""arbiter.appliers.install_app — Dispatch an InstallApp proposal to the forge.

``InstallApp(app_id, source="gallery")`` is minted by the Fit Reviewer
(``fit_review.gates._build_install_proposal``) when a bot's own reflection,
gated deterministically, names a gallery package that would cover an
evidenced need. Note the field name: **``action.app_id`` carries the gallery
catalog key** (``p-<8hex>``), not an app identity — the gate builds it as
``InstallApp(app_id=pkg_id, ...)``. The app's own id is the slug derived from
the package name, exactly as every other install surface derives it.

What this applier does is the cheap, synchronous half: validate the package,
run preflight, create the ForgeJob, and hand off. Forge then runs the install
(a few minutes) in a daemon thread. The proposal lands in ``applied`` and
``arbiter.forge_sweep`` promotes it to ``succeeded`` / ``failed_flagged`` from
the job's terminal status — the same division of labour ``build_app`` uses,
and the reason ``InstallApp`` is in ``apply._EXTERNAL_COMPLETION_KINDS``.
Blocking the apply call on forge would both block the admin server and
conflate "applier done" with "install done".

Reuse. The install sequence here is the one the admin UI's Install button,
``evo install`` and the ``action.app.install`` tool all run:
``load_gallery_package`` → ``preflight_check`` → ``create_install_job``
(+ the package's ``build_spec`` in the job context) → dispatch. What this
applier deliberately does NOT reimplement is the gallery route's richer
orchestration — OAuth prerequisite handling and dependency chains. A package
needing either is refused with ``fail_action="flag"``, which routes the
proposal to the operator-review queue with the reason attached, rather than
half-installed. The forge dispatch tail is shared with ``build_app`` via
``_forge_kickoff``.

Reversibility. The Fit Reviewer tags these ``reversibility="manual"`` and
carries no ``Claim``, so ``arbiter.apply`` never captures a snapshot in
production. ``revert`` reports the manual step rather than pretending to an
undo it structurally cannot perform — see its docstring.

Test seams: ``set_shared_dir``, and ``_kickoff_runner`` for the forge
dispatch.
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Callable, cast

from evolve_config import CANONICAL_SHARED_DIR

from arbiter.appliers._forge_kickoff import run_forge_job_kickoff
from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import Action, InstallApp

logger = logging.getLogger(__name__)


# Names this applier as the actor that stood in for forge's operator gate.
# The operator already approved by clicking Act on the proposal.
_AUTO_APPROVE_ACTOR = "api:rsi-installapp"

_SHARED_DIR = CANONICAL_SHARED_DIR


def set_shared_dir(path: Path | str) -> None:
    """Override the shared_dir used by this applier (test seam)."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _shared_dir_path() -> Path:
    return _SHARED_DIR


# Signature: (shared_dir, job_id, bot_id) -> None. Tests substitute one by
# patching this module attribute; production leaves it None. No setter and no
# named default: a test-only setter is a function nothing in production calls,
# and a default referenced only as a value is dead to every caller-graph check
# that reads calls.
KickoffRunner = Callable[[Path, str, str], None]
_kickoff_runner: KickoffRunner | None = None


def _resolve_runner() -> KickoffRunner:
    if _kickoff_runner is not None:
        return _kickoff_runner
    return functools.partial(
        run_forge_job_kickoff,
        auto_approve_actor=_AUTO_APPROVE_ACTOR,
        log_label="install_app",
    )


def _app_id_from_package(pkg: dict, pkg_id: str) -> str:
    """The slug every install surface derives from the package name.

    Kept identical to ``gallery_routes.api_gallery_install`` /
    ``evo.handlers.install`` / ``action_app._install_handler``: a fourth
    spelling of the same id would install the same package under a name the
    other three cannot find.
    """
    raw_name = pkg.get("name", pkg_id)
    return str(raw_name).lower().replace(" ", "-").replace("_", "-")


def _blocking_preflight_items(preflight: dict) -> list[dict]:
    """Unsatisfied build blockers, minus integrations.

    Same filter ``evo.handlers.install`` applies: integration items are the
    OAuth axis, which this applier refuses on separately and with a clearer
    message than "preflight blocker".
    """
    items = list(preflight.get("app_dependencies") or []) + list(
        preflight.get("requirements") or []
    )
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("severity") == "build_blocker"
        and item.get("state") != "satisfied"
        and item.get("type") != "integration"
    ]


def _missing_integrations(preflight: dict) -> list[dict]:
    """Unsatisfied integration requirements — the OAuth axis.

    The gallery route can park a job in ``awaiting_oauth`` and resume it when
    the operator finishes the flow in the UI. There is no such flow behind a
    proposal's Act button, so these are refused rather than queued into a
    state nothing here will ever advance.
    """
    return [
        item
        for item in (preflight.get("requirements") or [])
        if isinstance(item, dict)
        and item.get("type") == "integration"
        and item.get("state") != "satisfied"
        and item.get("severity") in ("build_blocker", "runtime_warning")
    ]


def _flag(message: str, **details) -> ApplyResult:
    """A refusal that reaches the operator-review queue.

    ``fail_action="flag"`` is read by ``arbiter.apply``: the proposal
    transitions to ``failed_flagged`` instead of sitting at ``approved_*``
    forever. Every refusal below is pre-side-effect — no job was created.
    """
    return ApplyResult(
        ok=False, details={"fail_action": "flag", **details}, message=message
    )


class InstallAppApplier:
    def capture_snapshot(self, action: Action, bot_id: str) -> dict:
        install = cast(InstallApp, action)
        # Nothing has been written yet and nothing pre-existing is touched,
        # so there is no prior state to restore — the snapshot records what
        # the apply is about to attempt, which is all revert has to name it
        # by. (In production this is never called: the Fit Reviewer's
        # proposals carry no Claim, and apply.py only snapshots when one is
        # present.)
        return {
            "action_kind": "InstallApp",
            "bot_id": bot_id,
            "pkg_id": install.app_id,
            "source": install.source,
        }

    def apply(self, action: Action, bot_id: str) -> ApplyResult:
        install = cast(InstallApp, action)
        pkg_id = (install.app_id or "").strip()
        shared = _shared_dir_path()

        if not pkg_id:
            return _flag("InstallApp carries no app_id (gallery pkg_id)")
        if install.source != "gallery":
            # "custom" has no installer behind it. Refusing beats inventing
            # a source-specific path nothing upstream emits.
            return _flag(
                f"InstallApp source {install.source!r} is not installable; "
                "only gallery packages can be installed from a proposal",
                pkg_id=pkg_id,
                source=install.source,
            )

        try:
            from evolve_admin.applications.gallery import (  # type: ignore
                installed_state,
                load_gallery_package,
                preflight_check,
            )
            from evolve_admin.applications.forge_jobs import (  # type: ignore
                create_install_job,
                save_job,
            )
        except ImportError as exc:
            # Not a flag: the pod is missing the admin package, which is an
            # environment fault, not a bad proposal. Leave it approved so a
            # later sweep on a healthy host can apply it.
            return ApplyResult(
                ok=False,
                details={"reason": "applications_unavailable"},
                message=f"applications package unavailable: {exc}",
            )

        try:
            pkg = load_gallery_package(pkg_id, shared)
        except Exception as exc:  # noqa: BLE001 — surface a clean refusal
            return _flag(f"gallery package load failed: {exc}", pkg_id=pkg_id)
        if pkg is None:
            return _flag(f"gallery package {pkg_id!r} not found", pkg_id=pkg_id)

        app_id = _app_id_from_package(pkg, pkg_id)

        # Already installed / installing → refuse. A second forge job for a
        # package the bot already runs is how a bot ends up with two copies
        # of one app; ``installed_state`` is the shared answer preflight and
        # the install-chain orchestrator both use.
        try:
            state = installed_state(pkg_id, bot_id, shared)
        except Exception as exc:  # noqa: BLE001
            return _flag(f"install-state check failed: {exc}", pkg_id=pkg_id)
        if state in ("installed", "installing"):
            return _flag(
                f"{bot_id} already has {pkg_id} ({state}); refusing to "
                "install a second copy",
                pkg_id=pkg_id,
                app_id=app_id,
                installed_state=state,
            )

        try:
            preflight = preflight_check(pkg_id, bot_id, shared)
        except Exception as exc:  # noqa: BLE001
            return _flag(f"preflight check failed: {exc}", pkg_id=pkg_id)

        oauth = _missing_integrations(preflight)
        if oauth:
            names = ", ".join(
                str(i.get("display_name") or i.get("id") or "?") for i in oauth[:6]
            )
            return _flag(
                f"{pkg_id} needs an integration set up first ({names}); the "
                "OAuth flow runs from the admin Apps page, not from a proposal",
                pkg_id=pkg_id,
                app_id=app_id,
                missing_integrations=names,
            )

        blockers = _blocking_preflight_items(preflight)
        if blockers:
            summary = "; ".join(
                f"{b.get('display_name') or b.get('id') or '?'}: "
                f"{b.get('message') or b.get('state') or 'unsatisfied'}"
                for b in blockers[:6]
            )
            return _flag(
                f"preflight found {len(blockers)} unresolved build blocker"
                f"{'s' if len(blockers) != 1 else ''} for {pkg_id}: {summary}",
                pkg_id=pkg_id,
                app_id=app_id,
                blocker_count=len(blockers),
            )

        try:
            job = create_install_job(
                pkg_id=pkg_id,
                app_id=app_id,
                bot_id=bot_id,
                gallery_version=pkg.get("pkg_version", ""),
                shared_dir=shared,
            )
            build_spec = pkg.get("build_spec", "")
            if build_spec:
                # The gallery/chat installs both stash the spec on the job;
                # forge reads it from there, so an install queued without it
                # builds from a thinner brief than the same package would
                # from the Apps page.
                job.context_snapshot["build_spec"] = build_spec
                save_job(job, shared)
        except Exception as exc:  # noqa: BLE001
            logger.exception("install_app: create_install_job raised")
            return ApplyResult(
                ok=False,
                details={"reason": "job_creation_failed", "pkg_id": pkg_id},
                message=f"failed to create forge install job: {exc}",
            )

        runner = _resolve_runner()
        threading.Thread(
            target=runner,
            args=(shared, job.job_id, bot_id),
            name=f"install_app-{job.job_id}",
            daemon=True,
        ).start()

        return ApplyResult(
            ok=True,
            details={
                "job_id": job.job_id,
                "pkg_id": pkg_id,
                "app_id": app_id,
                "bot_id": bot_id,
                "gallery_version": pkg.get("pkg_version", ""),
            },
            message=(
                f"forge install job {job.job_id} queued for {pkg_id} on "
                f"{bot_id}; the proposal resolves when forge finishes"
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        """There is no automatic undo, and this says so rather than pretending.

        The applier's only side effect is a forge job, whose id exists only
        *after* apply returns — it lands in ``ApplyResult.details`` and, via
        ``arbiter.apply``, in ``provenance.signals["_apply_details"]``, which
        is where ``forge_sweep`` reads it. ``revert`` is handed the
        **pre-apply** snapshot and nothing else (``verify.dispatch``
        ``_attempt_revert``), so it cannot reach that job: a cancel path here
        would be code that never runs on a real proposal.

        So this reports the manual step instead. It matches what the Fit
        Reviewer already tags these proposals: ``reversibility="manual"``.
        """
        # identity: see applications.app_identity.resolve_app_id — this is the
        # GALLERY CATALOG KEY the proposal named, echoed verbatim into an
        # operator message. Nothing is installed, so there is no manifest for
        # the resolver to read, and naming a different id than the proposal
        # showed would make the message point at the wrong thing.
        pkg_id = snapshot.get("pkg_id") or "?"
        return RevertResult(
            ok=False,
            details={"pkg_id": pkg_id, "bot_id": bot_id, "manual": True},
            message=(
                f"InstallApp has no automatic revert: the install of {pkg_id} "
                f"on {bot_id} runs in forge. Cancel it from Apps → Forge Jobs "
                "if it is still running, or uninstall the app from the Apps "
                "page if it finished."
            ),
        )


register_applier("InstallApp", InstallAppApplier())

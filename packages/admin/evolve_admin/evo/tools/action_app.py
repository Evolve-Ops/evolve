"""Action tool — install an app from the gallery onto a bot.

Phase 1.4 step 2. App installation is one of the few operations that
runs asynchronously: the tool creates a forge install job (cheap,
synchronous handshake), the forge sweep daemon picks it up and runs
the multi-step install in the background (~1–3 minutes), and the
post-action verify path reads the forge job state via the
``pod_state.forge_job`` companion tool defined below.

This is a write_risky operation — installing an app touches:
  - app manifests in the bot's workspace
  - sometimes plugin entries in openclaw.json
  - the forge job queue (which the operator + daemon both observe)

Under ``ask`` / ``auto-small`` authority, evo stages a confirmation
offer in chat (per AGENTS.md resolver-pattern teaching). Under
``auto``, runs directly.

OAuth-required apps (Slack, Google Workspace, etc.) handle the OAuth
prerequisite gate via the existing oauth_orchestrator. For v1, this
tool does NOT bypass that gate — if an app needs OAuth setup, the
job lands in ``awaiting_oauth`` state and the response surfaces the
gap. Operator completes OAuth (still UI-mediated for now); the
forge sweep resumes the job automatically.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). ``pkg_id`` is the GALLERY PACKAGE key the caller
# names: it is a required argument in this tool's JSON schema, the lookup key
# into load_gallery_package, and the ForgeJob's package slot. It travels
# alongside ``app_id`` (the app's own identity) rather than replacing it —
# ``job.pkg_id`` and the app id are deliberately different strings for a
# messaging- or RSI-driven build.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── action.app.install ──────────────────────────────────────────────────────


def _install_handler(
    shared_dir: Path,
    network_path: Path,
    bot_id: str,
    pkg_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Kick off a gallery-app install job for ``bot_id``.

    Returns the forge job id so the model can poll status via
    ``pod_state.forge_job``. The install itself is async (forge
    sweep runs it in the background); ok=True means the JOB was
    successfully created, not that the install has completed.
    """
    # Lazy imports — the applications package is heavy.
    try:
        from evolve_admin.applications.gallery import load_gallery_package
        from evolve_admin.applications.forge_jobs import create_install_job
    except ImportError as exc:
        return {"ok": False, "error": f"applications package unavailable: {exc}"}

    # Validate bot exists
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}
    if bot_id not in (net.get("bots") or {}):
        return {"ok": False, "error": f"bot '{bot_id}' is not registered"}

    # Load gallery package
    try:
        pkg = load_gallery_package(pkg_id, shared_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"gallery package load failed: {exc}"}
    if pkg is None:
        return {"ok": False, "error": f"gallery package '{pkg_id}' not found"}

    gallery_version = pkg.get("pkg_version", "")
    raw_name = pkg.get("name", pkg_id)
    app_id = raw_name.lower().replace(" ", "-").replace("_", "-")

    try:
        job = create_install_job(
            pkg_id=pkg_id,
            app_id=app_id,
            bot_id=bot_id,
            gallery_version=gallery_version,
            shared_dir=shared_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.app.install: create_install_job raised")
        return {"ok": False, "error": f"job creation failed: {exc}"}

    return {
        "ok": True,
        "bot_id": bot_id,
        "pkg_id": pkg_id,
        "app_id": app_id,
        "gallery_version": gallery_version,
        "job_id": job.job_id,
        "job_status": getattr(job, "status", "created"),
        "reason": reason or "evo installed via tool",
        # Post-action verify hint (spec §3.7 lever #4).
        # Install runs async via forge_sweep; the model polls
        # job status here. When status is 'completed' the install
        # finished successfully.
        "verify_via": {
            "tool": "pod_state.forge_job",
            "args": {"job_id": job.job_id},
            "expect": (
                "job_status transitions to 'completed' within "
                "1–3 minutes; intermediate states are 'queued', "
                "'running', 'awaiting_oauth' (OAuth prerequisite)."
            ),
        },
    }


def _install_validate(
    shared_dir: Path,
    network_path: Path,
    bot_id: str,
    pkg_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists + gallery package exists."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not pkg_id:
        return {"ok": False, "reason": "pkg_id is required"}

    try:
        from evolve_admin.config import load_network
        from evolve_admin.applications.gallery import load_gallery_package
    except ImportError as exc:
        return {"ok": False, "reason": f"applications package unavailable: {exc}"}

    net = load_network(network_path)
    if bot_id not in (net.get("bots") or {}):
        return {"ok": False, "reason": f"bot '{bot_id}' is not registered"}

    pkg = load_gallery_package(pkg_id, shared_dir)
    if pkg is None:
        return {
            "ok": False,
            "reason": f"gallery package '{pkg_id}' not found in pod catalog",
        }

    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "pkg_id": pkg_id,
            "pkg_name": pkg.get("name", pkg_id),
            "gallery_version": pkg.get("pkg_version", "?"),
            "estimated_duration_s": 60,
        },
    }


INSTALL_TOOL = Tool(
    name="action.app.install",
    description=(
        "Install a gallery app onto a bot. Creates a forge install job; "
        "the forge sweep daemon runs the install in the background "
        "(~1–3 minutes). Returns immediately with the job_id; the model "
        "can poll status via pod_state.forge_job. OAuth-required apps "
        "(Slack, Google Workspace, etc.) land in 'awaiting_oauth' state "
        "until the operator completes the OAuth prerequisite; the sweep "
        "auto-resumes the job after."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot to install on (from network.json::bots).",
            },
            "pkg_id": {
                "type": "string",
                "description": (
                    "Gallery package id — get from the pod's gallery "
                    "catalog. The package's pkg_version is used to "
                    "pin the install."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the audit log.",
            },
        },
        "required": ["bot_id", "pkg_id"],
        "additionalProperties": False,
    },
    handler=_install_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_install_validate,
    tags=("action", "app", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(INSTALL_TOOL)


# ─── pod_state.forge_job (companion read tool) ───────────────────────────────


def _forge_job_handler(
    shared_dir: Path,
    job_id: str,
) -> dict[str, Any]:
    """Look up a single forge job by id. Read-only. Used by
    ``action.app.install``'s verify_via to poll install completion.
    """
    try:
        from evolve_admin.applications.forge_jobs import load_job
    except ImportError as exc:
        return {"ok": False, "error": f"applications package unavailable: {exc}"}
    job = load_job(job_id, shared_dir)
    if job is None:
        return {"ok": False, "error": f"forge job '{job_id}' not found"}
    return {
        "ok": True,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "bot_id": job.bot_id,
        # identity: see resolve_app_id — the ForgeJob's package slot, reported beside its app_id; two fields, two questions.
        "pkg_id": job.pkg_id,
        "app_id": job.app_id,
        "status": getattr(job, "status", "?"),
        "created_at": job.created_at,
        "last_updated": job.last_updated,
        "current_step": next(
            (s.name for s in job.steps if getattr(s, "status", "") == "running"),
            None,
        ),
        "completed_steps": [
            s.name for s in job.steps if getattr(s, "status", "") == "completed"
        ],
        "failed_steps": [
            s.name for s in job.steps if getattr(s, "status", "") == "failed"
        ],
    }


# ─── action.app.audit (Phase 1.5a) ───────────────────────────────────────────


def _audit_handler(
    network_path: Path,
    bot_id: str,
    app_id: str | None = None,
    full_audit: bool = False,
    all_apps: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Queue a Tier-3 app audit on the bot and kick the runner.

    Wraps ``applications.audit_dispatch.request_audit`` — same path the
    UI's per-app Audit button uses. The audit itself runs in the bot's
    workspace (via launchctl); this just writes the request to the
    bot's audit inbox and kicks the runner.

    Returns immediately with the request_id. Audit completion is
    surfaced in the bot's outbox; not currently exposed as a read tool,
    so verify_via points at the request_id and tells the model to
    inform the operator that the audit is queued (no synchronous result).
    """
    try:
        from evolve_admin.applications.audit_dispatch import request_audit
        from evolve_admin.config import get_bot_user, load_network
    except ImportError as exc:
        return {"ok": False, "error": f"applications package unavailable: {exc}"}

    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    if bot_id not in (net.get("bots") or {}):
        return {"ok": False, "error": f"bot '{bot_id}' is not registered"}

    bot_user = get_bot_user(bot_id, net)
    if not bot_user:
        return {"ok": False, "error": f"could not resolve user for bot '{bot_id}'"}

    if not all_apps and not app_id:
        return {
            "ok": False,
            "error": "either app_id or all_apps=true is required",
        }

    apps_arg = None if all_apps else [app_id]
    try:
        result = request_audit(
            bot_id=bot_id,
            bot_user=bot_user,
            apps=apps_arg,
            full_audit=bool(full_audit),
            requested_by="evo:tool",
            kick=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.app.audit: request_audit raised")
        return {"ok": False, "error": f"audit request failed: {exc}"}

    return {
        "ok": bool(result.ok),
        "bot_id": bot_id,
        "request_id": result.request_id,
        "apps": apps_arg or "all",
        "full_audit": bool(full_audit),
        "kicked": bool(getattr(result, "kicked", False)),
        "reason": reason or "evo requested audit via tool",
        "error": getattr(result, "error", "") or "",
        # Audit runs asynchronously inside the bot. Regressed findings
        # surface as Signals on the next security_warden sweep; clean
        # runs produce no new signals. We point verify_via at the
        # signals stream filtered to this bot so the model can both:
        #   - confirm no new firing signals (clean audit)
        #   - find any new findings to triage (regressions)
        # Note: the audit takes minutes; the verify is meaningful
        # after that delay, not immediately.
        "verify_via": {
            "tool": "pod_state.signals.firing",
            "args": {"bot_id": bot_id},
            "expect": (
                "After the audit completes (~minutes), any regressed "
                "findings surface as new firing signals on this bot. "
                "Clean runs produce no new signals."
            ),
        },
    }


def _audit_validate(
    network_path: Path,
    bot_id: str,
    app_id: str | None = None,
    full_audit: bool = False,
    all_apps: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: bot exists, target is well-specified."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not all_apps and not app_id:
        return {
            "ok": False,
            "reason": "either app_id or all_apps=true is required",
        }
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"network.json read failed: {exc}"}
    if bot_id not in (net.get("bots") or {}):
        return {"ok": False, "reason": f"bot '{bot_id}' is not registered"}
    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "scope": "all apps" if all_apps else app_id,
            "full_audit": bool(full_audit),
        },
    }


AUDIT_TOOL = Tool(
    name="action.app.audit",
    description=(
        "Queue a Tier-3 app audit on a bot and kick the runner. The "
        "audit runs asynchronously inside the bot's workspace (~minutes); "
        "this tool returns immediately with a request_id. Use to "
        "re-evaluate an app whose code may have drifted from its "
        "manifest, or to spot-check before relying on the app for a "
        "high-stakes interaction. full_audit=true re-evaluates "
        "previously-accepted findings; all_apps=true scopes the audit "
        "to every eligible app on the bot."
    ),
    wire_description=(
        "Queue a Tier-3 app audit on a bot and kick the runner (async, "
        "~minutes; returns a request_id immediately). Use to re-evaluate an "
        "app whose code may have drifted from its manifest. full_audit=true "
        "re-evaluates previously-accepted findings; all_apps=true audits "
        "every eligible app on the bot."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "app_id": {
                "type": "string",
                "description": (
                    "App id to audit (required unless all_apps=true)."
                ),
            },
            "all_apps": {
                "type": "boolean",
                "description": "Audit every eligible app on the bot.",
                "default": False,
            },
            "full_audit": {
                "type": "boolean",
                "description": (
                    "Re-evaluate findings the operator previously accepted."
                ),
                "default": False,
            },
            "reason": {
                "type": "string",
                "description": "Reason for the audit request (audit log).",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_audit_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_audit_validate,
    tags=("action", "app", "audit"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(AUDIT_TOOL)


# ─── pod_state.forge_job tool definition ─────────────────────────────────────


FORGE_JOB_TOOL = Tool(
    name="pod_state.forge_job",
    description=(
        "Look up the status of one forge job by id. Use after "
        "action.app.install to confirm the install completed. Status "
        "values: 'created', 'queued', 'running', 'completed', 'failed', "
        "'awaiting_oauth'. The current_step / completed_steps / "
        "failed_steps fields surface mid-flight progress."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Forge job id (eg 'j-abc12345').",
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    handler=_forge_job_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "app", "forge"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(FORGE_JOB_TOOL)

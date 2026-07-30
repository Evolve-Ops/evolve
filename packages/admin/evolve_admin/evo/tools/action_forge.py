"""Action tools — Forge job approve/reject.

Closes two of the remaining gaps from
``docs/audit-evo-tool-coverage-2026-06-02.md`` for the Improve → Apps
→ Forge surface:

* ``action.forge.approve(job_id, notes?)`` — POST
  ``/api/forge/jobs/<job_id>/approve``. Same code path as the **✓
  Approve** button on a forge job row awaiting operator sign-off.

* ``action.forge.reject(job_id, reason?)`` — POST
  ``/api/forge/jobs/<job_id>/reject``. Same code path as the **✗
  Reject** button.

The matching ``action.forge.{dispatch,cancel}`` siblings are deferred —
dispatch is the "kick off a job from a manifest" entrypoint and wants
schema-level coupling with the BuildApp action_kind (which already
exists via ``action.proposal.apply``); cancel is rare enough to leave
on the UI for now. See the audit doc's medium-priority forge family
entry for the partial close.

Routes through admin-ui's HTTP surface (same shape PR #1982
established) — forge state lives under ``{shared_dir}/forge/`` and is
owned by the evolve user; evo's gateway has read access only.

Risk tier:
  * ``action.forge.approve`` — ``write_risky``. Approving a forge job
    starts a real build (npm/git/llm operations); reversible in the
    sense that the job can be cancelled mid-flight but cannot easily
    be undone post-completion. Requires confirmation under ``ask``;
    auto-execute only under ``auto`` authority.
  * ``action.forge.reject`` — ``write_safe``. Rejection marks the job
    rejected and doesn't run anything destructive. Easy to revisit
    by dispatching a new job.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Job IDs are typically short alphanumeric + dash/underscore. Reject
# unicode look-alikes and shell metacharacters here before they hit
# the URL path. Mirrors action_plugin._BOT_ID_RE / _PLUGIN_NAME_RE.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 10,
) -> tuple[int, dict[str, Any] | None, str | None]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen_admin(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        parsed: dict[str, Any] | None = None
        if err_body:
            try:
                obj = json.loads(err_body)
                if isinstance(obj, dict):
                    parsed = obj
            except json.JSONDecodeError:
                parsed = {"raw": err_body[:500]}
        return exc.code, parsed, None
    except urllib.error.URLError as exc:
        return 0, None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return 0, None, f"HTTP POST failed: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return status, None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return status, None, "admin server returned unexpected payload shape"
    return status, payload, None


def _resolve_base_url(
    network_path: Path | None,
) -> tuple[str | None, str | None]:
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import load_network, resolve_admin_base_url
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


def _is_valid_job_id(job_id: str) -> bool:
    return bool(job_id) and _JOB_ID_RE.fullmatch(job_id) is not None


# ─── action.forge.approve ────────────────────────────────────────────────────


def _approve_handler(
    job_id: str,
    approved_by: str = "evo",
    notes: str = "",
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Approve a forge job awaiting operator sign-off."""
    if not _is_valid_job_id(job_id):
        return {
            "ok": False,
            "error": (
                f"job_id must match {_JOB_ID_RE.pattern}; got "
                f"{job_id!r}"
            ),
        }

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        # url-encode the job_id for path safety even though the regex
        # already rejects shell metacharacters — defense in depth.
        safe_id = urllib.parse.quote(job_id, safe="")
        post_url = f"{base_url}/api/forge/jobs/{safe_id}/approve"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(
        post_url,
        {"approved_by": approved_by, "notes": notes},
        15,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the forge approval"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False, "error": err_msg, "http_status": status,
            "job_id": job_id,
        }

    return {
        "ok": True,
        "job_id": job_id,
        "approved_by": approved_by,
        "notes": notes,
        "message": (
            f"Forge job {job_id} approved — build will start on the "
            f"next dispatcher tick."
        ),
        "verify_via": {
            "tool": "pod_state.forge_job",
            "args": {"job_id": job_id},
            "expect": (
                "the job's status moves from 'awaiting_approval' to "
                "'queued' or 'running'"
            ),
        },
    }


def _approve_validate(
    job_id: str,
    approved_by: str = "evo",
    notes: str = "",
    network_path: Path | None = None,
) -> dict[str, Any]:
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "reason": "`job_id` is required"}
    if not _is_valid_job_id(job_id):
        return {
            "ok": False,
            "reason": (
                f"job_id has unexpected shape (must match "
                f"{_JOB_ID_RE.pattern})"
            ),
        }
    return {"ok": True, "context": {"job_id": job_id}}


APPROVE_TOOL = Tool(
    name="action.forge.approve",
    description=(
        "Approve a forge job that's awaiting operator sign-off — the "
        "build kicks off on the next dispatcher tick. Same code path "
        "as the **✓ Approve** button on the Apps → Forge job row. Use "
        "when the operator says 'approve forge job X', 'kick off the "
        "build', or after reviewing the synthesizer's plan. The job "
        "MUST be in state 'awaiting_approval' — call pod_state.forge_job "
        "first if uncertain. WRITE_RISKY: starts real npm/git/llm "
        "operations; reversible mid-flight via action.forge.cancel but "
        "not easily post-completion."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": (
                    "Forge job id. Discover via "
                    "pod_state.forge_job(job_id=…) or the Apps → "
                    "Forge listing."
                ),
            },
            "approved_by": {
                "type": "string",
                "description": (
                    "Actor identifier recorded in the job's "
                    "approval-trail. Default 'evo'."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Optional operator-readable note recorded with "
                    "the approval (e.g. 'reviewed manifest, looks "
                    "right')."
                ),
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    handler=_approve_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_approve_validate,
    tags=("action", "forge", "approve"),
)


register(APPROVE_TOOL)


# ─── action.forge.reject ─────────────────────────────────────────────────────


def _reject_handler(
    job_id: str,
    rejected_by: str = "evo",
    reason: str = "",
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Reject a forge job awaiting operator sign-off."""
    if not _is_valid_job_id(job_id):
        return {
            "ok": False,
            "error": (
                f"job_id must match {_JOB_ID_RE.pattern}; got "
                f"{job_id!r}"
            ),
        }

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        safe_id = urllib.parse.quote(job_id, safe="")
        post_url = f"{base_url}/api/forge/jobs/{safe_id}/reject"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(
        post_url,
        {"rejected_by": rejected_by, "reason": reason},
        15,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the forge rejection"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False, "error": err_msg, "http_status": status,
            "job_id": job_id,
        }

    return {
        "ok": True,
        "job_id": job_id,
        "rejected_by": rejected_by,
        "reason": reason,
        "message": (
            f"Forge job {job_id} rejected."
            + (f" Reason: {reason!r}" if reason else "")
        ),
        "verify_via": {
            "tool": "pod_state.forge_job",
            "args": {"job_id": job_id},
            "expect": "the job's status moves to 'rejected'",
        },
    }


def _reject_validate(
    job_id: str,
    rejected_by: str = "evo",
    reason: str = "",
    network_path: Path | None = None,
) -> dict[str, Any]:
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "reason": "`job_id` is required"}
    if not _is_valid_job_id(job_id):
        return {
            "ok": False,
            "reason": (
                f"job_id has unexpected shape (must match "
                f"{_JOB_ID_RE.pattern})"
            ),
        }
    return {"ok": True, "context": {"job_id": job_id}}


REJECT_TOOL = Tool(
    name="action.forge.reject",
    description=(
        "Reject a forge job that's awaiting operator sign-off — the "
        "build does NOT run. Same code path as the **✗ Reject** "
        "button on the Apps → Forge job row. Use when the operator "
        "says 'reject forge job X', 'this manifest looks wrong', or "
        "decides the synthesizer's plan needs reworking. The job MUST "
        "be in state 'awaiting_approval', 'queued', or 'running' — "
        "completed jobs cannot be rejected. WRITE_SAFE: rejection "
        "doesn't run anything destructive; revisiting just means "
        "dispatching a new job."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": (
                    "Forge job id. Discover via "
                    "pod_state.forge_job(job_id=…)."
                ),
            },
            "rejected_by": {
                "type": "string",
                "description": (
                    "Actor identifier recorded in the job's "
                    "rejection-trail. Default 'evo'."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional operator-readable rejection reason "
                    "(e.g. 'manifest references undefined skill X')."
                ),
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    handler=_reject_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_reject_validate,
    tags=("action", "forge", "reject"),
)


register(REJECT_TOOL)


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    def _approve_h(**kwargs: Any) -> dict[str, Any]:
        return _approve_handler(network_path=network_path, **kwargs)

    def _reject_h(**kwargs: Any) -> dict[str, Any]:
        return _reject_handler(network_path=network_path, **kwargs)

    register(Tool(
        name=APPROVE_TOOL.name,
        description=APPROVE_TOOL.description,
        input_schema=APPROVE_TOOL.input_schema,
        handler=_approve_h,
        risk_tier=APPROVE_TOOL.risk_tier,
        validate=lambda **kw: _approve_validate(
            network_path=network_path, **kw,
        ),
        tags=APPROVE_TOOL.tags,
    ))
    register(Tool(
        name=REJECT_TOOL.name,
        description=REJECT_TOOL.description,
        input_schema=REJECT_TOOL.input_schema,
        handler=_reject_h,
        risk_tier=REJECT_TOOL.risk_tier,
        validate=lambda **kw: _reject_validate(
            network_path=network_path, **kw,
        ),
        tags=REJECT_TOOL.tags,
    ))

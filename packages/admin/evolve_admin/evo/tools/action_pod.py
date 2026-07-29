"""Action tools — pod-wide lifecycle (pause / resume + pause-state read).

Phase 1.5a from spec §13.8. Pod-wide pause is the operator's "panic
button" — used when something is going wrong fleet-wide and the safest
move is to stop everything. Resume is the inverse; not destructive
because it's bringing the pod back to its normal state.

Both wrap ``evolve_admin.recovery.pause_all`` / ``resume_all`` — the
same code paths the admin UI's "Pause all bots" header button uses,
the same code paths the ``evolve-admin pause-all`` / ``resume-all`` CLI
uses. Tool just wraps the function call with structured input +
validate + verify_via.

Risk tiers:
  - ``action.pod.pause_all`` is ``destructive`` because it
    immediately disables every bot pod-wide. Reversible via resume_all
    but the blast radius is the entire pod; we want the explicit
    confirm gate regardless of operator authority.
  - ``action.pod.resume_all`` is ``write_risky``. It restores normal
    operation; not destructive but auto-running it would mask whatever
    issue caused the pause in the first place.

``pod_state.pause_state`` is the companion read tool — used by
verify_via after pause/resume to confirm the state flipped, and
useful on its own for "are we paused right now?" queries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _import_recovery():
    """Lazy import — recovery imports config + heavy launchctl helpers."""
    try:
        from evolve_admin import recovery
        return recovery
    except ImportError as exc:
        log.warning("action.pod.*: recovery module unavailable: %s", exc)
        return None


def _summarize_pause_result(result: Any) -> dict[str, Any]:
    """Shrink a PauseResult dataclass to the JSON shape the proxy needs.

    The full dataclass carries a per-bot list with timestamps + elapsed
    counts; we surface counts + the bot names so the LLM can compose a
    useful reply without overwhelming context.
    """
    per_bot = list(getattr(result, "per_bot", []) or [])
    bots_ok = []
    bots_failed = []
    for r in per_bot:
        d = r.to_dict() if hasattr(r, "to_dict") else (r if isinstance(r, dict) else {})
        (bots_ok if d.get("ok") else bots_failed).append(d.get("bot_id"))
    return {
        "ok": bool(getattr(result, "ok", False)),
        "action": getattr(result, "action", "?"),
        "started_at": getattr(result, "started_at", ""),
        "finished_at": getattr(result, "finished_at", ""),
        "elapsed_ms": int(getattr(result, "elapsed_ms", 0) or 0),
        "bots_total": len(per_bot),
        "bots_ok": bots_ok,
        "bots_failed": bots_failed,
        "state_before": getattr(result, "state_before", None),
        "state_after": getattr(result, "state_after", None),
        "dry_run": bool(getattr(result, "dry_run", False)),
    }


# ─── action.pod.pause_all ─────────────────────────────────────────────────────


def _pause_all_handler(
    network_path: Path,
    reason: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Pause every bot in the pod via ``recovery.pause_all``.

    Defense-in-depth: even though destructive-tier always asks, we also
    require ``confirm: true`` in args. A misclick on the destructive
    confirm button alone shouldn't take down the pod.
    """
    if not confirm:
        return {
            "ok": False,
            "error": (
                "action.pod.pause_all requires 'confirm': true. This "
                "stops every bot's gateway pod-wide."
            ),
        }
    if not reason or not reason.strip():
        return {
            "ok": False,
            "error": "reason is required (logged to the audit trail)",
        }

    # Phase E.3.4 path: admin daemon endpoint first. Falls back to
    # in-process recovery.pause_all on socket failure.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/recovery/pause-all",
        body={"reason": reason, "initiated_by": "evo:tool"},
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        # The recovery endpoint returns its own result shape; pass it
        # through with the verify_via hint appended.
        body["reason"] = reason
        body["verify_via"] = {
            "tool": "pod_state.pause_state",
            "args": {},
            "expect": "paused=true with a timestamped reason matching this call",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process recovery.pause_all (migration window only).
    recovery = _import_recovery()
    if recovery is None:
        return {"ok": False, "error": "recovery module unavailable"}

    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    try:
        result = recovery.pause_all(
            reason=reason,
            initiated_by="evo:tool",
            network=net,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.pod.pause_all: pause_all raised")
        return {
            "ok": False,
            "error": f"pause failed: {type(exc).__name__}: {exc}",
        }

    summary = _summarize_pause_result(result)
    summary["reason"] = reason
    # Post-action verify (spec §3.7 lever #4).
    summary["verify_via"] = {
        "tool": "pod_state.pause_state",
        "args": {},
        "expect": "paused=true with a timestamped reason matching this call",
    }
    return summary


def _pause_all_validate(
    network_path: Path,
    reason: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Dry-run: reason + confirm required."""
    if not reason or not reason.strip():
        return {"ok": False, "reason": "reason is required (audit trail)"}
    if not confirm:
        return {
            "ok": False,
            "reason": (
                "action.pod.pause_all requires confirm: true "
                "(defense in depth against accidental fleet outage)"
            ),
        }
    # Surface a tiny preview of how many bots will be affected.
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        bot_count = len((net.get("bots") or {}))
    except Exception:  # noqa: BLE001
        bot_count = -1
    return {
        "ok": True,
        "context": {
            "estimated_bots_affected": bot_count,
            "reversible_via": "action.pod.resume_all",
        },
    }


PAUSE_ALL_TOOL = Tool(
    name="action.pod.pause_all",
    description=(
        "Pause every bot in the pod. Writes the pod-wide pause-state "
        "flag and unloads each bot's OpenClaw gateway via launchctl. "
        "~30 seconds wall-clock. Idempotent — calling pause_all when "
        "already paused refreshes the reason + timestamp. Reversible "
        "via action.pod.resume_all. Requires explicit confirm: true "
        "(destructive-tier confirm button AND the flag — defense in "
        "depth against accidental fleet-wide outage)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Reason for pausing — required, logged to the audit "
                    "trail (pause-log.jsonl) and surfaced to other "
                    "monitors so they don't fight us by restarting bots."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be true to actually pause. Defense-in-depth "
                    "redundancy with the destructive-tier confirm button."
                ),
                "default": False,
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    handler=_pause_all_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_pause_all_validate,
    tags=("action", "pod", "lifecycle", "destructive"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(PAUSE_ALL_TOOL)


# ─── action.pod.resume_all ────────────────────────────────────────────────────


def _resume_all_handler(
    network_path: Path,
    reason: str | None = None,
) -> dict[str, Any]:
    """Resume every bot in the pod via ``recovery.resume_all``.

    Not destructive — it restores normal operation. But still
    write_risky because auto-running it would mask whatever issue
    caused the original pause. Operator should confirm intentionally.
    """
    # Phase E.3.4: admin daemon endpoint first.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", "/api/recovery/resume-all",
        body={"initiated_by": "evo:tool"},
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        body["reason"] = reason or "evo resumed via tool"
        body["verify_via"] = {
            "tool": "pod_state.pause_state",
            "args": {},
            "expect": "paused=false (state flag cleared)",
        }
        body.setdefault("via", "admin_daemon")
        return body
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process resume.
    recovery = _import_recovery()
    if recovery is None:
        return {"ok": False, "error": "recovery module unavailable"}

    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    try:
        result = recovery.resume_all(
            initiated_by="evo:tool",
            network=net,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.pod.resume_all: resume_all raised")
        return {
            "ok": False,
            "error": f"resume failed: {type(exc).__name__}: {exc}",
        }

    summary = _summarize_pause_result(result)
    summary["reason"] = reason or "evo resumed via tool"
    summary["verify_via"] = {
        "tool": "pod_state.pause_state",
        "args": {},
        "expect": "paused=false (state flag cleared)",
    }
    return summary


def _resume_all_validate(
    network_path: Path,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: surface current pause state so operator confirms with
    eyes open."""
    recovery = _import_recovery()
    current: dict | None = None
    if recovery is not None:
        try:
            current = recovery.read_pause_state(None)
        except Exception:  # noqa: BLE001
            current = None
    return {
        "ok": True,
        "context": {
            "currently_paused": bool(current and current.get("paused")),
            "current_pause_reason": (current or {}).get("reason"),
            "current_pause_initiated_by": (current or {}).get("initiated_by"),
        },
    }


RESUME_ALL_TOOL = Tool(
    name="action.pod.resume_all",
    description=(
        "Resume every bot in the pod after a pause_all. Bootstraps each "
        "gateway plist and clears the pod-wide pause flag. ~30 seconds. "
        "Idempotent — safe to call when not paused (no-op on the flag, "
        "re-bootstraps the gateways). Use when you've finished addressing "
        "whatever caused the pause."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Optional note for the audit log — what was fixed, "
                    "or why resume is safe now."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=_resume_all_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_resume_all_validate,
    tags=("action", "pod", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(RESUME_ALL_TOOL)


# ─── pod_state.pause_state (read companion) ───────────────────────────────────


def _pause_state_handler() -> dict[str, Any]:
    """Read the pod-wide pause-state flag.

    Returns ``{"paused": false}`` when no flag is set (the steady-state),
    or the full pause record when the pod is paused. Cheap; reads one
    JSON file from shared_dir.
    """
    recovery = _import_recovery()
    if recovery is None:
        return {"ok": False, "error": "recovery module unavailable"}
    try:
        state = recovery.read_pause_state(None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"pause-state read failed: {exc}"}

    if not state:
        return {"ok": True, "paused": False}
    return {
        "ok": True,
        "paused": bool(state.get("paused", True)),
        "paused_at": state.get("paused_at"),
        "initiated_by": state.get("initiated_by"),
        "reason": state.get("reason"),
    }


PAUSE_STATE_TOOL = Tool(
    name="pod_state.pause_state",
    description=(
        "Read the pod-wide pause flag. Returns paused=true with the "
        "timestamp / initiator / reason if the pod is currently paused, "
        "or paused=false if running normally. Cheap read; use as "
        "verify_via after action.pod.pause_all / action.pod.resume_all "
        "or on its own to answer 'is the pod paused?'."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    handler=_pause_state_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "lifecycle"),
    # Pod-wide pause is a public operational state — the gateway
    # behavior changes for every channel when paused, so a cross-bot
    # member already observes it indirectly. Exposing the read tool
    # closes the entity opacity gap without leaking anything new.
    authorization_scope="anyone",
)

register(PAUSE_STATE_TOOL)

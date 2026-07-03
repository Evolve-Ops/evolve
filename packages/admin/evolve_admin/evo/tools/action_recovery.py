"""Recovery tools — rollback candidate listing + rollback + reverse-rollback.

Three tools that together cover the Recovery admin UI page's main flow:
list candidate rollback targets → roll back to a target → reverse the
rollback if it was a mistake.

* ``pod_state.rollback_points(bot_id, limit?, only_backup?)`` — read.
  Lists candidate rollback targets for a bot (daily backup commits by
  default; full workspace git log when ``only_backup=false``). The
  operator picks a ``target`` from the response to feed into
  ``action.bot.rollback``. Wraps recovery.list_rollback_points.

* ``action.bot.rollback(bot_id, target, ...)`` — DESTRUCTIVE.
  Reverts a bot's openclaw.json to ``target`` (a commit ref from the
  rollback_points list, or 'HEAD~1', etc.) and restarts the bot's
  gateway. The pre-rollback state is captured as a snapshot so the
  operation can be undone via action.bot.reverse_rollback. Mirrors
  POST /api/recovery/rollback (server.py:19117).

* ``action.bot.reverse_rollback(rollback_id, ...)`` — write_risky.
  Undoes a prior rollback by restoring the captured pre-rollback
  snapshot. Used when a rollback turned out wrong (revealed a different
  bug, didn't fix the issue, etc.). Mirrors POST
  /api/recovery/reverse-rollback (server.py:19140).

Risk tiers reflect blast radius:

* rollback_points = read (no side effects).
* rollback = destructive (changes the bot's runtime config + restarts
  the gateway; reversal is possible only because we capture a
  snapshot). Always asks for confirmation.
* reverse_rollback = write_risky (restores a known-good snapshot; same
  shape of edit, less likely to be wrong, but still a config-edit +
  restart).

Memory references:
* feedback_diagnosis_must_survive_live_inspection — rollback is a
  destructive op; verify the live state matches the diagnosis before
  acting.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _load_network(network_path: Path) -> dict | None:
    try:
        from evolve_admin.config import load_network
    except ImportError as exc:
        log.warning("recovery tools: load_network unavailable: %s", exc)
        return None
    try:
        return load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("recovery tools: load_network failed: %s", exc)
        return None


def _bot_exists(network: dict, bot_id: str) -> bool:
    return bot_id in (network.get("bots") or {})


def _shared_dir_from(network: dict) -> Path:
    return Path(network.get("sharedDir") or "/Users/Shared/evolve")


# ─── pod_state.rollback_points ───────────────────────────────────────────────


def _points_handler(
    network_path: Path,
    bot_id: str,
    limit: int = 14,
    only_backup: bool = True,
) -> dict[str, Any]:
    """List candidate rollback targets for a bot.

    ``only_backup=True`` (default) returns only the nightly backup
    commits — what the operator usually wants for a "go back to a
    known-good state" workflow. ``only_backup=False`` returns the full
    workspace git log including intermediate accept-drift commits, for
    the "I want full context" history view.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    try:
        from evolve_admin import recovery as _recovery
    except ImportError as exc:
        return {"ok": False, "error": f"recovery module unavailable: {exc}"}

    bot_cfg = (network.get("bots") or {}).get(bot_id) or {}
    backup_url_configured = bool(bot_cfg.get("backupRepoUrl"))
    workspace = _recovery._bot_workspace(bot_id, network)
    workspace_is_git = _recovery._is_workspace_repo(workspace)

    try:
        points = _recovery.list_rollback_points(
            bot_id,
            network=network,
            limit=limit,
            only_backup=only_backup,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.rollback_points: list_rollback_points raised")
        return {
            "ok": False,
            "error": f"list_rollback_points failed: {exc}",
            "bot_id": bot_id,
            "backup_url_configured": backup_url_configured,
            "workspace_is_git": workspace_is_git,
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "points": [p.to_dict() for p in points],
        "backup_url_configured": backup_url_configured,
        "workspace_is_git": workspace_is_git,
        "only_backup": only_backup,
    }


POINTS_TOOL = Tool(
    name="pod_state.rollback_points",
    description=(
        "List candidate rollback targets for a bot — the commits the "
        "operator can roll back to. Each entry is a daily backup commit "
        "(when only_backup=true, the default) or a full workspace git "
        "log entry (when only_backup=false). The 'commit' field in each "
        "entry is what to pass as 'target' to action.bot.rollback. Use "
        "when the operator asks 'what can I roll back to?' or 'show me "
        "yesterday's backup for team_bot_a'. Read-only — no side effects."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — get from pod_state.bots.",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Max number of rollback points to return (1-100, "
                    "default 14)."
                ),
                "minimum": 1,
                "maximum": 100,
            },
            "only_backup": {
                "type": "boolean",
                "description": (
                    "When true (default), returns only nightly backup "
                    "commits — the 'known good daily snapshot' set the "
                    "Recovery dropdown uses. When false, returns the "
                    "full workspace git log including accept-drift "
                    "baseline resets and intermediate commits."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_points_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "recovery"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(POINTS_TOOL)


# ─── action.bot.rollback ─────────────────────────────────────────────────────


def _rollback_handler(
    network_path: Path,
    bot_id: str,
    target: str,
    reason: str | None = None,
    skip_restart: bool = False,
) -> dict[str, Any]:
    """Revert a bot's openclaw.json to ``target`` and restart the gateway.

    The pre-rollback config is captured as a snapshot so the operation
    can be undone via action.bot.reverse_rollback. ``skip_restart=True``
    lands the config change without bouncing the gateway — only useful
    if the operator is staging multiple changes and will restart
    manually at the end.
    """
    if not bot_id or not target:
        return {"ok": False, "error": "bot_id and target are required"}

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "error": f"unknown bot: {bot_id!r}"}

    try:
        from evolve_admin import recovery as _recovery
    except ImportError as exc:
        return {"ok": False, "error": f"recovery module unavailable: {exc}"}

    try:
        result = _recovery.rollback_bot(
            bot_id=bot_id,
            target=target,
            network=network,
            shared_dir=_shared_dir_from(network),
            # ``reason`` is folded into the initiated_by string so the
            # audit trail shows both the source ('evo') and the
            # operator's stated reason. The recovery module's audit
            # writer accepts arbitrary text here.
            initiated_by=f"evo: {reason}" if reason else "evo",
            skip_restart=skip_restart,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.rollback: rollback_bot raised")
        return {"ok": False, "error": f"rollback failed: {exc}"}

    payload = result.to_dict()
    payload["verify_via"] = {
        "tool": "pod_state.rollbacks",
        "args": {"bot_id": bot_id, "limit": 1},
        "expect": (
            "the most recent entry references this rollback (target="
            f"{target!r}, ok=true) and the gateway is restarted"
        ),
    }
    return payload


def _rollback_validate(
    network_path: Path,
    bot_id: str,
    target: str,
    reason: str | None = None,
    skip_restart: bool = False,
) -> dict[str, Any]:
    """Dry-run: confirm bot exists, target is a non-empty string, and the
    rollback_bot dry-run succeeds (so the operator sees up-front whether
    the target resolves to a valid commit)."""
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not target:
        return {
            "ok": False,
            "reason": (
                "target is required — pass a commit ref from "
                "pod_state.rollback_points"
            ),
        }

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    if not _bot_exists(network, bot_id):
        return {"ok": False, "reason": f"unknown bot: {bot_id!r}"}

    try:
        from evolve_admin import recovery as _recovery
    except ImportError as exc:
        return {"ok": False, "reason": f"recovery module unavailable: {exc}"}

    # Dry-run the rollback so the operator's confirmation button only
    # renders when the target actually resolves and the workspace is in
    # a state where rollback can proceed.
    try:
        dry = _recovery.rollback_bot(
            bot_id=bot_id,
            target=target,
            network=network,
            shared_dir=_shared_dir_from(network),
            initiated_by=f"evo dry-run: {reason}" if reason else "evo dry-run",
            skip_restart=True,
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"dry-run failed: {exc}"}

    if not dry.ok:
        return {
            "ok": False,
            "reason": (
                f"dry-run rejected: {dry.message or 'unknown reason'}. "
                "Check pod_state.rollback_points for a valid target."
            ),
        }

    return {
        "ok": True,
        "context": {
            "bot_id": bot_id,
            "target": target,
            "skip_restart": skip_restart,
            "dry_run_message": dry.message,
        },
        # Destructive — always confirm regardless of authority tier.
        "requires_confirmation": True,
    }


ROLLBACK_TOOL = Tool(
    name="action.bot.rollback",
    description=(
        "Roll back a bot's openclaw.json to a prior commit (target). "
        "Captures the pre-rollback state as a snapshot so the operation "
        "can be undone via action.bot.reverse_rollback. Restarts the "
        "bot's gateway by default (skip_restart=true to defer the "
        "restart — only useful when staging multiple changes). "
        "DESTRUCTIVE — always asks for confirmation regardless of "
        "authority tier. Get the target value from "
        "pod_state.rollback_points."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — get from pod_state.bots.",
            },
            "target": {
                "type": "string",
                "description": (
                    "Commit ref to roll back to. Get from "
                    "pod_state.rollback_points (the 'commit' field on "
                    "each point). Can also be a relative ref like "
                    "'HEAD~1', though backup-commit refs are safer."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Operator-facing reason for the rollback. Folded "
                    "into the audit record's initiated_by field."
                ),
            },
            "skip_restart": {
                "type": "boolean",
                "description": (
                    "When true, lands the config change without bouncing "
                    "the gateway. Only useful for staging multiple "
                    "changes. Default false."
                ),
            },
        },
        "required": ["bot_id", "target"],
        "additionalProperties": False,
    },
    handler=_rollback_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_rollback_validate,
    tags=("action", "bot", "recovery"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(ROLLBACK_TOOL)


# ─── action.bot.reverse_rollback ─────────────────────────────────────────────


def _reverse_handler(
    network_path: Path,
    rollback_id: str,
    reason: str | None = None,
    skip_restart: bool = False,
) -> dict[str, Any]:
    """Undo a prior rollback by restoring the captured pre-rollback snapshot."""
    if not rollback_id:
        return {"ok": False, "error": "rollback_id is required"}

    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "error": "failed to load network.json"}

    try:
        from evolve_admin import recovery as _recovery
    except ImportError as exc:
        return {"ok": False, "error": f"recovery module unavailable: {exc}"}

    try:
        result = _recovery.reverse_rollback(
            rollback_id=rollback_id,
            network=network,
            shared_dir=_shared_dir_from(network),
            initiated_by=f"evo: {reason}" if reason else "evo",
            skip_restart=skip_restart,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.bot.reverse_rollback: reverse_rollback raised")
        return {"ok": False, "error": f"reverse_rollback failed: {exc}"}

    payload = result.to_dict()
    payload["verify_via"] = {
        "tool": "pod_state.rollbacks",
        "args": {"limit": 5},
        "expect": (
            f"a new entry with reversed_from_rollback_id={rollback_id!r} "
            "appears at the top; the original entry now has reversed_by_rollback_id set"
        ),
    }
    return payload


def _reverse_validate(
    network_path: Path,
    rollback_id: str,
    reason: str | None = None,
    skip_restart: bool = False,
) -> dict[str, Any]:
    if not rollback_id:
        return {"ok": False, "reason": "rollback_id is required"}
    network = _load_network(network_path)
    if network is None:
        return {"ok": False, "reason": "failed to load network.json"}
    try:
        from evolve_admin import recovery as _recovery
    except ImportError as exc:
        return {"ok": False, "reason": f"recovery module unavailable: {exc}"}

    try:
        dry = _recovery.reverse_rollback(
            rollback_id=rollback_id,
            network=network,
            shared_dir=_shared_dir_from(network),
            initiated_by=f"evo dry-run: {reason}" if reason else "evo dry-run",
            skip_restart=True,
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"dry-run failed: {exc}"}

    if not dry.ok:
        return {
            "ok": False,
            "reason": (
                f"dry-run rejected: {dry.message or 'unknown reason'}. "
                "Check pod_state.rollbacks for valid rollback_ids."
            ),
        }

    return {
        "ok": True,
        "context": {
            "rollback_id": rollback_id,
            "skip_restart": skip_restart,
            "dry_run_message": dry.message,
        },
    }


REVERSE_TOOL = Tool(
    name="action.bot.reverse_rollback",
    description=(
        "Undo a prior rollback by restoring the captured pre-rollback "
        "snapshot. Use when a rollback turned out wrong — revealed a "
        "different bug, didn't fix the issue, or was applied to the "
        "wrong bot. Restarts the gateway by default. Get the "
        "rollback_id value from pod_state.rollbacks."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rollback_id": {
                "type": "string",
                "description": (
                    "Rollback id from pod_state.rollbacks. The 'id' "
                    "field on the entry to reverse."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Operator-facing reason — folded into the audit "
                    "record's initiated_by field."
                ),
            },
            "skip_restart": {
                "type": "boolean",
                "description": (
                    "When true, restores the snapshot without bouncing "
                    "the gateway. Default false."
                ),
            },
        },
        "required": ["rollback_id"],
        "additionalProperties": False,
    },
    handler=_reverse_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_reverse_validate,
    tags=("action", "bot", "recovery"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REVERSE_TOOL)

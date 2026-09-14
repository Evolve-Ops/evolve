"""pod_state.rollbacks — Recent rollback history.

Phase 1.5d read tool. Surfaces the rollback audit trail (the same
records that back the admin UI's Rollback page). Used when the
operator asks "what did we roll back recently?", "is there a rollback
for X?", or "show the last 5 rollbacks on team_bot_a".

Wraps ``recovery.list_rollback_history`` — newest-first list of
RollbackResult dicts with the bulky config blobs trimmed
(``has_pre_rollback_snapshot`` flag instead). Each entry includes
the rollback_id, target_commit, bot_id, timestamps, ok/message,
gateway_restart status, and reverse-rollback linkage if applicable.

A reverse-rollback (an "undo" of a prior rollback) shows up as a
normal entry with ``reversed_from_rollback_id`` pointing at its
predecessor. The original record's ``reversed_by_rollback_id``
points the other direction. This doubly-linked list keeps the
forward/back trail visible.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _handler(
    network_path: Path,
    bot_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return up to ``limit`` rollback records, newest first.

    ``bot_id`` filters the listing to one bot. ``limit`` is bounded
    server-side to 100 so a runaway model can't ask for everything.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    if bot_id and bot_id not in (net.get("bots") or {}):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    shared_dir = Path(net.get("sharedDir") or "/Users/Shared/evolve")

    try:
        from evolve_admin.recovery import list_rollback_history
    except ImportError as exc:
        return {"ok": False, "error": f"recovery module unavailable: {exc}"}

    try:
        entries = list_rollback_history(
            shared_dir=shared_dir,
            bot_id=bot_id,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.rollbacks: list_rollback_history raised")
        return {"ok": False, "error": f"rollback history read failed: {exc}"}

    return {
        "ok": True,
        "count": len(entries),
        "bot_id": bot_id,
        "limit": limit,
        "rollbacks": entries,
    }


ROLLBACKS_TOOL = Tool(
    name="pod_state.rollbacks",
    description=(
        "List recent rollback records (newest first). Each entry shows "
        "rollback_id, bot_id, target_commit, timestamps, ok/message, "
        "gateway_restart status, and any reverse-rollback linkage. "
        "Filter to one bot with bot_id; cap with limit (default 20, "
        "max 100). Config blobs are trimmed to keep the response "
        "small; the has_pre_rollback_snapshot flag indicates whether "
        "the rollback can itself be reversed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Optional — restrict to one bot.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned (default 20, max 100).",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "rollback", "history"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(ROLLBACKS_TOOL)

"""pod_state.errors — Recent raw error log lines per bot.

Phase 1.5d read tool. Surfaces the recent_errors field from heal.py's
per-bot status snapshot. Complementary to ``pod_state.signals.firing``:

  - Signals are CURATED, persisted observations from monitors —
    "this matters, here's why". They're the right surface for "what's
    wrong?" questions.

  - Errors here are RAW recent lines from the bot's error log,
    captured by heal.py's gateway-error-tail scanner. They're the
    right surface for "show me what's failing right now" or "what's
    the actual error message?" questions where the operator wants to
    see the verbatim line.

Each bot's heal-status file lives at ``{shared_dir}/status/<bot>.json``
and is rewritten every ~5 minutes by heal.py. The ``recent_errors``
field there is bounded (~last 10 lines) so the tool response stays
small.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _read_heal_status(shared_dir: Path, bot_id: str) -> dict | None:
    """Read one heal-status JSON or return None.

    The file is written by heal.py as the bot user; the evolve user
    has ACL read on shared_dir so a plain read suffices. Returns None
    on missing/malformed — the model handles "no data" gracefully.
    """
    p = shared_dir / "status" / f"{bot_id}.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _project_errors(bot_id: str, status: dict | None) -> dict[str, Any]:
    """Shrink a heal-status dict to the error-relevant subset.

    Keeps ``recent_errors`` (the raw log lines), the parallel
    ``recent_errors_ts`` (timestamps, when present), ``last_error``,
    and the freshness timestamp. Drops the rest of heal-status (which
    is covered by pod_state.bots).
    """
    if not status:
        return {
            "bot_id": bot_id,
            "recent_errors": [],
            "recent_errors_ts": [],
            "last_error": None,
            "last_error_ts": None,
            "status_ts": None,
            "note": "no heal-status file yet (bot not deployed or first heal not run)",
        }
    return {
        "bot_id": bot_id,
        "recent_errors": list(status.get("recent_errors") or []),
        "recent_errors_ts": list(status.get("recent_errors_ts") or []),
        "last_error": status.get("last_error"),
        "last_error_ts": status.get("last_error_ts"),
        "status_ts": status.get("ts"),
    }


def _handler(
    network_path: Path,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Return recent errors. ``bot_id`` filters to one bot."""
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    shared_dir = Path(net.get("sharedDir") or "/Users/Shared/evolve")
    bots = net.get("bots") or {}

    if bot_id:
        if bot_id not in bots:
            return {
                "ok": False,
                "error": f"bot '{bot_id}' is not registered in network.json",
            }
        bot_ids = [bot_id]
    else:
        # members order; fall back to bots dict order
        members = list(net.get("members") or [])
        bot_ids = [b for b in members if b in bots] or list(bots.keys())

    per_bot = [_project_errors(b, _read_heal_status(shared_dir, b)) for b in bot_ids]
    total_errors = sum(len(p["recent_errors"]) for p in per_bot)

    return {
        "ok": True,
        "count_bots": len(per_bot),
        "total_recent_errors": total_errors,
        "bots": per_bot,
    }


ERRORS_TOOL = Tool(
    name="pod_state.errors",
    description=(
        "Recent raw error log lines per bot, from heal.py's per-bot "
        "status snapshot (refreshed every ~5 minutes). Each bot entry "
        "includes recent_errors (the last ~10 raw lines), parallel "
        "recent_errors_ts (timestamps, where heal parsed them), "
        "last_error/last_error_ts (most recent), and status_ts "
        "(freshness). Use for 'show me the actual error message' "
        "queries. For curated 'this matters' surfaces, prefer "
        "pod_state.signals.firing — that's the observation layer; "
        "this is the raw-log layer."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Optional — restrict to one bot. Omit for pod-wide."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "errors", "observability"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(ERRORS_TOOL)

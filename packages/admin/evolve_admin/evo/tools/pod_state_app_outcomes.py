"""pod_state.app_outcomes — the outcome view (design §3.2, D-AP6).

Did the app do the job, not just what it cost. Wraps ``app_outcomes.
get_app_outcomes`` — the same reader the admin API's ``/api/apps/<app_id>``
detail route calls — so evo's numbers and the operator's screen can never
disagree, the discipline ``pod_state.app_usage`` already keeps for cost.

Honesty contract, evo MUST honour it: ``measured: false`` means no bot's
Tracker has EVER produced a row for this app_id — never "zero outcomes".
Most apps are not Tracker applications at all (only the Assistant and the
Project Manager, D-TM11, are). Every count carries ``card_ids`` — the rows
behind the number. A zero on reminders/proposals is usually honest (those
features, D-TM3/4, are still queued chips on most pods), not a failure to
check.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)

_WINDOW_BY_DAYS = {1: "d1", 7: "d7", 30: "d30"}


def _handler(
    network_path: Path,
    app_id: str | None = None,
    bot_id: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    if not app_id:
        return {"ok": False, "error": "app_id is required"}

    try:
        window = _WINDOW_BY_DAYS.get(int(days or 7))
    except (TypeError, ValueError):
        window = None
    if window is None:
        return {"ok": False, "error": "days must be 1, 7 or 30"}

    bots = list((net.get("bots") or {}).keys()) or list(net.get("members") or [])
    if bot_id:
        if bot_id not in bots:
            return {"ok": False,
                    "error": f"bot '{bot_id}' is not registered in network.json"}
        bots = [bot_id]

    try:
        from evolve_config import CANONICAL_SHARED_DIR
        from evolve_admin.app_outcomes import get_app_outcomes
    except ImportError as exc:
        return {"ok": False, "error": f"app_outcomes unavailable: {exc}"}

    shared_dir = Path(net.get("sharedDir") or CANONICAL_SHARED_DIR)

    try:
        out = get_app_outcomes(shared_dir, app_id, bots, period=window)
    except Exception:  # noqa: BLE001
        log.exception("pod_state.app_outcomes: read failed for %s", app_id)
        return {"ok": False, "error": "the outcome rollup could not be read"}

    return {"ok": True, **out}


APP_OUTCOMES_TOOL = Tool(
    name="pod_state.app_outcomes",
    description=(
        "Did an app do the job, not just what it cost — the Tracker "
        "(Board store) rolled up for one app_id: cards moved/resolved "
        "by the bot, bot jobs finished without the operator, reminders "
        "acknowledged/re-asked, proposals accepted/edited/dismissed, "
        "backlog age, blocked-over-3-days. "
        "`measured: false` = no bot's Tracker has ever had a row for "
        "this app_id — say 'not measured', never 'no outcomes'. Every "
        "count carries `card_ids`."
    ),
    wire_description=(
        "Outcome view for one app_id: cards moved/resolved by the bot, "
        "jobs finished without the operator, reminders, proposals, "
        "backlog. measured:false = no Tracker rows anywhere, never "
        "zero. Every count carries card_ids."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "Required — one app id.",
            },
            "bot_id": {
                "type": "string",
                "description": "Optional — one bot. Omit for pod-wide.",
            },
            "days": {
                "type": "integer",
                "enum": [1, 7, 30],
                "description": "Window in days (default 7).",
            },
        },
        "required": ["app_id"],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "outcomes", "apps", "tracker"),
    authorization_scope="admin",
)

register(APP_OUTCOMES_TOOL)

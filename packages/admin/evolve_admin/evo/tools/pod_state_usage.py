"""pod_state.usage — Per-bot + pod-total spend rollup with intraday + daily arrays.

Returns the same per-day cost numbers the admin UI's Usage Summary card
and the burst detector read — sourced from the raw turn JSONL, not the
end-of-day aggregator. So evo sees today's spend so far, yesterday's
full total, and a per-day array for the trailing 28 days that lets it
describe trend and pick out spikes (e.g. "2026-05-20 was 4×
baseline").

Why live JSONL: the legacy aggregate at ``metrics/{date}/{bot}.json``
is written the morning AFTER the day it covers, so a rollup based on
it shows zero for "today" all day and missed the 2026-05-20 4× pod
spike (see ``docs/incident-cost-alerting-blackout-2026-05-20.md``).
Reading the same JSONL the Usage Summary card reads keeps the
operator-facing UI and evo's view in lockstep.

Wraps ``pod_rollup.compute_spend_rollup_live``. When no turn dir is
readable for any bot (fresh pod, tests without fixtures), the wrapper
falls back to the legacy aggregate-based rollup and reports
``source: "lagged_metrics"`` so evo can hedge its answer.

Why 28d (not 30d): bots have weekly cycles; a 30d window biases
toward whichever month-edge it caught. 28d gives every weekday equal
weight across 4 weeks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _handler(
    network_path: Path,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Return the live spend rollup. ``bot_id`` filters to a single bot.

    Defensive: if the metrics dir is empty (fresh pod) the response is
    still well-formed (all zeros). Failures in pod_rollup are surfaced
    as ``{"ok": False, "error": "..."}`` so the model paraphrases the
    failure mode instead of dropping a stack trace.
    """
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
        bots = {bot_id: bots[bot_id]}

    try:
        from evolve_admin.pod_rollup import compute_spend_rollup_live
    except ImportError as exc:
        return {"ok": False, "error": f"pod_rollup unavailable: {exc}"}

    try:
        rollup = compute_spend_rollup_live(shared_dir, bots)
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.usage: compute_spend_rollup_live raised")
        return {"ok": False, "error": f"rollup failed: {exc}"}

    rollup["ok"] = True
    return rollup


USAGE_TOOL = Tool(
    name="pod_state.usage",
    description=(
        "Pod-wide spend rollup with intraday + per-day arrays. Sourced "
        "from the raw turn JSONL — same data the admin UI's Usage "
        "Summary card and the burst detector read, so values match what "
        "the operator sees. Per-bot shape includes:\n"
        " - usd_today / turns_today (partial day, intraday)\n"
        " - usd_yesterday / turns_yesterday (most recent full day)\n"
        " - usd_7d / usd_28d (sums incl. today)\n"
        " - daily_7d / daily_28d arrays, oldest→newest, each entry is "
        "{date, usd, turns} so you can describe trend and call out the "
        "spike day(s) instead of just an average.\n"
        "Pod-level: pod_total_today / pod_total_yesterday / pod_total_7d "
        "/ pod_total_28d / pod_daily_7d / pod_daily_28d / "
        "projected_month_end. 28d (not 30d) avoids week-cycle bias. "
        "projected_month_end is null in the first 2 days of a month "
        "(too noisy to extrapolate). Filter to one bot with bot_id; "
        "otherwise returns every bot in network.json::bots. ``source`` "
        "is ``live_jsonl`` when intraday data is available; "
        "``lagged_metrics`` when the wrapper had to fall back to the "
        "end-of-day aggregate (then usd_today / turns_today are 0 — "
        "say so, don't claim 0 spend)."
    ),
    wire_description=(
        "Pod-wide spend rollup with intraday + per-day arrays — same "
        "numbers as the admin UI's Usage Summary card. Per-bot and "
        "pod totals: usd_today, usd_yesterday, usd_7d/28d, daily "
        "arrays (oldest→newest), projected_month_end. Filter with "
        "bot_id. If source is 'lagged_metrics', usd_today is 0 — say "
        "so, don't claim zero spend."
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
    tags=("pod_state", "usage", "cost"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(USAGE_TOOL)

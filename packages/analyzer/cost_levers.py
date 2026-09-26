"""cost_levers — the per-bot on/off state of the experience-preserving cost levers.

D-CS5 (``internal/assessment-cost-spikes-product-2026-09-13.md``): the levers
that remove redundant re-reading and housekeeping without changing which model
answers the user **ship enabled for every bot, with a per-bot opt-out** in
``network.json`` — never a per-bot opt-in. So the only value a reader can find
that turns a lever off is an explicit ``false``:

    {"bots": {"<bot>": {"cost": {"levers": {"<lever>": false}}}}}   # one bot
    {"cost": {"levers": {"<lever>": false}}}                        # the pod

Bot beats pod, so one bot can opt out of (or back into) a pod-wide setting.
Anything that is not a boolean is ignored — a typo is not an opt-out.

Mirrored in the plugin by ``packages/plugin/src/observer/housekeeping.ts``
(``leverEnabled``) — the gateway decides the flush's model from its own read of
the same file, so the two MUST agree; ``test_cost_levers`` pins the shared
cases.
"""

from __future__ import annotations

from typing import Any

#: Compaction + the pre-compaction memory flush on the cheap rung, tool-less
#: but for the flush's own read/append-only write, thinking off where OC gives
#: us the knob. Brief: compaction-and-memory-flush-on-cheap-rung.
HOUSEKEEPING_CHEAP_RUNG = "housekeeping_cheap_rung"

#: Every lever this module knows, with the operator-facing label the Cost page
#: prints beside its state. Levers from sibling briefs add a row here.
LEVERS: dict[str, str] = {
    HOUSEKEEPING_CHEAP_RUNG: "Housekeeping on the cheap rung",
}


def lever_enabled(network: Any, bot_id: str, lever: str) -> bool:
    """True unless ``network.json`` explicitly sets the lever to ``false``."""
    net = network if isinstance(network, dict) else {}
    bot = ((net.get("bots") or {}).get(bot_id) or {}) if isinstance(net.get("bots"), dict) else {}
    bot_val = _lever_value(bot, lever)
    if isinstance(bot_val, bool):
        return bot_val
    pod_val = _lever_value(net, lever)
    if isinstance(pod_val, bool):
        return pod_val
    return True


def levers_for_bot(network: Any, bot_id: str) -> dict[str, bool]:
    """``{lever: enabled}`` for every known lever — the Cost page's row."""
    return {name: lever_enabled(network, bot_id, name) for name in LEVERS}


def _lever_value(node: Any, lever: str) -> Any:
    if not isinstance(node, dict):
        return None
    cost = node.get("cost")
    if not isinstance(cost, dict):
        return None
    levers = cost.get("levers")
    if not isinstance(levers, dict):
        return None
    return levers.get(lever)

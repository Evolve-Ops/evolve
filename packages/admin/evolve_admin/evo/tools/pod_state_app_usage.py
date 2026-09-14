"""pod_state.app_usage — per-app usage rollup, split by attribution grade.

The read side of AL-1.3 (internal/build-AL-1.3-usage-by-app.md,
internal/design-app-attribution-2026-08-15.md §8). Wraps
``analyzer/usage_by_app.load_usage_by_app`` — the same
``{shared}/{bot}/usage-by-app.json`` the admin UI's Cost → By App card
and the app tiles read, so evo's numbers and the operator's screen can
never disagree.

The honesty contract travels with the data, and evo MUST honour it when
it renders an answer:

* ``total`` per app is ``scheduled + explicit`` — the deterministic
  grades. ``inferred`` is returned BESIDE it and must never be added in
  or described as fact ("probably served by X" is the honest phrasing).
* ``unattributed`` is the pod's no-signal bucket, and ``coverage``
  carries its share of turns and cost. An answer that quotes per-app
  cost without that share is misleading — on a pod mid-rollout most
  turns carry no attribution at all, and ``legacy_schema_turns`` says
  how many of those simply predate attribution shipping.
* ``evolve_overhead`` is Evolve's own scaffolding (forge dispatches, the
  plugin-subagent lanes). Not an app; never attribute it to one.
* ``measured: false`` means the daily rollup has not run for that bot —
  that is "not measured", never "no usage".
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
    """Return the rollup for one app or all, across one bot or the pod."""
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"network.json read failed: {exc}"}

    try:
        window = _WINDOW_BY_DAYS.get(int(days or 7))
    except (TypeError, ValueError):
        window = None
    if window is None:
        return {"ok": False, "error": "days must be 1, 7 or 30"}

    bots = list((net.get("bots") or {}).keys()) or list(net.get("members") or [])
    if bot_id:
        if bot_id not in bots:
            return {
                "ok": False,
                "error": f"bot '{bot_id}' is not registered in network.json",
            }
        bots = [bot_id]

    try:
        # Both live in the analyzer package; one guard covers both. The
        # shared-dir default is platform-keyed (macOS /Users/Shared/evolve,
        # Linux /var/lib/evolve) — never a hardcoded macOS path.
        from evolve_config import CANONICAL_SHARED_DIR
        from usage_by_app import has_attributed_turns, load_usage_by_app
    except ImportError as exc:
        return {"ok": False, "error": f"usage_by_app unavailable: {exc}"}

    shared_dir = Path(net.get("sharedDir") or CANONICAL_SHARED_DIR)

    out: dict[str, Any] = {}
    for bot in bots:
        try:
            payload = load_usage_by_app(shared_dir, bot)
        except Exception:  # noqa: BLE001
            log.exception("pod_state.app_usage: rollup read failed for %s", bot)
            payload = {}
        if not payload:
            out[bot] = {"measured": False, "apps": {}}
            continue
        apps = payload.get("apps") or {}
        if app_id:
            apps = {k: v for k, v in apps.items() if k == app_id}
        out[bot] = {
            "measured": True,
            "generated_at": payload.get("generated_at"),
            "apps": {
                k: {
                    "last_seen_ts": v.get("last_seen_ts"),
                    "first_seen_ts": v.get("first_seen_ts"),
                    **(v.get(window) or {}),
                }
                for k, v in apps.items()
            },
            "unattributed": (payload.get("unattributed") or {}).get(window),
            "evolve_overhead": (payload.get("evolve_overhead") or {}).get(window),
            "coverage": (payload.get("coverage") or {}).get(window),
            # False ⇒ the per-app view for this bot is still the pre-AL-1.3
            # file-mtime footprint (usage_logger), not real attribution.
            "attributed": has_attributed_turns(payload, window),
        }

    return {"ok": True, "window": window, "days": int(days or 7), "bots": out}


APP_USAGE_TOOL = Tool(
    name="pod_state.app_usage",
    description=(
        "Per-app usage (turns, tokens, cost_estimated) from the daily "
        "attribution rollup, per bot, for a 1/7/30-day window. Filter with "
        "app_id and/or bot_id.\n"
        "Per app: `total` = scheduled + explicit (deterministic); "
        "`inferred` rides SEPARATELY — report it as a guess, never add it "
        "to the total. Plus first_seen_ts / last_seen_ts.\n"
        "Per bot: `unattributed` (turns with no app signal at all) and "
        "`coverage.unattributed_turns_share` — ALWAYS state that share "
        "when quoting per-app numbers; a small app number under a large "
        "unattributed share means 'not attributed yet', not 'unused'. "
        "`coverage.legacy_schema_turns` counts turns written before "
        "attribution shipped. `evolve_overhead` is Evolve's own "
        "scaffolding (forge, subagent lanes) — not an app. "
        "`measured: false` (or `attributed: false`) means the rollup has "
        "no attribution for that bot yet — say 'not measured', never "
        "'no usage'."
    ),
    wire_description=(
        "Per-app turns/tokens/cost for a 1/7/30d window, per bot. `total` "
        "= scheduled+explicit; `inferred` stays separate (a guess, never "
        "added in). Always quote coverage.unattributed_turns_share "
        "alongside — low app numbers under a high unattributed share mean "
        "'not attributed yet', not 'unused'. measured/attributed false = "
        "not measured, not zero."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {
                "type": "string",
                "description": "Optional — one app id. Omit for every app.",
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
        "required": [],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "usage", "apps", "cost"),
    authorization_scope="admin",
)

register(APP_USAGE_TOOL)

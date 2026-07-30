"""Pod-state read tool — bots.

Exposes ``pod_state.bots`` — every bot's status (online/active/offline,
version, role, port, live_at) plus the **same tile-derived health
chips** the admin UI dashboard renders next to each bot (scan_needed,
cost_spike, security_critical, repo_puller_stale, etc.). The projection
drops the verbose ``live_data`` blob (raw HTTP response from each bot's
gateway) because the model usually wants the headline status, not the
full gateway payload — but exposes the per-bot keys most useful for
triage queries.

Use case: model says "is personal_bot online?" or "which bots are running an
older Evolve version?" or "what's the role of each bot in this pod?"
or — the case this tool was extended to answer — "what does the
'scan needed' pill on the evo tile mean?"

Tile chips are computed by the analyzer's ``compute_tile_data``, the
same function the admin server calls when rendering /api/status. The
``health_chips`` list is what the dashboard tiles render verbatim,
so the model sees exactly what the operator sees.

A per-bot drill-down tool (``pod_state.bot_detail``) lands separately
when there's a use case that needs the live_data blob.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _project_bot(
    bot_id: str,
    bot_dict: dict[str, Any],
    *,
    tile_chips: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pick the operator-relevant fields from a network_status bot entry.

    network_status returns a rich blob per bot — role, port, live,
    live_data (raw gateway payload), last_metric_date, last_metric,
    evolve_version, evolve_synced, continuity_engine_enabled, multiUser,
    etc. We project the headline status + version + identity bits, skip
    live_data + last_metric (verbose, model can query specific data via
    other tools when needed).

    ``tile_chips`` is the list of health chips the admin UI renders on
    the bot's dashboard tile (scan_needed, cost_spike, repo_puller_stale,
    security_critical, …). Pre-computed by the caller via
    ``analyzer.tile_metrics.compute_tile_data`` so this projection stays
    a pure shape transform. When the chip computation isn't available
    (e.g. analyzer not importable), the field is omitted rather than
    set to an empty list — that way "no chips computed" is
    distinguishable from "computed and all clear".
    """
    # Derive a single-word status for at-a-glance summarization.
    # Mirrors the legacy admin-UI logic: live → online, else if there's
    # a recent metric → active, else offline. Gateway-down (where the
    # gateway probe is fresh and unreachable) is offline regardless of
    # metric recency.
    gateway_down = (
        bot_dict.get("gateway_status_fresh") is True
        and bot_dict.get("gateway_reachable") is False
    )
    if gateway_down:
        status = "offline"
    elif bot_dict.get("live"):
        status = "online"
    elif bot_dict.get("last_metric_date"):
        status = "active"
    else:
        status = "offline"

    out: dict[str, Any] = {
        "bot_id": bot_id,
        "role": bot_dict.get("role") or "member",
        "port": bot_dict.get("port"),
        "status": status,
        "last_metric_date": bot_dict.get("last_metric_date"),
    }
    # Version-drift signals: include when the bot has a recorded version.
    ver = bot_dict.get("evolve_version")
    if ver:
        out["evolve_version"] = ver
        out["evolve_synced"] = bot_dict.get("evolve_synced") is not False
    # Continuity engine opt-in / out (default-on per memory)
    ce = bot_dict.get("continuity_engine_enabled")
    if ce is not None:
        out["continuity_engine_enabled"] = bool(ce)
    # Tile-derived health chips (UI pills) — same shape compute_tile_data
    # returns, with the model-irrelevant ``nav`` field stripped. Only set
    # the field when the caller actually computed chips; absence means
    # "not computed" rather than "empty list (all clear)".
    if tile_chips is not None:
        out["tile_chips"] = [
            {k: v for k, v in chip.items() if k != "nav"}
            for chip in tile_chips
        ]
    return out


def _compute_chips_for_bot(
    shared_dir: Path,
    bot_id: str,
    bot_data: dict[str, Any],
    network: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Call analyzer.tile_metrics.compute_tile_data and pull ``health_chips``.

    Returns the list of chips on success, ``None`` when the chip
    computation isn't available (analyzer not importable / per-bot
    failure). Each chip in the live path is a dict with keys
    ``id, severity, label, detail`` (and ``nav`` which the projector
    strips). Defensive — never raises; failures collapse to None so
    the model sees "chips not computed" rather than a partial dict.
    """
    try:
        from tile_metrics import compute_tile_data
    except ImportError as exc:
        log.debug("pod_state.bots: tile_metrics unavailable: %s", exc)
        return None
    try:
        tile = compute_tile_data(
            shared_dir=shared_dir,
            bot_id=bot_id,
            bot_data=bot_data,
            network=network,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pod_state.bots: compute_tile_data failed for %s: %s",
            bot_id, exc,
        )
        return None
    chips = tile.get("health_chips")
    return chips if isinstance(chips, list) else None


def _shape_response_from_status_data(
    status_data: dict[str, Any],
    bot_id: str | None,
) -> dict[str, Any]:
    """Re-shape /api/status output into the tool's response shape.

    /api/status returns the daemon's full per-bot blob (each bot's
    network_status entry + a precomputed ``tile`` dict that includes
    ``health_chips``). The tool's contract is a different shape — flat
    list of projected bots with ``tile_chips`` plucked off the tile.

    Same projection ``_project_bot`` already did, just sourcing the
    chips from ``bot_data.tile.health_chips`` (the daemon-computed list)
    instead of an in-process compute_tile_data call.
    """
    bots_dict = status_data.get("bots") or {}

    def _chips_from_tile(bot_data: dict[str, Any]) -> list[dict[str, Any]] | None:
        tile = bot_data.get("tile") if isinstance(bot_data, dict) else None
        if not isinstance(tile, dict):
            return None
        chips = tile.get("health_chips")
        return chips if isinstance(chips, list) else None

    if bot_id:
        b = bots_dict.get(bot_id)
        if b is None:
            return {
                "count": 0,
                "bots": [],
                "error": f"bot '{bot_id}' not found in network.json::members",
            }
        return {
            "count": 1,
            "bots": [_project_bot(bot_id, b, tile_chips=_chips_from_tile(b))],
        }

    projected = [
        _project_bot(bid, b, tile_chips=_chips_from_tile(b))
        for bid, b in bots_dict.items()
    ]
    return {
        "count": len(projected),
        "bots": projected,
        "primary": status_data.get("primary"),
        "network_id": status_data.get("network_id"),
    }


def _fetch_pod_state_via_daemon() -> dict[str, Any] | None:
    """Phase E.3.3 — fetch pod state via the admin daemon's /api/status route.

    Returns the daemon's /api/status JSON dict, or None if the daemon
    is unreachable. /api/status already computes the full tile_metrics
    chip set + breakers + key status, so the projection downstream is
    just a re-shape of fields the daemon already produced. After Phase
    E.2.b cutover, the in-process tile_metrics call won't work
    (cross-bot reads fail on the unprivileged ``evo`` user), so this
    becomes the only working path.
    """
    try:
        from ..admin_client import AdminDaemonUnavailable, get_json
        status, body = get_json("/api/status")
        if status == 200 and isinstance(body, dict):
            return body
        log.warning(
            "pod_state.bots: /api/status returned %d via admin daemon; "
            "falling back to in-process network_status",
            status,
        )
    except AdminDaemonUnavailable as exc:
        # WARNING, not DEBUG — mute fallbacks hid the 2026-07-28 outage
        # for 10 days. log_daemon_fallback carries socket path + errno.
        from ..admin_client import log_daemon_fallback
        log_daemon_fallback("pod_state.bots GET /api/status", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("pod_state.bots: admin daemon /api/status raised: %s", exc)
    return None


def _handler(
    network_path: Path,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Return per-bot status across the pod.

    If ``bot_id`` is provided, returns just that bot (1-element list).
    Otherwise returns all bots in network.json::members order.

    Each bot includes ``tile_chips`` — the same health pills the admin
    UI renders on the bot's dashboard tile. The model sees what the
    operator sees on the dashboard, closing the gap reported in
    docs/spec-evo-oc-native-2026-05-19.md §3.5 (operator asks evo about
    a tile pill, evo had no idea it existed).

    Phase E.3.3 path: first try the admin daemon's /api/status endpoint
    (which already runs network_status + tile_metrics with full ACL
    access). Falls back to the in-process path during the migration
    window. After Phase E.2.b cutover, in-process cross-bot reads stop
    working and the daemon path is the only viable one.
    """
    # Primary path: admin daemon /api/status.
    daemon_data = _fetch_pod_state_via_daemon()
    if daemon_data is not None:
        return _shape_response_from_status_data(daemon_data, bot_id)

    # Fallback path: original in-process implementation.
    try:
        from evolve_admin.status import network_status
    except ImportError as exc:
        log.warning("pod_state.bots: network_status unavailable: %s", exc)
        return {"count": 0, "bots": [], "error": "network_status unavailable"}

    try:
        ns = network_status(network_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.bots: network_status call failed")
        return {"count": 0, "bots": [], "error": f"network_status failed: {exc}"}

    # Resolve sharedDir + the full network dict once — tile_metrics
    # needs both for chip rules that consult shared-dir paths
    # (applications/<bot>/.scan-status.json, repo-puller mtime, etc.).
    import json
    shared_dir = Path("/Users/Shared/evolve")  # default; override below
    network_obj: dict[str, Any] = {}
    try:
        network_obj = json.loads(Path(network_path).read_text())
        shared_dir = Path(network_obj.get("sharedDir") or shared_dir)
    except Exception:  # noqa: BLE001
        # Network file missing/unreadable on the proxy side is unusual
        # (network_status above would have failed too) but defensive.
        pass

    bots_dict = ns.get("bots") or {}
    if bot_id:
        b = bots_dict.get(bot_id)
        if b is None:
            return {
                "count": 0,
                "bots": [],
                "error": f"bot '{bot_id}' not found in network.json::members",
            }
        chips = _compute_chips_for_bot(shared_dir, bot_id, b, network_obj)
        return {"count": 1, "bots": [_project_bot(bot_id, b, tile_chips=chips)]}

    projected = []
    for bid, b in bots_dict.items():
        chips = _compute_chips_for_bot(shared_dir, bid, b, network_obj)
        projected.append(_project_bot(bid, b, tile_chips=chips))
    return {
        "count": len(projected),
        "bots": projected,
        "primary": ns.get("primary"),
        "network_id": ns.get("network_id"),
    }


TOOL = Tool(
    name="pod_state.bots",
    description=(
        "List every bot in the pod with its status, role, version, "
        "port, AND the same tile-derived health chips the admin UI "
        "dashboard renders next to each bot. Chip ids include "
        "'scan_needed', 'cost_spike', 'security_critical', "
        "'repo_puller_stale', 'apps_untested' — each with severity "
        "(info/warn/critical) and a short detail string. Use this "
        "tool when the operator asks about anything they see on a bot "
        "tile (a 'scan needed' pill, an upgrade badge, a security "
        "warning) so your answer matches exactly what's on screen. "
        "Status is 'online' (gateway reachable + recent metric), "
        "'active' (recent metric but gateway probe says offline), or "
        "'offline'. Filter to one bot with bot_id. Result also "
        "includes 'primary' (the primary bot id) and 'network_id'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Restrict to just this bot. Omit to return all "
                    "bots in network.json::members."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "bots"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(TOOL)

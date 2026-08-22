"""describe_bot — composed snapshot of one bot.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.

Returns the bot's role, integrations, recent firing signals, and 7-day
spend in a single envelope so the primary bot can answer "tell me
about admin_bot" with one tool call instead of fanning out.

Pure composition — no new I/O patterns beyond what the other pod_state
functions already do. Each piece is independently testable through
its source function; this one stays small.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .signals import list_signals
from .spend import spend_rollup


def describe_bot(
    shared_dir: Path,
    *,
    bot_id: str,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a composed snapshot of ``bot_id``.

    Returns
    -------
    {
        "bot_id": str,
        "role": "primary" | "member" | None,
        "tier": str | None,
        "integrations": [str, ...],          # from network.bots[id].integrations
        "apps_installed": int,               # rough count from network.bots[id].apps
        "firing_signals": {count, recent: [...]},   # last 5
        "spend_7d_usd": float,
        "found": bool,
    }

    ``found=False`` when the bot is not in ``network.json``. Returning
    a structured "not found" rather than raising lets the LLM say
    "I don't recognize that bot" instead of surfacing an exception.
    """
    net = network if network is not None else _load_network(shared_dir)
    bots_block = net.get("bots") or {}

    if not bot_id or bot_id not in bots_block:
        return {
            "bot_id": bot_id or "",
            "found": False,
            "role": None,
            "tier": None,
            "integrations": [],
            "apps_installed": 0,
            "firing_signals": {"count": 0, "recent": []},
            "spend_7d_usd": 0.0,
        }

    cfg = bots_block[bot_id] or {}
    if not isinstance(cfg, dict):
        cfg = {}

    # Pull the latest few firing signals via the same library
    # function the bot already trusts.
    signal_result = list_signals(
        shared_dir, state="firing", bot_id=bot_id, limit=5,
    )
    # spend_rollup tolerates missing rollup files (returns 0).
    spend_result = spend_rollup(
        shared_dir, window="7d", bot_id=bot_id, network=net,
    )

    integrations = _extract_integrations(cfg)
    apps_count = _extract_apps_count(cfg)

    return {
        "bot_id": bot_id,
        "found": True,
        "role": cfg.get("role"),
        "tier": cfg.get("tier"),
        "integrations": integrations,
        "apps_installed": apps_count,
        "firing_signals": {
            "count": signal_result["count"],
            "recent": signal_result["signals"],
        },
        "spend_7d_usd": spend_result["total_usd"],
    }


def _extract_integrations(cfg: dict[str, Any]) -> list[str]:
    """Best-effort list of integration ids on this bot.

    Different parts of network.json represent integrations differently
    (a dict keyed by id, a list of ids, sometimes a per-channel block).
    Try the dict-of-id shape first since that's what the wizard
    writes today.
    """
    raw = cfg.get("integrations")
    if isinstance(raw, dict):
        return sorted(raw.keys())
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def _extract_apps_count(cfg: dict[str, Any]) -> int:
    raw = cfg.get("apps")
    if isinstance(raw, dict):
        return len(raw)
    if isinstance(raw, list):
        return len(raw)
    return 0


def _load_network(shared_dir: Path) -> dict[str, Any]:
    import json
    path = Path(shared_dir) / "network.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

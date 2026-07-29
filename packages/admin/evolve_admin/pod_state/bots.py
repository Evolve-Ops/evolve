"""pod_status — high-level "what's in the pod" snapshot.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.

Composed from ``network.json`` (the source of truth for pod membership
per ``feedback_explicit_pod_membership.md``) plus a count of firing
signals from the signal store. Deliberately minimal: when the bot asks
"what bots do I have?" we want one cheap call, not a fan-out across
every per-bot config file.

Last-audit timestamps would be useful (spec §5.1) but the audit cache
is process-local to the admin server today; surfacing it requires
either promoting the cache to disk or routing this call through the
admin process. Out of scope for the pure-Python layer — the HTTP
route can augment if needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any



def pod_status(
    shared_dir: Path,
    *,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a summary of the pod.

    Pass ``network`` if you've already loaded it (avoids a second
    read); otherwise we load from ``{shared_dir}/network.json``.

    Returns
    -------
    {
        "primary_id": str | None,
        "bots": [
            {"bot_id": str, "role": "primary"|"member", "tier": str|None,
             "firing_signals": int},
            ...
        ],
        "firing_signals_total": int,
    }
    """
    net = network if network is not None else _load_network(shared_dir)
    bots_block = net.get("bots") or {}

    primary_id = _resolve_primary_id(bots_block)

    # One pass over the signal store; bucket counts per-bot for cheap
    # cross-reference. Pod-wide signals (bot_id None) go into a
    # separate counter so we don't over-report on member bots.
    firing_by_bot, firing_pod_wide = _firing_counts(shared_dir)

    bot_summaries: list[dict[str, Any]] = []
    for bot_id, cfg in sorted(bots_block.items()):
        if not isinstance(cfg, dict):
            continue
        bot_summaries.append({
            "bot_id": bot_id,
            "role": cfg.get("role") or "member",
            "tier": cfg.get("tier"),
            "firing_signals": firing_by_bot.get(bot_id, 0),
        })

    return {
        "primary_id": primary_id,
        "bots": bot_summaries,
        "firing_signals_total": sum(firing_by_bot.values()) + firing_pod_wide,
        "firing_signals_pod_wide": firing_pod_wide,
    }


def _load_network(shared_dir: Path) -> dict[str, Any]:
    """Read network.json from shared_dir. Returns {} on missing/corrupt
    rather than raising — the bot reporting "no pod configured" is a
    better LLM experience than a 500."""
    import json
    path = Path(shared_dir) / "network.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_primary_id(bots_block: dict[str, Any]) -> str | None:
    """Find the bot with role=='primary' in the bots block. None if
    no primary is configured (legacy pre-wizard pods)."""
    for bot_id, cfg in bots_block.items():
        if isinstance(cfg, dict) and cfg.get("role") == "primary":
            return bot_id
    return None


def _firing_counts(shared_dir: Path) -> tuple[dict[str, int], int]:
    """Return (per-bot firing count, pod-wide firing count)."""
    try:
        from signals import store as _store  # type: ignore[import-not-found]
    except ImportError:
        return {}, 0

    per_bot: dict[str, int] = {}
    pod_wide = 0
    try:
        for sig in _store.iter_active(shared_dir, state="firing"):
            if sig.bot_id:
                per_bot[sig.bot_id] = per_bot.get(sig.bot_id, 0) + 1
            else:
                pod_wide += 1
    except (OSError, ValueError):
        return {}, 0
    return per_bot, pod_wide

"""spend_rollup — per-bot cost / token summary over a trailing window.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``{shared_dir}/metrics/{bot_id}/cost-{YYYY-MM-DD}.json``
daily rollups written by ``packages/analyzer/cost_rollup.py``.

The existing ``evo cost`` handler (``evolve_admin.evo.handlers.cost``)
reads the same files and renders them as a Team_bot_a-style message; this
function returns structured JSON for the bot's LLM to render itself.
Math is duplicated rather than shared because the handler's API is
private (``_load_rollups`` / ``_sum_window``) and the file format is
the actual contract. Follow-up: consolidate into a shared rollup
reader module if a third caller appears.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any


# Supported windows. Keys are user-facing; values are the day counts.
_WINDOWS: dict[str, int] = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
}

DEFAULT_WINDOW = "7d"


def spend_rollup(
    shared_dir: Path,
    *,
    window: str = DEFAULT_WINDOW,
    bot_id: str | None = None,
    network: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return spend / token totals for ``window`` (1d|7d|30d).

    ``bot_id=None`` returns pod-wide totals across every bot listed in
    ``network.json``'s ``bots`` block. Pass ``network`` if you've
    already loaded it; otherwise we load it from
    ``{shared_dir}/network.json``.

    Returns
    -------
    {
        "window": "7d",
        "days": 7,
        "bot_id": str | None,
        "total_usd": float,
        "input_tokens": int,
        "output_tokens": int,
        "by_bot": {bot_id: {total_usd, input_tokens, output_tokens}}  # pod-wide only
        "by_model_top": [{"model": str, "cost_usd": float}, ...top 3...],
        "as_of": "YYYY-MM-DD",
        "filters": {"window": str, "bot_id": str | None, "requested_window": str | None},
    }

    Missing rollup files count as "no data" (not "zero") — the response
    surfaces zeros and the bot can tell admin "no spend recorded yet"
    rather than fabricating a confident zero.

    ``filters.requested_window`` echoes whatever the caller passed so
    the LLM can detect coercion (parallel to ``list_signals``'s filters
    echo — silent broadening on a typoed window was caught in second-
    pass review of Bundle 3-part-2).
    """
    norm_window = window if window in _WINDOWS else DEFAULT_WINDOW
    days = _WINDOWS[norm_window]
    today = date.today()

    filters = {
        "window": norm_window,
        "bot_id": bot_id,
        "requested_window": window,
    }
    if bot_id is None:
        result = _pod_wide(shared_dir, network, norm_window, days, today)
    else:
        result = _per_bot(shared_dir, bot_id, norm_window, days, today)
    result["filters"] = filters
    return result


# ── Per-bot ──────────────────────────────────────────────────────────────────


def _per_bot(
    shared_dir: Path, bot_id: str, window: str, days: int, today: date
) -> dict[str, Any]:
    rollups = _load_rollups(shared_dir, bot_id, days=days, today=today)
    totals = _sum_window(rollups, today, days)
    return {
        "window": window,
        "days": days,
        "bot_id": bot_id,
        "total_usd": totals["total_usd"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "by_model_top": _top_models(rollups, today, days, n=3),
        "as_of": today.isoformat(),
    }


# ── Pod-wide ─────────────────────────────────────────────────────────────────


def _pod_wide(
    shared_dir: Path,
    network: dict[str, Any] | None,
    window: str,
    days: int,
    today: date,
) -> dict[str, Any]:
    net = network if network is not None else _load_network(shared_dir)
    bots_block = net.get("bots") or {}

    total_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    by_bot: dict[str, dict[str, Any]] = {}
    # Pod-wide top-model rollup pools all bots' by_model buckets.
    pooled_by_model: dict[date, dict] = {}

    for bot_id in sorted(bots_block.keys()):
        if not isinstance(bots_block.get(bot_id), dict):
            continue
        rollups = _load_rollups(shared_dir, bot_id, days=days, today=today)
        if not rollups:
            continue
        bot_totals = _sum_window(rollups, today, days)
        total_usd += bot_totals["total_usd"]
        input_tokens += bot_totals["input_tokens"]
        output_tokens += bot_totals["output_tokens"]
        if bot_totals["total_usd"] > 0:
            by_bot[bot_id] = bot_totals
        # Merge into pooled_by_model: each per-day rollup contributes
        # to the same date bucket.
        for d, r in rollups.items():
            pooled_by_model.setdefault(d, {"by_model": {}, "total_usd": 0})
            for model, bucket in (r.get("by_model") or {}).items():
                if not isinstance(bucket, dict):
                    continue
                pooled = pooled_by_model[d]["by_model"].setdefault(model, {
                    "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                })
                pooled["cost_usd"] += float(bucket.get("cost_usd") or 0.0)

    return {
        "window": window,
        "days": days,
        "bot_id": None,
        "total_usd": total_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "by_bot": by_bot,
        "by_model_top": _top_models(pooled_by_model, today, days, n=3),
        "as_of": today.isoformat(),
    }


# ── Rollup IO + math ────────────────────────────────────────────────────────


def _load_rollups(
    shared_dir: Path, bot_id: str, *, days: int, today: date
) -> dict[date, dict]:
    base = Path(shared_dir) / "metrics" / bot_id
    if not base.exists():
        return {}
    out: dict[date, dict] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        path = base / f"cost-{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            out[d] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _sum_window(
    rollups: dict[date, dict], today: date, days: int
) -> dict[str, float | int]:
    """Sum total_usd / tokens across the trailing ``days``-day window."""
    start = today - timedelta(days=days - 1)
    total_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    for d, r in rollups.items():
        if not (start <= d <= today):
            continue
        total_usd += float(r.get("total_usd") or 0.0)
        input_tokens += int(r.get("input_tokens") or 0)
        output_tokens += int(r.get("output_tokens") or 0)
    return {
        "total_usd": total_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _top_models(
    rollups: dict[date, dict], today: date, days: int, *, n: int = 3
) -> list[dict[str, Any]]:
    """Top-N models by spend across the window."""
    start = today - timedelta(days=days - 1)
    by_model: dict[str, float] = {}
    for d, r in rollups.items():
        if not (start <= d <= today):
            continue
        for model, bucket in (r.get("by_model") or {}).items():
            if not isinstance(bucket, dict):
                continue
            cost = float(bucket.get("cost_usd") or 0.0)
            by_model[model] = by_model.get(model, 0.0) + cost
    sorted_models = sorted(by_model.items(), key=lambda kv: -kv[1])
    return [
        {"model": model, "cost_usd": cost}
        for model, cost in sorted_models[:n]
        if cost > 0
    ]


def _load_network(shared_dir: Path) -> dict[str, Any]:
    """Read network.json from shared_dir; return {} on missing/corrupt."""
    path = Path(shared_dir) / "network.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

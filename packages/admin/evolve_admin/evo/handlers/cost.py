"""``evo cost`` — 1d / 7d / 30d spend summary.

Scope: per-bot on every bot except the sysadmin bot (``evolve``), which
reports pod-wide. Reads pre-aggregated daily rollups from
``{shared_dir}/metrics/{bot_id}/cost-{YYYY-MM-DD}.json`` written by
``packages/analyzer/cost_rollup.py``.

No HTTP calls, no LLM. Missing rollup files = "no data that day" (not
"zero spend"), matching the convention Budget Hawk uses.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import fmt_usd, is_pod_wide_caller, pod_member_bots, speak


_WINDOWS: tuple[tuple[str, int], ...] = (
    ("Today",   1),
    ("7 days",  7),
    ("30 days", 30),
)


def render(
    *, role: Role, bot_id: str, args: str, network: dict[str, Any]
) -> "object":  # DispatchResult — late import to avoid circular
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    today = date.today()

    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(shared_dir, network, today)
    else:
        body = _render_bot(shared_dir, bot_id, today)
    return speak("cost", body, role)


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot
# ─────────────────────────────────────────────────────────────────────────────


def _render_bot(shared_dir: Path, bot_id: str, today: date) -> str:
    rollups = _load_rollups(shared_dir, bot_id, days=30, today=today)
    if not rollups:
        return (
            f"**Cost — {bot_id}**\n\n"
            "No cost data recorded yet. Either nothing has run, or the "
            "cost rollup job hasn't filled in numbers for this bot."
        )

    lines = [f"**Cost — {bot_id}**", ""]
    for label, days in _WINDOWS:
        total = _sum_window(rollups, today, days)
        lines.append(f"{label}: {fmt_usd(total)}")

    top = _top_model_30d(rollups)
    if top is not None:
        model, model_total = top
        lines.append("")
        lines.append(f"Top model (30d): {model} ({fmt_usd(model_total)})")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Pod-wide
# ─────────────────────────────────────────────────────────────────────────────


def _render_pod(shared_dir: Path, network: dict[str, Any], today: date) -> str:
    members = pod_member_bots(network)
    by_bot: dict[str, dict[str, float]] = {}
    for bot in members:
        rollups = _load_rollups(shared_dir, bot, days=30, today=today)
        by_bot[bot] = {
            label: _sum_window(rollups, today, days) for label, days in _WINDOWS
        }

    totals = {label: sum(b[label] for b in by_bot.values()) for label, _ in _WINDOWS}

    lines = ["**Cost — pod**", ""]
    for label, _ in _WINDOWS:
        lines.append(f"{label}: {fmt_usd(totals[label])}")

    active_bots = [(bot, b["30 days"]) for bot, b in by_bot.items() if b["30 days"] > 0]
    if active_bots:
        lines.append("")
        lines.append("By bot (30d):")
        for bot, total in sorted(active_bots, key=lambda x: -x[1]):
            lines.append(f"  {bot}: {fmt_usd(total)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Rollup IO
# ─────────────────────────────────────────────────────────────────────────────


def _load_rollups(
    shared_dir: Path, bot_id: str, *, days: int, today: date
) -> dict[date, dict]:
    """Return ``{date: rollup_dict}`` for trailing ``days`` of cost data.

    Missing files are skipped silently — that's "no spend recorded",
    not "zero". Malformed files are also skipped (best-effort).
    """
    base = shared_dir / "metrics" / bot_id
    if not base.exists():
        return {}
    out: dict[date, dict] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        path = base / f"cost-{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            out[d] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _sum_window(rollups: dict[date, dict], today: date, days: int) -> float:
    """Sum total_usd across the trailing ``days``-day window ending ``today``."""
    start = today - timedelta(days=days - 1)
    return sum(
        float(r.get("total_usd") or 0.0)
        for d, r in rollups.items()
        if start <= d <= today
    )


def _top_model_30d(rollups: dict[date, dict]) -> tuple[str, float] | None:
    """Return the (model_id, cost_usd) with highest 30d spend, or None."""
    by_model: dict[str, float] = {}
    for r in rollups.values():
        for model, bucket in (r.get("by_model") or {}).items():
            if not isinstance(bucket, dict):
                continue
            cost = float(bucket.get("cost_usd") or 0.0)
            by_model[model] = by_model.get(model, 0.0) + cost
    if not by_model:
        return None
    top_model = max(by_model, key=lambda k: by_model[k])
    if by_model[top_model] <= 0:
        return None
    return top_model, by_model[top_model]

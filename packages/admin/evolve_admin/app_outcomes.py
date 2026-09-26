"""app_outcomes.py — the outcome view's reader (design §3.2, D-AP6).

``get_app_outcomes`` is the ONE reader for ``{shared}/{bot}/outcome-by-
app.json`` (built by ``analyzer/outcome_by_app.py``) — the admin API's
``/api/apps/<app_id>`` detail route and the ``pod_state.app_outcomes`` evo
tool both call it, the same way ``usage_by_app.load_usage_by_app`` is the
one reader AL-1.3's usage view goes through (design-app-attribution §8).

Tri-state honesty (the brief's own words): **an app with no Tracker rows
reads ``cannot-measure``, never zero.** Concretely: this rollup is written
per bot (one file, keyed by the Tracker application each of that bot's
lists resolves to — see ``outcome_by_app``'s module docstring). An
``app_id`` this function is asked about might not be a Tracker application
at all (most apps aren't — the Tracker is one of many stores a platform app
can use), so ``measured: False`` here means exactly "no bot's Tracker has
ever produced a row for this app_id", which is the honest default for
every app that isn't the Assistant or the Project Manager.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Iterable

WINDOWS = ("d1", "d7", "d30")
DEFAULT_PERIOD = "d7"


def _outcome_by_app():
    """The analyzer rollup module, or ``None`` when it can't be imported.

    Same shape as ``routes_apps._load_usage``'s ``importlib.import_module
    ("usage_by_app")`` — the admin and analyzer packages share one uv
    workspace, but the import stays soft so an admin process running
    without the analyzer package degrades to "not measured" rather than
    failing the whole request.
    """
    try:
        return importlib.import_module("outcome_by_app")
    except Exception:
        return None


def _empty_metric() -> dict[str, Any]:
    return {"count": 0, "card_ids": [], "truncated": False}


def _merge_metric(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum one metric's ``{count, card_ids, truncated}`` across bots."""
    ids: set[str] = set()
    truncated = False
    for part in parts:
        truncated = truncated or bool(part.get("truncated"))
        ids.update(part.get("card_ids") or [])
    return {
        "count": sum(p.get("count", 0) for p in parts),
        "card_ids": sorted(ids),
        "truncated": truncated,
    }


def _merge_window(windows: list[dict[str, Any]], metrics: Iterable[str]) -> dict[str, Any]:
    return {metric: _merge_metric([w.get(metric) or _empty_metric() for w in windows])
            for metric in metrics}


def _merge_backlog(backlogs: list[dict[str, Any]]) -> dict[str, Any]:
    """Backlog figures are AS-OF-TODAY snapshots, not day-bucketed counts —
    counts sum across bots; an age with no open cards anywhere is ``None``,
    never a fabricated 0."""
    ages_p50 = [b["backlog_age_p50_days"] for b in backlogs
                if b.get("backlog_age_p50_days") is not None]
    ages_max = [b["backlog_age_max_days"] for b in backlogs
                if b.get("backlog_age_max_days") is not None]
    return {
        "open_cards": sum(b.get("open_cards", 0) for b in backlogs),
        # A pod-wide max is the true max; a pod-wide "p50" of several
        # bots' own medians is a median of medians, not the true p50 —
        # reported as such rather than claimed as more than it is.
        "backlog_age_p50_of_bot_medians_days": (
            sorted(ages_p50)[len(ages_p50) // 2] if ages_p50 else None),
        "backlog_age_max_days": max(ages_max) if ages_max else None,
        "blocked_over_3_days": sum(b.get("blocked_over_3_days", 0) for b in backlogs),
    }


def get_app_outcomes(
    shared_dir: Path, app_id: str, bots: Iterable[str], *, period: str = DEFAULT_PERIOD,
) -> dict[str, Any]:
    """One app's outcome-view payload, across every bot that has it.

    Returns ``{app_id, period, measured: False}`` when no bot's Tracker has
    ever produced a row for ``app_id`` — the tri-state honesty the brief
    requires, computed here (not in the rollup, which simply omits an
    app_id it never saw) because "no rows anywhere" is a statement about
    the WHOLE pod, and only a reader that has looked across every bot can
    make it.
    """
    if period not in WINDOWS:
        raise ValueError(f"period must be one of {WINDOWS}")

    mod = _outcome_by_app()
    if mod is None:
        return {"app_id": app_id, "period": period, "measured": False,
                "reason": "the outcome rollup is unavailable on this pod"}

    per_bot_entries: dict[str, dict[str, Any]] = {}
    for bot_id in bots:
        try:
            payload = mod.load_outcome_by_app(shared_dir, bot_id)
        except Exception:
            payload = {}
        entry = (payload.get("apps") or {}).get(app_id)
        if entry:
            per_bot_entries[bot_id] = entry

    if not per_bot_entries:
        return {"app_id": app_id, "period": period, "measured": False}

    windows = [e.get("windows", {}).get(period) or {} for e in per_bot_entries.values()]
    metrics = getattr(mod, "METRICS", ())
    total = _merge_window(windows, metrics)

    users: dict[str, list[dict[str, Any]]] = {}
    for entry in per_bot_entries.values():
        for user_id, user_windows in (entry.get("users") or {}).items():
            users.setdefault(user_id, []).append(user_windows.get(period) or {})
    users_out = {user_id: _merge_window(parts, metrics) for user_id, parts in users.items()}

    daily: dict[str, dict[str, int]] = {}
    for entry in per_bot_entries.values():
        for day, counts in (entry.get("daily") or {}).items():
            bucket = daily.setdefault(day, {m: 0 for m in metrics})
            for metric, count in counts.items():
                bucket[metric] = bucket.get(metric, 0) + count

    backlog = _merge_backlog([e.get("backlog") or {} for e in per_bot_entries.values()])

    return {
        "app_id": app_id,
        "period": period,
        "measured": True,
        "bots": sorted(per_bot_entries),
        "total": total,
        "users": users_out,
        "daily": daily,
        "backlog": backlog,
    }

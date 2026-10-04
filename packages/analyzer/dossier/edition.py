"""dossier.edition — assemble one weekly edition from the collectors.

The payload is deliberately boring: numbers, ids, and the window each was
measured over. No narration, no scoring, no interpretation — those are the
Pod Intelligence surface's job, later, reading a spine that by then has
history. Everything this module decides is a decision the future reader
cannot un-make, so it decides as little as possible.

``build_edition`` is pure with respect to time and roster: pass ``now`` and it
is fully deterministic given the same on-disk state, which is what makes the
idempotence test an equality assertion rather than a fuzzy one.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dossier import sources
from dossier.window import EditionWindow, iso_z

#: Bumped only for a breaking change to the on-disk shape. A reader that sees
#: a version it does not know MUST refuse the edition rather than guess.
SCHEMA_VERSION = 1


def build_edition(
    shared_dir: Path,
    network: dict[str, Any],
    window: EditionWindow,
    *,
    now: datetime,
    bots: list[str] | None = None,
    bot_home: Callable[[str], Path] | None = None,
    home_overrides: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Collect every producer and shape the edition payload.

    ``bots`` / ``bot_home`` / ``home_overrides`` are injection seams for
    tests and for a fixture pod; production passes none of them and gets the
    real roster gate (``pod_baseline.census.pod_roster`` — the same gate
    deploy uses, so planned-but-unprovisioned bots never appear as phantom
    all-null rows) and the real home resolution.
    """
    roster = list(bots) if bots is not None else _pod_roster(network)
    home = bot_home or _default_bot_home(network)

    costs = sources.collect_costs(shared_dir, roster, window)
    per_app = sources.collect_per_app(shared_dir, roster)
    users = sources.collect_users(shared_dir, roster)
    drafts = sources.collect_drafts(roster, bot_home=home)
    drift = sources.collect_drift(shared_dir, network, home_overrides=home_overrides)
    signals = sources.collect_signals(shared_dir, window)
    activity = sources.collect_activity(shared_dir, roster, window)
    # The fire history's window is the trailing 28 days as of the edition's
    # own last day — NOT the seven days of the week. A four-week strip is
    # what makes "ran on 25 of the last 28 days" a sentence about a habit
    # rather than about one week's luck.
    #
    # Clamped to TODAY for a week still in progress. Without the clamp a
    # Wednesday run of an open week counts Thursday through Sunday as days
    # the app failed to run — marking the future as missed, which is the one
    # way a reliability strip can be actively defamatory.
    fires = sources.collect_fires(
        shared_dir, roster,
        today=min(window.days[-1], now.astimezone(window.start.tzinfo).date()),
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "edition_id": window.edition_id,
        "computed_at": iso_z(now),
        # An edition computed over a window that had already fully elapsed is
        # a final measurement; one computed mid-week is a provisional read of
        # an open window. Only the first is immutable (dossier.store).
        "sealed": window.complete,
        "window": window.to_dict(),
        "pod": {
            "shared_dir": str(shared_dir),
            "pod_id": network.get("pod_id") or network.get("podId") or None,
            "timezone": window.timezone,
            "roster": sources.collect_roster(network, roster, activity, costs),
        },
        "per_bot": _per_bot(roster, costs, drafts, activity),
        "per_app": per_app,
        "users": users,
        "drafts": drafts,
        "drift": drift,
        "signals": signals,
        "fires": fires,
        "costs": costs,
    }


def _per_bot(
    roster: list[str],
    costs: dict[str, Any] | None,
    drafts: dict[str, Any] | None,
    activity: dict[str, Any] | None,
) -> dict[str, Any]:
    """The per-bot view — a re-key of the same measurements, not new ones.

    Every bot on the roster gets a row even when every producer was silent
    for it, because "this bot exists and nothing measured it" is exactly the
    fact a longitudinal reader needs. The row's values are then null, per the
    tri-state law, never zero.
    """
    cost_by_bot = (costs or {}).get("by_bot") or {}
    drafts_by_bot = (drafts or {}).get("by_bot") or {}
    activity_by_bot = (activity or {}).get("by_bot") or {}
    out: dict[str, Any] = {}
    for bot in roster:
        cost_row = cost_by_bot.get(bot) if costs is not None else None
        activity_row = activity_by_bot.get(bot) if activity is not None else None
        out[bot] = {
            "costs": cost_row,
            "drafts": drafts_by_bot.get(bot) if drafts is not None else None,
            "activity": activity_row,
            # Tri-state, one level down: with NEITHER producer we do not know
            # whether the bot was active, and `false` would be a claim we
            # cannot support. The annotation footprint answers first because
            # it exists on pods where cost_rollup is not scheduled.
            "active_in_window": _active(activity_row, cost_row, activity, costs),
        }
    return out


def _active(
    activity_row: Any,
    cost_row: Any,
    activity: dict[str, Any] | None,
    costs: dict[str, Any] | None,
) -> bool | None:
    if activity is not None and isinstance(activity_row, dict):
        return bool(activity_row.get("active"))
    if costs is not None:
        return cost_row is not None
    return None


def _pod_roster(network: dict[str, Any]) -> list[str]:
    try:
        from pod_baseline.census import pod_roster

        return list(pod_roster(network))
    except Exception:
        members = [b for b in (network.get("members") or []) if isinstance(b, str) and b]
        primary = network.get("primary")
        if isinstance(primary, str) and primary and primary not in members:
            members.append(primary)
        return sorted(set(members))


def _default_bot_home(network: dict[str, Any]) -> Callable[[str], Path]:
    from evolve_config import bot_home

    return lambda bot_id: bot_home(bot_id, network)

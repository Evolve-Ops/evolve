"""recent_watchdog — read the tail of the WatchdogEvent log.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``{shared_dir}/watchdog/<YYYY-MM-DD>.jsonl``, read via
``analyzer.generators.evolve_watchdog.events.read_events_range``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any



DEFAULT_HOURS = 24
MAX_HOURS = 24 * 7  # one week tail — enough context for "why did X happen this week?"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def recent_watchdog(
    shared_dir: Path,
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    bot_id: str | None = None,
) -> dict[str, Any]:
    """Return WatchdogEvents from the last ``hours`` hours.

    Most-recent first (the LLM almost always wants "what just
    happened?"). Filtered by ``bot_id`` if provided; pod-wide events
    (bot_id is None on the event) are included only when no filter
    is applied — keeps "show me team_bot_a's watchdog events" from returning
    every pod-wide event the bot doesn't care about.

    Returns ``{"events": [{...}, ...], "count": N, "filters": {...}}``.
    """
    from generators.evolve_watchdog.events import read_events_range  # type: ignore[import-not-found]

    capped_hours = max(1, min(int(hours) if hours else DEFAULT_HOURS, MAX_HOURS))
    capped_limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=capped_hours)

    matches: list[Any] = []
    try:
        for ev in read_events_range(shared_dir, start, now):
            if bot_id is not None and ev.bot_id != bot_id:
                continue
            matches.append(ev)
    except (OSError, ValueError):
        # Corrupt log dir / unreadable files: surface "no events" rather
        # than failing the tool call. Same pattern as iter_active in
        # the signal store.
        matches = []

    # Reverse so newest-first; cap after sorting so we always get the
    # most recent N, not the oldest N read.
    matches.sort(key=lambda e: e.timestamp, reverse=True)
    matches = matches[:capped_limit]

    return {
        "events": [ev.to_dict() for ev in matches],
        "count": len(matches),
        "filters": {
            "hours": capped_hours,
            "limit": capped_limit,
            "bot_id": bot_id,
        },
    }

"""In-turn cost checkpoint evidence (D-CS12) — read-only.

The plugin (``packages/plugin/src/breakers/InTurnCheckpoint.ts``) writes
``{shared_dir}/breakers/<bot>/`` ``in-turn-checkpoints.jsonl`` and
``in-turn-checkpoint-status.json``; the pod report, Cost page and ``health``
read them here. Missing / malformed → no rows / ``None``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER_FILENAME = "in-turn-checkpoints.jsonl"
STATUS_FILENAME = "in-turn-checkpoint-status.json"
TRIP_EVENT = "checkpoint_in_turn"


def read_trips(
    shared_dir: Path, bot_id: str, *, days: int = 7, now: datetime | None = None,
) -> list[dict]:
    """``checkpoint_in_turn`` rows for ``bot_id`` in the last ``days`` days."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: list[dict] = []
    try:
        lines = (Path(shared_dir) / "breakers" / bot_id / LEDGER_FILENAME).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        if row.get("event") == TRIP_EVENT and ts >= since:
            out.append(row)
    return out


def count_by_bot(
    shared_dir: Path, members: list[str], *, days: int = 7, now: datetime | None = None,
) -> dict[str, int]:
    """``{bot: trips}`` for bots with at least one in-turn checkpoint."""
    counts = {b: len(read_trips(shared_dir, b, days=days, now=now)) for b in members}
    return {b: n for b, n in counts.items() if n}


def read_status(shared_dir: Path, bot_id: str) -> dict | None:
    """The plugin's status record, or None when absent / unreadable."""
    try:
        data = json.loads((Path(shared_dir) / "breakers" / bot_id / STATUS_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

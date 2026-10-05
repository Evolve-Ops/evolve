"""No-progress gate evidence — read-only.

The plugin (``packages/plugin/src/breakers/NoProgressGate.ts``) writes
``{shared_dir}/breakers/<bot>/`` ``no-progress.jsonl`` (one ``no_progress``
row per stopped run) and ``no-progress-status.json``; the pod report, Cost
page and ``health`` read them here. Missing / malformed → no rows / ``None``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER_FILENAME = "no-progress.jsonl"
STATUS_FILENAME = "no-progress-status.json"
STOP_EVENT = "no_progress"


def read_stops(
    shared_dir: Path, bot_id: str, *, days: int = 7, now: datetime | None = None,
) -> list[dict]:
    """``no_progress`` rows for ``bot_id`` in the last ``days`` days."""
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
        if row.get("event") == STOP_EVENT and ts >= since:
            out.append(row)
    return out


def count_by_bot(
    shared_dir: Path, members: list[str], *, days: int = 7, now: datetime | None = None,
) -> dict[str, int]:
    """``{bot: stops}`` for bots with at least one no-progress stop."""
    counts = {b: len(read_stops(shared_dir, b, days=days, now=now)) for b in members}
    return {b: n for b, n in counts.items() if n}


def read_status(shared_dir: Path, bot_id: str) -> dict | None:
    """The plugin's status record, or None when absent / unreadable."""
    try:
        data = json.loads((Path(shared_dir) / "breakers" / bot_id / STATUS_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def controls_by_bot(shared_dir: Path, members: list[str]) -> dict[str, dict]:
    """Per bot, for the Cost page's controls chip: is the failing-validation
    predicate evaluated, and the model-call ceiling when set."""
    out: dict[str, dict] = {}
    for b in members:
        st = read_status(shared_dir, b)
        if st is None:
            out[b] = {"failing_validation": "unknown", "max_model_calls_per_run": None}
            continue
        ceiling = st.get("max_model_calls_per_run")
        out[b] = {
            "failing_validation": "evaluated" if int(st.get("results_with_status") or 0) > 0 else "not_evaluated",
            "max_model_calls_per_run": ceiling if isinstance(ceiling, int) and ceiling > 0 else None,
        }
    return out

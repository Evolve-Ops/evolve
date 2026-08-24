#!/usr/bin/env python3
"""delivery_status — read-only most-recent delivery outcome per app.

The ``delivery_monitor`` daemon (:mod:`delivery_monitor`) classifies every
scheduled, user-facing delivery window tri-state and appends one row per
window to ``{shared_dir}/delivery_monitor/ledger/<YYYY-MM-DD>.jsonl``.
That ledger is the *real* per-app activity signal — whether the 07:00
briefing actually reached the user — as distinct from
:mod:`usage_logger`'s evidence-file **mtime** sweep, which is a
maintenance footprint ("was anything this app cares about edited
lately?"), not usage.

This module is the reporting-side **reader**. It does not classify (the
classifier is :func:`delivery_monitor.classify_window`) and writes
nothing — it folds the already-classified rows into the most-recent
outcome per ``app_id`` so the admin UI's Apps page can show honest
delivery status on each card instead of inferring "Active" from file
edits.

Ledger row shape (see ``delivery_monitor._ledger_row`` and the
coverage-row writer in ``delivery_monitor.run``)::

    {
      "ts": "...",            # when the monitor classified the window (UTC)
      "bot_id": "...", "app_id": "...", "action_id": "...",
      "window_start": "...", "window_end": "...",   # job-TZ iso, may be null
      "outcome": "on_time" | "late" | "missed"
                 | "unmeasurable" | "disabled" | "unmonitorable",
      "diagnosis": "did_not_run" | "ran_undelivered" | null,
      "suspected_cause": "host_asleep" | "dst" | "script_error" | ...,  # opt.
      "delivered_at": "..." | null,
      "healed": bool,
    }

Privacy: filters by ``bot_id``; the ledger is pod-wide but a single-bot
caller never receives another bot's rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The monitor appends ~one row per app per elapsed window; a daily
# briefing yields ~1 row/day. Thirty days bounds the file walk (≤31 small
# JSONL reads) while still catching the most-recent run of weekly /
# fortnightly schedules. Matches the 90-day ledger retention's spirit
# without reading the whole archive on every card render.
DEFAULT_LOOKBACK_DAYS = 30


def _parse_ts(raw: object) -> datetime | None:
    """Parse an ISO-8601 stamp from a ledger row; tz-naive → UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def latest_status_by_app(
    shared_dir: Path | str,
    bot_id: str,
    *,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict]:
    """Most-recent classified ledger row per ``app_id`` for one bot.

    Returns ``{app_id: status_dict}`` where ``status_dict`` carries the
    raw classification fields plus a ``last_event_ts`` convenience stamp
    (the best "when did this last happen" — ``delivered_at`` →
    ``window_start`` → classification ``ts``). The presentation layer
    maps ``outcome``/``diagnosis`` to label + semantic color.

    Per-app selection is **miss-aware**, not naive newest-wins: an app
    can have several scheduled actions (a 07:00 and a 17:00 delivery).
    We take the most-recent row *per action*, then for the app surface
    the most-recent **missed** action if any exists, else the newest row
    overall. This stops a later success of one action from masking an
    earlier (still-unresolved) miss of another — exactly the
    looks-fine-when-it-isn't failure this honest-reporting work targets.
    Single-action apps are unaffected (their newest row wins either way).

    Apps with no ledger row in the lookback window are simply **absent**
    from the result — the caller renders an honest "delivery not yet
    recorded" / "usage not measured" rather than inferring activity from
    file mtimes. ``_workspace`` pseudo-rows (the per-bot
    workspace-unreadable signal) are skipped — they are not an app.

    Pure, side-effect-free: a missing ledger dir yields ``{}``; corrupt
    lines are skipped, never raised.
    """
    now = now or datetime.now(timezone.utc)
    ledger_dir = Path(shared_dir) / "delivery_monitor" / "ledger"

    # (app_id, action_id) -> (classification_ts, row) — newest ts per action.
    per_action: dict[tuple[str, str], tuple[datetime, dict]] = {}
    for offset in range(max(0, lookback_days) + 1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            text = (ledger_dir / f"{day}.jsonl").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("bot_id") != bot_id:
                continue
            app_id = row.get("app_id")
            if not isinstance(app_id, str) or not app_id or app_id.startswith("_"):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None:
                continue
            key = (app_id, str(row.get("action_id") or ""))
            cur = per_action.get(key)
            if cur is None or ts > cur[0]:
                per_action[key] = (ts, row)

    # Collapse per-action rows to one row per app (miss-aware).
    best: dict[str, tuple[datetime, dict]] = {}
    for (app_id, _action_id), (ts, row) in per_action.items():
        is_miss = row.get("outcome") == "missed"
        cur = best.get(app_id)
        if cur is None:
            best[app_id] = (ts, row)
            continue
        cur_is_miss = cur[1].get("outcome") == "missed"
        # A miss outranks a non-miss regardless of recency; within the same
        # rank, newest wins.
        if is_miss and not cur_is_miss:
            best[app_id] = (ts, row)
        elif is_miss == cur_is_miss and ts > cur[0]:
            best[app_id] = (ts, row)

    out: dict[str, dict] = {}
    for app_id, (ts, row) in best.items():
        last_event = (
            _parse_ts(row.get("delivered_at"))
            or _parse_ts(row.get("window_start"))
            or ts
        )
        out[app_id] = {
            "outcome": row.get("outcome"),
            "diagnosis": row.get("diagnosis"),
            "suspected_cause": row.get("suspected_cause"),
            "window_start": row.get("window_start"),
            "window_end": row.get("window_end"),
            "delivered_at": row.get("delivered_at"),
            "healed": bool(row.get("healed")),
            "ts": row.get("ts"),
            "last_event_ts": last_event.isoformat() if last_event else None,
        }
    return out

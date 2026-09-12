"""
better_engine/snooze.py — Snooze escalation (§6).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Recommendation

# Snooze duration schedule in days (§6)
SNOOZE_SCHEDULE = [1, 2, 4, 7]


def snooze_recommendation(
    rec: "Recommendation",
    *,
    days_override: int | None = None,
) -> "Recommendation":
    """Snooze a recommendation. By default uses the escalating schedule
    (§6) — first snooze 1 day, then 2, 4, 7. Slice 5b8 added the
    ``days_override`` parameter so the conversational-approval handler
    can honor a user-stated duration ("remind me next week" → 7) without
    bypassing the schedule when no hint is present.

    Sets snooze_until, increments snooze_count, sets status="snoozed",
    updates updated_at. Does NOT call record_feedback — the caller (engine) does.
    """
    from .model import now_iso

    if days_override is not None and days_override > 0:
        days = int(days_override)
    else:
        idx = min(rec.snooze_count, len(SNOOZE_SCHEDULE) - 1)
        days = SNOOZE_SCHEDULE[idx]
    rec.snooze_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    rec.snooze_count += 1
    rec.status = "snoozed"
    rec.updated_at = now_iso()
    return rec


def wake_snoozed(recs: list["Recommendation"]) -> list["Recommendation"]:
    """Reactivate any snoozed recommendations whose snooze_until is in the past.

    Returns the same list (mutated in-place), with affected recs set to "pending".
    """
    from .model import now_iso

    now = datetime.now(timezone.utc)
    for rec in recs:
        if rec.status != "snoozed" or not rec.snooze_until:
            continue
        try:
            until = datetime.fromisoformat(rec.snooze_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if now >= until:
            rec.status = "pending"
            rec.snooze_until = None
            rec.updated_at = now_iso()

    return recs

"""housekeeping_cost — what compaction and the memory flush cost, on their own line.

Brief: ``compaction-and-memory-flush-on-cheap-rung`` §4. OpenClaw's pre-compaction
memory flush is a model turn nobody asked for: it runs on the conversation's
session, with the rebuilt context re-cached, to write one note. On the measured
day (``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2) one such
turn cost ~$1.90 and read, on every surface, as part of the conversation.

The plugin now tags those turns ``source: "memory_flush"`` (``compaction`` is
reserved for an OC that runs its summariser as an agent turn; today that cost is
folded into OC's own per-turn accounting and never reaches a separate record).
Pre-fix rows carry OC's raw trigger, ``"memory"`` — counted here too, so the
first receipt after deploy is not missing the week before it.

The receipt contract: a bot's **conversation** line excludes housekeeping, and a
second line under it shows housekeeping with its cost. The pod total is still
every dollar spent — this splits the number, it never hides part of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

#: Turn ``source`` values that are OC housekeeping, not conversation.
HOUSEKEEPING_SOURCES: frozenset[str] = frozenset({"memory_flush", "compaction", "memory"})

#: The receipt's name for the line — plain words, no OC jargon.
RECEIPT_LABEL = "housekeeping (compaction + memory notes)"


def is_housekeeping_turn(turn: Mapping[str, Any]) -> bool:
    """True for a turn row (or cost_event) that is OC housekeeping."""
    for key in ("source", "trigger_kind"):
        val = turn.get(key)
        if isinstance(val, str) and val.strip().lower() in HOUSEKEEPING_SOURCES:
            return True
    return False


@dataclass(frozen=True)
class HousekeepingSpend:
    """One bot's housekeeping over a window. ``measurable`` False = a floor."""

    usd: float
    measurable: bool = True


def housekeeping_over_local_days(
    bot_id: str,
    *,
    days: int = 7,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
) -> HousekeepingSpend | None:
    """Housekeeping spend for ``bot_id`` over the same pod-local window the
    weekly receipt uses (``live_spend.total_over_local_days``), or ``None``
    when the turns could not be read ("I could not look", never $0.00)."""
    import live_spend  # type: ignore[import]

    result = live_spend.total_over_local_days(
        bot_id, days=days, now=now, turn_filter=is_housekeeping_turn, log=log,
    )
    if result is None:
        return None
    usd, measurable = result
    return HousekeepingSpend(usd=round(usd, 4), measurable=measurable)


def receipt_bot_lines(
    bot_id: str,
    total_usd: float | None,
    housekeeping: HousekeepingSpend | None,
) -> list[str]:
    """The receipt's lines for one bot: conversation first, housekeeping second.

    ``total_usd`` is the bot's whole spend over the window (``None`` = could
    not read). The first line is that total minus housekeeping; the second,
    indented, is housekeeping — shown only when there was some, so a bot that
    never compacted keeps its one line.
    """
    if total_usd is None:
        return [f"  {bot_id}: n/a (could not read turns)"]
    if housekeeping is None:
        return [
            f"  {bot_id}: ${total_usd:.2f}",
            f"    {RECEIPT_LABEL}: unreadable — included above",
        ]
    hk = min(max(housekeeping.usd, 0.0), total_usd)
    lines = [f"  {bot_id}: ${total_usd - hk:.2f}"]
    if hk > 0:
        floor = "" if housekeeping.measurable else " (floor — some runs unpriced)"
        lines.append(f"    {RECEIPT_LABEL}: ${hk:.2f}{floor}")
    return lines

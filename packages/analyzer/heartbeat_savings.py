"""heartbeat_savings — what the model-free due-check actually saved.

Decision: ``internal/decision-evolve-overhead-2026-09-07.md`` D-OH3 (a
heartbeat or cron with nothing due never calls a model) and D-OH5 (the
overhead ledger is the scoreboard).  §2 of that decision measured the row
this module reports on: 125 heartbeat and cron turns across five bots over
two days cost $3.02 — every one already on the cheapest model, each $0.02–
0.04 because it dragged tens of thousands of cached tokens into a call whose
usual answer was ``NO_REPLY``.

The plugin's ``HeartbeatDueCheck`` now answers "is anything due" from files
and the clock, and OC skips the model turn entirely when the answer is no.
That saving is an ABSENCE — fewer rows in the turns file — and an absence is
exactly what a silently dead heartbeat looks like too.  So the plugin writes
down every decision it makes, skips and wakes alike, and this module turns
those records into a number the weekly receipt can print.

Inputs
------
``{shared_dir}/{bot}/turns/heartbeat-decisions-<YYYY-MM-DD>.jsonl``
    One record per heartbeat/cron trigger the due-check ruled on
    (``HeartbeatSkipLedger.ts`` owns the schema).  ``outcome`` is
    ``skipped_nothing_due`` or ``woke_due``.

The bots' turn files, via ``usage_analytics.load_turns``
    Used only to price a skip: what a heartbeat turn on THIS pod costs when
    it does run.

How the dollar figure is estimated, and what it is not
-----------------------------------------------------
``saved ≈ skipped × median(cost of a heartbeat/cron turn that ran)``.

The median, not the mean: one runaway heartbeat session (the 40-turn retry
storms of 2026-05-20) would otherwise value every skip at a price no ordinary
tick ever pays.  And an estimate needs a comparable: if the window holds no
PRICED heartbeat turn, the saving is reported as ``None`` with the reason,
never as ``$0.00``.  A zero there would read as "the feature saved nothing",
which is the silent-zero failure ``docs/principle-tri-state-status.md``
forbids — the honest statement is "we skipped N calls and have nothing on
this pod to price them against".
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: ``source`` / ``channel`` values that mean "a clock fired this turn".
#: Mirrors the plugin's ``inferTriggerKind`` heartbeat/cron_app arms and the
#: turn-collector's own labels, which differ ("cron" vs "cron_app") because
#: they were written years apart.
SCHEDULED_KINDS: frozenset[str] = frozenset({"heartbeat", "cron", "cron_app", "cron-event"})

DECISIONS_PREFIX = "heartbeat-decisions-"

SKIPPED = "skipped_nothing_due"
WOKE = "woke_due"


@dataclass(frozen=True)
class HeartbeatSavings:
    """What the due-check did over one window, and what it is worth."""

    days: int
    bots: tuple[str, ...]
    #: Bots that produced at least one decision record — i.e. that have a
    #: ``HEARTBEAT.json``. The rest are running today's behaviour unchanged.
    bots_with_conditions: tuple[str, ...]
    skipped: int
    woke: int
    #: Heartbeat/cron turns that reached a model in the window (from the turn
    #: files, so it counts wakes AND every scheduled turn on a bot with no
    #: conditions file). ``None`` when the turn files could not be read.
    ran: int | None
    #: Number of those that carried a usable cost.
    priced_runs: int
    median_run_usd: float | None
    saved_usd: float | None
    #: Why ``saved_usd`` is None, when it is. Empty string otherwise.
    unpriced_reason: str

    @property
    def total_decisions(self) -> int:
        return self.skipped + self.woke


# ── reading the decisions ledger ────────────────────────────────────────────


def decisions_dir(shared_dir: Path | str, bot_id: str) -> Path:
    return Path(shared_dir) / bot_id / "turns"


def _window_days(days: int, end_date: datetime | None) -> list[str]:
    """The UTC date strings the ledger filenames use, oldest first.

    UTC because the writer names the file from ``toISOString()`` — the same
    reason ``usage_analytics.load_turns`` documents at length.  A receipt
    that bucketed these pod-locally would miss the file being appended to.
    """
    end = end_date or datetime.now(timezone.utc)
    if end.tzinfo is not None:
        end = end.astimezone(timezone.utc)
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def read_decisions(
    shared_dir: Path | str,
    bot_id: str,
    *,
    days: int = 7,
    end_date: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every decision record for ``bot_id`` in the window.

    Unreadable or half-written lines are skipped rather than raised: this
    feeds a receipt, and a receipt that dies on one truncated append tells
    the operator nothing at all.
    """
    out: list[dict[str, Any]] = []
    base = decisions_dir(shared_dir, bot_id)
    for day in _window_days(days, end_date):
        path = base / f"{DECISIONS_PREFIX}{day}.jsonl"
        try:
            text = path.read_text()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rec.setdefault("instance", bot_id)
                out.append(rec)
    return out


# ── pricing a skip ──────────────────────────────────────────────────────────


def is_scheduled_turn(turn: dict[str, Any]) -> bool:
    """True when a turn record was fired by a clock, not by a person."""
    source = str(turn.get("source") or "").strip().lower()
    channel = str(turn.get("channel") or "").strip().lower()
    return source in SCHEDULED_KINDS or channel in SCHEDULED_KINDS


def _turn_cost(turn: dict[str, Any]) -> float | None:
    """The recorded cost of one turn, or None when it carries no usable one.

    ``cost`` is ``None`` — never 0 — on a turn nothing could price, so a
    missing key and a null are the same answer here: unknown.  A real 0.0 is
    dropped too: a zero-cost model turn prices no skip.
    """
    raw = turn.get("cost")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def measure(
    shared_dir: Path | str,
    bot_ids: Sequence[str],
    *,
    days: int = 7,
    end_date: datetime | None = None,
    load_turns: Callable[..., Iterable[dict[str, Any]]] | None = None,
) -> HeartbeatSavings:
    """Count the window's decisions and value the skips.

    ``load_turns`` is injectable so this is testable without a pod; the
    default resolves ``usage_analytics.load_turns`` lazily and degrades to
    "turns unreadable" (``ran=None``) rather than raising, because a receipt
    must still be able to report the skip COUNT when pricing is unavailable.
    """
    bots = tuple(bot_ids)
    skipped = 0
    woke = 0
    with_conditions: list[str] = []
    for bot in bots:
        records = read_decisions(shared_dir, bot, days=days, end_date=end_date)
        if records:
            with_conditions.append(bot)
        for rec in records:
            outcome = str(rec.get("outcome") or "")
            if outcome == SKIPPED:
                skipped += 1
            elif outcome == WOKE:
                woke += 1

    loader = load_turns
    if loader is None:
        try:
            from usage_analytics import load_turns as _load  # type: ignore[import]

            loader = _load
        except Exception:  # noqa: BLE001 — analyzer path unavailable
            loader = None

    ran: int | None = None
    costs: list[float] = []
    if loader is not None:
        try:
            turns = [
                t
                for bot in bots
                for t in loader(bot, days=days, end_date=end_date)
                if is_scheduled_turn(t)
            ]
            ran = len(turns)
            costs = [c for c in (_turn_cost(t) for t in turns) if c is not None]
        except Exception:  # noqa: BLE001 — pricing is a nicety; counts are not
            ran = None
            costs = []

    median = round(statistics.median(costs), 6) if costs else None
    saved: float | None = None
    reason = ""
    if median is None:
        reason = (
            "no priced heartbeat or cron turn in the window to price a skip against"
            if ran
            else "no heartbeat or cron turn in the window to price a skip against"
        )
    elif skipped == 0:
        saved = 0.0
    else:
        saved = round(median * skipped, 4)

    return HeartbeatSavings(
        days=days,
        bots=bots,
        bots_with_conditions=tuple(with_conditions),
        skipped=skipped,
        woke=woke,
        ran=ran,
        priced_runs=len(costs),
        median_run_usd=median,
        saved_usd=saved,
        unpriced_reason=reason,
    )


# ── the receipt line ────────────────────────────────────────────────────────


def receipt_lines(
    shared_dir: Path | str,
    bot_ids: Sequence[str],
    *,
    days: int = 7,
    end_date: datetime | None = None,
    load_turns: Callable[..., Iterable[dict[str, Any]]] | None = None,
) -> list[str]:
    """The lines the weekly receipt appends about idle-heartbeat spend.

    Always exactly one line, and never silence: on a pod where no bot has a
    ``HEARTBEAT.json`` yet, the line says so and names the saving as
    available rather than achieved.  A receipt that printed nothing would
    read as "nothing to report" on a pod that is still paying the full idle
    burn D-OH3 exists to remove.
    """
    try:
        m = measure(
            shared_dir, bot_ids, days=days, end_date=end_date, load_turns=load_turns,
        )
    except Exception as exc:  # noqa: BLE001 — a receipt never dies on one line
        return [f"heartbeats: savings unreadable ({exc})"]

    if not m.bots_with_conditions:
        ran = "n/a" if m.ran is None else str(m.ran)
        return [
            f"heartbeats: {ran} run / 0 skipped — no bot has a HEARTBEAT.json yet, "
            f"so every scheduled turn still calls a model (D-OH3)"
        ]

    ran = "n/a" if m.ran is None else str(m.ran)
    if m.saved_usd is None:
        tail = f"$ saved unknown — {m.unpriced_reason}"
    else:
        tail = f"${m.saved_usd:.2f} saved"
        if m.median_run_usd is not None:
            tail += f" (median ${m.median_run_usd:.4f}/run × {m.skipped})"
    return [
        f"heartbeats: {ran} run / {m.skipped} skipped over {m.days}d — {tail}"
    ]

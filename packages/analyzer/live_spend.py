"""live_spend.py — per-pod-local-day spend, read from live turn JSONL.

The one place that answers "what has this bot spent on pod-local day D?"
from the turn records themselves, for every surface that needs it.

Why this module exists
----------------------
Two surfaces used to answer that question from
``{shared_dir}/metrics/<date>/<bot>.json`` — the daily rollup written by
``measure.py``. That file **cannot** answer it. ``measure.py`` runs from
launchd at 01:00 pod-local with ``--date`` defaulting to ``date.today()``,
so the file named for day D is written one hour into D and never
regenerated. Measured on the mini for 2026-09-03, the pod's rollup files
held **$0.95 against $55.52** of real pod-local-day spend — **1.7%**. The
heaviest bot's file said ``$0.0078`` where the day's live total was
``$51.45``.

It fails on its own terms too, not just against the pod-local day: summing
the very annotations it itself reads, the UTC day each file is NAMED for
came to $25.28, of which the file captured $0.95 — 3.8%.

Read as "today's spend", that is not a small error; it is a floor of
approximately zero — which is why the Cost Measures cap-warning chip
(80% of the daily cap) and the trust-page spend reason could not fire,
and why every weekly summary was a fraction of real spend.

``spend_alert`` moved off that file in the 2026-05-20 fix and grew the
live-JSONL reader; ``spend_caps`` could not reuse it because
``spend_alert`` imports ``spend_caps``, and an import back would be a
cycle. So the reader lives here, below both, and ``spend_alert``
re-exports it under its established names.

The UTC / pod-local boundary
----------------------------
Turn JSONL is stored in **UTC-named** files (``TurnObserver`` writes
``turns-${new Date().toISOString().slice(0, 10)}.jsonl``) and every
record's ``ts`` is a ``Z`` timestamp. The day an operator means — the day
caps roll on — is the **pod-local** day (see ``pod_time``). A pod-local
day straddles two UTC files, so both halves of the boundary have to be
crossed deliberately:

  * **Load a day wider than you need.** ``N`` pod-local days need ``N+1``
    UTC files: the offset is under 24h, so the window can spill by at
    most one file, at either sign of offset.
  * **Bucket on the turn's INSTANT**, never on a ``ts[:10]`` prefix. The
    prefix compares a UTC string to a pod-local date, which is the exact
    defect PR #4002 fixed one layer in: west of UTC it selected zero
    turns every evening from 17:00 local to local midnight.

Getting only one half right still yields zero — you cannot bucket a turn
that was never read off disk.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from typing import Callable

from turn_cost import sum_turn_costs

# Module-level sentinel — returned by :func:`load_live_turns` when discovery
# fails entirely (usage_analytics import error or load_turns raised).
# Distinguishes "this bot is genuinely idle" from "I have no way to tell";
# callers must propagate it rather than collapsing both to $0.00, which is
# the silent-zero shape of the 2026-05-20 incident.
LIVE_LOAD_FAILED: object = object()


def _noop_log(_msg: str) -> None:
    """Default log sink. Callers that own a daemon log pass their own."""


def _network_path() -> str | None:
    """Path to network.json for usage_analytics's bot discovery, or None.

    ``None`` is the honest answer when ``evolve_config`` cannot resolve it —
    and harmless here, because every caller passes an explicit ``bot_id``,
    so ``load_turns`` never needs the file to discover the bot list. A
    hardcoded ``/Users/...`` fallback would be macOS-only besides.
    """
    try:
        from evolve_config import resolve_network_path  # type: ignore[import]
        return str(resolve_network_path())
    except Exception:
        return None


def pod_tz_or_local() -> tzinfo:
    """The pod's timezone, falling back to the system local zone.

    Single seam — tests patch this. The fallback is the same zone
    ``pod_today()`` would have used; the two must never disagree or a turn
    can fall outside every bucket.
    """
    try:
        from pod_time import pod_tz  # type: ignore[import]
        return pod_tz()
    except Exception:
        local = datetime.now().astimezone().tzinfo
        return local if local is not None else timezone.utc


def local_day_iso(ts: object, tz: tzinfo) -> str | None:
    """A turn's pod-local date from its UTC ``ts``; None if unparseable.

    Delegates to ``pod_time`` — the one home for placing a UTC instant on
    the pod's calendar — with an inline fallback for the same reason
    :func:`pod_tz_or_local` has one.
    """
    try:
        from pod_time import pod_local_day_iso  # type: ignore[import]
        return pod_local_day_iso(ts, tz)
    except ImportError:
        if not isinstance(ts, str) or len(ts) < 10:
            return None
        raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date().isoformat()


def load_live_turns(
    bot_id: str,
    *,
    days: int = 1,
    end: datetime | None = None,
    log: Callable[[str], None] | None = None,
):
    """Load live turn records via ``usage_analytics.load_turns``.

    ``days`` is a count of **UTC** files, matching ``load_turns``' own
    window — callers bucketing by a pod-local day widen it themselves (see
    :func:`spend_by_local_day`).

    Returns:
      * ``list[dict]`` — successfully read (may be empty if no rows yet)
      * :data:`LIVE_LOAD_FAILED` — discovery / read raised

    The sentinel exists because returning ``[]`` in both cases is the same
    bug the metrics-file path had: "no data" indistinguishable from "I
    can't see your data."
    """
    emit = log or _noop_log
    try:
        from usage_analytics import load_turns  # type: ignore[import]
    except Exception as exc:
        emit(f"[live_spend] usage_analytics import failed: {exc}")
        return LIVE_LOAD_FAILED
    try:
        return load_turns(
            bot_id,
            days=days,
            end_date=end,
            network_path=_network_path(),
        )
    except Exception as exc:
        emit(f"[live_spend] load_turns({bot_id}) raised: {exc}")
        return LIVE_LOAD_FAILED


@dataclass(frozen=True)
class DaySpend:
    """One bot-day's spend, with "couldn't price it" carried beside the number.

    ``usd`` is the total over the turns that COULD be priced. When
    ``unpriced_turns`` is non-zero the figure is a **floor**, not the day's
    spend — the cap path must not read it as "under cap" (audit B6).
    """

    usd: float = 0.0
    priced_turns: int = 0
    unpriced_turns: int = 0
    unpriced_providers: tuple[str, ...] = ()

    @property
    def measurable(self) -> bool:
        return self.unpriced_turns == 0


def _local_day_end_utc(day: date, tz: tzinfo) -> datetime:
    """The UTC instant at which pod-local ``day`` ends (next local midnight)."""
    try:
        from pod_time import pod_local_day_start_utc  # type: ignore[import]
        return pod_local_day_start_utc(day + timedelta(days=1), tz)
    except ImportError:
        return datetime.combine(
            day + timedelta(days=1), dt_time(0, 0), tzinfo=tz,
        ).astimezone(timezone.utc)


def spend_by_local_day(
    bot_id: str,
    *,
    days: int = 1,
    end_day: date | None = None,
    now: datetime | None = None,
    exempt_subkinds: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, DaySpend] | None:
    """Spend for ``bot_id`` bucketed by pod-local day, or ``None`` on failure.

    Covers the ``days`` pod-local days ending with ``end_day`` — which
    defaults to the local day containing ``now`` (``days=1`` is that day
    alone; ``days=7`` is it plus the six before it). Keys are ``YYYY-MM-DD``
    pod-local dates; days with no turns are simply absent, so callers read a
    missing key as ``DaySpend()``.

    ``end_day`` moves the LOAD window as well as the selection, so asking for
    a past day reads the files that day actually lives in. Selecting on one
    day while loading around another is how you get a confident $0.00 from a
    bucket nothing was ever read into.

    ``None`` means live-JSONL discovery failed — the "did not run" contract.
    It is never a stand-in for a genuinely idle bot; that is ``{}``.

    Loads ``days + 1`` UTC files. The offset between the storage day and the
    policy day is under 24h, so a pod-local window spills into at most one
    extra UTC file, and anchoring the load at ``now`` covers that spill at
    either sign of offset.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        # A naive instant is UTC by the writers' contract; leaving it naive
        # would make .astimezone() reinterpret it as system-local and shift
        # the whole window by the offset.
        now = now.replace(tzinfo=timezone.utc)
    tz = pod_tz_or_local()
    last = end_day if end_day is not None else now.astimezone(tz).date()
    # Anchor the load on the window's own end, never past ``now`` (there is
    # nothing to read in the future, and a later anchor would just shift the
    # UTC files off the days we want).
    anchor = min(now, _local_day_end_utc(last, tz))
    turns = load_live_turns(bot_id, days=days + 1, end=anchor, log=log)
    if turns is LIVE_LOAD_FAILED or not isinstance(turns, list):
        return None

    wanted = {(last - timedelta(days=i)).isoformat() for i in range(days)}
    # Bucket first, price second: one sum_turn_costs pass per day keeps the
    # unpriced count attributable to the day it happened on, which a single
    # pooled total would flatten away.
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in turns:
        if exempt_subkinds and t.get("forge_subkind") in exempt_subkinds:
            continue
        d = local_day_iso(t.get("ts"), tz)
        if d in wanted:
            by_day[d].append(t)  # type: ignore[index]

    out: dict[str, DaySpend] = {}
    for d, rows in by_day.items():
        total = sum_turn_costs(rows)
        out[d] = DaySpend(
            usd=round(total.usd, 6),
            priced_turns=total.priced_turns,
            unpriced_turns=total.unpriced_turns,
            unpriced_providers=total.unpriced_providers,
        )
    return out


def load_day_spend_detail(
    bot_id: str,
    day: date,
    *,
    now: datetime | None = None,
    exempt_subkinds: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> DaySpend | None:
    """One pod-local day's spend as a :class:`DaySpend`, ``None`` on failure.

    ``day`` names the pod-local day to read; the load window follows it, so
    a past day reads the files it actually lives in.

    This is the shape the cap path uses: it needs to tell "the bot spent
    $0.00 today" apart from "232 turns ran and I could not price any of
    them", which a bare float cannot express.
    """
    by_day = spend_by_local_day(
        bot_id, days=1, end_day=day, now=now,
        exempt_subkinds=exempt_subkinds, log=log,
    )
    if by_day is None:
        return None
    return by_day.get(day.isoformat(), DaySpend())


def load_day_spend(
    bot_id: str,
    day: date,
    *,
    now: datetime | None = None,
    exempt_subkinds: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> float | None:
    """One pod-local day's spend as a float, or ``None`` on discovery failure.

    NOTE (audit B6): the float covers the turns that could be **priced**, so
    it cannot distinguish "spent nothing" from "could not price anything".
    Surfaces that must tell those apart use :func:`load_day_spend_detail`.
    """
    detail = load_day_spend_detail(
        bot_id, day, now=now, exempt_subkinds=exempt_subkinds, log=log,
    )
    if detail is None:
        return None
    return round(detail.usd, 6)


def total_over_local_days(
    bot_id: str,
    *,
    days: int,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[float, bool] | None:
    """``(usd, measurable)`` summed across ``days`` pod-local days, or ``None``.

    ``measurable`` is False when any day in the window contained a turn that
    could not be priced — the total is then a floor, and a caller comparing
    it to a threshold should say so rather than report it as the spend.
    """
    by_day = spend_by_local_day(bot_id, days=days, now=now, log=log)
    if by_day is None:
        return None
    usd = sum(d.usd for d in by_day.values())
    measurable = all(d.measurable for d in by_day.values())
    return round(usd, 6), measurable

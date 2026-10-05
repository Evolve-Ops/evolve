"""oc_usage_cost — OpenClaw's own per-call ``usage.cost``, read from the transcript DB.

D-CS2 (``internal/assessment-cost-spikes-product-2026-09-13.md`` §3). Three
times Evolve's price arithmetic disagreed with what the provider billed: the
family-substring table (fixed, #4038), the missing 1-hour cache-write rate
(team-bot-a, 2026-09-12: Evolve $6.50 against OC's $9.36 —
``internal/finding-cache-retention-doubled-the-bill-2026-09-12.md`` §2.5), and
a stated config that hid the second. OpenClaw's ``calculateUsageCost`` already
prices every model call correctly — including the 1-hour tier, which it bills
at ``2 x input`` on ``cacheWrite1h`` and at the ordinary write rate on the rest
— and writes the result into each agent's transcript DB. This module reads it,
so Evolve stops re-implementing the provider.

Where the number lives (measured on the mini, 2026-09-26)::

    {bot_home}/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite
        transcript_events(session_id, seq, event_json, created_at ms)
        event_json.message.role == "assistant"
        event_json.message.usage = {input, output, cacheRead, cacheWrite,
                                    cacheWrite1h, cost: {input, output,
                                    cacheRead, cacheWrite, total}}

``cacheWrite`` INCLUDES ``cacheWrite1h``; the 5-minute share is the difference.
There is one DB per named agent (``main``, and one per heartbeat/cron agent) —
reading ``main`` alone misses every heartbeat run, so all are enumerated.

Two consumers:

* :func:`enrich_turns` — stamps OC's cost onto Evolve's turn records (one
  record per agent run) so :func:`turn_cost.turn_cost_detail` resolves them as
  ``"oc"``. A turn is stamped only when the calls matched to it reproduce its
  token counts, so a turn straddling the read window or a session we joined
  late keeps its estimate rather than taking a partial sum.
* :func:`load_oc_calls` — the raw per-call rows, for the daily drift check
  (``cost_truth``) and the cost rollup / 30-day backfill.

Read-only and best-effort by contract: the evolve user holds an ACL read on
``.openclaw`` and opens the DB with ``mode=ro`` (never ``immutable=1``, which
is WAL-blind and would miss the day's newest calls). Any failure — no DB, no
permission, a schema OC changed underneath us — returns ``None`` and the caller
keeps Evolve's estimate, labelled ``estimate``. Nothing here may raise into a
cap path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

#: Rate dimensions, in the order every surface prints them. ``cache_write_5m``
#: and ``cache_write_1h`` are split because they are billed at different rates
#: (1.25x vs 2.00x input on Anthropic) and folding them is how the 2x hid.
DIMENSIONS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

#: A call's ts may trail its turn record's ts by the time the plugin takes to
#: write the record; the reverse (call AFTER its record) is clock noise only.
_MATCH_TOLERANCE = timedelta(seconds=5)

#: Token-count agreement required before a turn takes OC's sum. The measured
#: case agrees exactly; the slack only absorbs an off-by-one in a counter.
_TOKEN_TOLERANCE_ABS = 2
_TOKEN_TOLERANCE_REL = 0.005

#: Env switch for a pod (or a test run) that must not touch transcript DBs.
DISABLE_ENV = "EVOLVE_OC_COST_DISABLE"


@dataclass(frozen=True)
class OcCall:
    """One assistant model call, as OpenClaw recorded and priced it."""

    ts: datetime
    session_id: str
    agent: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int      # includes cache_write_1h_tokens
    cache_write_1h_tokens: int
    #: ``usage.cost.total``, or ``None`` when OC wrote no cost block.
    cost: float | None
    #: OC's own per-part split (``input``/``output``/``cacheRead``/``cacheWrite``).
    cost_parts: tuple[tuple[str, float], ...] = ()

    @property
    def tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    @property
    def priced_by_oc(self) -> bool:
        """True when OC's figure is usable as truth.

        ``0.0`` with real tokens is OC saying "I have no price for this
        provider" (observed 2026-08-31 on xai/grok-4) — not a free call.
        """
        if self.cost is None:
            return False
        return self.cost > 0 or self.tokens == 0

    def as_turn(self) -> dict:
        """The call in the turn-record shape ``turn_cost`` prices."""
        return {
            "model": self.model,
            "provider": self.provider,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
        }


def _disabled() -> bool:
    return (os.environ.get(DISABLE_ENV) or "").strip() not in ("", "0")


def agent_db_paths(bot_id: str, *, home: Path | None = None) -> list[Path]:
    """Every agent transcript DB for ``bot_id``, ``main`` first."""
    if home is None:
        try:
            from evolve_config import bot_home  # type: ignore[import]
            home = bot_home(bot_id)
        except Exception:
            return []
    agents = Path(home) / ".openclaw" / "agents"
    try:
        found = sorted(agents.glob("*/agent/openclaw-agent.sqlite"))
    except OSError:
        return []
    return sorted(found, key=lambda p: (p.parent.parent.name != "main", str(p)))


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int(v: object) -> int:
    try:
        return int(v or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _call_from_event(session_id: str, agent: str, raw: str) -> OcCall | None:
    try:
        ev = json.loads(raw)
    except (TypeError, ValueError):
        return None
    msg = ev.get("message") if isinstance(ev, dict) else None
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    ts = _parse_ts(ev.get("timestamp"))
    if ts is None:
        return None
    cost_block = usage.get("cost")
    cost: float | None = None
    parts: list[tuple[str, float]] = []
    if isinstance(cost_block, dict):
        try:
            cost = float(cost_block.get("total"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            cost = None
        for k in ("input", "output", "cacheRead", "cacheWrite"):
            try:
                parts.append((k, float(cost_block.get(k) or 0.0)))
            except (TypeError, ValueError):
                continue
    cw = _int(usage.get("cacheWrite"))
    cw1h = min(_int(usage.get("cacheWrite1h")), cw) if cw else _int(usage.get("cacheWrite1h"))
    return OcCall(
        ts=ts,
        session_id=session_id,
        agent=agent,
        model=str(msg.get("model") or ""),
        provider=str(msg.get("provider") or ""),
        input_tokens=_int(usage.get("input")),
        output_tokens=_int(usage.get("output")),
        cache_read_tokens=_int(usage.get("cacheRead")),
        cache_write_tokens=cw,
        cache_write_1h_tokens=cw1h,
        cost=cost,
        cost_parts=tuple(parts),
    )


# Memo: (db path, window) -> (db mtime, wal mtime, calls). The spend daemon
# ticks every few minutes; a DB that has not changed is not re-read.
_MEMO: dict[tuple[str, int, int], tuple[float, float, list[OcCall]]] = {}
_MEMO_MAX = 256


def _stat_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _read_db(db: Path, start: datetime, end: datetime) -> list[OcCall] | None:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    key = (str(db), start_ms, end_ms)
    m_db, m_wal = _stat_mtime(db), _stat_mtime(Path(f"{db}-wal"))
    hit = _MEMO.get(key)
    if hit is not None and hit[0] == m_db and hit[1] == m_wal:
        return hit[2]
    agent = db.parent.parent.name
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        # ``created_at`` is the coarse pre-filter (it tracks the event's
        # timestamp, and a re-import only ever moves it LATER), widened by a
        # day; the event's own ``timestamp`` is the authoritative bound below.
        rows = conn.execute(
            "SELECT session_id, event_json FROM transcript_events "
            "WHERE created_at >= ? AND event_json LIKE '%\"usage\"%'",
            (start_ms - 86_400_000,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    calls: list[OcCall] = []
    for session_id, raw in rows:
        call = _call_from_event(str(session_id), agent, raw)
        if call is not None and start <= call.ts < end:
            calls.append(call)
    if len(_MEMO) >= _MEMO_MAX:
        _MEMO.clear()
    _MEMO[key] = (m_db, m_wal, calls)
    return calls


def reset_cache() -> None:
    """Drop memoized reads. Tests call this between fixtures."""
    _MEMO.clear()


def load_oc_calls(
    bot_id: str,
    start: datetime,
    end: datetime,
    *,
    home: Path | None = None,
) -> list[OcCall] | None:
    """Every priced-or-not assistant call for ``bot_id`` in ``[start, end)``.

    ``None`` means "could not read OC's numbers at all" (disabled, no DB, every
    DB unreadable) — the caller keeps its estimate. ``[]`` means the DBs were
    read and the bot made no calls in the window.
    """
    if _disabled():
        return None
    dbs = agent_db_paths(bot_id, home=home)
    if not dbs:
        return None
    out: list[OcCall] = []
    read_any = False
    for db in dbs:
        calls = _read_db(db, start, end)
        if calls is None:
            continue
        read_any = True
        out.extend(calls)
    if not read_any:
        return None
    out.sort(key=lambda c: c.ts)
    return out


# ── Stamping OC's cost onto Evolve's turn records ────────────────────────────


def _close(a: int, b: int) -> bool:
    return abs(a - b) <= max(_TOKEN_TOLERANCE_ABS, _TOKEN_TOLERANCE_REL * max(a, b))


def _tokens_agree(turn: dict, calls: list[OcCall]) -> bool:
    pairs = (
        ("input_tokens", sum(c.input_tokens for c in calls)),
        ("output_tokens", sum(c.output_tokens for c in calls)),
        ("cache_read_tokens", sum(c.cache_read_tokens for c in calls)),
        ("cache_write_tokens", sum(c.cache_write_tokens for c in calls)),
    )
    return all(_close(_int(turn.get(field)), total) for field, total in pairs)


def attach_oc_costs(turns: list[dict], calls: Iterable[OcCall]) -> int:
    """Stamp OC's cost onto the turns its calls belong to. Returns the count.

    Calls join turns on ``session_id``; within a session each call belongs to
    the first turn whose ``ts`` is at or after it (the plugin writes a turn
    record when its run ends). A turn is stamped only when every matched call
    carries an OC price AND the matched calls reproduce the turn's token
    counts — anything else (a partial window, a session read mid-run) leaves
    the estimate in place, labelled as one.

    A stamped turn keeps what it said before under ``cost_estimate`` /
    ``cost_estimate_source`` so the drift check can compare the two per turn.
    """
    by_session: dict[str, list[OcCall]] = defaultdict(list)
    for c in calls:
        if c.session_id:
            by_session[c.session_id].append(c)
    if not by_session:
        return 0
    turns_by_session: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for t in turns:
        sid = t.get("session_id")
        ts = _parse_ts(t.get("ts"))
        if sid and ts is not None and sid in by_session:
            turns_by_session[sid].append((ts, t))

    stamped = 0
    for sid, rows in turns_by_session.items():
        rows.sort(key=lambda r: r[0])
        pending = sorted(by_session[sid], key=lambda c: c.ts)
        i = 0
        for ts, turn in rows:
            mine: list[OcCall] = []
            while i < len(pending) and pending[i].ts <= ts + _MATCH_TOLERANCE:
                mine.append(pending[i])
                i += 1
            if not mine or not all(c.priced_by_oc for c in mine):
                continue
            if not _tokens_agree(turn, mine):
                continue
            if str(turn.get("cost_source") or "") != "oc":
                turn["cost_estimate"] = turn.get("cost")
                turn["cost_estimate_source"] = turn.get("cost_source")
            turn["cost"] = round(sum(c.cost or 0.0 for c in mine), 8)
            turn["cost_source"] = "oc"
            turn["cache_write_1h_tokens"] = sum(c.cache_write_1h_tokens for c in mine)
            turn["oc_calls"] = len(mine)
            stamped += 1
    return stamped


def enrich_turns(
    turns: list[dict],
    bot_ids: Iterable[str],
    start: datetime,
    end: datetime,
) -> int:
    """Best-effort :func:`attach_oc_costs` for every bot in ``bot_ids``.

    Called from ``usage_analytics.load_turns`` — the one loader every cost
    reader shares — so the cap, the burst window, the Usage page and the
    receipt all read OC's figure without each learning to. Never raises.
    """
    total = 0
    by_bot: dict[str, list[dict]] = defaultdict(list)
    for t in turns:
        by_bot[str(t.get("instance") or "")].append(t)
    for bid in bot_ids:
        rows = by_bot.get(bid)
        if not rows:
            continue
        try:
            calls = load_oc_calls(bid, start, end)
            if calls:
                total += attach_oc_costs(rows, calls)
        except Exception:  # noqa: BLE001 — an unreadable DB keeps the estimate
            continue
    return total


# ── Per-dimension figures (drift check + reconcile) ──────────────────────────


def oc_dimension_costs(call: OcCall) -> dict[str, float]:
    """OC's own figure split by :data:`DIMENSIONS`.

    OC reports one ``cacheWrite`` dollar figure covering both tiers; it is
    apportioned by the tiers' 1.25 : 2.00 price ratio on their token counts,
    which is exactly how ``calculateUsageCost`` built it.
    """
    parts = dict(call.cost_parts)
    out = {d: 0.0 for d in DIMENSIONS}
    out["input"] = parts.get("input", 0.0)
    out["output"] = parts.get("output", 0.0)
    out["cache_read"] = parts.get("cacheRead", 0.0)
    cw = parts.get("cacheWrite", 0.0)
    w1h = call.cache_write_1h_tokens * 2.00
    w5m = (call.cache_write_tokens - call.cache_write_1h_tokens) * 1.25
    denom = w1h + w5m
    if denom > 0:
        out["cache_write_1h"] = cw * w1h / denom
        out["cache_write_5m"] = cw * w5m / denom
    return out

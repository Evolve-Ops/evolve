#!/usr/bin/env python3
"""
cost_rollup.py — Daily per-bot cost rollup writer.

For each bot, aggregates the day's ``cost_event`` records into a single
JSON summary that Budget Hawk's ``spend_reader`` consumes. cost_event
records are written by ``cost_event_converter.py`` (and historically by
the now-silent plugin ``llm_output`` writer); this script just rolls
them up.

Inputs (per bot, per date):
  - ``{shared_dir}/annotations/{bot}/cost_events-{date}.jsonl`` —
    every line is a cost_event record (current source of truth).
  - ``{shared_dir}/annotations/{bot}/{date}.jsonl`` filtered to
    ``type == cost_event`` — legacy plugin emissions, kept readable
    for backfills covering ≤ 2026-04-21.

Output (per bot, per date):
  ``{shared_dir}/metrics/{bot}/cost-{date}.json``

Schema:
    {
      "schema_version": 1,
      "bot_id": "admin_bot",
      "date": "2026-05-05",
      "generated_at": "2026-05-05T22:00:00Z",
      "total_usd": 1.234,
      "input_tokens": 12345,
      "output_tokens": 6789,
      "cache_read_tokens": 98765,
      "cache_write_tokens": 4321,
      "event_count": 42,
      "unpriced_events": 0,        # events no pricing source could cost;
                                   # non-zero ⇒ total_usd is a floor
      "source": "oc",              # v3: "oc" = OpenClaw's own per-call
                                   # usage.cost (transcript DB); "estimate"
                                   # = Evolve's price table; "mixed" = both
      "estimate_usd": 1.02,        # v3: what the price table said, kept
                                   # beside the truth for the drift check
      "by_model": {
        "claude-sonnet-4-6": {
          "cost_usd": 1.0,
          "input_tokens": ...,
          "output_tokens": ...,
          "cache_read_tokens": ...,
          "cache_write_tokens": ...,
          "event_count": ...
        }
      }
    }

Runs as the ``evolve`` user — the metrics dir is owned by evolve and
not writable by bot users. Idempotent: each run reads the source JSONL
and overwrites the rollup atomically (tempfile + rename), so it is
safe to call as often as cheap reads allow. Refreshing today's rollup
mid-day lets Budget Hawk catch a spike before midnight rolls over.

Usage:
    python3 cost_rollup.py                       # all members, last 14 days
    python3 cost_rollup.py --backfill 30         # all members, last 30 days
    python3 cost_rollup.py --bot-id admin_bot
    python3 cost_rollup.py --bot-id admin_bot --date 2026-05-05
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from evolve_util import atomic_write_json

# The pod's ONE per-turn cost rule (audit B6): recorded cost_usd wins when
# non-zero, a zero with real tokens is re-estimated (catalog → offline
# tables), and an event neither can price is COUNTED as unpriced rather
# than summed as $0. OC records cost 0 for providers it has no pricing
# for (observed 2026-08-31: xai/grok-4 turns with ~70k tokens and
# usage.cost all zeros), so a raw cost_usd sum silently under-counts.
from turn_cost import load_pricing_catalog, turn_cost as _turn_cost

# v2 (2026-08-31): additive — ``unpriced_events`` beside total_usd (and per
# model bucket), zero-cost events re-priced via turn_cost. Readers use
# .get(); no consumer gates on the integer.
#
# v3 (D-CS2): additive — ``source`` / ``estimate_usd`` / ``oc_calls``. When
# OpenClaw's transcript DB is readable, ``total_usd`` and ``by_model`` are
# built from OC's own per-call ``usage.cost`` rather than the price table.
COST_ROLLUP_SCHEMA_VERSION = 3
DEFAULT_BACKFILL_DAYS = 14

#: How far back the one-time OC re-roll reaches (D-CS2 §4): far enough that
#: every cap baseline and 28-day trend starts from OC's figure.
OC_BACKFILL_DAYS = 30
_OC_BACKFILL_MARKER = ".oc-cost-backfill-v1"


# ─────────────────────────────────────────────────────────────────────────────
# Read side
# ─────────────────────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def iter_cost_events(
    shared_dir: Path, bot_id: str, target_date: date
) -> Iterator[dict]:
    """Yield cost_event records for one (bot, date).

    Reads three sources, all best-effort:
      1. Converter file (every line is a cost_event) — current source
         of truth for cost_event_converter writes.
      2. Legacy annotations file filtered to ``type==cost_event`` —
         plugin emissions ≤ 2026-04-21.
      3. Observability-derived spans projected via
         :func:`observability.opik_client.span_to_cost_event`. This is
         the path that retires the "wait for upstream cost events"
         MVP blocker — Opik spans carry ``total_cost`` directly.

    Sources are unioned; no deduplication. When both the converter and
    observability paths see the same call, both rows show up. That's a
    known transient overcount until the converter path is retired
    (post-V1.5).
    """
    annotations_dir = shared_dir / "annotations" / bot_id
    converter_path = (
        annotations_dir / f"cost_events-{target_date.isoformat()}.jsonl"
    )
    yield from _read_jsonl(converter_path)
    legacy_path = annotations_dir / f"{target_date.isoformat()}.jsonl"
    for record in _read_jsonl(legacy_path):
        if record.get("type") == "cost_event":
            yield record
    yield from _iter_observability_cost_events(shared_dir, bot_id, target_date)


def _iter_observability_cost_events(
    shared_dir: Path, bot_id: str, target_date: date
) -> Iterator[dict]:
    """Project observability spans into cost_event dicts for one (bot, date).

    Returns an empty iterator if observability isn't installed/configured
    or if no spans cover the date. Best-effort throughout — exceptions
    are swallowed so a misconfigured observability backend never breaks
    the rollup writer.
    """
    try:
        from observability import (
            SpanFilter,
            get_client,
            span_to_cost_event,
        )
    except ImportError:
        return

    try:
        client = get_client({}, shared_dir=shared_dir)
    except Exception:
        return

    # Day-bounded window (inclusive of all events whose end_time is
    # within the calendar day in UTC).
    day_start = datetime.combine(target_date, datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    day_end = day_start + timedelta(days=1)

    try:
        spans = client.search_spans(
            SpanFilter(
                bot_id=bot_id,
                span_type="llm",
                since=day_start,
                until=day_end,
                limit=10000,
            )
        )
    except Exception:
        return

    for span in spans:
        try:
            record = span_to_cost_event(span)
        except Exception:
            continue
        if record is None:
            continue
        record.setdefault("bot_id", bot_id)
        # Date-of-day check: ``span_to_cost_event`` uses ``end_time``;
        # ensure it lands in target_date even when the SpanFilter
        # admitted spans that straddle midnight.
        ts = record.get("ts") or ""
        if isinstance(ts, str) and len(ts) >= 10 and ts[:10] != target_date.isoformat():
            continue
        yield record


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate
# ─────────────────────────────────────────────────────────────────────────────


def _empty_model_bucket() -> dict:
    return {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "event_count": 0,
        "unpriced_events": 0,
    }


def _resolve_event_cost(
    ev: dict, catalog: dict | None, shared_dir: Path | None = None,
) -> float | None:
    """One cost_event's USD via the per-turn cost rule (turn_cost).

    Recorded ``cost_usd`` wins when non-zero. A zero with real token
    counts is re-estimated from the event's model/provider — the
    converter passes OC's recorded cost through verbatim, and OC records
    $0 for providers it has no pricing for. ``None`` = can't price; a
    zero-token event is genuinely $0.
    """
    recorded = float(ev.get("cost_usd") or 0.0)
    if recorded:
        return recorded
    tokens = (
        int(ev.get("input_tokens") or 0) + int(ev.get("output_tokens") or 0)
        + int(ev.get("cache_read_tokens") or 0)
        + int(ev.get("cache_write_tokens") or 0)
    )
    if tokens <= 0:
        return 0.0
    return _turn_cost(
        {
            "model": ev.get("model") or "",
            "provider": ev.get("provider") or "",
            "cost": recorded,
            "input_tokens": int(ev.get("input_tokens") or 0),
            "output_tokens": int(ev.get("output_tokens") or 0),
            "cache_read_tokens": int(ev.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(ev.get("cache_write_tokens") or 0),
        },
        catalog=catalog,
        # Scopes the no-catalog fallback load to THIS pod's shared dir
        # rather than turn_cost's env/canonical default.
        shared_dir=shared_dir,
    )


def aggregate(
    events: Iterable[dict],
    *,
    catalog: dict | None = None,
    shared_dir: Path | None = None,
) -> dict:
    """Sum cost + token fields across cost_event records.

    Returns the rollup body (without bot_id / date metadata). Models
    are keyed by ``model`` field; missing/empty model name groups under
    "unknown" so by_model never has an empty-string key.

    Costs resolve through :func:`_resolve_event_cost`; events that
    cannot be priced at all are counted in ``unpriced_events`` (total
    and per model bucket) rather than summed as zero — when that count
    is non-zero, ``total_usd`` is a floor, not the day's spend.
    """
    total_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    event_count = 0
    unpriced_events = 0
    by_model: dict[str, dict] = {}

    for ev in events:
        event_count += 1
        cost = _resolve_event_cost(ev, catalog, shared_dir)
        in_t = int(ev.get("input_tokens") or 0)
        out_t = int(ev.get("output_tokens") or 0)
        cr_t = int(ev.get("cache_read_tokens") or 0)
        cw_t = int(ev.get("cache_write_tokens") or 0)

        if cost is None:
            unpriced_events += 1
        else:
            total_usd += cost
        input_tokens += in_t
        output_tokens += out_t
        cache_read_tokens += cr_t
        cache_write_tokens += cw_t

        model = ev.get("model") or "unknown"
        bucket = by_model.setdefault(model, _empty_model_bucket())
        if cost is None:
            bucket["unpriced_events"] += 1
        else:
            bucket["cost_usd"] += cost
        bucket["input_tokens"] += in_t
        bucket["output_tokens"] += out_t
        bucket["cache_read_tokens"] += cr_t
        bucket["cache_write_tokens"] += cw_t
        bucket["event_count"] += 1

    by_model_out = {
        m: {**b, "cost_usd": round(b["cost_usd"], 6)}
        for m, b in by_model.items()
    }

    return {
        "total_usd": round(total_usd, 6),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "event_count": event_count,
        "unpriced_events": unpriced_events,
        "by_model": by_model_out,
    }


def aggregate_with_oc(
    events: Iterable[dict],
    calls: Iterable,
    *,
    catalog: dict | None = None,
    shared_dir: Path | None = None,
) -> dict:
    """The rollup body with OpenClaw's own per-call cost where it has one (D-CS2).

    Per EVENT, not per call: each cost_event takes OC's summed cost for the
    calls that reproduce its token counts (``oc_usage_cost.attach_oc_costs``)
    and keeps Evolve's estimate otherwise. The transcript DB is not a complete
    ledger — measured on the mini for 2026-09-25, one bot had 14 of 67 billed
    turns with no assistant message persisted at all, so a sum of OC's calls
    alone read $3.12 against a turn-level $5.69. Summing calls would have
    UNDER-stated spend; this never drops a turn. OC calls in a session no
    event covers are added on OC's figure — spend Evolve had no record of.
    """
    import oc_usage_cost  # type: ignore[import]
    from turn_cost import cost_source_label, turn_cost_detail

    rows = [
        {
            "ts": ev.get("ts"), "session_id": ev.get("session_id"),
            "model": ev.get("model") or "", "provider": ev.get("provider") or "",
            "input_tokens": int(ev.get("input_tokens") or 0),
            "output_tokens": int(ev.get("output_tokens") or 0),
            "cache_read_tokens": int(ev.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(ev.get("cache_write_tokens") or 0),
            "cost": float(ev.get("cost_usd") or 0.0),
        }
        for ev in events
    ]
    calls = list(calls)
    oc_usage_cost.attach_oc_costs(rows, calls)
    covered = {r["session_id"] for r in rows if r.get("session_id")}
    for c in calls:
        if c.session_id not in covered and c.priced_by_oc:
            rows.append({**c.as_turn(), "cost": float(c.cost or 0.0),
                         "cost_source": "oc", "cost_estimate": None})

    agg = aggregate([], catalog=catalog, shared_dir=shared_dir)
    total = est_total = 0.0
    oc_n = est_n = 0
    by_model: dict[str, dict] = {}
    for r in rows:
        cost, resolution = turn_cost_detail(r, catalog=catalog, shared_dir=shared_dir)
        if resolution == "oc":
            prior = r.get("cost_estimate")
            estimate = (
                float(prior) if prior else
                turn_cost_detail({**r, "cost": 0, "cost_source": None},
                                 catalog=catalog, shared_dir=shared_dir)[0]
            )
        else:
            estimate = cost
        label = cost_source_label(resolution)
        oc_n += label == "oc"
        est_n += label == "estimate"
        bucket = by_model.setdefault(r["model"] or "unknown", _empty_model_bucket())
        if cost is None:
            agg["unpriced_events"] += 1
            bucket["unpriced_events"] += 1
        else:
            total += cost
            bucket["cost_usd"] += cost
        if estimate is not None:
            est_total += estimate
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_write_tokens"):
            agg[field] += int(r[field])
            bucket[field] += int(r[field])
        bucket["event_count"] += 1
    agg.update({
        "total_usd": round(total, 6),
        "event_count": len(rows),
        "by_model": {
            m: {**b, "cost_usd": round(b["cost_usd"], 6)} for m, b in by_model.items()
        },
        "source": (
            None if not (oc_n or est_n)
            else "oc" if not est_n else "estimate" if not oc_n else "mixed"
        ),
        "estimate_usd": round(est_total, 6),
        "oc_calls": sum(int(r.get("oc_calls") or (r.get("cost_source") == "oc"))
                        for r in rows),
    })
    return agg


def _oc_calls_for_day(bot_id: str, target_date: date) -> list | None:
    """OC's calls for the UTC day (the rollup's day), or ``None`` when the
    transcript DB cannot be read — the caller then keeps the estimate."""
    try:
        import oc_usage_cost  # type: ignore[import]
    except Exception:
        return None
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    try:
        return oc_usage_cost.load_oc_calls(bot_id, start, start + timedelta(days=1))
    except Exception:  # noqa: BLE001 — never lose the estimate rollup over it
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Write side
# ─────────────────────────────────────────────────────────────────────────────


def write_rollup(
    shared_dir: Path, bot_id: str, target_date: date,
    *, now: datetime | None = None,
) -> dict | None:
    """Compute the rollup for one (bot, date) and write to disk.

    Returns the rollup dict written, or ``None`` when there were no
    events for that date. Days with no events do not get a zero-rollup
    file — Budget Hawk's spend_reader treats missing files as "no data"
    rather than "zero spend", which is the right call for inactive
    bots (e.g. personal_bot as of 2026-05-05).
    """
    events = list(iter_cost_events(shared_dir, bot_id, target_date))
    catalog = load_pricing_catalog(shared_dir)
    calls = _oc_calls_for_day(bot_id, target_date)
    if not events and not calls:
        return None

    if calls:
        # D-CS2: OC's own figure wherever it covers a turn; the table's
        # estimate, labelled as one, wherever it does not.
        agg = aggregate_with_oc(events, calls, catalog=catalog, shared_dir=shared_dir)
    else:
        agg = aggregate(events, catalog=catalog, shared_dir=shared_dir)
        agg["source"] = "estimate" if agg["event_count"] > agg["unpriced_events"] else None
        agg["estimate_usd"] = agg["total_usd"]
        agg["oc_calls"] = 0
    generated_at = (now or datetime.now(timezone.utc)).isoformat()
    if generated_at.endswith("+00:00"):
        generated_at = generated_at[:-6] + "Z"

    rollup = {
        "schema_version": COST_ROLLUP_SCHEMA_VERSION,
        "bot_id": bot_id,
        "date": target_date.isoformat(),
        "generated_at": generated_at,
        **agg,
    }
    out_path = (
        shared_dir / "metrics" / bot_id / f"cost-{target_date.isoformat()}.json"
    )
    # 0o644: the rollup is read by other components (Budget Hawk et al).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, rollup, mode=0o644)
    return rollup


def refresh_all(
    shared_dir: Path,
    bots: Iterable[str],
    days: int = DEFAULT_BACKFILL_DAYS,
    *,
    today: date | None = None,
    log_fn: "callable | None" = None,
) -> list[tuple[str, date, dict | None]]:
    """Refresh trailing ``days`` of rollups for every bot in ``bots``.

    Returns one (bot_id, date, rollup-or-None) tuple per (bot, date)
    pair attempted. Callers can use the None entries to count
    no-data days for logging / observability.

    Per-bot fault isolation: a write failure for one bot logs a warning
    and continues with the rest. Without this, a single broken bot dir
    (e.g. wrong ownership on ``metrics/<bot>/`` so the evolve user can
    not write the temp file there) raised out of the entire pass,
    silently leaving every later bot's rollups stale. That regression
    landed 2026-05-07 and went undetected for 10 days because
    ``better_engine_refresh.py``'s outer ``try`` caught it as
    "non-fatal" — the same broken bot fired the same exception every
    15 min and nobody saw the cascade.

    ``log_fn`` is the caller's logger (``better_engine_refresh._log``
    in production). When omitted, ``print`` to stderr — keeps the CLI
    path informative without forcing a logger on every caller.
    """
    if log_fn is None:
        def log_fn(msg: str) -> None:  # type: ignore[misc]
            print(msg, file=sys.stderr)

    end_date = today or date.today()
    # One-time D-CS2 re-roll: the first pass after this ships rewrites the
    # trailing OC_BACKFILL_DAYS from OC's figure, so the caps' baselines and
    # the 28-day trend start true instead of converging over a fortnight.
    bots = list(bots)
    marker = Path(shared_dir) / "metrics" / _OC_BACKFILL_MARKER
    backfill_once = (
        days < OC_BACKFILL_DAYS and not marker.exists() and _any_oc_db(bots)
    )
    if backfill_once:
        days = OC_BACKFILL_DAYS
    results: list[tuple[str, date, dict | None]] = []
    for bot_id in bots:
        for i in range(days):
            d = end_date - timedelta(days=i)
            try:
                r = write_rollup(shared_dir, bot_id, d)
            except Exception as exc:
                # Per-bot, per-date isolation: log and continue. A
                # PermissionError on one date for one bot shouldn't
                # poison later dates or other bots.
                log_fn(
                    f"[cost_rollup] write_rollup failed for bot={bot_id} "
                    f"date={d.isoformat()}: {exc.__class__.__name__}: {exc}"
                )
                r = None
            results.append((bot_id, d, r))
    if backfill_once:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(end_date.isoformat() + "\n")
        except OSError as exc:
            log_fn(f"[cost_rollup] could not write backfill marker: {exc}")
    return results


def _any_oc_db(bots: Iterable[str]) -> bool:
    """True when at least one bot has a readable-looking transcript DB — the
    one-time re-roll waits for OC data rather than burning its marker on a
    pod (or a pass) that cannot see any."""
    try:
        import oc_usage_cost  # type: ignore[import]
        return any(oc_usage_cost.agent_db_paths(b) for b in bots)
    except Exception:  # noqa: BLE001
        return False


def backfill_from_oc(
    shared_dir: Path, bots: Iterable[str], *, days: int = OC_BACKFILL_DAYS,
    today: date | None = None,
) -> list[tuple[str, date, dict | None]]:
    """Re-roll the trailing ``days`` of rollups from OC's per-call cost.

    The operator form of the one-time re-roll ``refresh_all`` performs on its
    own; safe to re-run (each rollup is recomputed and overwritten).
    """
    return refresh_all(shared_dir, bots, days=days, today=today)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _load_members(shared_dir: Path) -> list[str]:
    network_path = shared_dir / "network.json"
    if not network_path.exists():
        return []
    try:
        net = json.loads(network_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return list(net.get("members") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-bot daily cost rollup writer")
    parser.add_argument(
        "--shared-dir", default="/Users/Shared/evolve",
        help="Path to the shared evolve directory",
    )
    parser.add_argument(
        "--bot-id",
        help="Roll up a single bot (default: all members in network.json)",
    )
    parser.add_argument(
        "--date",
        help="Single date to roll up (YYYY-MM-DD); overrides --backfill",
    )
    parser.add_argument(
        "--backfill", type=int, default=DEFAULT_BACKFILL_DAYS,
        help=f"Trailing days to refresh (default: {DEFAULT_BACKFILL_DAYS})",
    )
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir)
    if args.bot_id:
        bots = [args.bot_id]
    else:
        bots = _load_members(shared_dir)
        if not bots:
            print(
                "[cost_rollup] no members in network.json — nothing to do",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.date:
        target = date.fromisoformat(args.date)
        for bot_id in bots:
            r = write_rollup(shared_dir, bot_id, target)
            status = (
                f"${r['total_usd']:.4f} ({r['event_count']} events)"
                if r else "no events"
            )
            print(f"[cost_rollup] {bot_id} {target}: {status}")
        return

    results = refresh_all(shared_dir, bots, days=args.backfill)
    bot_totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0, 0])
    for bot_id, _d, r in results:
        if r is not None:
            bot_totals[bot_id][0] += r["total_usd"]
            bot_totals[bot_id][1] += 1
        else:
            bot_totals[bot_id][2] += 1
    for bot_id in sorted(bot_totals):
        cost, written, missing = bot_totals[bot_id]
        print(
            f"[cost_rollup] {bot_id}: ${cost:.4f} across {written} day(s), "
            f"{missing} day(s) had no events"
        )


if __name__ == "__main__":
    main()

"""cost_ledger — Rollups over cost_event records.

Budget Hawk v2 relies on per-call cost lineage — every billable LLM call
emits a cost_event record (see packages/plugin/src/observer/CostLedger.ts).
This module turns the stream of per-call events into the shapes the detectors
and the admin UI actually consume.

Two-layer API:

  1. read_events(bot_id, days) → Iterator[dict]
     Thin wrapper around observations.access.window(...).cost_events(). Single
     place to change if the underlying storage ever moves.

  2. rollup_* functions
     Pure aggregations over an iterable of events. Return plain dicts so
     they're trivially JSON-serialisable for the admin API, and trivially
     comparable in tests.

All rollups are tolerant of missing fields — old records without the newer
cache_state / trigger_kind fields flow through as "unknown".
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any  # noqa: F401  (used in type hints below)

from observations.access import window


# ─────────────────────────────────────────────────────────────────────────────
# Reader
# ─────────────────────────────────────────────────────────────────────────────


def read_events(
    bot_id: str,
    days: int = 7,
    shared_dir: Path | str = Path("/Users/Shared/evolve"),
    *,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Yield cost_event records for ``bot_id`` in the trailing ``days``.

    Passing ``now`` is for tests — production omits it and uses wall clock.

    The source of truth depends on the observability config:
      * If observability is configured and reachable, cost_events derived
        from Opik spans are merged in via :func:`read_events_from_observability`
        (each span's ``total_cost`` is recorded directly by the producer,
        retiring the "wait for upstream cost events" blocker).
      * The legacy plugin path (``observations/access.py::cost_events``)
        always runs — backfills + plugin emissions still flow through it.

    Records are unioned, not deduplicated by event ID — we keep the
    legacy path as the durable record until observability has been
    proven in production. When two paths report the same call, both
    show up in rollups; that's a known overcount transient until the
    legacy path is retired (post-V1.5).
    """
    if now is None:
        w = window(bot_id, days=days, shared_dir=shared_dir)
    else:
        end = now
        start = end - timedelta(days=days)
        w = window(bot_id, start=start, end=end, shared_dir=shared_dir)
    yield from w.cost_events()

    # Observability-derived events. Best-effort — failing here must
    # never break rollups that work today off the converter file.
    try:
        yield from read_events_from_observability(
            bot_id, days=days, shared_dir=shared_dir, now=now,
        )
    except Exception:
        return


def read_events_from_observability(
    bot_id: str,
    days: int = 7,
    shared_dir: Path | str = Path("/Users/Shared/evolve"),
    *,
    now: datetime | None = None,
    client: Any = None,
    config: dict | None = None,
) -> Iterator[dict]:
    """Yield cost_event records derived from observability spans (Opik path).

    Pulls LLM spans for ``bot_id`` over the trailing window and projects
    each into the cost_event shape via
    :func:`observability.opik_client.span_to_cost_event`. The conversion
    preserves the existing cost_event schema so cost_rollup + cost_ledger
    aggregators stay unchanged.

    ``client`` is injectable for tests. Default: factory-built from
    ``config`` (or empty config → JSONL backend at ``shared_dir``).

    This is the path that retires the "wait for upstream cost events"
    blocker from MVP: spans recorded by producers (including our future
    cost_event_converter hook into the gateway) carry ``total_cost``
    directly per the Opik SDK signature, so we don't depend on OpenClaw
    emitting a separate ``cost_event`` annotation.
    """
    # Local import to avoid a hard dependency on observability for code
    # paths that don't need it (and to avoid circular imports during
    # package load).
    from observability import (
        SpanFilter,
        get_client,
        span_to_cost_event,
    )

    if client is None:
        client = get_client(config or {}, shared_dir=shared_dir)

    now_dt = now or datetime.now(timezone.utc)
    since = now_dt - timedelta(days=days)

    query = SpanFilter(
        bot_id=bot_id,
        span_type="llm",
        since=since,
        until=now_dt,
        limit=5000,
    )

    for span in client.search_spans(query):
        record = span_to_cost_event(span)
        if record is None:
            continue
        # cost_events historically have ``bot_id`` populated; if the
        # span omitted it, stamp it in so downstream aggregators don't
        # bucket under "".
        record.setdefault("bot_id", bot_id)
        yield record


# ─────────────────────────────────────────────────────────────────────────────
# Rollups
# ─────────────────────────────────────────────────────────────────────────────


def _ts_date(record: dict) -> str:
    """Extract the YYYY-MM-DD date from a cost_event record.

    Falls back to empty string when no usable timestamp is present — those
    records are still counted in aggregates but get bucketed under "" (so
    tests can notice) rather than silently dropping.
    """
    raw = record.get("ts")
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    return ""


def rollup_per_bot_day(events: Iterable[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    """Aggregate events by (bot_id, date).

    Returns:
        Dict keyed by (bot_id, YYYY-MM-DD) → {
            "event_count":     int,
            "cost_usd":        float,
            "input_tokens":    int,
            "output_tokens":   int,
            "cache_read":      int,
            "cache_write":     int,
        }
    """
    acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "event_count": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
        }
    )
    for e in events:
        key = (e.get("bot_id", ""), _ts_date(e))
        slot = acc[key]
        slot["event_count"] += 1
        slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
        slot["input_tokens"] += int(e.get("input_tokens", 0) or 0)
        slot["output_tokens"] += int(e.get("output_tokens", 0) or 0)
        slot["cache_read"] += int(e.get("cache_read_tokens", 0) or 0)
        slot["cache_write"] += int(e.get("cache_write_tokens", 0) or 0)
    # Round for stable serialisation
    for slot in acc.values():
        slot["cost_usd"] = round(slot["cost_usd"], 6)
    return dict(acc)


def rollup_per_trigger_kind(events: Iterable[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate events by trigger_kind.

    Returns:
        {
          "user_turn":      {"event_count": ..., "cost_usd": ...},
          "heartbeat":      {...},
          "cron_app":       {...},
          "subagent":       {...},
          "summarizer":     {...},
          "classifier":     {...},
          "task_extractor": {...},
          "fallback":       {...},
          "unknown":        {...},
        }
    """
    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"event_count": 0, "cost_usd": 0.0}
    )
    for e in events:
        kind = e.get("trigger_kind") or "unknown"
        slot = acc[kind]
        slot["event_count"] += 1
        slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
    for slot in acc.values():
        slot["cost_usd"] = round(slot["cost_usd"], 6)
    return dict(acc)


def rollup_per_cache_state(events: Iterable[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate events by cache_state. Same shape as per_trigger_kind."""
    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"event_count": 0, "cost_usd": 0.0}
    )
    for e in events:
        state = e.get("cache_state") or "unknown"
        slot = acc[state]
        slot["event_count"] += 1
        slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
    for slot in acc.values():
        slot["cost_usd"] = round(slot["cost_usd"], 6)
    return dict(acc)


def rollup_per_application(
    events: Iterable[dict],
    session_summaries: Iterable[dict],
    *,
    unattributed_key: str = "(unattributed)",
) -> dict[str, dict[str, Any]]:
    """Attribute cost to detected applications by joining events to summaries.

    Each session_summary carries an ``applications_invoked: list[str]`` field
    that SessionSummarizer populates from keyword patterns + deployment-specific
    ``applicationPatterns``. We attribute a session's cost to its applications
    by **even split** — if a session spent $1 and invoked ["health-nutrition",
    "calendar"], each app gets $0.50.

    Sessions with no apps detected land under ``unattributed_key`` so operators
    can see how much of their spend isn't yet tagged — a gentle prompt to
    extend the keyword patterns.

    Args:
        events: iterable of cost_event dicts (any time window)
        session_summaries: iterable of session_summary dicts covering the
            same window. We build an in-memory map, so caller should keep
            this bounded (per-bot per-week ≈ hundreds of summaries typically).

    Returns:
        Dict keyed by application tag → {
            "cost_usd":    float,
            "event_count": int,   # events attributed to this app
            "session_count": int, # distinct sessions
        }
    """
    # Build session_id → applications map. Single pass; keep only the fields
    # we actually need so we don't pin large summary objects in memory.
    session_apps: dict[str, list[str]] = {}
    for summary in session_summaries:
        sid = summary.get("session_id")
        apps = summary.get("applications_invoked") or []
        if sid and isinstance(apps, list):
            session_apps[sid] = [str(a) for a in apps if a]

    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost_usd": 0.0, "event_count": 0, "session_ids": set()}
    )
    for e in events:
        cost = float(e.get("cost_usd", 0.0) or 0.0)
        sid = e.get("session_id") or ""
        apps = session_apps.get(sid, [])
        if not apps:
            slot = acc[unattributed_key]
            slot["cost_usd"] += cost
            slot["event_count"] += 1
            if sid:
                slot["session_ids"].add(sid)
            continue
        # Even split across applications. This is the simplest defensible
        # attribution — fancier weighting (e.g. by keyword-match strength)
        # can be layered later without changing callers.
        share = cost / len(apps)
        for app in apps:
            slot = acc[app]
            slot["cost_usd"] += share
            # event_count counts the whole event per app it touched — if two
            # apps share a session, each of them sees N events (accurate for
            # "how many calls were in a session this app was part of").
            slot["event_count"] += 1
            if sid:
                slot["session_ids"].add(sid)

    return {
        app: {
            "cost_usd": round(slot["cost_usd"], 6),
            "event_count": slot["event_count"],
            "session_count": len(slot["session_ids"]),
        }
        for app, slot in acc.items()
    }


def rollup_per_session(events: Iterable[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate events by session_id. Used for Session Lineage UI + sprawl detector.

    Schema v2 fields (``user_id``, ``user_display_name``, ``channel_id``,
    ``channel_kind``, ``timestamp_local``) are tracked off the FIRST event
    in the session (chronologically). Cost alerts use these to tell the
    operator who triggered the session — the first turn is the
    user-supplied one in almost every case (heartbeats / cron sessions
    don't carry external user ids so they stay None either way).
    """
    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "event_count": 0,
            "cost_usd": 0.0,
            "trigger_kinds": set(),
            "first_ts": "",
            "last_ts": "",
            # v2 enrichment — populated from the earliest-ts event with the
            # field present. Stays None when the session has no external
            # user (heartbeat, cron, subagent).
            "first_user_id": None,
            "first_user_display_name": None,
            "first_channel_id": None,
            "first_channel_kind": None,
            "first_timestamp_local": None,
        }
    )
    for e in events:
        sid = e.get("session_id") or ""
        if not sid:
            continue
        slot = acc[sid]
        slot["event_count"] += 1
        slot["cost_usd"] += float(e.get("cost_usd", 0.0) or 0.0)
        kind = e.get("trigger_kind") or "unknown"
        slot["trigger_kinds"].add(kind)
        ts = e.get("ts") or ""
        is_new_first = False
        if isinstance(ts, str):
            if not slot["first_ts"] or ts < slot["first_ts"]:
                slot["first_ts"] = ts
                is_new_first = True
            if not slot["last_ts"] or ts > slot["last_ts"]:
                slot["last_ts"] = ts
        if is_new_first:
            # Pull v2 enrichment from this event since it now anchors
            # "first turn." Don't overwrite with None when the field is
            # absent on the v2 record — keep whatever earlier (later-ts)
            # populated value we had. Hold-strong policy: a populated
            # value from any seen event survives unless a strictly-earlier
            # event also carries a non-None replacement.
            for key, src in (
                ("first_user_id", "user_id"),
                ("first_user_display_name", "user_display_name"),
                ("first_channel_id", "channel_id"),
                ("first_channel_kind", "channel_kind"),
                ("first_timestamp_local", "timestamp_local"),
            ):
                v = e.get(src)
                if v is not None and v != "":
                    slot[key] = v
        else:
            # Late-arriving event for this session — only fill in fields
            # we don't yet have. Prevents the order events arrive in
            # from changing which user_id we land on. (Real-world
            # callers pre-sort or read JSONL in append order, but we
            # keep this hold-strong so the rollup is idempotent.)
            for key, src in (
                ("first_user_id", "user_id"),
                ("first_user_display_name", "user_display_name"),
                ("first_channel_id", "channel_id"),
                ("first_channel_kind", "channel_kind"),
                ("first_timestamp_local", "timestamp_local"),
            ):
                if slot[key] in (None, "") and e.get(src) not in (None, ""):
                    slot[key] = e.get(src)
    # Freeze sets to sorted lists for JSON-friendliness
    out: dict[str, dict[str, Any]] = {}
    for sid, slot in acc.items():
        out[sid] = {
            "event_count": slot["event_count"],
            "cost_usd": round(slot["cost_usd"], 6),
            "trigger_kinds": sorted(slot["trigger_kinds"]),
            "first_ts": slot["first_ts"],
            "last_ts": slot["last_ts"],
            "first_user_id": slot["first_user_id"],
            "first_user_display_name": slot["first_user_display_name"],
            "first_channel_id": slot["first_channel_id"],
            "first_channel_kind": slot["first_channel_kind"],
            "first_timestamp_local": slot["first_timestamp_local"],
        }
    return out


def top_spikes(events: Iterable[dict], *, n: int = 20) -> list[dict]:
    """Return the ``n`` single events with the highest cost_usd.

    Feeds the Spike Explorer UI. Events are returned richly — full record
    including trigger_kind, cache_state, session_id — so the UI can render
    them without any join.
    """
    # heapq.nlargest is O(n log k); for typical volumes (thousands of events,
    # k=20) this is fast enough that simple beats clever.
    return heapq.nlargest(
        n,
        events,
        key=lambda e: float(e.get("cost_usd", 0.0) or 0.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bot-list helper (feeds cross-bot rollups in the admin API)
# ─────────────────────────────────────────────────────────────────────────────


def discover_bot_ids(shared_dir: Path | str = Path("/Users/Shared/evolve")) -> list[str]:
    """Return bot_ids that have an annotations subdirectory on disk.

    Directory layout: ``{shared_dir}/annotations/{bot_id}/{YYYY-MM-DD}.jsonl``.
    A bot without a subdirectory simply hasn't written annotations yet; it's
    not an error.
    """
    root = Path(shared_dir) / "annotations"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())

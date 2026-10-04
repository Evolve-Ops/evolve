"""observability.session_rollup — group per-turn cascade spans into session summaries.

The plugin's `CascadeTelemetry` (TypeScript-side; see
`packages/plugin/src/observer/CascadeTelemetry.ts`) writes one Opik-shaped
span per agent_end to:

    {sharedDir}/{botId}/spans/spans-YYYY-MM-DD.jsonl

This module reads those files (plus the central
`{sharedDir}/observability/spans/` directory written by Python-side
error/audit producers) and aggregates per-turn rows into per-session
summaries. Consumers:

  * Phase 1 admin UI surfaces — show per-session struggle distribution.
  * Phase 4 audit layer — Haiku reads sampled sessions, judges whether
    routing was right.
  * Phase 3 deprecation — replaces analyzer-side aggregations that today
    derive metrics from the productive/maintenance session_class label
    (see spec § 5.2).

Per internal/spec-tier-cascade-2026-05-26.md § 2.3 and § 3.

Schema (matches OpikSpan attribute conventions written by
CascadeTelemetry):

    span.attributes:
      session_id: str
      turn_index: int
      cascade.tier_used: "tier0" | "tier1" | "tier2" | "tier3" | None
      cascade.tier_chosen_by: str
      cascade.struggle.score: float | None
      cascade.struggle.features.<name>: float
      cascade.struggle.raw.<name>: float
      legacy.session_class: str | None     # read-compat during Phase 1-3

This module is pure read-and-aggregate — no I/O writes, no SDK calls. It
loads OpikSpan via the existing JsonlBackend search path so it transparently
covers both per-bot span files and the central observability/spans/ dir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from observability.opik_client import (
    JsonlBackend,
    OpikSpan,
    SpanFilter,
)


# Tier ordering for "max tier reached" / "min tier seen" comparisons.
# Lower index = cheaper.
_TIER_ORDER = {"tier3": 0, "tier2": 1, "tier1": 2, "tier0": 3}


def _tier_rank(tier: str | None) -> int:
    """Sortable rank; unknown tiers rank below tier3 (so they don't win max)."""
    if tier is None:
        return -1
    return _TIER_ORDER.get(tier, -1)


# ─────────────────────────────────────────────────────────────────────────────
# SessionSummary
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionSummary:
    """One row per (bot_id, session_id). All time fields UTC."""

    session_id: str
    bot_id: str | None
    start_time: datetime
    end_time: datetime
    turn_count: int

    # Tier usage across the session (from cascade.tier_used — what
    # actually ran, per failure-mode review F8 / spec § 6.3).
    max_tier: str | None
    """Highest tier (most capable) actually used during the session.
    Phase 1 will almost always equal min_tier since cascade isn't yet
    deciding routing — populated for Phase 2 onwards."""

    min_tier: str | None
    """Lowest tier the session ran at. Useful for "did this session
    de-escalate?" Phase 2+."""

    tier_distribution: dict[str, int]
    """Count of turns at each tier, e.g. {'tier2': 5, 'tier3': 2}."""

    # Tier divergence — when cascade.tier_intended != cascade.tier_used.
    # Critical signal per failure F8: OC dropped the model override and
    # ran something other than what cascade wanted. Without this, the
    # calibration loop is built on a lie about which model billed.
    divergent_turn_count: int = 0
    """How many turns showed tier_used != tier_intended."""

    # Struggle aggregates.
    max_struggle: float = 0.0
    """Highest struggle score observed across all turns. The single
    number most predictive of 'should this session have escalated'."""

    mean_struggle: float = 0.0
    """Average struggle score across turns with a score recorded."""

    turns_with_struggle: int = 0
    """How many turns had any struggle signal at all (score > 0)."""

    # Source-of-decision counts. Phase 1: all "classifier."
    # Phase 2+: cascade_count rises, classifier_count drops to 0.
    chosen_by_counts: dict[str, int] = field(default_factory=dict)

    # Session source — from cascade.trigger_kind on the first span (rare
    # to change mid-session). Used for stratified aggregates by source
    # (user_turn vs heartbeat vs cron_app etc.) per spec § 2.4.
    trigger_kind: str | None = None

    # Cost roll-up — read directly from total_cost on each span.
    total_cost_usd: float = 0.0

    # Legacy session_class read-compat. Removed in Phase 3.
    legacy_session_class: str | None = None
    """Modal session_class label from the legacy classifier (Phase 1-3
    only). None when no spans carried it."""

    # Provenance — sources we read this summary from.
    span_count: int = 0
    has_failed_turn: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Loading: walks per-bot spans/ dirs and the central observability/spans/
# ─────────────────────────────────────────────────────────────────────────────


def iter_turn_spans(
    shared_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    bot_id: str | None = None,
    limit: int = 100_000,
) -> Iterator[OpikSpan]:
    """Yield cascade turn spans across all bots (or one bot) in the window.

    Reads from two locations because the plugin and the Python producers
    write to different roots:

      1. ``{sharedDir}/{botId}/spans/spans-YYYY-MM-DD.jsonl`` — written by
         CascadeTelemetry.ts (hot path). One file per bot per day.
      2. ``{sharedDir}/observability/spans/YYYY-MM-DD.jsonl`` — written by
         Python producers (embedding_monitor, watchdog, reporter).
         Cascade turn spans don't normally land here, but we read it for
         completeness so any future plugin-emitted-to-central-dir spans
         aren't silently missed.

    The yield filter is ``producer == "cascade_telemetry"`` so non-cascade
    spans (error producers etc.) are excluded.

    Window defaults to last 7 days if neither ``since`` nor ``until`` is given —
    long enough to cover Phase 1's 2-week validation window across multiple calls.
    """
    shared = Path(shared_dir)

    now = datetime.now(timezone.utc)
    since = since or (now - timedelta(days=7))
    until = until or now

    yielded = 0

    # ── Per-bot span files ────────────────────────────────────────────────
    if bot_id:
        bot_dirs = [shared / bot_id]
    else:
        bot_dirs = [
            p for p in shared.iterdir()
            if p.is_dir() and (p / "spans").is_dir()
        ] if shared.is_dir() else []

    for bot_dir in bot_dirs:
        spans_dir = bot_dir / "spans"
        if not spans_dir.is_dir():
            continue
        # Use the JsonlBackend's per-day iteration logic — but it expects
        # a different root layout. Easiest: enumerate the day files
        # directly and parse line-by-line, same as JsonlBackend does.
        for span in _iter_jsonl_dir(spans_dir, since, until):
            if span.producer != "cascade_telemetry":
                continue
            if bot_id and span.bot_id != bot_id:
                continue
            yield span
            yielded += 1
            if yielded >= limit:
                return

    # ── Central observability/spans dir ────────────────────────────────────
    # JsonlBackend's normal path. We use SpanFilter to scope.
    central_root = shared / "observability" / "spans"
    if central_root.is_dir():
        backend = JsonlBackend(shared)
        q = SpanFilter(
            producer="cascade_telemetry",
            bot_id=bot_id,
            since=since,
            until=until,
            limit=limit - yielded,
        )
        for span in backend.search_spans(q):
            yield span
            yielded += 1
            if yielded >= limit:
                return


def _iter_jsonl_dir(
    dir_path: Path,
    since: datetime,
    until: datetime,
) -> Iterator[OpikSpan]:
    """Walk per-day JSONL files in a directory, yielding parsed spans.

    Tolerates malformed lines and stray non-JSONL files — returns nothing
    if the directory is missing or every file is broken.
    """
    if not dir_path.is_dir():
        return
    # Filename shape: "spans-YYYY-MM-DD.jsonl" or "YYYY-MM-DD.jsonl".
    start_day = since.astimezone(timezone.utc).date()
    end_day = until.astimezone(timezone.utc).date()
    for path in sorted(dir_path.glob("*.jsonl")):
        # Extract the date portion from the filename to gate cheap.
        stem = path.stem  # "spans-2026-05-26" or "2026-05-26"
        if stem.startswith("spans-"):
            day_str = stem[len("spans-"):]
        else:
            day_str = stem
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < start_day or day > end_day:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                import json
                raw = json.loads(line)
                span = OpikSpan.from_dict(raw)
            except (ValueError, KeyError, TypeError):
                continue
            # Enforce the time window precisely (filename gate is a
            # coarse pre-filter).
            if span.end_time < since or span.start_time > until:
                continue
            yield span


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────


def rollup_sessions(spans: Iterable[OpikSpan]) -> list[SessionSummary]:
    """Group spans by session_id and produce one SessionSummary per session.

    Spans without a session_id attribute are skipped (they're not cascade
    turn spans by definition). Spans are sorted within each session by
    turn_index for stable iteration.

    Pure function — no I/O. Takes an iterable so the caller controls the
    source (iter_turn_spans, a manual list, a test fixture).
    """
    grouped: dict[str, list[OpikSpan]] = {}
    for s in spans:
        sid = s.attributes.get("session_id") if isinstance(s.attributes, dict) else None
        if not sid:
            continue
        grouped.setdefault(sid, []).append(s)

    summaries: list[SessionSummary] = []
    for session_id, session_spans in grouped.items():
        # Sort by turn_index, falling back to end_time for spans missing it.
        def _sort_key(sp: OpikSpan) -> tuple[int, datetime]:
            idx = sp.attributes.get("turn_index") if isinstance(sp.attributes, dict) else None
            return (int(idx) if isinstance(idx, int) else 10**9, sp.end_time)

        session_spans.sort(key=_sort_key)
        summaries.append(_summarize_one(session_id, session_spans))
    return summaries


def _summarize_one(session_id: str, spans: list[OpikSpan]) -> SessionSummary:
    """Build a SessionSummary from one session's turn spans.

    The caller (rollup_sessions) gates on non-empty groups, so receiving
    an empty spans list here indicates an upstream bug. We raise
    ValueError rather than silently returning a sentinel — silent
    sentinels hide the bug class round-3 review (Low #9) flagged.
    """
    if not spans:
        raise ValueError(
            f"_summarize_one called with empty spans for session_id={session_id!r}; "
            "caller should filter empty groups upstream."
        )

    tier_dist: dict[str, int] = {}
    chosen_by: dict[str, int] = {}
    struggles: list[float] = []
    legacy_classes: list[str] = []
    trigger_kinds: list[str] = []
    total_cost = 0.0
    failed = False
    divergent_turns = 0
    max_tier_rank = -1
    min_tier_rank = 10**9
    max_tier_name: str | None = None
    min_tier_name: str | None = None

    bot_id = spans[0].bot_id
    start_time = spans[0].start_time
    end_time = spans[-1].end_time

    for sp in spans:
        # Tier accounting — uses tier_used (truth), not tier_intended.
        attrs = sp.attributes if isinstance(sp.attributes, dict) else {}
        tier = attrs.get("cascade.tier_used")
        if isinstance(tier, str):
            tier_dist[tier] = tier_dist.get(tier, 0) + 1
            r = _tier_rank(tier)
            if r > max_tier_rank:
                max_tier_rank = r
                max_tier_name = tier
            if r < min_tier_rank and r >= 0:
                min_tier_rank = r
                min_tier_name = tier

        # Divergence accounting — tier_used != tier_intended means OC
        # didn't honor cascade's verdict. Per failure F8.
        if attrs.get("cascade.tier_divergent") is True:
            divergent_turns += 1

        # Chosen-by counts.
        cb = attrs.get("cascade.tier_chosen_by")
        if isinstance(cb, str):
            chosen_by[cb] = chosen_by.get(cb, 0) + 1

        # Trigger kind (session source).
        tk = attrs.get("cascade.trigger_kind")
        if isinstance(tk, str):
            trigger_kinds.append(tk)

        # Struggle.
        score = attrs.get("cascade.struggle.score")
        if isinstance(score, (int, float)):
            struggles.append(float(score))

        # Legacy classifier label (read-compat; removed Phase 3).
        legacy = attrs.get("legacy.session_class")
        if isinstance(legacy, str):
            legacy_classes.append(legacy)

        if sp.total_cost is not None:
            total_cost += float(sp.total_cost)
        if sp.is_error():
            failed = True
        # Pick bot_id from any span carrying one.
        if not bot_id and sp.bot_id:
            bot_id = sp.bot_id

    turns_with_struggle = sum(1 for s in struggles if s > 0)
    max_struggle = max(struggles) if struggles else 0.0
    mean_struggle = (sum(struggles) / len(struggles)) if struggles else 0.0

    # Modal legacy session_class (mode = most common; ties broken by
    # first-seen). Returns None if no span carried it.
    legacy_session_class: str | None = None
    if legacy_classes:
        counts: dict[str, int] = {}
        for c in legacy_classes:
            counts[c] = counts.get(c, 0) + 1
        legacy_session_class = max(counts.items(), key=lambda kv: kv[1])[0]

    # Modal trigger_kind (sessions rarely change kind mid-run, but mode
    # is the right aggregation for the unusual case where they do).
    trigger_kind: str | None = None
    if trigger_kinds:
        tk_counts: dict[str, int] = {}
        for k in trigger_kinds:
            tk_counts[k] = tk_counts.get(k, 0) + 1
        trigger_kind = max(tk_counts.items(), key=lambda kv: kv[1])[0]

    return SessionSummary(
        session_id=session_id,
        bot_id=bot_id,
        start_time=start_time,
        end_time=end_time,
        turn_count=len(spans),
        max_tier=max_tier_name,
        min_tier=min_tier_name if min_tier_rank < 10**9 else None,
        tier_distribution=tier_dist,
        divergent_turn_count=divergent_turns,
        max_struggle=max_struggle,
        mean_struggle=mean_struggle,
        turns_with_struggle=turns_with_struggle,
        chosen_by_counts=chosen_by,
        trigger_kind=trigger_kind,
        total_cost_usd=total_cost,
        legacy_session_class=legacy_session_class,
        span_count=len(spans),
        has_failed_turn=failed,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience
# ─────────────────────────────────────────────────────────────────────────────


def rollup_for_window(
    shared_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    bot_id: str | None = None,
) -> list[SessionSummary]:
    """End-to-end helper: walk → group → summarize.

    Default window is the last 7 days (matches iter_turn_spans default).
    Returns summaries sorted by end_time descending (most recent first).
    """
    spans = list(iter_turn_spans(shared_dir, since=since, until=until, bot_id=bot_id))
    summaries = rollup_sessions(spans)
    summaries.sort(key=lambda s: s.end_time, reverse=True)
    return summaries

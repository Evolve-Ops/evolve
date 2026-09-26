"""cache_shape — the prompt cache's SHAPE, as opposed to its hit rate.

Motivated by ``internal/finding-cost-forensics-power-bot-2026-09-04.md`` §2.
The measured day reported a 79-87% prompt-cache "hit rate" while the provider
console billed a cache-WRITE column of 43k, 46k, 52k, 61k, 74k and 82k tokens
on consecutive turns. Both numbers were correct. They disagree because they
count different things:

* **Intra-turn** hits — one agentic turn made thirteen model calls, and calls
  2-13 read what call 1 had just written. Token-weighted over all calls, that
  is most of the input and it dominates any hit-rate figure computed per call.
* **Cross-turn** hits — the turns themselves were ten to thirty minutes apart
  against a five-minute cache window, so call 1 of nearly every turn re-wrote
  the ~45k prefix from cold at the write premium.

A metric that cannot separate those two reads as "the cache is working" on the
exact day the cache was being paid for and never used. So this module counts
the second thing, and only the second thing:

  **prefix re-warm** — a turn, other than the first of its session, whose
  ``cache_write_tokens`` exceeds :data:`PREFIX_REWARM_FLOOR_TOKENS`. One
  re-warm is one full-price rewrite of the fixed prefix.

  **cross-turn cache hit rate** — the share of eligible turns that were NOT
  re-warms. Counted per turn, never token-weighted, because token weighting is
  precisely what lets the intra-turn reads drown the signal.

Three things are built on that definition:

1. :func:`cross_turn_cache` — the metric, and the two receipt lines it feeds.
2. :func:`resolve_auto_retention` — the evidence gate behind ``cache_retention:
   "auto"``. Anthropic's one-hour cache costs 2.00x base to write against the
   five-minute cache's 1.25x, so it only pays where the prefix would otherwise
   be re-written more than ~1.65 times per hour-long window. ``auto`` measures
   that ratio from the bot's own inter-turn gaps and picks; where there is not
   enough data to size it, ``auto`` declines and changes nothing.
3. :func:`classify_rotation` — why sessions rotate. Rotating costs a full
   prefix write, so a bot that opens a new session per message pays the fixed
   prefix on every message no matter how good its cache settings are. The
   rotation rule Evolve owns is ``agents.defaults.session.reset.idleMinutes``
   (gap-filled to 120 by ``deploy.py``); this says whether the observed
   rotation is that rule firing or something else minting session ids.

Everything here is a pure function over records except :func:`measure_bot`,
which reads the turn files, and :func:`receipt_lines`, which calls it. Purity
is the point: the auto-retention decision is a config change, and a config
change whose reasoning cannot be replayed on a fixture is not one an operator
can argue with.

Deliberately NOT here: any write. This module measures and decides; the
materializer and the Cost page apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

_log = logging.getLogger(__name__)


# ── The provider's cache, in numbers ─────────────────────────────────────────
# These describe Anthropic's prompt cache. Two older copies exist —
# ``routes_analytics``' TTL-recommendation closure (seconds) and
# ``context_census.CACHE_TTL_MINUTES`` (minutes) — and they are not merged
# here: both sit inside large, well-tested surfaces whose refactor is a
# bigger change than this chip should carry. What IS enforced is that they
# agree: ``tests/test_cache_shape_constant_parity.py`` fails if any copy
# drifts. That is the context-observability spec's own rule — "whenever two
# readers exist for one setting, assert they agree" — applied to the setting
# rather than to a promise about it.

#: Effective prompt-cache lifetime, seconds, per ``cacheRetention`` value.
#: ``None`` = unset, which inherits OpenClaw's default of "short".
CACHE_TTL_SECONDS: dict[str | None, int] = {"long": 3600, "short": 300, None: 300}

#: Price multipliers on the model's base input rate: a premium to WRITE a
#: cache entry scaled by how long it must live, a flat discount to READ one.
CACHE_WRITE_MULT: dict[str, float] = {"short": 1.25, "long": 2.00}
CACHE_READ_MULT = 0.10

#: Break-even re-write factor for the one-hour tier. With W_s writes under a
#: five-minute cache and W_l under a one-hour one (every non-write turn being
#: a read), "long" is cheaper exactly when
#: ``W_s / W_l > (2.00 - 0.10) / (1.25 - 0.10)`` ≈ 1.652. A bot whose turns
#: are HOURS apart breaks a one-hour cache as reliably as a five-minute one,
#: so W_s == W_l, the ratio is 1.0, and "long" buys nothing while charging
#: 2.00x on every write.
LONG_RETENTION_BREAKEVEN = (
    (CACHE_WRITE_MULT["long"] - CACHE_READ_MULT)
    / (CACHE_WRITE_MULT["short"] - CACHE_READ_MULT)
)

#: Minimum in-session gaps before the break-even arithmetic is trustworthy.
#: Below this the economics cannot be sized, and picking "long" blind is how a
#: heartbeat bot ends up paying the 2.00x premium for nothing.
MIN_GAPS_FOR_ECONOMICS = 10

#: Minimum days of window before ``auto`` will decide anything.
MIN_WINDOW_DAYS_FOR_AUTO = 3

#: How far past the break-even a NEW decision must sit before it is allowed
#: to overturn the tier already deployed. A bot parked at the threshold —
#: 1.71 one week, 1.58 the next as a weekend rolls out of the window — would
#: otherwise flip every deploy, and each flip invalidates the live cache and
#: re-primes it at full price: the exact cost the tier exists to avoid.
AUTO_FLIP_MARGIN = 0.15

#: A decision older than this may be overturned without clearing the margin.
#: Traffic shape genuinely changes; the margin is there to damp noise, not to
#: freeze a bot on last month's pattern.
AUTO_FLIP_MIN_DWELL_DAYS = 7

#: How much history ``auto`` reads to size the break-even, and the default
#: for every measurement entry point here. A week: long enough to cover a
#: weekday/weekend split, short enough that a bot whose usage pattern changed
#: is followed rather than remembered. Callers do NOT re-declare it — the
#: deploy-time resolver and the Cost page must answer the same question over
#: the same window, and two copies of the number is how they stop doing that.
AUTO_TIER_WINDOW_DAYS = 7

#: A turn writing more cache than this re-warmed the prefix rather than
#: touching it. Matches ``context_census.DEFAULT_PREFIX_FLOOR_TOKENS`` — the
#: same floor the cold-miss definition uses, so the two surfaces agree on what
#: counts as a miss.
PREFIX_REWARM_FLOOR_TOKENS = 1000


def is_prefix_rewarm(
    read_tokens: int, write_tokens: int, floor: int = PREFIX_REWARM_FLOOR_TOKENS,
) -> bool:
    """THE re-warm predicate. Both publishing surfaces call this one.

    ``read == 0 and write > floor`` — the cold-miss definition
    ``context_census.cache_report`` already shipped, adopted here so the
    weekly receipt and the context-efficiency census cannot report different
    counts for the same event. Before this was shared, ``cross_turn_cache``
    used ``write > floor`` alone: a record that wrote 45k AND read 3k was a
    re-warm on the receipt and a hit in the census, and an operator holding
    both had no way to tell which was broken.

    The stricter form reads a nonzero cache_read as evidence that SOME cached
    prefix survived — so it counts only the unambiguous case, a write from
    cold. Where a record aggregates several model calls, a later call's read
    lands in the same field and suppresses the re-warm; that is the cost of
    having one definition rather than two, and it errs toward under-counting
    rather than toward the 100%-of-nothing inversion.
    """
    return read_tokens == 0 and write_tokens > floor


#: A turn writing more cache than it read, above this many written tokens, is
#: CACHE THRASH. Deliberately far above :data:`PREFIX_REWARM_FLOOR_TOKENS`:
#: thrash is judged on every turn including a session's first, and a first
#: turn's priming write (read 0, write one prefix — ~45k on the measured day,
#: internal/finding-cost-forensics-power-bot-2026-09-04.md §2) must not read as
#: thrash. Matches ``breakers.audit_generator``'s cache-write floor.
THRASH_FLOOR_TOKENS = 100_000


def is_cache_thrash(
    read_tokens: int, write_tokens: int, floor: int = THRASH_FLOOR_TOKENS,
) -> bool:
    """The CACHE-THRASH predicate — not a re-warm, and never counted as one.

    ``write > read and write > floor``. :func:`is_prefix_rewarm` stays the
    cold-miss definition (one prefix rewritten from nothing, ``read == 0``).
    Thrash is the shape that definition cannot represent: a turn record that
    aggregates many model calls, each re-caching a churning tail on top of a
    prefix the others read. The later reads land in the same field and
    suppress the re-warm, so the 2026-09-17 turn (read 5,382,819, write
    8,186,626 across 118 calls —
    internal/incident-post-mortem-2026-09-17-single-turn-cache-thrash.md §5)
    scored as a cache HIT while costing 81% of the day.

    Named differently on every surface from the re-warm: two predicates under
    one name is the defect ``hold-fix-4146-two-definitions-of-a-re-warm``
    exists to remove, so this adds a second question, not a second answer.
    """
    return write_tokens > read_tokens and write_tokens > floor


#: Fewer per-call rows than this cannot separate a genuine pin from ordinary
#: two-call noise (a session's first two calls can share a cache-read figure
#: by coincidence; three ruling it out is the same bar
#: :data:`MIN_GAPS_FOR_ECONOMICS` sets for the retention decision — "a small
#: sample can't be trusted", stated once per shape of claim).
IMAGE_TURN_CACHE_PIN_MIN_CALLS = 3


def image_turn_cache_pin(calls: Sequence[dict]) -> tuple[str, str]:
    """``(verdict, reason)`` for one run's image-turn cache-pin defect.

    Motivated by
    ``internal/incident-post-mortem-2026-09-20-image-turn-cache-thrash-and-poll-loop.md``
    §3: OpenClaw 2026.9.2 re-materializes an inbound-media user message fresh
    on every single provider call within a run (never memoized, never
    written back), and the rebuild is not byte-stable — so every call after
    the first re-writes the whole conversation past the system prompt at
    full price. The observed shape is unmistakable and different from an
    ordinary cold cache: ``cache_read_tokens`` reads the SAME figure call
    after call while ``cache_write_tokens`` climbs every call (one tool
    round's worth added each time), where a healthy run's reads grow as the
    conversation grows and re-warms are rare, isolated spikes.

    ``calls`` is one run's per-call usage records **in call order**, each a
    dict carrying (at minimum) ``cache_read_tokens`` and
    ``cache_write_tokens`` — the per-call shape ``message.usage`` carries in
    OpenClaw's own ``transcript_events``, the same fields the queued
    ``per-call-cache-figures-on-the-turn-record`` brief will land on the turn
    record. Import that reader once it ships rather than re-deriving these
    rows; this function only ever takes them.

    This predicate does NOT exclude a run's own priming call for you — unlike
    :func:`is_prefix_rewarm`, which is hard-coded to skip a session's first
    turn, callers here are expected to pass the slice they mean to test (an
    aggregator building a per-run view should drop the first call, whose
    cold ``cache_read_tokens == 0`` is the priming write and not evidence
    either way, exactly as :func:`cross_turn_cache` drops a session's first
    turn before ever calling :func:`is_prefix_rewarm`). Keeping the filtering
    at the caller/aggregation layer, not here, is this module's existing
    split — see the module docstring's item 1.

    Verdicts:

    ``"unknown"``
        Fewer than :data:`IMAGE_TURN_CACHE_PIN_MIN_CALLS` calls were given,
        or any call's cache fields could not be read (missing, ``None``, or
        unparseable) — the tri-state "could not look" contract
        (``docs/principle-tri-state-status.md``), never silently folded into
        "not pinned".
    ``"pinned"``
        Some run of :data:`IMAGE_TURN_CACHE_PIN_MIN_CALLS` or more
        CONSECUTIVE calls shares one ``cache_read_tokens`` figure while
        ``cache_write_tokens`` strictly grows call over call within that
        run — the post-mortem's §3 shape (46,007 pinned for 254 consecutive
        calls, writes climbing 7,702 -> 186,686). The match is a
        longest-consecutive-run search, not a whole-sequence requirement:
        a real turn can HEAL mid-run once a later user-role message (e.g. a
        subagent completion event) becomes the active one — reads jump back
        up and writes fall to hundreds of tokens for the remaining calls
        (§3's own seq 1121-1124) — and the calls before that point still
        document the defect. Verified against live pod data during this
        chip's build: a whole-sequence equality check under-counted both of
        the post-mortem's own incident turns (they end in a short healed
        tail), which is why this is a substring search rather than an
        ``all()`` over the full input.
    ``"not_pinned"``
        No run of :data:`IMAGE_TURN_CACHE_PIN_MIN_CALLS` calls shares a read
        figure with growing writes — the ordinary cache-hit shape, where
        reads grow as the conversation grows and re-warms are rare, isolated
        spikes.
    """
    if len(calls) < IMAGE_TURN_CACHE_PIN_MIN_CALLS:
        return "unknown", (
            f"{len(calls)} call(s) provided — needs "
            f"{IMAGE_TURN_CACHE_PIN_MIN_CALLS} to tell a pin from ordinary "
            f"two-call noise."
        )
    reads: list[int] = []
    writes: list[int] = []
    for call in calls:
        read = _to_int_or_none(call.get("cache_read_tokens"))
        write = _to_int_or_none(call.get("cache_write_tokens"))
        if read is None or write is None:
            return "unknown", "a call's cache fields could not be read."
        reads.append(read)
        writes.append(write)

    # Longest run of consecutive calls that share one cache-read figure while
    # cache-write strictly grows call over call. ``run_start`` anchors the
    # read figure the current run is pinned at; a call breaks the run (starts
    # a fresh one AT that call) the moment either condition fails.
    best_len = 1
    best_start = 0
    run_start = 0
    for i in range(1, len(reads)):
        if reads[i] != reads[run_start] or writes[i] <= writes[i - 1]:
            run_start = i
        run_len = i - run_start + 1
        if run_len > best_len:
            best_len, best_start = run_len, run_start

    if best_len >= IMAGE_TURN_CACHE_PIN_MIN_CALLS:
        pinned_read = reads[best_start]
        best_end = best_start + best_len - 1
        return "pinned", (
            f"cache read pinned at {pinned_read:,} tokens across "
            f"{best_len} consecutive calls (of {len(calls)} given) while "
            f"cache write grew {writes[best_start]:,} -> {writes[best_end]:,} "
            f"tokens — those calls re-wrote the conversation after the "
            f"system prompt (post-mortem 2026-09-20 §3)."
        )
    return "not_pinned", (
        "no run of consecutive calls shared a cache-read figure with "
        "growing writes — the ordinary cache-hit shape."
    )


#: The values ``per_bot_cache_retention`` accepts. ``"auto"`` resolves through
#: :func:`resolve_auto_retention`; ``None`` means "no override" and is left
#: alone (OpenClaw's own default applies).
RETENTION_VALUES = ("short", "long", "auto")

#: Operator-facing labels. ``"5m"`` / ``"1h"`` are the brief's spelling of the
#: two tiers; ``short`` / ``long`` are OpenClaw's field values.
RETENTION_LABEL: dict[str | None, str] = {
    "short": "5m",
    "long": "1h",
    "auto": "auto",
    None: "unset (5m default)",
}

#: The session-rotation knob Evolve owns, and the value ``deploy.py`` gap-fills
#: when a bot has none. Named here so the rotation classifier and any operator
#: text quote one string.
ROTATION_KNOB = "agents.defaults.session.reset.idleMinutes"
DEFAULT_IDLE_RESET_MINUTES = 120


# ── Gap distribution ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GapStats:
    """Inter-turn gaps for one bot over one window.

    Gaps are measured between consecutive turns WITHIN a session, never
    across a session boundary: a new session starts cold by definition, and
    counting the wall-clock hole before it as a "gap the cache failed to
    survive" would blame the cache for a rotation.

    ``writes_short`` / ``writes_long`` are the number of cache writes the
    window would have cost under each tier — one per session (the priming
    write) plus one per gap that outlives that tier's window.

    ``window_days`` is the OBSERVED span — whole days between the earliest
    and the latest dated record actually read — never the number of days the
    caller asked for. Echoing the request is how the "needs 3 days of
    history" gate became unreachable: every production caller takes the
    default 7-day window, so ``window_days`` read 7 for a bot deployed this
    morning and the gate never fired.
    """

    window_days: int = 0
    session_count: int = 0
    gaps: tuple[float, ...] = ()
    writes_short: int = 0
    writes_long: int = 0

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    @property
    def p50_gap_seconds(self) -> float:
        return _nearest_rank(self.gaps, 0.5)

    @property
    def p95_gap_seconds(self) -> float:
        """Nearest-rank 0.95 — on a sample this size usually just the largest
        gap. Kept in the metrics dict; deliberately absent from the reasoning
        strings an operator reads (see :func:`_nearest_rank`)."""
        return _nearest_rank(self.gaps, 0.95)

    @property
    def rewrite_factor(self) -> float:
        """``writes_short / writes_long`` — the ratio the break-even tests.

        1.0 when the one-hour cache would not avoid a single write. Defined
        as 1.0 rather than undefined when there are no writes at all, so a
        silent bot reads as "long buys nothing" rather than as a divide-by-
        zero the caller has to special-case.
        """
        return (self.writes_short / self.writes_long) if self.writes_long else 1.0

    @property
    def has_economics(self) -> bool:
        return self.gap_count >= MIN_GAPS_FOR_ECONOMICS

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "session_count": self.session_count,
            "gap_count": self.gap_count,
            "p50_gap_seconds": round(self.p50_gap_seconds, 1),
            "p95_gap_seconds": round(self.p95_gap_seconds, 1),
            "writes_under_short_cache": self.writes_short,
            "writes_under_long_cache": self.writes_long,
            "rewrite_factor": round(self.rewrite_factor, 3),
            "long_retention_breakeven": round(LONG_RETENTION_BREAKEVEN, 3),
        }


def _nearest_rank(sorted_values: Sequence[float], q: float) -> float:
    """The value at nearest rank ``q`` — an INDEX PICK, not an interpolated
    percentile.

    ``idx = min(int(len * q), len - 1)``, so on small samples the high
    quantiles collapse onto the maximum: with 14 gaps, ``q=0.95`` picks index
    13, the largest value. That is fine for the median and for reasoning
    about shape, and it is why the high quantile is kept out of the
    operator-facing reasoning strings — presenting the largest gap in a
    fourteen-gap sample as a 95th percentile would overstate what was
    measured. Named for what it does so no caller mistakes it for one.
    """
    if not sorted_values:
        return 0.0
    idx = min(int(len(sorted_values) * q), len(sorted_values) - 1)
    return sorted_values[idx]


def _parse_ts(raw: object) -> datetime | None:
    """Parse an ISO-8601 instant, tolerating a trailing ``Z`` and a naive
    stamp (read as UTC). ``None`` on anything else — a record we cannot place
    in time contributes no gap rather than a wrong one."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def gap_stats(records: Iterable[dict]) -> GapStats:
    """Build a :class:`GapStats` from any records carrying ``session_id`` and
    ``ts``. Source-agnostic on purpose: cost events (per model call) and turn
    records (per turn) both fit, and the caller decides which billing unit the
    question is about.

    Records missing either field are skipped. A session with one record
    contributes its priming write but no gap.

    ``window_days`` comes out of the records, not out of a parameter: it is
    ``max(ts) - min(ts)`` across every dated record, floored to whole days.
    There is deliberately no way for a caller to assert a history it does not
    have.
    """
    by_session: dict[str, list[datetime]] = {}
    for rec in records:
        sid = rec.get("session_id")
        ts = _parse_ts(rec.get("ts"))
        if not isinstance(sid, str) or not sid or ts is None:
            continue
        by_session.setdefault(sid, []).append(ts)

    gaps: list[float] = []
    for stamps in by_session.values():
        stamps.sort()
        for i in range(1, len(stamps)):
            delta = (stamps[i] - stamps[i - 1]).total_seconds()
            if delta >= 0:
                gaps.append(delta)
    gaps.sort()

    dated = [ts for stamps in by_session.values() for ts in stamps]
    span_days = (
        int((max(dated) - min(dated)).total_seconds() // 86400) if dated else 0
    )

    sessions = len(by_session)
    short_ttl = CACHE_TTL_SECONDS["short"]
    long_ttl = CACHE_TTL_SECONDS["long"]
    return GapStats(
        window_days=span_days,
        session_count=sessions,
        gaps=tuple(gaps),
        writes_short=sessions + sum(1 for g in gaps if g > short_ttl),
        writes_long=sessions + sum(1 for g in gaps if g > long_ttl),
    )


# ── auto: which tier the evidence buys ───────────────────────────────────────


@dataclass(frozen=True)
class AutoDecision:
    """What ``auto`` resolved to, and the sentence that says why.

    ``retention`` is ``None`` when ``auto`` DECLINED — not enough window, not
    enough gaps. Declining leaves the knob unwritten, which is the behavior a
    pod has today, so an undecidable bot changes nothing rather than
    defaulting into a premium it cannot be shown to earn.
    """

    retention: str | None
    reason: str
    stats: GapStats = field(default_factory=GapStats)

    @property
    def decided(self) -> bool:
        return self.retention is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "retention": self.retention,
            "label": RETENTION_LABEL[self.retention],
            "reason": self.reason,
            "metrics": self.stats.as_dict(),
        }


def _fmt_dur(seconds: float) -> str:
    """Human-readable duration for the reasoning strings."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes}m" if not rest else f"{minutes}m {rest}s"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


def resolve_auto_retention(stats: GapStats) -> AutoDecision:
    """Pick the cache tier the measured gap distribution pays for.

    The rule is the break-even, not a gap threshold. A median gap "between 5
    and 60 minutes" is the shape that motivates the one-hour tier, but it is
    the wrong TEST for it: a bot with a 6-minute median and a 4-hour p95
    breaks both caches on the same turns, and a bot with a 30-second median
    and a 20-minute p95 clears the break-even without its median entering the
    band at all. What decides is how many writes the longer window actually
    avoids, which is :attr:`GapStats.rewrite_factor` against
    :data:`LONG_RETENTION_BREAKEVEN`.

    Declines (``retention=None``) rather than guessing when the window is too
    short or there are too few gaps to size the ratio.
    """
    if stats.window_days < MIN_WINDOW_DAYS_FOR_AUTO:
        return AutoDecision(
            None,
            f"auto declined: {stats.window_days}d of history, "
            f"needs {MIN_WINDOW_DAYS_FOR_AUTO}d before it will move a cache tier.",
            stats,
        )
    if not stats.has_economics:
        return AutoDecision(
            None,
            f"auto declined: {stats.gap_count} inter-turn gaps over "
            f"{stats.window_days}d, needs {MIN_GAPS_FOR_ECONOMICS} to size whether a "
            f"1h cache would pay for itself. Leaving the tier unset.",
            stats,
        )

    factor = stats.rewrite_factor
    # No high quantile here: ``_nearest_rank`` is an index pick, and on the
    # ten-to-thirty gaps this decision typically sees a "p95" is just the
    # largest gap wearing a statistic's name. The write counts already carry
    # the shape the decision turns on.
    common = (
        f"{stats.writes_short} writes under a 5m cache vs {stats.writes_long} under a "
        f"1h one ({factor:.1f}x re-write factor, break-even "
        f"~{LONG_RETENTION_BREAKEVEN:.2f}x); median gap "
        f"{_fmt_dur(stats.p50_gap_seconds)} over {stats.window_days}d of history"
    )
    if factor > LONG_RETENTION_BREAKEVEN:
        return AutoDecision(
            "long",
            f"auto chose 1h: {common}. The longer window avoids enough writes to "
            f"cover its {CACHE_WRITE_MULT['long']:.2f}x write premium.",
            stats,
        )
    return AutoDecision(
        "short",
        f"auto chose 5m: {common}. A 1h cache would expire on nearly the same turns "
        f"here and bill {CACHE_WRITE_MULT['long']:.2f}x instead of "
        f"{CACHE_WRITE_MULT['short']:.2f}x on every write for it.",
        stats,
    )


def clears_flip_margin(
    retention: str | None, stats: GapStats, *, margin: float = AUTO_FLIP_MARGIN,
) -> bool:
    """Does ``retention`` sit far enough past the break-even to overturn the
    tier already deployed?

    ``"long"`` needs ``rewrite_factor >= breakeven * (1 + margin)``;
    ``"short"`` needs ``rewrite_factor <= breakeven / (1 + margin)``. Anything
    inside that band is a decision the arithmetic cannot separate from the one
    already in force, so the deployed tier stands. Declines (``None``) never
    clear — an undecidable measurement is not evidence to move on.
    """
    if retention not in ("short", "long"):
        return False
    factor = stats.rewrite_factor
    if retention == "long":
        return factor >= LONG_RETENTION_BREAKEVEN * (1.0 + margin)
    return factor <= LONG_RETENTION_BREAKEVEN / (1.0 + margin)


def resolve_retention(
    configured: str | None, stats: GapStats | None,
) -> AutoDecision:
    """Resolve a configured ``per_bot_cache_retention`` to what to deploy.

    ``"short"`` / ``"long"`` pass straight through — an explicit operator
    choice is not re-litigated against the data. ``"auto"`` goes to
    :func:`resolve_auto_retention`. ``None`` (no override) stays ``None``:
    unset is unset, and turning "the operator has not chosen" into a measured
    choice on the next deploy is a fleet-wide config change nobody asked for.
    """
    if configured in ("short", "long"):
        return AutoDecision(
            configured,
            f"cache tier pinned to {RETENTION_LABEL[configured]} by the operator.",
            stats or GapStats(),
        )
    if configured == "auto":
        if stats is None:
            return AutoDecision(
                None,
                "auto declined: could not measure this bot's inter-turn gaps "
                "(turns unreadable). Leaving the tier unset.",
                GapStats(),
            )
        return resolve_auto_retention(stats)
    return AutoDecision(
        None, "no cache tier set; OpenClaw's 5m default applies.", stats or GapStats(),
    )


# ── Cross-turn cache, the honest number ──────────────────────────────────────


@dataclass(frozen=True)
class CrossTurnCache:
    """Prefix re-warms and the cross-turn hit rate over a set of turns.

    ``eligible`` excludes each session's first turn: that turn has no cached
    prefix to re-read and its write is the priming write, not a re-warm.

    ``unreadable`` counts eligible-position turns whose cache fields could
    not be read at all (``cache_write_tokens`` absent, ``None`` or
    unparseable). Those are excluded from ``eligible`` rather than scored as
    zero-write successes: a week whose records predate the field, or during
    which the provider stopped reporting cache usage, must read as UNMEASURED
    and not as a perfect week. This is the module's own ``None`` means
    "could not look" contract, enforced at field level as well as at record
    level.

    ``thrash_turns`` / ``thrash_write_tokens`` / ``thrash_max_write_tokens``
    count :func:`is_cache_thrash` over EVERY readable turn, first-of-session
    included (thrash is intra-turn, so session position does not excuse it).
    Reported by magnitude: an 8.19M-token turn and a 150k one are one turn
    each, and a count alone would render them the same.
    """

    turns: int = 0
    sessions: int = 0
    eligible: int = 0
    rewarms: int = 0
    rewarm_tokens: int = 0
    active_hours: float = 0.0
    unreadable: int = 0
    thrash_turns: int = 0
    thrash_write_tokens: int = 0
    thrash_max_write_tokens: int = 0

    @property
    def hit_rate(self) -> float | None:
        """Share of eligible turns that re-read the prefix. ``None`` when
        there were no eligible turns — never 0.0, which would read as "the
        cache missed every time"."""
        if not self.eligible:
            return None
        return (self.eligible - self.rewarms) / self.eligible

    @property
    def rewarms_per_active_hour(self) -> float | None:
        """Re-warms per hour of ACTIVE time (the span each session covered,
        summed), not per wall-clock hour. A bot idle for twenty hours has not
        earned a low rate by being asleep."""
        if self.active_hours <= 0:
            return None
        return self.rewarms / self.active_hours

    def as_dict(self) -> dict[str, Any]:
        rate = self.hit_rate
        per_hour = self.rewarms_per_active_hour
        return {
            "turns": self.turns,
            "sessions": self.sessions,
            "eligible_turns": self.eligible,
            "prefix_rewarms": self.rewarms,
            "prefix_rewarm_tokens": self.rewarm_tokens,
            "cross_turn_hit_rate": None if rate is None else round(rate, 4),
            "active_hours": round(self.active_hours, 2),
            "rewarms_per_active_hour": None if per_hour is None else round(per_hour, 2),
            "rewarm_floor_tokens": PREFIX_REWARM_FLOOR_TOKENS,
            "unreadable_turns": self.unreadable,
            "cache_thrash_turns": self.thrash_turns,
            "cache_thrash_write_tokens": self.thrash_write_tokens,
            "cache_thrash_max_write_tokens": self.thrash_max_write_tokens,
            "cache_thrash_floor_tokens": THRASH_FLOOR_TOKENS,
        }


def cross_turn_cache(
    turns: Iterable[dict], *, floor: int = PREFIX_REWARM_FLOOR_TOKENS,
) -> CrossTurnCache:
    """Count prefix re-warms across TURN records (``turns-*.jsonl`` shape).

    Counted per turn, not token-weighted. Token weighting is what produced
    the finding's 79-87% on a day whose every turn re-wrote the prefix: one
    turn's thirteen internal calls read the prefix twelve times and wrote it
    once, and the twelve reads outweigh the write in any token ratio while
    the write is the whole cost.

    Turn records carry an aggregate of that turn's calls, so a re-warm shows
    as a large ``cache_write_tokens`` on the turn — regardless of how much
    ``cache_read_tokens`` its later calls also accumulated. The floor keeps a
    small incremental write (the conversation's own growth being cached
    forward) from reading as a full prefix rewrite.
    """
    by_session: dict[str, list[tuple[datetime | None, dict]]] = {}
    counted = 0
    for rec in turns:
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        counted += 1
        by_session.setdefault(sid, []).append((_parse_ts(rec.get("ts")), rec))

    eligible = rewarms = rewarm_tokens = unreadable = 0
    thrash_turns = thrash_tokens = thrash_max = 0
    active_seconds = 0.0
    for rows in by_session.values():
        # Undated rows sort last and are never treated as the session's first
        # turn — "I could not place this in time" must not silently become
        # "this was the priming write", which would hide a re-warm.
        rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min.replace(tzinfo=timezone.utc)))
        stamps = [ts for ts, _ in rows if ts is not None]
        if len(stamps) >= 2:
            active_seconds += (stamps[-1] - stamps[0]).total_seconds()
        for idx, (_ts, rec) in enumerate(rows):
            written = _to_int_or_none(rec.get("cache_write_tokens"))
            if written is not None and is_cache_thrash(
                _to_int(rec.get("cache_read_tokens")), written,
            ):
                thrash_turns += 1
                thrash_tokens += written
                thrash_max = max(thrash_max, written)
            if idx == 0:
                continue
            if written is None:
                # Could not look. Not a hit, not a miss — and above all not
                # counted, because a missing field scored as a zero write
                # reports an unmeasured week as a 100% one.
                unreadable += 1
                continue
            eligible += 1
            if is_prefix_rewarm(_to_int(rec.get("cache_read_tokens")), written, floor):
                rewarms += 1
                rewarm_tokens += written

    return CrossTurnCache(
        turns=counted,
        sessions=len(by_session),
        eligible=eligible,
        rewarms=rewarms,
        rewarm_tokens=rewarm_tokens,
        active_hours=active_seconds / 3600.0,
        unreadable=unreadable,
        thrash_turns=thrash_turns,
        thrash_write_tokens=thrash_tokens,
        thrash_max_write_tokens=thrash_max,
    )


def _to_int_or_none(value: object) -> int | None:
    """Token counts as written by the plugin: an int, or a string on an older
    record. ``None`` when the field is absent, null, or unparseable — the
    tri-state the callers need to tell "zero tokens" from "could not look"
    (docs/principle-tri-state-status.md)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _to_int(value: object) -> int:
    """:func:`_to_int_or_none` with unreadable folded to 0. For the fields
    where absence genuinely is zero (a record that reports no cache read
    read nothing); never for the field the re-warm count turns on."""
    parsed = _to_int_or_none(value)
    return 0 if parsed is None else parsed


# ── Session rotation, made explicit ──────────────────────────────────────────


@dataclass(frozen=True)
class RotationStats:
    """How often sessions rotate, and whether the idle rule explains it."""

    sessions: int = 0
    turns: int = 0
    single_turn_sessions: int = 0
    #: Longest in-session gap observed, seconds. If the idle rule were doing
    #: the rotating, no in-session gap could exceed ``idleMinutes``.
    max_in_session_gap_seconds: float = 0.0
    median_in_session_gap_seconds: float = 0.0

    @property
    def turns_per_session(self) -> float | None:
        return (self.turns / self.sessions) if self.sessions else None

    @property
    def single_turn_share(self) -> float | None:
        return (self.single_turn_sessions / self.sessions) if self.sessions else None


#: A pod where this share of sessions carry exactly one turn is not having
#: conversations — it is minting a session per message.
SINGLE_TURN_SESSION_SHARE_ALERT = 0.5


def rotation_stats(turns: Iterable[dict]) -> RotationStats:
    """Per-session turn counts and in-session gaps from turn records."""
    by_session: dict[str, list[datetime]] = {}
    counted = 0
    for rec in turns:
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        counted += 1
        ts = _parse_ts(rec.get("ts"))
        stamps = by_session.setdefault(sid, [])
        if ts is not None:
            stamps.append(ts)

    single = sum(1 for s in by_session.values() if len(s) <= 1)
    gaps: list[float] = []
    for stamps in by_session.values():
        stamps.sort()
        for i in range(1, len(stamps)):
            delta = (stamps[i] - stamps[i - 1]).total_seconds()
            if delta >= 0:
                gaps.append(delta)
    gaps.sort()
    return RotationStats(
        sessions=len(by_session),
        turns=counted,
        single_turn_sessions=single,
        max_in_session_gap_seconds=gaps[-1] if gaps else 0.0,
        median_in_session_gap_seconds=_nearest_rank(gaps, 0.5),
    )


def classify_rotation(
    stats: RotationStats, idle_minutes: int | None,
) -> tuple[str, str]:
    """``(verdict, reason)`` for why this bot's sessions rotate.

    Verdicts:

    ``insufficient_data``
        Fewer than two sessions; nothing to say.
    ``per_message``
        Most sessions carry exactly one turn while the in-session gaps that
        DO exist sit comfortably inside the idle window. The idle rule cannot
        be doing this — something upstream is handing OpenClaw a fresh
        session id per message. ``evo.proxy.derive_session_id`` already logs
        a warning on its own uuid4 fallback; that log is where to look next.
    ``idle_rotation``
        Sessions carry several turns and the idle window is short enough to
        be plausibly cutting conversations. Rotating costs a full prefix
        write, so a window below the observed gaps is a per-message rewrite
        wearing a different name.
    ``healthy``
        Sessions accumulate turns and the idle window is comfortably wider
        than the gaps between them.
    """
    idle = idle_minutes if isinstance(idle_minutes, int) and idle_minutes > 0 else None
    idle_seconds = idle * 60 if idle is not None else None
    knob = f"{ROTATION_KNOB} = {idle if idle is not None else 'unset'}"

    if stats.sessions < 2:
        return "insufficient_data", (
            f"{stats.sessions} session(s) in the window — not enough to say how "
            f"rotation behaves. ({knob}.)"
        )

    share = stats.single_turn_share or 0.0
    # An UNKNOWN idle window cannot be ruled out, so it must not be read as
    # "the idle rule is innocent". deploy.py gap-fills the knob on every bot,
    # so this is the rare case, and staying quiet about a bot whose rotation
    # rule we cannot see beats naming the wrong cause.
    inside_window = (
        idle_seconds is not None
        and stats.max_in_session_gap_seconds < idle_seconds
    )
    if share >= SINGLE_TURN_SESSION_SHARE_ALERT and inside_window:
        return "per_message", (
            f"{int(share * 100)}% of sessions carry a single turn "
            f"({stats.single_turn_sessions} of {stats.sessions}), and no in-session "
            f"gap reached the idle window ({knob}; longest in-session gap "
            f"{_fmt_dur(stats.max_in_session_gap_seconds)}). The idle rule is not "
            f"what is rotating these — something upstream is minting a session id "
            f"per message, and each one pays the fixed prefix again. Check the "
            f"gateway log for evo.proxy.derive_session_id's uuid4-fallback warning."
        )
    if (
        idle_seconds is not None
        and stats.median_in_session_gap_seconds > 0
        and idle_seconds <= stats.median_in_session_gap_seconds * 2
    ):
        return "idle_rotation", (
            f"The idle window ({knob}) is within 2x the median in-session gap "
            f"({_fmt_dur(stats.median_in_session_gap_seconds)}), so ordinary pauses "
            f"rotate the session. Each rotation is a full prefix write; widen the "
            f"window, or accept the rewrite deliberately."
        )
    return "healthy", (
        f"{stats.turns} turns over {stats.sessions} sessions "
        f"({(stats.turns_per_session or 0):.1f} per session), median in-session gap "
        f"{_fmt_dur(stats.median_in_session_gap_seconds)} against {knob}. Sessions "
        f"are accumulating turns rather than rotating per message."
    )


# ── Reading the pod ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BotCacheShape:
    """One bot's measured cache shape. ``None`` fields mean "could not look",
    never "zero" — the drift-monitor contract (unreadable is not clean)."""

    bot_id: str
    window_days: int
    gaps: GapStats | None = None
    cross_turn: CrossTurnCache | None = None
    rotation: RotationStats | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "window_days": self.window_days,
            "gaps": self.gaps.as_dict() if self.gaps else None,
            "cross_turn": self.cross_turn.as_dict() if self.cross_turn else None,
        }


def load_turn_records(
    bot_id: str, *, days: int = AUTO_TIER_WINDOW_DAYS,
    now: datetime | None = None, network_path: str | None = None,
) -> list[dict] | None:
    """Turn records for ``bot_id`` over the trailing ``days`` UTC files.

    ``None`` on any read failure — the caller must be able to tell "no turns"
    from "I could not look at the turns", because they mean opposite things
    for a config decision.

    The window is UTC because the turn FILENAMES are (see
    ``usage_analytics.load_turns``); nothing here buckets by a pod-local day,
    so the storage calendar is the right one.
    """
    try:
        from usage_analytics import load_turns  # type: ignore[import]
    except Exception as exc:  # noqa: BLE001 — analyzer path unavailable
        _log.warning("cache_shape: usage_analytics unavailable (%s)", exc)
        return None
    try:
        rows = load_turns(
            bot_id, days=days, end_date=now, network_path=network_path,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("cache_shape: load_turns(%s) raised: %s", bot_id, exc)
        return None
    return [r for r in rows if isinstance(r, dict)]


def measure_bot(
    bot_id: str, *, days: int = AUTO_TIER_WINDOW_DAYS,
    now: datetime | None = None, network_path: str | None = None,
) -> BotCacheShape:
    """Measure one bot's gap distribution, cross-turn cache and rotation.

    Reads the turn records once and derives all three from them, so the three
    numbers on the Cost page and in the receipt describe the same window and
    the same rows. Soft-fails to a shape with ``None`` members.

    TURN records, not per-call cost events, are the source. For the cache
    question that is the right grain in both directions: the calls inside one
    agentic turn are seconds apart and would never break either cache window,
    so folding them in adds rows that can only dilute the ratio, and it is
    exactly that dilution the finding caught (§2 — a token-weighted per-call
    hit rate of 79-87% on a day whose every turn re-warmed from cold).
    """
    rows = load_turn_records(bot_id, days=days, now=now, network_path=network_path)
    if rows is None:
        return BotCacheShape(bot_id=bot_id, window_days=days)
    return BotCacheShape(
        bot_id=bot_id,
        window_days=days,
        gaps=gap_stats(rows),
        cross_turn=cross_turn_cache(rows),
        rotation=rotation_stats(rows),
    )


def read_idle_reset_minutes(bot_id: str) -> int | None:
    """This bot's deployed ``session.reset.idleMinutes``, or ``None``.

    Reads the DEPLOYED value (openclaw.json), not an intent recorded
    somewhere else: the rotation the turns show is the rule the bot was
    actually running while they were produced.
    """
    try:
        from cost_profiles import read_openclaw_cost_settings  # type: ignore[import]
        settings = read_openclaw_cost_settings(bot_id) or {}
    except Exception as exc:  # noqa: BLE001
        _log.warning("cache_shape: reading idle-reset for %s failed: %s", bot_id, exc)
        return None
    value = ((settings.get("session") or {}).get("reset") or {}).get("idleMinutes")
    return value if isinstance(value, int) and value > 0 else None


def measure_gap_stats(
    bot_id: str, *, days: int = AUTO_TIER_WINDOW_DAYS,
    now: datetime | None = None, network_path: str | None = None,
) -> GapStats | None:
    """Just the gap distribution — what the materializer needs to resolve
    ``auto``. ``None`` when the turns could not be read, which
    :func:`resolve_retention` renders as a decline."""
    return measure_bot(
        bot_id, days=days, now=now, network_path=network_path,
    ).gaps


# ── The receipt ──────────────────────────────────────────────────────────────

#: How many days the receipt's cache lines cover. The same week the ``auto``
#: resolver reads, so the receipt's re-warm figure and the tier decision
#: describe the same stretch of traffic.
RECEIPT_WINDOW_DAYS = AUTO_TIER_WINDOW_DAYS

#: The definition-of-done threshold, and the trigger for the per-bot receipt
#: line. One re-warm per hour of ACTIVE time is what a correctly-sized cache
#: tier buys on conversational traffic; a bot above it is paying the write
#: premium on gaps its tier should have covered.
REWARMS_PER_ACTIVE_HOUR_TARGET = 1.0


def receipt_lines(
    members: Sequence[str], *, now: datetime | None = None,
    days: int = RECEIPT_WINDOW_DAYS, network_path: str | None = None,
) -> list[str]:
    """The lines the weekly receipt appends about the prompt cache's shape.

    Always at least one line. A receipt that omits the comparison when no
    turns were readable reads as "checked and fine" — the same silence the
    estimate-vs-bill line exists to end.

    Reports the CROSS-TURN hit rate, which is the number the finding says the
    intra-turn figure was hiding, and the re-warm rate per active hour, which
    is the thing an operator can act on.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    total = CrossTurnCache()
    read_any = False
    unreadable: list[str] = []
    rotation_notes: list[str] = []
    hot_bots: list[str] = []
    thrash_bots: list[tuple[int, str]] = []
    for bot_id in members:
        shape = measure_bot(
            bot_id, days=days, now=stamp, network_path=network_path,
        )
        if shape.cross_turn is None:
            unreadable.append(bot_id)
            continue
        read_any = True
        if shape.rotation is not None:
            verdict, reason = classify_rotation(
                shape.rotation, read_idle_reset_minutes(bot_id),
            )
            # Only the two verdicts an operator can act on. "healthy" and
            # "insufficient_data" are the silent majority and would bury the
            # one bot that is paying a prefix write per message.
            if verdict in ("per_message", "idle_rotation"):
                rotation_notes.append(f"  {bot_id}: {reason}")
        ct = shape.cross_turn
        # A pod total averages a bot that re-warms six times an hour against
        # three that are asleep, and the pod reads as fixed. The per-bot line
        # fires on exactly the definition-of-done threshold the total is
        # measured against, so the one bot paying cannot hide inside it.
        per_bot_rate = ct.rewarms_per_active_hour
        if per_bot_rate is not None and per_bot_rate > REWARMS_PER_ACTIVE_HOUR_TARGET:
            hot_bots.append(
                f"  {bot_id}: {per_bot_rate:.1f} prefix re-warms per active hour "
                f"({ct.rewarms} over {ct.active_hours:.1f}h of activity, "
                f"{ct.rewarm_tokens:,} tokens rewritten) — above the "
                f"{REWARMS_PER_ACTIVE_HOUR_TARGET:.0f}/h target"
            )
        if ct.thrash_turns:
            thrash_bots.append((ct.thrash_write_tokens, (
                f"  {bot_id}: {ct.thrash_turns} cache-thrash turns, "
                f"{ct.thrash_write_tokens:,} tokens written (largest "
                f"{ct.thrash_max_write_tokens:,})"
            )))
        total = CrossTurnCache(
            turns=total.turns + ct.turns,
            sessions=total.sessions + ct.sessions,
            eligible=total.eligible + ct.eligible,
            rewarms=total.rewarms + ct.rewarms,
            rewarm_tokens=total.rewarm_tokens + ct.rewarm_tokens,
            active_hours=total.active_hours + ct.active_hours,
            unreadable=total.unreadable + ct.unreadable,
            thrash_turns=total.thrash_turns + ct.thrash_turns,
            thrash_write_tokens=total.thrash_write_tokens + ct.thrash_write_tokens,
            thrash_max_write_tokens=max(
                total.thrash_max_write_tokens, ct.thrash_max_write_tokens,
            ),
        )

    lines: list[str] = []
    if not read_any:
        lines.append("cross-turn cache hit: turns unreadable — not measured this week")
        return lines

    rate = total.hit_rate
    if rate is None:
        lines.append(
            f"cross-turn cache hit: nothing to measure — every session in the last "
            f"{days}d was a single turn ({total.sessions} sessions)"
        )
    else:
        lines.append(
            f"cross-turn cache hit: {rate * 100:.0f}% of {total.eligible} follow-on "
            f"turns re-read the prefix (per turn, not token-weighted — the "
            f"token figure counts one turn's own calls reading each other)"
        )
    per_hour = total.rewarms_per_active_hour
    per_day = total.rewarms / days if days else 0.0
    if per_hour is None:
        lines.append(
            f"prefix re-warms: {total.rewarms} over {days}d "
            f"({per_day:.1f}/day; no active span to rate them against)"
        )
    else:
        lines.append(
            f"prefix re-warms: {per_day:.1f}/day, {per_hour:.1f} per active hour "
            f"({total.rewarms} over {days}d, {total.rewarm_tokens:,} tokens rewritten)"
        )
    if total.unreadable:
        # Said out loud, not folded into the rate. A turn whose cache fields
        # are missing is unmeasured, and a rate computed as if it were a
        # zero-write success reports a blind week as a perfect one.
        lines.append(
            f"  {total.unreadable} turns not counted — no cache fields"
        )
    # Thrash is a separate line, never folded into the re-warm count or the
    # hit rate: a turn that read 5.4M and wrote 8.2M is a "hit" by the re-warm
    # definition and the costliest turn on record by this one. Rendered by
    # tokens, largest first, so one 8M-token turn cannot read like one 150k.
    lines.append(
        f"cache thrash (turn wrote more than it read, >"
        f"{THRASH_FLOOR_TOKENS // 1000}k written): {total.thrash_turns} turns, "
        f"{total.thrash_write_tokens:,} tokens written"
        + (f", largest {total.thrash_max_write_tokens:,}" if total.thrash_turns else "")
    )
    lines.extend(line for _tok, line in sorted(thrash_bots, reverse=True))
    # Session rotation is upstream of every cache setting: a bot that opens a
    # new session per message pays the fixed prefix every message no matter
    # which tier it is on. Named here so the re-warm figure above has a cause
    # attached rather than being a number to stare at.
    lines.extend(hot_bots)
    lines.extend(rotation_notes)
    if unreadable:
        lines.append(
            f"  not counted: turns unreadable for {', '.join(sorted(unreadable))}"
        )
    return lines


__all__ = [
    "AUTO_FLIP_MARGIN",
    "AUTO_FLIP_MIN_DWELL_DAYS",
    "AUTO_TIER_WINDOW_DAYS",
    "AutoDecision",
    "BotCacheShape",
    "CACHE_READ_MULT",
    "CACHE_TTL_SECONDS",
    "CACHE_WRITE_MULT",
    "CrossTurnCache",
    "DEFAULT_IDLE_RESET_MINUTES",
    "GapStats",
    "IMAGE_TURN_CACHE_PIN_MIN_CALLS",
    "LONG_RETENTION_BREAKEVEN",
    "MIN_GAPS_FOR_ECONOMICS",
    "MIN_WINDOW_DAYS_FOR_AUTO",
    "PREFIX_REWARM_FLOOR_TOKENS",
    "REWARMS_PER_ACTIVE_HOUR_TARGET",
    "RETENTION_LABEL",
    "RETENTION_VALUES",
    "ROTATION_KNOB",
    "RotationStats",
    "SINGLE_TURN_SESSION_SHARE_ALERT",
    "THRASH_FLOOR_TOKENS",
    "classify_rotation",
    "clears_flip_margin",
    "cross_turn_cache",
    "gap_stats",
    "image_turn_cache_pin",
    "is_cache_thrash",
    "is_prefix_rewarm",
    "load_turn_records",
    "measure_bot",
    "measure_gap_stats",
    "read_idle_reset_minutes",
    "receipt_lines",
    "resolve_auto_retention",
    "resolve_retention",
    "rotation_stats",
]

"""cascade.preflight_audit — grade pre-flight intent router decisions.

Reads cascade spans, categorizes each into one of six outcomes, and
computes per-bot rates that audit_runner emits as Signals + serves to
the operator UI.

Categories
----------

For every span where the pre-flight router RAN (i.e., user_turn trigger,
router enabled for the bot), classify the outcome:

  agreement         pre-flight picked the right tier; turn went as expected
  over_escalation   pre-flight picked tier1; turn was trivially handled
                    (short reply, no tool calls, low cost, success)
                    → wasted Opus pricing for a Sonnet/Haiku-suitable response
  under_escalation  pre-flight picked tier3; turn struggled
                    (failure flag, struggle features fired, OR cost spike)
                    → user might have done better on tier2/1
  cascade_corrected pre-flight picked tier2/3; cascade later escalated
                    the NEXT turn in the same session to tier1
                    → cascade is "fixing" pre-flight's under-call
  overridden        pre-flight ran but a higher-priority driver (operator
                    chip, spend_cap, runaway) won. Expected behavior,
                    not a misrouting — excluded from rates.
  abstained         pre-flight had no opinion; legacy classifier handled.
                    Excluded from rates (no decision to grade).

For spans where the router didn't run at all (heartbeat / cron / opted-
out bot / pre-PR-2334 history), the category is "preflight_not_run" and
the span is dropped from analysis entirely.

Rates
-----

For each bot in the window:

  agreement_rate         = agreement / (decisions)
  over_escalation_rate   = over_escalation / (decisions)
  under_escalation_rate  = under_escalation / (decisions)
  cascade_corrected_rate = cascade_corrected / (decisions)

Where ``decisions = agreement + over_escalation + under_escalation +
cascade_corrected`` (excludes overridden + abstained — those aren't
graded decisions).

A "decision" only counts when pre-flight produced a tier AND that tier
drove routing (i.e., tier_chosen_by == "preflight"). The rate
denominator is the population of TRUE pre-flight-driven decisions, not
all spans where the router ran.

Signal emission
---------------

audit_runner._collect_preflight_disagreement_signals turns these rates
into operator-facing Signals:

  preflight_over_escalation   rate > 15% + >= 30 decisions
  preflight_under_escalation  rate > 15% + >= 30 decisions
  preflight_cascade_corrected rate > 10% + >= 30 decisions

Thresholds are conservative — at 15%, 1-in-6 turns is misrouted; that's
worth surfacing. Below 30 decisions per bot per window, sample is too
small to draw conclusions and we emit nothing.

Output also includes per-layer (regex/bot_prior/haiku) and per-reason
(regex:design_imperative, haiku:tier1, etc.) breakdowns so the operator
can attribute miscalibrations to specific rules.

Spec: internal/spec-preflight-intent-router-2026-06-06.md.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

# ── Category constants — exported for tests ─────────────────────────────────

CAT_AGREEMENT = "agreement"
CAT_OVER_ESCALATION = "over_escalation"
CAT_UNDER_ESCALATION = "under_escalation"
CAT_CASCADE_CORRECTED = "cascade_corrected"
CAT_OVERRIDDEN = "overridden"
CAT_ABSTAINED = "abstained"
CAT_NOT_RUN = "preflight_not_run"

# Categories that COUNT as graded decisions (denominator for rates).
GRADED_CATEGORIES = frozenset({
    CAT_AGREEMENT,
    CAT_OVER_ESCALATION,
    CAT_UNDER_ESCALATION,
    CAT_CASCADE_CORRECTED,
})


# ── Trivial / struggle detection — tunable knobs ────────────────────────────

# A tier1-routed turn is "over-escalated" when ALL of these hold. Each
# bound is set conservatively: a turn must be unambiguously simple to be
# called wasted Opus pricing. False positives here would create alert
# fatigue.
_TRIVIAL_MAX_COST_USD = 0.05
_TRIVIAL_MAX_OUTPUT_TOKENS = 200
_TRIVIAL_MAX_TOOL_CALLS = 1


def _is_trivial(span: dict) -> bool:
    """Return True when a turn is so simple that tier1 was overkill.

    Requires ALL of: cost < $0.05, output < 200 tokens, ≤1 tool call,
    AND cascade.success == True (a failed turn isn't trivial even if it
    looks short — failure IS the signal that something went wrong).
    """
    attrs = span.get("attributes") or {}
    cost = span.get("total_cost")
    if cost is None or cost >= _TRIVIAL_MAX_COST_USD:
        return False
    usage = span.get("usage") or {}
    output_tokens = usage.get("output_tokens", 0) or 0
    if output_tokens >= _TRIVIAL_MAX_OUTPUT_TOKENS:
        return False
    # tool_count_per_turn raw from struggle features (added by
    # PreflightIntentRouter Phase 2 + StruggleDetector tool_count
    # feature). Fall back to 0 when absent (older spans).
    tool_count = attrs.get("cascade.struggle.raw.tool_count_per_turn", 0) or 0
    if tool_count > _TRIVIAL_MAX_TOOL_CALLS:
        return False
    # Success required — a failed short turn isn't trivial.
    if attrs.get("cascade.success") is False:
        return False
    return True


# A tier3-routed turn struggled when ANY of these hold. We use ANY (not
# ALL) because each signal is genuinely independent evidence — a failed
# turn is bad regardless of cost; a high-cost tier3 turn is bad regardless
# of success flag.
_STRUGGLE_MIN_SCORE = 0.4
_STRUGGLE_MAX_TIER3_COST_USD = 0.20


def _is_struggle(span: dict) -> bool:
    """Return True when a tier3-routed turn shows signs of struggling.

    Triggers on ANY of:
      - cascade.struggle.score >= 0.4 (real feature signal present)
      - cascade.success == False (OC marked failure)
      - total_cost > $0.20 (tier3 turn that cost more than expected —
        often indicates a long context that needed a stronger model)
    """
    attrs = span.get("attributes") or {}
    score = attrs.get("cascade.struggle.score")
    if isinstance(score, (int, float)) and score >= _STRUGGLE_MIN_SCORE:
        return True
    if attrs.get("cascade.success") is False:
        return True
    cost = span.get("total_cost")
    if isinstance(cost, (int, float)) and cost > _STRUGGLE_MAX_TIER3_COST_USD:
        return True
    return False


# ── Cascade-correction detection ────────────────────────────────────────────

def _find_cascade_corrections(spans: Iterable[dict]) -> set[tuple[str, int]]:
    """Identify (session_id, turn_index) pairs where pre-flight picked
    tier2/3 but the cascade controller later escalated the NEXT turn in
    the same session to tier1. The earlier span is the "cascade-
    corrected" one — it's where pre-flight under-shot.

    Returns a set of (session_id, turn_index) tuples for the EARLIER
    spans in each corrected pair.

    Note: requires multi-turn sessions; in low-volume installs this set
    will often be empty. That's correct — not a bug.
    """
    # Group by session, sort by turn_index
    by_session: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for span in spans:
        attrs = span.get("attributes") or {}
        sid = attrs.get("session_id") or span.get("trace_id")
        if not isinstance(sid, str):
            continue
        turn_idx = attrs.get("turn_index")
        if not isinstance(turn_idx, int):
            continue
        by_session[sid].append((turn_idx, span))

    corrections: set[tuple[str, int]] = set()
    for sid, items in by_session.items():
        items.sort(key=lambda p: p[0])
        for i in range(len(items) - 1):
            this_turn_idx, this_span = items[i]
            _, next_span = items[i + 1]
            this_attrs = this_span.get("attributes") or {}
            next_attrs = next_span.get("attributes") or {}
            preflight_tier = this_attrs.get("cascade.preflight.tier")
            if preflight_tier not in ("tier2", "tier3"):
                continue
            next_tier = next_attrs.get("cascade.tier_used")
            next_driver = next_attrs.get("cascade.tier_chosen_by")
            if next_tier == "tier1" and next_driver == "cascade":
                corrections.add((sid, this_turn_idx))
    return corrections


# ── Per-span categorization ─────────────────────────────────────────────────

def categorize_span(span: dict, cascade_corrections: set[tuple[str, int]] | None = None) -> str:
    """Categorize a single span. Returns one of the CAT_* constants.

    ``cascade_corrections`` is the output of ``_find_cascade_corrections``
    over the same span population — required to detect the
    cascade_corrected category (which depends on a turn-pair). Pass an
    empty set when not computing corrections (single-span analysis).
    """
    attrs = span.get("attributes") or {}
    preflight_layer = attrs.get("cascade.preflight.layer")

    # Router didn't run on this span at all.
    if preflight_layer is None:
        return CAT_NOT_RUN

    # Router ran but abstained — no opinion to grade.
    if preflight_layer == "abstain":
        return CAT_ABSTAINED

    # Router had an opinion. Did it drive routing?
    tier_chosen_by = attrs.get("cascade.tier_chosen_by")
    if tier_chosen_by != "preflight":
        # Higher-priority slot won (operator chip, spend_cap, etc.).
        # Not a misrouting — expected.
        return CAT_OVERRIDDEN

    # Pre-flight drove the decision. Was the tier right?
    preflight_tier = attrs.get("cascade.preflight.tier")

    # Check cascade-correction FIRST (overrides agreement when the next
    # turn cascaded up — that means pre-flight under-shot regardless of
    # this turn's outcome).
    corrections = cascade_corrections or set()
    sid = attrs.get("session_id") or span.get("trace_id")
    turn_idx = attrs.get("turn_index")
    if isinstance(sid, str) and isinstance(turn_idx, int):
        if (sid, turn_idx) in corrections:
            return CAT_CASCADE_CORRECTED

    if preflight_tier == "tier1":
        if _is_trivial(span):
            return CAT_OVER_ESCALATION
        return CAT_AGREEMENT

    if preflight_tier == "tier3":
        if _is_struggle(span):
            return CAT_UNDER_ESCALATION
        return CAT_AGREEMENT

    # tier2 — neither over nor under by definition (it's the default).
    return CAT_AGREEMENT


# ── Aggregated stats ────────────────────────────────────────────────────────

# Approximate haiku call cost in USD. The haiku layer prompt is ~150
# input tokens + ~3 output tokens. Pricing at $0.80 input / $4.00 output
# per million → ~$0.000132 per call. Rounded slightly conservatively.
_HAIKU_COST_PER_CALL_USD = 0.00015


def compute_preflight_stats(spans: Iterable[dict]) -> dict[str, Any]:
    """Build the full per-bot stats dict from a span population.

    Output shape:
        {
            "by_bot": {
                "<bot_id>": {
                    "total_spans": int,             # all spans for this bot
                    "preflight_ran": int,           # router invoked
                    "decisions": int,               # graded denominator
                    "categories": {<cat>: int, ...},
                    "rates": {
                        "agreement_rate": float,
                        "over_escalation_rate": float,
                        "under_escalation_rate": float,
                        "cascade_corrected_rate": float,
                    },
                    "by_layer": {<layer>: {
                        "count": int,
                        "agreement": int,
                        "over_escalation": int,
                        "under_escalation": int,
                        "cascade_corrected": int,
                    }, ...},
                    "by_reason": { <reason>: {same shape}, ... },
                    "haiku_call_count": int,
                    "haiku_latency_ms_p50": float | None,
                    "haiku_latency_ms_p95": float | None,
                    "haiku_estimated_cost_usd": float,
                },
                ...
            },
            "totals": {
                "spans_seen": int,
                "preflight_ran": int,
                "haiku_calls": int,
                "haiku_estimated_cost_usd": float,
            },
        }

    All rates are 0.0 when the denominator is 0 (callers can distinguish
    via ``decisions``).
    """
    # Materialize the span list once — we walk it multiple times.
    span_list = list(spans)
    corrections = _find_cascade_corrections(span_list)

    by_bot: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total_spans": 0,
        "preflight_ran": 0,
        "decisions": 0,
        "categories": defaultdict(int),
        "by_layer": defaultdict(lambda: defaultdict(int)),
        "by_reason": defaultdict(lambda: defaultdict(int)),
        "_haiku_latencies": [],
        "haiku_call_count": 0,
    })

    for span in span_list:
        bot_id = span.get("bot_id") or (span.get("attributes") or {}).get("bot_id")
        if not isinstance(bot_id, str) or not bot_id:
            continue
        bot = by_bot[bot_id]
        bot["total_spans"] += 1

        attrs = span.get("attributes") or {}
        layer = attrs.get("cascade.preflight.layer")
        if layer is None:
            continue  # router didn't run; skip
        bot["preflight_ran"] += 1

        # Haiku-call accounting
        if layer == "haiku":
            bot["haiku_call_count"] += 1
            latency = attrs.get("cascade.preflight.latency_ms")
            if isinstance(latency, (int, float)):
                bot["_haiku_latencies"].append(float(latency))

        category = categorize_span(span, corrections)
        # Skip the "not run" guard above already filtered this, but the
        # explicit drop here keeps the layer/reason buckets honest.
        if category == CAT_NOT_RUN:
            continue

        bot["categories"][category] += 1

        if category in GRADED_CATEGORIES:
            bot["decisions"] += 1

        # Layer / reason breakdowns — only graded decisions land in
        # these buckets so per-reason rates are interpretable.
        reason = attrs.get("cascade.preflight.reason") or "<unknown>"
        bot["by_layer"][layer]["count"] += 1
        bot["by_reason"][reason]["count"] += 1
        if category in GRADED_CATEGORIES:
            bot["by_layer"][layer][category] += 1
            bot["by_reason"][reason][category] += 1

    # Finalize rates + latency percentiles + cost projection
    out_by_bot: dict[str, dict[str, Any]] = {}
    total_spans = 0
    total_preflight_ran = 0
    total_haiku_calls = 0
    for bot_id, bot in by_bot.items():
        decisions = bot["decisions"]
        rates: dict[str, float] = {
            "agreement_rate": _safe_rate(bot["categories"][CAT_AGREEMENT], decisions),
            "over_escalation_rate": _safe_rate(bot["categories"][CAT_OVER_ESCALATION], decisions),
            "under_escalation_rate": _safe_rate(bot["categories"][CAT_UNDER_ESCALATION], decisions),
            "cascade_corrected_rate": _safe_rate(bot["categories"][CAT_CASCADE_CORRECTED], decisions),
        }
        haiku_latencies = bot.pop("_haiku_latencies")
        p50, p95 = _percentiles(haiku_latencies)
        out_by_bot[bot_id] = {
            "total_spans": bot["total_spans"],
            "preflight_ran": bot["preflight_ran"],
            "decisions": decisions,
            # Convert defaultdicts → plain dicts so JSON-serialization
            # of the API response stays predictable.
            "categories": dict(bot["categories"]),
            "rates": rates,
            "by_layer": {k: dict(v) for k, v in bot["by_layer"].items()},
            "by_reason": {k: dict(v) for k, v in bot["by_reason"].items()},
            "haiku_call_count": bot["haiku_call_count"],
            "haiku_latency_ms_p50": p50,
            "haiku_latency_ms_p95": p95,
            "haiku_estimated_cost_usd": round(
                bot["haiku_call_count"] * _HAIKU_COST_PER_CALL_USD, 6,
            ),
        }
        total_spans += bot["total_spans"]
        total_preflight_ran += bot["preflight_ran"]
        total_haiku_calls += bot["haiku_call_count"]

    return {
        "by_bot": out_by_bot,
        "totals": {
            "spans_seen": total_spans,
            "preflight_ran": total_preflight_ran,
            "haiku_calls": total_haiku_calls,
            "haiku_estimated_cost_usd": round(
                total_haiku_calls * _HAIKU_COST_PER_CALL_USD, 6,
            ),
        },
    }


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _percentiles(values: list[float]) -> tuple[float | None, float | None]:
    """Return (p50, p95) of a value list, or (None, None) when empty.

    Uses nearest-rank percentile — fine for low-volume telemetry; pure
    Python so the module stays import-light.
    """
    if not values:
        return None, None
    sorted_v = sorted(values)
    n = len(sorted_v)
    p50_idx = max(0, min(n - 1, int(round(0.5 * (n - 1)))))
    p95_idx = max(0, min(n - 1, int(round(0.95 * (n - 1)))))
    return round(sorted_v[p50_idx], 2), round(sorted_v[p95_idx], 2)


# ── Signal emission thresholds (consumed by audit_runner) ───────────────────

PREFLIGHT_MIN_DECISIONS = 30
PREFLIGHT_OVER_ESCALATION_THRESHOLD = 0.15
PREFLIGHT_UNDER_ESCALATION_THRESHOLD = 0.15
PREFLIGHT_CASCADE_CORRECTED_THRESHOLD = 0.10

# Sparse-bot tier (added 2026-06-08). Low-volume bots — household /
# personal-assistant deployments where a single user sends a handful of
# turns per day — would never accumulate 30 graded pre-flight decisions
# in the audit window. They'd wrongly appear healthy even when every
# single "help me figure out" message was escalating to opus.
#
# The 2026-06-07 over-escalation incident demonstrated this gap:
# the regex was tripping on casual idiom across the pod, but the
# audit layer couldn't fire below 30 decisions on low-volume bots
# even though the cost impact per turn was ~18x (opus vs sonnet).
#
# Lower confidence path: 5+ decisions AND ≥3 absolute miscategorized
# decisions AND a HIGHER rate threshold (25% vs 15%) AND we emit at
# severity="info" instead of "warn" — calling out that the sample is
# small but the pattern is worth a human look. Bots with <5 graded
# decisions still emit nothing (the 1-of-2 case is genuine noise).
PREFLIGHT_SPARSE_MIN_DECISIONS = 5
PREFLIGHT_SPARSE_MIN_COUNT = 3
PREFLIGHT_SPARSE_OVER_ESCALATION_THRESHOLD = 0.25
PREFLIGHT_SPARSE_UNDER_ESCALATION_THRESHOLD = 0.25
PREFLIGHT_SPARSE_CASCADE_CORRECTED_THRESHOLD = 0.20

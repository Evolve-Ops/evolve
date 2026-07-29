"""cascade.anomaly_detector — origin-aware anomaly classification.

Per docs/spec-tier-cascade-2026-05-26.md § 2.6 cost management.

Detects when a current metric value (cost/turn, context-tokens/turn,
sessions/day, tier mix) exceeds the bot's rolling baseline by an
origin-dependent multiple. The threshold table is asymmetric by design:

  user_initiated (chat):              inform 3×, warn 10×
  ui_chip Power (explicit):           inform —, warn 10×
  background_user_visible (briefing): inform 2×, warn 3×
  background_pure (cron/heartbeat):   inform 1.5×, warn 2×
  cascade_escalation (autonomous t1): inform 1.2×, warn 1.5×

Forbearance asymmetry: user-initiated work is what we want to encourage
(3× inform is generous). Cascade-decided tier1 is the most suspicious
(1.5× warn). The dangerous-combo detector handles the specific
"background + tier1 + cascade-decided + huge context" pattern as a
separate single-occurrence signal — this module is for general
distribution-based detection.

Two-stage auto-bootstrap (round-2 review tuning-F1, F8):
  Stage 1 (first 14 days per bot OR < min_baseline_observations turns):
    Use pod-median baseline. Conservative — fewer anomalies fire.
  Stage 2 (after sufficient bot-specific data):
    Switch to bot-specific baseline. More precise.

Holdout cohort is the un-contaminated reference (HoldoutCohort.ts on
plugin side; spans carry `cascade.holdout: true`). The baseline
computation prefers holdout spans when available — the cascade-affected
distribution is too noisy to baseline against itself.

**Phase 2 (this PR): advisory-only.** Module computes anomaly verdicts
but does NOT emit live Signals yet. A separate runner (Phase 3) reads
the verdicts and produces Signal records. This keeps the detector
itself a pure function — easier to test, no I/O dependencies.

Pure module. No I/O writes. Reads spans via the JsonlBackend abstraction.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

# ─────────────────────────────────────────────────────────────────────────────
# Origin-aware thresholds (spec § 2.6)
# ─────────────────────────────────────────────────────────────────────────────

#: Names for the session-origin classification used in the threshold table.
#: Match what cascade telemetry spans carry as cascade.trigger_kind + cascade.tier_chosen_by.
Origin = Literal[
    "user_initiated",
    "ui_chip",
    "background_user_visible",
    "background_pure",
    "cascade_escalation",
]


@dataclass
class OriginThresholds:
    """Per-origin (inform, warn) multipliers."""

    inform: float | None  # None = no inform-level threshold (e.g., ui_chip)
    warn: float


DEFAULT_ORIGIN_THRESHOLDS: dict[Origin, OriginThresholds] = {
    "user_initiated":           OriginThresholds(inform=3.0,  warn=10.0),
    "ui_chip":                  OriginThresholds(inform=None, warn=10.0),
    "background_user_visible":  OriginThresholds(inform=2.0,  warn=3.0),
    "background_pure":          OriginThresholds(inform=1.5,  warn=2.0),
    "cascade_escalation":       OriginThresholds(inform=1.2,  warn=1.5),
}


# ─────────────────────────────────────────────────────────────────────────────
# Origin classification helper
# ─────────────────────────────────────────────────────────────────────────────


def classify_origin(
    trigger_kind: str | None,
    tier_used: str | None,
    tier_chosen_by: str | None,
    consent_source: str | None,
) -> Origin:
    """Map a span's (trigger_kind, tier_used, tier_chosen_by, consent_source)
    to one of the five Origin labels for threshold lookup.

    Order of checks matters — earlier rules are more specific.
    Per spec § 2.6 forbearance asymmetry table.
    """
    # Most-specific: cascade autonomously escalated to tier1 (system spent without consent).
    if tier_chosen_by == "cascade" and tier_used == "tier1":
        return "cascade_escalation"

    # User explicitly picked Power via UI chip → ui_chip (highest forbearance).
    if consent_source == "ui_chip":
        return "ui_chip"

    # Background classification by trigger_kind.
    # background_user_visible is a subset (morning briefing pattern) —
    # we don't have a clean way to detect "user-visible" yet without
    # an explicit per-bot config flag. For Phase 2, default cron_app
    # and heartbeat both to background_pure. When a bot's
    # tiers.json::cascade.background.user_visible: true is honored
    # later, that override flips the classification.
    if trigger_kind in ("heartbeat", "cron_app"):
        return "background_pure"

    # subagent inherits parent kind in spec, but we don't track parent
    # at this layer. For now treat as user_initiated (safer fallback —
    # higher forbearance per open Q#7).
    if trigger_kind in ("user_turn", "subagent"):
        return "user_initiated"

    # Unknown / unclassified → treat as user_initiated (safer default).
    return "user_initiated"


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly classification (pure function)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AnomalyResult:
    """The verdict of classify_anomaly()."""

    is_anomalous: bool
    severity: Literal["inform", "warn"] | None  # None when not anomalous
    threshold_used: float                        # the actual multiplier
    ratio: float                                 # observed value / baseline
    origin: Origin
    baseline_value: float
    observed_value: float


def classify_anomaly(
    observed_value: float,
    baseline_value: float,
    origin: Origin,
    thresholds: dict[Origin, OriginThresholds] | None = None,
) -> AnomalyResult:
    """Decide whether observed_value is anomalous relative to baseline.

    Pure function. No I/O. Same inputs → same outputs.

    Anomaly fires when observed > baseline × threshold. Warn-level
    fires when observed > baseline × warn_threshold. Inform fires
    above inform_threshold but below warn (if inform is defined).
    """
    if thresholds is None:
        thresholds = DEFAULT_ORIGIN_THRESHOLDS
    th = thresholds[origin]

    # Guard against zero/negative baseline — can't compute a meaningful
    # ratio. Treat as not-anomalous (no baseline = no reference).
    if baseline_value <= 0:
        return AnomalyResult(
            is_anomalous=False, severity=None, threshold_used=0.0,
            ratio=0.0, origin=origin,
            baseline_value=baseline_value, observed_value=observed_value,
        )

    ratio = observed_value / baseline_value

    if ratio > th.warn:
        return AnomalyResult(
            is_anomalous=True, severity="warn", threshold_used=th.warn,
            ratio=ratio, origin=origin,
            baseline_value=baseline_value, observed_value=observed_value,
        )
    if th.inform is not None and ratio > th.inform:
        return AnomalyResult(
            is_anomalous=True, severity="inform", threshold_used=th.inform,
            ratio=ratio, origin=origin,
            baseline_value=baseline_value, observed_value=observed_value,
        )
    return AnomalyResult(
        is_anomalous=False, severity=None,
        threshold_used=th.inform if th.inform is not None else th.warn,
        ratio=ratio, origin=origin,
        baseline_value=baseline_value, observed_value=observed_value,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Baseline computation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BaselineStats:
    """Aggregate stats for a single metric/bot/window."""

    n: int = 0
    mean: float = 0.0
    median: float = 0.0
    p95: float = 0.0


@dataclass
class BotBaseline:
    """Per-bot baseline across all tracked metrics. Phase 4 audit layer
    feeds these to the anomaly detector."""

    bot_id: str
    window_days: int
    cost_per_turn: BaselineStats = field(default_factory=BaselineStats)
    context_tokens_per_turn: BaselineStats = field(default_factory=BaselineStats)
    sessions_per_day: BaselineStats = field(default_factory=BaselineStats)
    tier1_share_of_cost: float = 0.0  # fraction
    tier2_share_of_cost: float = 0.0
    tier3_share_of_cost: float = 0.0
    # Provenance: was this baseline computed from bot-specific data or
    # bootstrapped from pod median?
    source: Literal["bot_specific", "pod_median_bootstrap", "insufficient_data"] = "insufficient_data"


def _stats_from_values(values: list[float]) -> BaselineStats:
    """Compute mean/median/p95 over a list of floats."""
    if not values:
        return BaselineStats()
    if len(values) == 1:
        v = float(values[0])
        return BaselineStats(n=1, mean=v, median=v, p95=v)
    sorted_vals = sorted(values)
    try:
        cuts = statistics.quantiles(sorted_vals, n=20, method="inclusive")
        p95 = float(cuts[18])
    except (ValueError, IndexError):
        p95 = float(sorted_vals[-1])
    return BaselineStats(
        n=len(values),
        mean=float(statistics.mean(values)),
        median=float(statistics.median(sorted_vals)),
        p95=p95,
    )


def compute_baseline_from_spans(
    spans: Iterator[dict],
    bot_id: str,
    *,
    window_days: int = 30,
    min_observations: int = 50,
    bootstrap_after_days: int = 14,
    earliest_span_at: datetime | None = None,
    pod_fallback: BotBaseline | None = None,
) -> BotBaseline:
    """Compute one bot's baseline from a stream of cascade telemetry span dicts.

    Spans should be pre-filtered to:
      - this bot_id
      - within the window
      - producer == "cascade_telemetry" (turn spans, not error spans)

    **Auto-bootstrap (spec § 2.6 + § 2.3 Component 5):**

    1. If we have ≥ min_observations from this bot AND it's been
       ≥ bootstrap_after_days since the bot's first span → use
       bot-specific baseline.
    2. Otherwise, fall back to pod_fallback (the pod median baseline).
       Source tagged as "pod_median_bootstrap".
    3. If neither is available → tag "insufficient_data", anomaly
       detection effectively disabled until data accumulates.

    Holdout-preference is the caller's responsibility — they should
    pass holdout-only spans here when available, falling back to all
    spans otherwise. This module doesn't read the holdout flag
    directly (keeps it pure-data-in pure-data-out).
    """
    cost_per_turn_values: list[float] = []
    context_tokens_values: list[float] = []
    tier_cost: dict[str, float] = {"tier1": 0.0, "tier2": 0.0, "tier3": 0.0}
    # Per-day set of unique session_ids — len(set) per day is the
    # sessions-per-day distribution we want. Prior implementation
    # incremented a counter with the result of `sid not in sessions_seen`
    # AFTER calling `sessions_seen.add(sid)` on the same line, so the
    # check was always False and every per-day count stayed at 0.
    sessions_by_day: dict[str, set[str]] = defaultdict(set)

    for span in spans:
        attrs = span.get("attributes") or {}
        # Cost-per-turn
        total_cost = span.get("total_cost") or 0.0
        if isinstance(total_cost, (int, float)) and total_cost > 0:
            cost_per_turn_values.append(float(total_cost))

        # Context tokens
        usage = span.get("usage") or {}
        in_tok = usage.get("input_tokens") or 0
        if isinstance(in_tok, (int, float)) and in_tok > 0:
            context_tokens_values.append(float(in_tok))

        # Tier mix (by cost).
        # Spans with `cascade.tier_used == null` (model not in the bot's
        # tier config — reverse-lookup miss) are intentionally excluded.
        # Their cost still flows into `cost_per_turn_values` for the
        # per-turn baseline, but tier shares are computed only over
        # known-tier cost. Effect: tier1_share + tier2_share + tier3_share
        # may sum to < 1.0 when unknown-model cost is in play. Callers
        # comparing shares to thresholds should account for this — or
        # interpret the shortfall as "unknown-tier activity rate."
        tier = attrs.get("cascade.tier_used")
        if tier in tier_cost and isinstance(total_cost, (int, float)):
            tier_cost[tier] += float(total_cost)

        # Sessions-per-day: collect unique session_ids per day.
        sid = attrs.get("session_id")
        ts = span.get("end_time") or span.get("start_time")
        if isinstance(sid, str) and isinstance(ts, str):
            day = ts[:10]
            sessions_by_day[day].add(sid)

    sessions_per_day_values: list[float] = [
        float(len(sids)) for sids in sessions_by_day.values()
    ]

    total_cost = sum(tier_cost.values())

    bot_baseline = BotBaseline(
        bot_id=bot_id,
        window_days=window_days,
        cost_per_turn=_stats_from_values(cost_per_turn_values),
        context_tokens_per_turn=_stats_from_values(context_tokens_values),
        sessions_per_day=_stats_from_values(sessions_per_day_values),
        tier1_share_of_cost=(tier_cost["tier1"] / total_cost) if total_cost > 0 else 0.0,
        tier2_share_of_cost=(tier_cost["tier2"] / total_cost) if total_cost > 0 else 0.0,
        tier3_share_of_cost=(tier_cost["tier3"] / total_cost) if total_cost > 0 else 0.0,
    )

    # Decide whether to use bot-specific or fall back to pod median.
    has_enough_observations = bot_baseline.cost_per_turn.n >= min_observations
    has_been_running_long_enough = True  # assume so unless caller tells us otherwise
    if earliest_span_at is not None:
        age_days = (datetime.now(timezone.utc) - earliest_span_at).days
        has_been_running_long_enough = age_days >= bootstrap_after_days

    if has_enough_observations and has_been_running_long_enough:
        bot_baseline.source = "bot_specific"
    elif pod_fallback is not None and pod_fallback.cost_per_turn.n > 0:
        # Use pod median values but keep the bot_id.
        bot_baseline.cost_per_turn = pod_fallback.cost_per_turn
        bot_baseline.context_tokens_per_turn = pod_fallback.context_tokens_per_turn
        bot_baseline.sessions_per_day = pod_fallback.sessions_per_day
        bot_baseline.tier1_share_of_cost = pod_fallback.tier1_share_of_cost
        bot_baseline.tier2_share_of_cost = pod_fallback.tier2_share_of_cost
        bot_baseline.tier3_share_of_cost = pod_fallback.tier3_share_of_cost
        bot_baseline.source = "pod_median_bootstrap"
    else:
        bot_baseline.source = "insufficient_data"

    return bot_baseline


def compute_pod_median_baseline(
    per_bot_baselines: list[BotBaseline],
) -> BotBaseline:
    """Aggregate per-bot baselines into a pod-median fallback.

    Used for bootstrap — when a new bot hasn't accumulated enough
    data of its own, the anomaly detector uses these pod-median
    stats. Takes the median (not mean) across bots to reduce
    sensitivity to outlier bots (e.g., admin_bot's audit-heavy
    distribution).
    """
    populated = [b for b in per_bot_baselines if b.cost_per_turn.n > 0]
    if not populated:
        return BotBaseline(bot_id="<pod_median>", window_days=0)

    def _med(field: str, attr: str) -> float:
        vals = [getattr(getattr(b, field), attr) for b in populated]
        return float(statistics.median(vals)) if vals else 0.0

    def _med_float(field: str) -> float:
        vals = [getattr(b, field) for b in populated]
        return float(statistics.median(vals)) if vals else 0.0

    return BotBaseline(
        bot_id="<pod_median>",
        window_days=populated[0].window_days,
        cost_per_turn=BaselineStats(
            n=sum(b.cost_per_turn.n for b in populated),
            mean=_med("cost_per_turn", "mean"),
            median=_med("cost_per_turn", "median"),
            p95=_med("cost_per_turn", "p95"),
        ),
        context_tokens_per_turn=BaselineStats(
            n=sum(b.context_tokens_per_turn.n for b in populated),
            mean=_med("context_tokens_per_turn", "mean"),
            median=_med("context_tokens_per_turn", "median"),
            p95=_med("context_tokens_per_turn", "p95"),
        ),
        sessions_per_day=BaselineStats(
            n=sum(b.sessions_per_day.n for b in populated),
            mean=_med("sessions_per_day", "mean"),
            median=_med("sessions_per_day", "median"),
            p95=_med("sessions_per_day", "p95"),
        ),
        tier1_share_of_cost=_med_float("tier1_share_of_cost"),
        tier2_share_of_cost=_med_float("tier2_share_of_cost"),
        tier3_share_of_cost=_med_float("tier3_share_of_cost"),
        source="bot_specific",  # the pod median IS the bot-specific data for pod-level
    )

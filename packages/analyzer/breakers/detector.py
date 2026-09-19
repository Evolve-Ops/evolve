"""breakers.detector — the activity-shape rule.

Per spec §5.1.1, the L1 cost breaker's primary detector rule. Two
positive-trigger prongs (A and B) ORed, gated by two negative-trigger
clauses (C and D) that must both hold:

  GATE   auto_turns_in_window >= MIN_AUTO_TURNS_GATE

  TRIP IF (A OR B) AND C AND D, where:

    A. RATE SPIKE
       auto_rate_per_hr >= max(baseline.auto_rate * SPIKE_MULTIPLIER,
                               MIN_AUTO_FLOOR_PER_HR)
       — catches volume blowouts like security_bot 2026-05-20 (130 turns / 12h).

    B. TIER SHIFT (rate may be normal but mix changed)
       window_high_tier_share >= baseline.auto_high_tier_share + TIER_SHIFT_DELTA
       — catches the heartbeat-on-wrong-model pattern (team_bot_a 2026-04-17:
       same 1/hr rate, but auto activity flipped from ~0% high-tier
       (haiku) to ~100% high-tier (sonnet)).

    C. HIGH-TIER FLOOR (don't trip on cheap auto activity)
       window_high_tier_share >= HIGH_TIER_SHARE_FLOOR
       — the spike must include meaningful high-tier activity. An
       all-haiku burst is not the kind of cost incident this catches.

    D. HUMAN QUIESCENT (the spike is auto-only)
       human_rate_per_hr < baseline.human_rate * HUMAN_QUIESCENT_MULTIPLIER
                           + HUMAN_QUIESCENT_BASE
       — if the user is actively chatting at elevated rates, the burn
       is likely tracking real use; don't trip.

For cold-start bots (Baseline.cold_start == True), the rate-spike
prong falls back to absolute floors instead of multiplicative
thresholds — there's no baseline to multiply.

First-cut thresholds are conservative (favor no-trip on negatives over
catching every positive). The backtest harness in breakers.backtest is
the calibration tool: replay 90 days of audit data, tune until both
recall and false-positive targets are met (spec §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from breakers.baseline import Baseline
from breakers.classify import classify_turn, parse_ts


@dataclass(frozen=True)
class DetectorConfig:
    """Tunable thresholds for the activity-shape detector.

    All defaults are first-cut conservative values. The backtest
    harness tunes these against the 90-day audit corpus.
    """

    # Gate: minimum auto turns in window before the rule even evaluates.
    # Below this we never trip — keeps the detector quiet on idle bots.
    min_auto_turns_gate: int = 5

    # Prong A — rate spike
    spike_multiplier: float = 5.0
    min_auto_floor_per_hr: float = 5.0  # absolute floor (warm bots)
    cold_start_auto_floor_per_hr: float = 10.0  # higher floor when no baseline

    # Prong B — tier shift
    tier_shift_delta: float = 0.40   # +40pp shift in high-tier share

    # Clause C — high-tier floor
    high_tier_share_floor: float = 0.30

    # Clause D — human quiescent
    human_quiescent_multiplier: float = 2.0
    human_quiescent_base: float = 0.5  # turns/hr — buffer for small bursts


DEFAULT_CONFIG = DetectorConfig()


@dataclass(frozen=True)
class Decision:
    """The detector's output for one evaluation window."""

    bot_id: str
    window_start: datetime
    window_end: datetime
    trip: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


def _bucket_window(
    turns: Iterable[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int, int, int]:
    """Return (auto, human, auto_high_tier, ambiguous) counts for the window."""
    auto = 0
    human = 0
    auto_high = 0
    ambig = 0
    for t in turns:
        ts = parse_ts(t)
        if ts is None or not (window_start <= ts < window_end):
            continue
        c = classify_turn(t)
        if c.bucket == "auto":
            auto += 1
            if c.model_tier == "high":
                auto_high += 1
        elif c.bucket == "human":
            human += 1
        else:
            ambig += 1
    return auto, human, auto_high, ambig


def evaluate_window(
    *,
    bot_id: str,
    turns: Iterable[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    baseline: Baseline,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> Decision:
    """Evaluate one candidate window. Returns a Decision."""
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    window_hours = max(
        (window_end - window_start).total_seconds() / 3600.0,
        1.0 / 3600.0,  # 1-second floor, just to avoid div-by-zero
    )

    # Materialize turns once — callers may pass a generator.
    if not isinstance(turns, (list, tuple)):
        turns = list(turns)

    auto, human, auto_high, ambig = _bucket_window(
        turns, window_start=window_start, window_end=window_end
    )

    auto_rate = auto / window_hours
    human_rate = human / window_hours
    window_high_tier_share = (auto_high / auto) if auto > 0 else 0.0

    metrics: dict[str, Any] = {
        "auto_turns": auto,
        "human_turns": human,
        "ambiguous_turns": ambig,
        "auto_rate_per_hr": auto_rate,
        "human_rate_per_hr": human_rate,
        "window_high_tier_share": window_high_tier_share,
        "baseline_auto_rate_per_hr": baseline.auto_rate_per_hr,
        "baseline_human_rate_per_hr": baseline.human_rate_per_hr,
        "baseline_high_tier_share": baseline.auto_high_tier_share,
        "baseline_days_with_data": baseline.days_with_data,
        "cold_start": baseline.cold_start,
    }

    # GATE — minimum auto activity for the rule to evaluate.
    if auto < config.min_auto_turns_gate:
        return Decision(
            bot_id=bot_id,
            window_start=window_start,
            window_end=window_end,
            trip=False,
            reason=f"below gate: {auto} auto turns < {config.min_auto_turns_gate}",
            metrics=metrics,
        )

    # PRONG A — rate spike
    if baseline.cold_start:
        rate_threshold = config.cold_start_auto_floor_per_hr
        rate_threshold_source = "cold-start floor"
    else:
        rate_threshold = max(
            baseline.auto_rate_per_hr * config.spike_multiplier,
            config.min_auto_floor_per_hr,
        )
        rate_threshold_source = (
            f"{config.spike_multiplier}× baseline "
            f"({baseline.auto_rate_per_hr:.2f}/hr)"
        )
    prong_a = auto_rate >= rate_threshold

    # PRONG B — tier shift
    if baseline.cold_start or baseline.auto_turns == 0:
        # No reliable baseline tier share. Tier shift can't be computed
        # meaningfully; rely on prong A (and clause C still gates).
        prong_b = False
        tier_shift_threshold = None
    else:
        tier_shift_threshold = (
            baseline.auto_high_tier_share + config.tier_shift_delta
        )
        prong_b = window_high_tier_share >= tier_shift_threshold

    # CLAUSE C — high-tier floor
    clause_c = window_high_tier_share >= config.high_tier_share_floor

    # CLAUSE D — human quiescent
    human_threshold = (
        baseline.human_rate_per_hr * config.human_quiescent_multiplier
        + config.human_quiescent_base
    )
    clause_d = human_rate < human_threshold

    metrics["clauses"] = {
        "A_rate_spike": prong_a,
        "B_tier_shift": prong_b,
        "C_high_tier_floor": clause_c,
        "D_human_quiescent": clause_d,
        "rate_threshold_per_hr": rate_threshold,
        "rate_threshold_source": rate_threshold_source,
        "tier_shift_threshold": tier_shift_threshold,
        "human_threshold_per_hr": human_threshold,
    }

    trip = (prong_a or prong_b) and clause_c and clause_d

    if trip:
        triggers = []
        if prong_a:
            triggers.append(
                f"auto rate {auto_rate:.2f}/hr ≥ {rate_threshold:.2f}/hr"
                f" ({rate_threshold_source})"
            )
        if prong_b:
            triggers.append(
                f"high-tier share {window_high_tier_share:.0%} "
                f"≥ baseline {baseline.auto_high_tier_share:.0%} + "
                f"{config.tier_shift_delta:.0%}"
            )
        reason = (
            f"trip: {' AND '.join(triggers)}; high-tier share "
            f"{window_high_tier_share:.0%} ≥ floor "
            f"{config.high_tier_share_floor:.0%}; human rate "
            f"{human_rate:.2f}/hr < threshold {human_threshold:.2f}/hr"
        )
    else:
        # Identify the dominant negative clause for the no-trip reason.
        if not (prong_a or prong_b):
            reason = (
                f"no spike: auto rate {auto_rate:.2f}/hr < threshold "
                f"{rate_threshold:.2f}/hr, tier share "
                f"{window_high_tier_share:.0%} (baseline "
                f"{baseline.auto_high_tier_share:.0%})"
            )
        elif not clause_c:
            reason = (
                f"low tier mix: high-tier share {window_high_tier_share:.0%} "
                f"< floor {config.high_tier_share_floor:.0%}"
            )
        elif not clause_d:
            reason = (
                f"human active: rate {human_rate:.2f}/hr ≥ threshold "
                f"{human_threshold:.2f}/hr (likely real chat)"
            )
        else:
            reason = "no trigger"

    return Decision(
        bot_id=bot_id,
        window_start=window_start,
        window_end=window_end,
        trip=trip,
        reason=reason,
        metrics=metrics,
    )

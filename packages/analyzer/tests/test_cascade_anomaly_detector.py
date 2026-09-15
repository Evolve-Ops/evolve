"""tests/test_cascade_anomaly_detector.py — origin-aware anomaly classification.

Per spec § 2.6 cost-management anomaly detector.

Covers:
  - classify_origin: span fields → Origin label
  - classify_anomaly: observed vs baseline at each origin's threshold table
  - compute_baseline_from_spans: rolling stats + auto-bootstrap fallback
  - compute_pod_median_baseline: pod-level aggregate from per-bot baselines
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from cascade.anomaly_detector import (  # noqa: E402
    AnomalyResult,
    BaselineStats,
    BotBaseline,
    DEFAULT_ORIGIN_THRESHOLDS,
    OriginThresholds,
    classify_anomaly,
    classify_origin,
    compute_baseline_from_spans,
    compute_pod_median_baseline,
)


# ─────────────────────────────────────────────────────────────────────────────
# classify_origin
# ─────────────────────────────────────────────────────────────────────────────


def test_origin_cascade_escalation_when_tier1_chosen_by_cascade():
    # The most-specific case — overrides anything else.
    result = classify_origin(
        trigger_kind="heartbeat",  # would otherwise be background
        tier_used="tier1",
        tier_chosen_by="cascade",
        consent_source=None,
    )
    assert result == "cascade_escalation"


def test_origin_ui_chip_when_consent_source_ui_chip():
    # ui_chip consent is the highest-forbearance category.
    result = classify_origin(
        trigger_kind="user_turn",
        tier_used="tier1",
        tier_chosen_by="user_request",
        consent_source="ui_chip",
    )
    assert result == "ui_chip"


def test_origin_background_pure_for_heartbeat():
    result = classify_origin(
        trigger_kind="heartbeat",
        tier_used="tier3",
        tier_chosen_by="classifier",
        consent_source=None,
    )
    assert result == "background_pure"


def test_origin_background_pure_for_cron_app():
    result = classify_origin(
        trigger_kind="cron_app",
        tier_used="tier3",
        tier_chosen_by="classifier",
        consent_source=None,
    )
    assert result == "background_pure"


def test_origin_user_initiated_for_user_turn():
    result = classify_origin(
        trigger_kind="user_turn",
        tier_used="tier2",
        tier_chosen_by="classifier",
        consent_source=None,
    )
    assert result == "user_initiated"


def test_origin_user_initiated_for_subagent():
    # Subagent treated as user-initiated (safer fallback per spec § 2.4 open Q#7)
    result = classify_origin(
        trigger_kind="subagent",
        tier_used="tier2",
        tier_chosen_by="classifier",
        consent_source=None,
    )
    assert result == "user_initiated"


def test_origin_unknown_defaults_to_user_initiated():
    # Defensive: unrecognized trigger_kind → safer default.
    result = classify_origin(
        trigger_kind=None,
        tier_used="tier2",
        tier_chosen_by="classifier",
        consent_source=None,
    )
    assert result == "user_initiated"


def test_origin_cascade_escalation_beats_ui_chip_when_tier1_decided_by_cascade():
    # Edge case: cascade decided tier1 even though consent_source somehow
    # also reads ui_chip. cascade_escalation is more specific (1.5× warn
    # vs 10× warn for ui_chip) — the stricter classification wins.
    result = classify_origin(
        trigger_kind="user_turn",
        tier_used="tier1",
        tier_chosen_by="cascade",
        consent_source="ui_chip",  # stale or wrong field?
    )
    assert result == "cascade_escalation"


# ─────────────────────────────────────────────────────────────────────────────
# classify_anomaly — threshold table
# ─────────────────────────────────────────────────────────────────────────────


def test_anomaly_user_initiated_inform_at_3x():
    # 3.5× baseline → inform-level anomaly
    r = classify_anomaly(observed_value=3.5, baseline_value=1.0, origin="user_initiated")
    assert r.is_anomalous
    assert r.severity == "inform"
    assert r.threshold_used == 3.0


def test_anomaly_user_initiated_warn_at_10x():
    # 11× baseline → warn-level
    r = classify_anomaly(observed_value=11.0, baseline_value=1.0, origin="user_initiated")
    assert r.is_anomalous
    assert r.severity == "warn"
    assert r.threshold_used == 10.0


def test_anomaly_user_initiated_not_anomalous_at_2x():
    r = classify_anomaly(observed_value=2.0, baseline_value=1.0, origin="user_initiated")
    assert not r.is_anomalous
    assert r.severity is None


def test_anomaly_ui_chip_no_inform_only_warn():
    # ui_chip has inform=None — only fires at 10×, NOT below.
    r = classify_anomaly(observed_value=5.0, baseline_value=1.0, origin="ui_chip")
    assert not r.is_anomalous, "5× should NOT fire ui_chip warn (10×) and there's no inform"

    r2 = classify_anomaly(observed_value=11.0, baseline_value=1.0, origin="ui_chip")
    assert r2.is_anomalous
    assert r2.severity == "warn"


def test_anomaly_background_pure_lower_thresholds():
    # background_pure: inform=1.5×, warn=2×
    r = classify_anomaly(observed_value=1.7, baseline_value=1.0, origin="background_pure")
    assert r.is_anomalous
    assert r.severity == "inform"

    r2 = classify_anomaly(observed_value=2.5, baseline_value=1.0, origin="background_pure")
    assert r2.is_anomalous
    assert r2.severity == "warn"


def test_anomaly_cascade_escalation_strictest():
    # cascade_escalation: inform=1.2×, warn=1.5×. Smallest forbearance.
    r = classify_anomaly(observed_value=1.3, baseline_value=1.0, origin="cascade_escalation")
    assert r.is_anomalous
    assert r.severity == "inform"

    r2 = classify_anomaly(observed_value=1.6, baseline_value=1.0, origin="cascade_escalation")
    assert r2.is_anomalous
    assert r2.severity == "warn"


def test_anomaly_zero_baseline_not_anomalous():
    # Defensive: can't compute ratio against 0. Treat as not anomalous.
    r = classify_anomaly(observed_value=100.0, baseline_value=0.0, origin="background_pure")
    assert not r.is_anomalous


def test_anomaly_result_records_observed_and_baseline():
    # The result captures both raw values for downstream Signal payloads.
    r = classify_anomaly(observed_value=5.0, baseline_value=2.0, origin="background_pure")
    assert r.observed_value == 5.0
    assert r.baseline_value == 2.0
    assert r.ratio == 2.5


# ─────────────────────────────────────────────────────────────────────────────
# compute_baseline_from_spans
# ─────────────────────────────────────────────────────────────────────────────


def _span(*, total_cost=0.0, input_tokens=0, tier="tier2", session_id="s1", end_time="2026-05-20T00:00:00Z"):
    """Build a synthetic span dict matching the cascade telemetry shape."""
    return {
        "total_cost": total_cost,
        "usage": {"input_tokens": input_tokens},
        "attributes": {
            "cascade.tier_used": tier,
            "session_id": session_id,
        },
        "end_time": end_time,
        "start_time": end_time,
    }


def test_baseline_basic_shape():
    spans = [
        _span(total_cost=0.05, input_tokens=1000, session_id="s1"),
        _span(total_cost=0.10, input_tokens=2000, session_id="s2"),
        _span(total_cost=0.15, input_tokens=3000, session_id="s3"),
    ]
    b = compute_baseline_from_spans(iter(spans), "team_bot_a")
    assert b.bot_id == "team_bot_a"
    assert b.cost_per_turn.n == 3
    # mean of [0.05, 0.10, 0.15] = 0.10
    assert abs(b.cost_per_turn.mean - 0.10) < 1e-9


def test_baseline_insufficient_data():
    # 3 spans is far below min_observations (default 50)
    spans = [_span(total_cost=0.01)]
    b = compute_baseline_from_spans(iter(spans), "newbot")
    assert b.source == "insufficient_data"


def test_baseline_bootstrap_from_pod_fallback():
    # New bot with few spans + pod fallback present → use pod baseline.
    pod = BotBaseline(
        bot_id="<pod_median>",
        window_days=30,
        cost_per_turn=BaselineStats(n=500, mean=0.05, median=0.04, p95=0.20),
        source="bot_specific",
    )
    spans = [_span(total_cost=1.0)]  # only 1 obs
    b = compute_baseline_from_spans(iter(spans), "newbot", pod_fallback=pod)
    assert b.source == "pod_median_bootstrap"
    # Borrowed the pod's stats.
    assert abs(b.cost_per_turn.mean - 0.05) < 1e-9


def test_baseline_bot_specific_when_enough_data():
    # 100 spans (≥ min_observations=50) → bot-specific, no pod fallback used.
    spans = [_span(total_cost=0.05) for _ in range(100)]
    b = compute_baseline_from_spans(iter(spans), "team_bot_a")
    assert b.source == "bot_specific"


def test_baseline_sessions_per_day_counts_unique_sessions():
    # Regression for code-review MEDIUM #8 — the prior implementation
    # incremented days_seen[day] by `1 if sid not in sessions_seen else 0`
    # AFTER calling `sessions_seen.add(sid)` on the line above, so the
    # condition was always False and every per-day count stayed at 0.
    #
    # Synthesize 3 spans across 2 days with 2 unique sessions per day
    # (one span repeats a session_id within its day). Expected:
    # sessions_per_day.mean = 2.0 — not 0, not 3 (turn count).
    spans = [
        _span(total_cost=0.01, session_id="s1", end_time="2026-05-20T00:00:00Z"),
        _span(total_cost=0.01, session_id="s2", end_time="2026-05-20T01:00:00Z"),
        _span(total_cost=0.01, session_id="s2", end_time="2026-05-20T02:00:00Z"),  # dup on day 1
        _span(total_cost=0.01, session_id="s3", end_time="2026-05-21T00:00:00Z"),
        _span(total_cost=0.01, session_id="s4", end_time="2026-05-21T01:00:00Z"),
    ]
    b = compute_baseline_from_spans(iter(spans), "team_bot_a")
    assert b.sessions_per_day.n == 2  # two days
    assert abs(b.sessions_per_day.mean - 2.0) < 1e-9


def test_baseline_tier_share_of_cost():
    # Cost split: tier1=10, tier2=5, tier3=1 → tier1 ≈ 62.5%, tier2 ≈ 31.25%
    spans = [
        _span(total_cost=5.0, tier="tier1", session_id="s1"),
        _span(total_cost=5.0, tier="tier1", session_id="s2"),
        _span(total_cost=5.0, tier="tier2", session_id="s3"),
        _span(total_cost=1.0, tier="tier3", session_id="s4"),
    ]
    b = compute_baseline_from_spans(iter(spans), "team_bot_a")
    total = 5.0 + 5.0 + 5.0 + 1.0
    assert abs(b.tier1_share_of_cost - 10.0 / total) < 1e-9
    assert abs(b.tier2_share_of_cost - 5.0 / total) < 1e-9
    assert abs(b.tier3_share_of_cost - 1.0 / total) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# compute_pod_median_baseline
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_median_empty_input():
    result = compute_pod_median_baseline([])
    assert result.cost_per_turn.n == 0


def test_pod_median_takes_median_not_mean():
    # Three bots with cost_per_turn means [0.05, 0.10, 100.0]
    # Median = 0.10 (outlier doesn't dominate)
    bots = [
        BotBaseline(
            bot_id=f"bot{i}",
            window_days=30,
            cost_per_turn=BaselineStats(n=100, mean=v, median=v, p95=v * 2),
        )
        for i, v in enumerate([0.05, 0.10, 100.0])
    ]
    pod = compute_pod_median_baseline(bots)
    assert abs(pod.cost_per_turn.mean - 0.10) < 1e-9


def test_pod_median_ignores_empty_bots():
    bots = [
        BotBaseline(bot_id="empty", window_days=30),  # n=0
        BotBaseline(
            bot_id="team_bot_a",
            window_days=30,
            cost_per_turn=BaselineStats(n=100, mean=0.05, median=0.05, p95=0.10),
        ),
    ]
    pod = compute_pod_median_baseline(bots)
    # Only team_bot_a contributed → median = team_bot_a's value
    assert abs(pod.cost_per_turn.mean - 0.05) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# Spec lock-in
# ─────────────────────────────────────────────────────────────────────────────


def test_default_origin_thresholds_match_spec():
    """Lock-in for spec § 2.6 threshold table."""
    t = DEFAULT_ORIGIN_THRESHOLDS
    assert t["user_initiated"].inform == 3.0
    assert t["user_initiated"].warn == 10.0
    assert t["ui_chip"].inform is None
    assert t["ui_chip"].warn == 10.0
    assert t["background_user_visible"].inform == 2.0
    assert t["background_user_visible"].warn == 3.0
    assert t["background_pure"].inform == 1.5
    assert t["background_pure"].warn == 2.0
    assert t["cascade_escalation"].inform == 1.2
    assert t["cascade_escalation"].warn == 1.5

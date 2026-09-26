"""cache_invalidation_ratio resolver + registration (roadmap item 1.1).

Before this, ``cache_ttl_tuner``'s UpdateAgentDefaults claim targeted an
UNREGISTERED metric (``cache_invalidation_ratio``), so the verify daemon
raised ``UnknownMetricError`` → after 3 retries force-flagged the proposal
instead of grading it. The optimizer loop could never close on a
cache-economics claim.

This registers a resolver that reads ``cache_state`` off the cost ledger —
the same telemetry the ``session_economics`` monitor aggregates when it
emits the ``cache_invalidation_elevated`` Signal that motivates the
proposal — and proves it resolves through the registry and satisfies the
Claim contract end to end (succeeded / failed against real-shaped data,
never UnknownMetricError).
"""

from __future__ import annotations

import importlib
from datetime import datetime

import pytest

cache_metrics = importlib.import_module("metrics.resolvers.cache_metrics")
registry = importlib.import_module("metrics.registry")
evaluate = importlib.import_module("verify.evaluate")
proposal_schema = importlib.import_module("schema.proposal")

AS_OF = datetime(2026, 6, 9, 12, 0, 0)


@pytest.fixture
def fake_events(monkeypatch):
    """Patch cost_ledger.read_events (imported locally inside the resolver)."""

    def _set(events):
        monkeypatch.setattr(
            "cost_ledger.read_events", lambda *a, **k: iter(events)
        )

    return _set


def _cache_events(*, warm: int, invalidated: int, fresh: int = 0):
    """Build cost_event-shaped records carrying ``cache_state``.

    Mirrors the fields the plugin's CostLedger.ts emits and that
    ``session_economics._aggregate_cache`` reads. Fresh events are NOT
    cache-participating and must be excluded from the ratio.
    """
    events: list[dict] = []
    events += [{"cache_state": "warm", "cache_read_tokens": 1000} for _ in range(warm)]
    events += [
        {"cache_state": "invalidated", "cache_write_tokens": 1000}
        for _ in range(invalidated)
    ]
    events += [{"cache_state": "fresh", "input_tokens": 1000} for _ in range(fresh)]
    return events


# ── the resolver ─────────────────────────────────────────────────────────────


def test_ratio_is_invalidated_over_participating(fake_events):
    # 30 invalidated / (70 warm + 30 invalidated) = 0.30
    fake_events(_cache_events(warm=70, invalidated=30))
    mv = cache_metrics.resolve_cache_invalidation_ratio("team_bot_a", AS_OF)
    assert mv.value == pytest.approx(0.30)
    assert mv.confidence == 1.0


def test_fresh_events_excluded_from_participating_set(fake_events):
    # fresh events are not cache-participating; ratio over warm+invalidated only.
    # 20 invalidated / (40 warm + 20 invalidated) = 0.3333..., fresh ignored.
    fake_events(_cache_events(warm=40, invalidated=20, fresh=500))
    mv = cache_metrics.resolve_cache_invalidation_ratio("team_bot_a", AS_OF)
    assert mv.value == pytest.approx(20 / 60)
    assert mv.confidence == 1.0


def test_thin_window_is_low_confidence_not_a_real_zero(fake_events):
    """Below the min-events floor the ratio must be unresolved/retry, never a
    real low reading that would spuriously satisfy a down-claim."""
    fake_events(_cache_events(warm=5, invalidated=1))  # 6 < 50
    mv = cache_metrics.resolve_cache_invalidation_ratio("team_bot_a", AS_OF)
    assert mv.confidence < evaluate.METRIC_CONFIDENCE_FLOOR


def test_empty_window_is_low_confidence(fake_events):
    fake_events([])
    mv = cache_metrics.resolve_cache_invalidation_ratio("team_bot_a", AS_OF)
    assert mv.value == 0.0
    assert mv.confidence < evaluate.METRIC_CONFIDENCE_FLOOR


def test_lookback_matches_claim_window():
    """The shared constant must equal cache_ttl_tuner's claim window_days (7),
    or the baseline (pre-flip) and resolve (post-flip) windows are asymmetric."""
    assert cache_metrics.CACHE_INVALIDATION_LOOKBACK_DAYS == 7


def test_min_events_matches_monitor_floor():
    """Resolver's high-confidence floor must equal the monitor's fire floor so
    the baseline-producing and verify ends stay symmetric."""
    import session_economics

    assert (
        cache_metrics.CACHE_INVALIDATION_MIN_EVENTS
        == session_economics.DEFAULTS["cache_min_events"]
    )


# ── registration + the verify daemon's calling path ───────────────────────────


def test_metric_is_registered():
    assert "cache_invalidation_ratio" in registry.known()


def test_resolves_through_the_registry_not_unknown_metric(fake_events):
    """This is the exact path verify/daemon uses — it must NOT raise
    UnknownMetricError anymore (the roadmap-1.1 force-flag bug)."""
    fake_events(_cache_events(warm=80, invalidated=20))  # 0.20
    mv = registry.resolve("cache_invalidation_ratio", "team_bot_a", AS_OF)
    assert mv.value == pytest.approx(0.20)


def test_unregistered_metric_still_raises(fake_events):
    """Sanity check: the registry still force-flags genuinely-unknown metrics —
    we fixed cache_invalidation_ratio specifically, not the whole error path."""
    with pytest.raises(registry.UnknownMetricError):
        registry.resolve("cache_invalidation_ratio_typo", "team_bot_a", AS_OF)


# ── end-to-end: a cache_ttl_tuner claim grades succeeded / failed ──────────────
#
# cache_ttl_tuner emits Claim(metric=cache_invalidation_ratio, direction=down,
# magnitude=0.0, window_days=7, baseline=<observed ratio>). With magnitude 0,
# "down" holds iff the post-flip ratio is at or below the baseline.


def _ttl_tuner_claim(baseline: float):
    return proposal_schema.Claim(
        metric="cache_invalidation_ratio",
        direction="down",
        magnitude=0.0,
        window_days=7,
        baseline=baseline,
        fallback="revert",
    )


def test_down_claim_succeeds_when_invalidation_drops(fake_events):
    """baseline 0.45 (pre-flip), post-flip reading 0.10 → claim held."""
    claim = _ttl_tuner_claim(baseline=0.45)
    fake_events(_cache_events(warm=90, invalidated=10))  # 0.10
    current = registry.resolve("cache_invalidation_ratio", "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.result == "succeeded"
    assert result.threshold_met is True


def test_down_claim_fails_when_invalidation_rises(fake_events):
    """baseline 0.20, post-flip reading 0.50 (worse) → claim did not hold;
    fallback=revert walks the change back without operator intervention."""
    claim = _ttl_tuner_claim(baseline=0.20)
    fake_events(_cache_events(warm=50, invalidated=50))  # 0.50
    current = registry.resolve("cache_invalidation_ratio", "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.result == "failed"
    assert result.threshold_met is False


def test_thin_window_grades_unresolved_not_succeeded(fake_events):
    """A claim verified against a thin window must be unresolved (retry), not
    silently graded as a win on noisy data."""
    claim = _ttl_tuner_claim(baseline=0.45)
    fake_events(_cache_events(warm=3, invalidated=1))  # 4 participating < 50
    current = registry.resolve("cache_invalidation_ratio", "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.result == "unresolved"

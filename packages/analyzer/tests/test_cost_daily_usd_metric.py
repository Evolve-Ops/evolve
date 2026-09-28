"""cost.daily_usd resolver + registration (roadmap item 1.1).

Before this, Budget Hawk's TierAdjustment claims targeted an UNREGISTERED metric
(`cost.daily_usd`), so the verify daemon raised UnknownMetricError → force-flag,
and the optimizer loop could never close on a dollar-denominated claim. This
registers a noise-robust trailing-average $/day resolver and proves it resolves
through the registry and satisfies the Claim contract end to end.
"""

from __future__ import annotations

import importlib
from datetime import datetime

import pytest

cost_metrics = importlib.import_module("metrics.resolvers.cost_metrics")
registry = importlib.import_module("metrics.registry")
evaluate = importlib.import_module("verify.evaluate")
proposal_schema = importlib.import_module("schema.proposal")

AS_OF = datetime(2026, 6, 9, 12, 0, 0)


@pytest.fixture
def fake_events(monkeypatch):
    """Patch cost_ledger.read_events (imported locally inside the resolver)."""
    def _set(events):
        monkeypatch.setattr("cost_ledger.read_events", lambda *a, **k: iter(events))
    return _set


# ── the resolver ───────────────────────────────────────────────────────────────


def test_averages_total_over_the_window(fake_events):
    # $14 over a 7-day window → $2.00/day
    fake_events([{"cost_usd": 7.0}, {"cost_usd": 7.0}])
    mv = cost_metrics.resolve_cost_daily_usd("team_bot_a", AS_OF)
    assert mv.value == pytest.approx(2.0)
    assert mv.confidence == 1.0


def test_no_data_is_low_confidence_not_zero_dollars(fake_events):
    """Missing data must be unresolved/retry, never a real $0/day that would
    spuriously satisfy a down-claim."""
    fake_events([])
    mv = cost_metrics.resolve_cost_daily_usd("team_bot_a", AS_OF)
    assert mv.value == 0.0
    assert mv.confidence < evaluate.METRIC_CONFIDENCE_FLOOR


def test_lookback_matches_claim_window():
    """The shared constant must equal Budget Hawk's claim window_days (7), or the
    baseline (pre) and resolve (post) windows would be asymmetric."""
    assert cost_metrics.DAILY_USD_LOOKBACK_DAYS == 7


# ── registration + the verify daemon's calling path ─────────────────────────────


def test_metric_is_registered():
    assert "cost.daily_usd" in registry.known()


def test_resolves_through_the_registry(fake_events):
    """This is the exact path verify/daemon uses — it must NOT raise
    UnknownMetricError anymore."""
    fake_events([{"cost_usd": 3.5}])  # $3.50 / 7d = $0.50/day
    mv = registry.resolve("cost.daily_usd", "team_bot_a", AS_OF)
    assert mv.value == pytest.approx(0.5)


# ── end-to-end: the metric satisfies the Claim contract ─────────────────────────


def test_down_claim_succeeds_when_spend_drops(fake_events):
    """baseline $2/day, claim down by $0.50; post-change reading $1/day → met."""
    claim = proposal_schema.Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=0.50,
        window_days=7,
        baseline=2.0,
    )
    fake_events([{"cost_usd": 7.0}])  # $7 / 7d = $1.00/day
    current = registry.resolve("cost.daily_usd", "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.threshold_met is True


def test_down_claim_not_met_when_spend_barely_moves(fake_events):
    claim = proposal_schema.Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=0.50,
        window_days=7,
        baseline=2.0,
    )
    fake_events([{"cost_usd": 12.6}])  # $12.60 / 7d = $1.80/day → only $0.20 drop
    current = registry.resolve("cost.daily_usd", "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.threshold_met is False

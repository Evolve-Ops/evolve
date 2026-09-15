"""Apply-time baseline fill (roadmap item 1.2).

Budget Hawk's cost.daily_usd claims ship a placeholder baseline=0.0 and
baseline_at_apply=True. The applier must overwrite that with a live metric
reading taken when the change actually lands — the proposal may have queued for
days, and the true pre-change baseline is only knowable at apply time. Claims
that author a real creation-time baseline (baseline_at_apply=False) must be left
untouched.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

apply_mod = importlib.import_module("arbiter.apply")
registry = importlib.import_module("metrics.registry")
proposal_schema = importlib.import_module("schema.proposal")

AS_OF = datetime(2026, 6, 9, tzinfo=timezone.utc)


def _proposal(claim) -> SimpleNamespace:
    # _fill_apply_time_baseline only touches .claim, .bot_id, .id
    return SimpleNamespace(claim=claim, bot_id="team_bot_a", id="p-1")


def _claim(*, baseline=0.0, baseline_at_apply=True, metric="cost.daily_usd"):
    return proposal_schema.Claim(
        metric=metric,
        direction="down",
        magnitude=0.50,
        window_days=7,
        baseline=baseline,
        baseline_at_apply=baseline_at_apply,
    )


def test_fills_baseline_from_live_reading(monkeypatch):
    monkeypatch.setattr(
        "metrics.registry.resolve",
        lambda *a, **k: registry.MetricValue(value=2.0, confidence=1.0, source_note="x"),
    )
    prop = _proposal(_claim(baseline=0.0))
    filled = apply_mod._fill_apply_time_baseline(prop, as_of=AS_OF)
    assert filled is True
    assert prop.claim.baseline == 2.0


def test_does_not_clobber_authored_baseline(monkeypatch):
    """A claim that opted OUT keeps its creation-time baseline (the resolver must
    not even be consulted)."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("resolve must not be called for baseline_at_apply=False")

    monkeypatch.setattr("metrics.registry.resolve", _boom)
    prop = _proposal(_claim(baseline=0.42, baseline_at_apply=False))
    filled = apply_mod._fill_apply_time_baseline(prop, as_of=AS_OF)
    assert filled is False
    assert prop.claim.baseline == 0.42
    assert called["n"] == 0


def test_low_confidence_keeps_placeholder(monkeypatch):
    monkeypatch.setattr(
        "metrics.registry.resolve",
        lambda *a, **k: registry.MetricValue(value=9.9, confidence=0.3, source_note="no data"),
    )
    prop = _proposal(_claim(baseline=0.0))
    filled = apply_mod._fill_apply_time_baseline(prop, as_of=AS_OF)
    assert filled is False
    assert prop.claim.baseline == 0.0  # not the 9.9 low-confidence reading


def test_unknown_metric_does_not_raise(monkeypatch):
    def _raise(*a, **k):
        raise registry.UnknownMetricError("nope")

    monkeypatch.setattr("metrics.registry.resolve", _raise)
    prop = _proposal(_claim(baseline=0.0))
    filled = apply_mod._fill_apply_time_baseline(prop, as_of=AS_OF)
    assert filled is False
    assert prop.claim.baseline == 0.0


def test_budget_hawk_claims_opt_in():
    """The generator's claims must carry the flag, or the fill never fires."""
    src = importlib.import_module("generators.budget_hawk.proposals")
    import inspect
    text = inspect.getsource(src)
    # every cost.daily_usd Claim in this module pairs with baseline_at_apply=True
    assert text.count("baseline_at_apply=True") == text.count('metric="cost.daily_usd"')

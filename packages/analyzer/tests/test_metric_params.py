"""Parameterized (cell-scoped) metric resolution (roadmap item 1.3).

The verify daemon resolves metrics as ``resolve(name, bot_id, as_of)`` — no way to
pass the ``(noun, verb)`` of a *cluster cell*. So Efficiency Hawk's
``cluster.engagement_trend`` claim was structurally unreachable: its registered
resolver was a scaffold returning confidence 0.3 (below the 0.5 floor → forever
unresolved).

Fix: carry the cell identity as query params on the metric name
(``cluster.engagement_trend?noun=scheduling&verb=asked``); the registry parses them
and forwards them to resolvers that declare them. 2-arg resolvers are untouched, and
the daemon/Claim schema don't change at all.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

registry = importlib.import_module("metrics.registry")
import metrics.resolvers  # noqa: E402,F401 — registers all resolvers
cluster_metrics = importlib.import_module("metrics.resolvers.cluster_metrics")

AS_OF = datetime(2026, 6, 9, tzinfo=timezone.utc)


# ── name parsing / building ─────────────────────────────────────────────────────


def test_parameterized_and_split_roundtrip():
    name = registry.parameterized("cluster.engagement_trend", noun="scheduling", verb="asked")
    base, params = registry._split_params(name)
    assert base == "cluster.engagement_trend"
    assert params == {"noun": "scheduling", "verb": "asked"}


def test_plain_name_has_no_params():
    assert registry._split_params("cost.daily_usd") == ("cost.daily_usd", {})


def test_spec_of_strips_params():
    assert registry.spec_of("cluster.engagement_trend?noun=x&verb=y").name == "cluster.engagement_trend"


# ── resolve forwards params only where declared ─────────────────────────────────


def test_resolve_forwards_declared_params(monkeypatch):
    captured = {}

    def fake(bot_id, as_of, *, noun=None, verb=None):
        captured.update(noun=noun, verb=verb)
        return registry.MetricValue(value=1.0, confidence=1.0)

    monkeypatch.setitem(registry._REGISTRY, "x.metric",
                        (registry.MetricSpec("x.metric", "", "", ""), fake))
    registry.resolve("x.metric?noun=a&verb=b", "team_bot_a", AS_OF)
    assert captured == {"noun": "a", "verb": "b"}


def test_resolve_does_not_pass_params_to_2arg_resolver(monkeypatch):
    def two_arg(bot_id, as_of):  # declares no params — must not receive them
        return registry.MetricValue(value=2.0)

    monkeypatch.setitem(registry._REGISTRY, "y.metric",
                        (registry.MetricSpec("y.metric", "", "", ""), two_arg))
    assert registry.resolve("y.metric?noun=a", "team_bot_a", AS_OF).value == 2.0  # no crash


def test_unknown_base_still_raises():
    import pytest
    with pytest.raises(registry.UnknownMetricError):
        registry.resolve("nope.metric?noun=a", "team_bot_a", AS_OF)


# ── the cluster claim is now reachable with context ─────────────────────────────


def _tuple(noun, verb, engagement, day, session):
    since = AS_OF - timedelta(days=30)
    return SimpleNamespace(
        noun=noun, verb=verb, engagement=engagement,
        timestamp_start=(since + timedelta(days=day)).isoformat(),
        session_id=session,
    )


def test_cluster_engagement_trend_resolves_with_params(monkeypatch):
    tuples = [
        _tuple("scheduling", "asked", 5, 2, "s1"),
        _tuple("scheduling", "asked", 3, 10, "s2"),
        _tuple("scheduling", "asked", 1, 20, "s3"),   # declining over time
        _tuple("other", "did", 9, 5, "s4"),           # different cell → ignored
    ]
    monkeypatch.setattr(cluster_metrics, "_iter_tuples", lambda *a, **k: iter(tuples))
    mv = registry.resolve("cluster.engagement_trend?noun=scheduling&verb=asked", "team_bot_a", AS_OF)
    assert mv.confidence >= 0.5   # a REAL reading now, not the 0.3 scaffold
    assert mv.value < 0           # engagement declining → negative slope


def test_cluster_engagement_trend_unresolvable_without_params():
    mv = registry.resolve("cluster.engagement_trend", "team_bot_a", AS_OF)
    assert mv.confidence < 0.5    # scaffold path → daemon leaves unresolved/retry

"""cluster.engagement_level + Efficiency Hawk claim correction (1.3 follow-up).

1.3 fixed the plumbing (cell-scoped metric params) but exposed that Efficiency
Hawk claimed `cluster.engagement_trend` (a SLOPE) with `baseline=ratio` (a
turns/session LEVEL) — a units mismatch that would spuriously always-succeed.

The fix: add `cluster.engagement_level` (engagement_total / distinct_sessions,
identical to the detector's `ratio`) and point the streamline claim at it, so the
verify daemon compares like-for-like (did turns/session actually fall?).
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

registry = importlib.import_module("metrics.registry")
import metrics.resolvers  # noqa: E402,F401 — registers resolvers
cluster_metrics = importlib.import_module("metrics.resolvers.cluster_metrics")
evaluate = importlib.import_module("verify.evaluate")
proposal_schema = importlib.import_module("schema.proposal")

_ANALYZER = Path(__file__).parent.parent
AS_OF = datetime(2026, 6, 9, tzinfo=timezone.utc)


def _tuple(noun, verb, engagement, session, day=1):
    since = AS_OF - timedelta(days=14)
    return SimpleNamespace(
        noun=noun, verb=verb, engagement=engagement, session_id=session,
        timestamp_start=(since + timedelta(days=day)).isoformat(),
    )


def test_engagement_level_is_total_over_distinct_sessions(monkeypatch):
    # 3 distinct sessions, total engagement 24 → 8.0 turns/session
    tuples = [
        _tuple("scheduling", "asked", 10, "s1"),
        _tuple("scheduling", "asked", 8, "s2"),
        _tuple("scheduling", "asked", 6, "s3"),
        _tuple("other", "did", 99, "s9"),  # different cell → ignored
    ]
    monkeypatch.setattr(cluster_metrics, "_iter_tuples", lambda *a, **k: iter(tuples))
    mv = registry.resolve("cluster.engagement_level?noun=scheduling&verb=asked", "team_bot_a", AS_OF)
    assert mv.value == 8.0
    assert mv.confidence == 1.0


def test_engagement_level_unresolvable_without_params():
    assert registry.resolve("cluster.engagement_level", "team_bot_a", AS_OF).confidence < 0.5


def test_engagement_level_no_sessions_is_low_confidence(monkeypatch):
    monkeypatch.setattr(cluster_metrics, "_iter_tuples", lambda *a, **k: iter([]))
    mv = registry.resolve("cluster.engagement_level?noun=x&verb=y", "team_bot_a", AS_OF)
    assert mv.confidence < 0.5  # missing data → unresolved, not a real 0.0 level


def test_streamline_claim_targets_the_level_metric():
    """The generator must author the LEVEL metric (parameterized), not the
    bare slope metric that caused the spurious-success mismatch."""
    src = (_ANALYZER / "generators" / "efficiency_hawk" / "observe.py").read_text()
    assert 'parameterized("cluster.engagement_level"' in src
    assert 'metric="cluster.engagement_trend"' not in src  # the mismatched claim is gone


def test_streamline_claim_closes_when_engagement_drops(monkeypatch):
    """End-to-end: a cluster detected at 8 turns/session drops to 5 → the
    'down by ≥1.5' claim holds when resolved against the real level metric."""
    claim = proposal_schema.Claim(
        metric=registry.parameterized("cluster.engagement_level", noun="scheduling", verb="asked"),
        direction="down",
        magnitude=1.5,
        window_days=14,
        baseline=8.0,
    )
    # post-change: 10 engagement over 2 sessions = 5.0 turns/session
    tuples = [_tuple("scheduling", "asked", 5, "s1"), _tuple("scheduling", "asked", 5, "s2")]
    monkeypatch.setattr(cluster_metrics, "_iter_tuples", lambda *a, **k: iter(tuples))
    current = registry.resolve(claim.metric, "team_bot_a", AS_OF)
    result = evaluate.evaluate_claim(claim, current.value, current.confidence)
    assert result.threshold_met is True  # 8.0 − 5.0 = 3.0 ≥ 1.5


def test_streamline_claim_fails_when_engagement_unchanged(monkeypatch):
    claim = proposal_schema.Claim(
        metric=registry.parameterized("cluster.engagement_level", noun="scheduling", verb="asked"),
        direction="down", magnitude=1.5, window_days=14, baseline=8.0,
    )
    # post-change: still 8.0 turns/session
    tuples = [_tuple("scheduling", "asked", 8, "s1"), _tuple("scheduling", "asked", 8, "s2")]
    monkeypatch.setattr(cluster_metrics, "_iter_tuples", lambda *a, **k: iter(tuples))
    current = registry.resolve(claim.metric, "team_bot_a", AS_OF)
    assert evaluate.evaluate_claim(claim, current.value, current.confidence).threshold_met is False

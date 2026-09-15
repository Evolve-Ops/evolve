"""tests/test_rsi_l4_metrics.py — L4 cluster + app metric resolvers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from metrics import known, resolve  # noqa: E402
from metrics.resolvers import cluster_metrics as cm  # noqa: E402
from observations.tuples import write_tuples  # noqa: E402
from schema import ObservationTuple, new_tuple_id  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _seed_tuples(tmp_path, bot_id, *, days_back=0, n=5, noun="fitness", verb="tracking"):
    day = _NOW - timedelta(days=days_back)
    ts = day.isoformat()
    tuples = [
        ObservationTuple(
            id=new_tuple_id(),
            bot_id=bot_id,
            session_id=f"s{i}",
            segment_id="seg",
            noun=noun,
            verb=verb,
            mood="neutral",
            engagement=3,
            timestamp_start=ts,
            timestamp_end=ts,
            source_hash=f"sh-{days_back}-{i}",
        )
        for i in range(n)
    ]
    write_tuples(tuples, shared_dir=tmp_path, bot_id=bot_id, day=day)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


def test_l4_metrics_registered():
    for expected in (
        "app.invocations_per_week",
        "app.session_count",
        "cluster.engagement_trend",
        "cluster.last_activity_days",
        "user.positive_action_rate",
    ):
        assert expected in known()


def test_scaffolded_metrics_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        resolve("cluster.last_activity_days", "team_bot_a", _NOW)
    with pytest.raises(NotImplementedError):
        resolve("user.positive_action_rate", "team_bot_a", _NOW)


# ─────────────────────────────────────────────────────────────────────────────
# app.invocations_per_week (for-app variant)
# ─────────────────────────────────────────────────────────────────────────────


def test_app_invocations_counts_matching_nouns(tmp_path):
    cm.set_shared_dir(tmp_path)
    cm.set_app_nouns_resolver(lambda b, app: ["fitness"])
    try:
        _seed_tuples(tmp_path, "team_bot_a", days_back=0, n=4, noun="fitness")
        _seed_tuples(tmp_path, "team_bot_a", days_back=1, n=3, noun="email")  # different noun
        v = cm.resolve_app_invocations_per_week_for_app("team_bot_a", "fitness-app", _NOW)
        assert v.value == 4.0
    finally:
        cm.set_shared_dir(Path("/Users/Shared/evolve"))
        cm.set_app_nouns_resolver(cm._default_app_nouns)


def test_app_invocations_excludes_stale_outside_window(tmp_path):
    cm.set_shared_dir(tmp_path)
    cm.set_app_nouns_resolver(lambda b, app: ["fitness"])
    try:
        _seed_tuples(tmp_path, "team_bot_a", days_back=30, n=5, noun="fitness")  # too old
        v = cm.resolve_app_invocations_per_week_for_app("team_bot_a", "fitness-app", _NOW)
        assert v.value == 0.0
    finally:
        cm.set_shared_dir(Path("/Users/Shared/evolve"))
        cm.set_app_nouns_resolver(cm._default_app_nouns)


def test_app_invocations_unscoped_returns_low_confidence():
    # The unscoped metric call returns a placeholder with low confidence
    v = resolve("app.invocations_per_week", "team_bot_a", _NOW)
    assert v.value == 0.0
    assert v.confidence < 0.5


# ─────────────────────────────────────────────────────────────────────────────
# app.session_count (for-app variant)
# ─────────────────────────────────────────────────────────────────────────────


def test_app_session_count_counts_distinct_sessions(tmp_path):
    cm.set_shared_dir(tmp_path)
    cm.set_app_nouns_resolver(lambda b, app: ["fitness"])
    try:
        _seed_tuples(tmp_path, "team_bot_a", days_back=1, n=3, noun="fitness")
        # 3 tuples across 3 different sessions (s0, s1, s2) per _seed_tuples
        v = cm.resolve_app_session_count_for_app("team_bot_a", "fitness-app", _NOW)
        assert v.value == 3.0
    finally:
        cm.set_shared_dir(Path("/Users/Shared/evolve"))
        cm.set_app_nouns_resolver(cm._default_app_nouns)


# ─────────────────────────────────────────────────────────────────────────────
# cluster.engagement_trend (for-cluster variant)
# ─────────────────────────────────────────────────────────────────────────────


def test_engagement_trend_positive_when_rising(tmp_path):
    cm.set_shared_dir(tmp_path)
    try:
        # Day 10 ago: 1 tuple engagement=1
        # Day 5 ago: 3 tuples engagement=3 each → total 9
        # Day 0: 5 tuples engagement=5 each → total 25
        for days_back, n, eng in [(10, 1, 1), (5, 3, 3), (0, 5, 5)]:
            day = _NOW - timedelta(days=days_back)
            tuples = [
                ObservationTuple(
                    id=new_tuple_id(),
                    bot_id="team_bot_a",
                    session_id=f"s-{days_back}-{i}",
                    segment_id="seg",
                    noun="fitness",
                    verb="tracking",
                    mood="neutral",
                    engagement=eng,
                    timestamp_start=day.isoformat(),
                    timestamp_end=day.isoformat(),
                    source_hash=f"sh-{days_back}-{i}",
                )
                for i in range(n)
            ]
            write_tuples(tuples, shared_dir=tmp_path, bot_id="team_bot_a", day=day)

        v = cm.resolve_cluster_engagement_trend_for_cluster(
            "team_bot_a", "fitness", "tracking", _NOW, lookback_days=30
        )
        assert v.value > 0  # rising
    finally:
        cm.set_shared_dir(Path("/Users/Shared/evolve"))


def test_engagement_trend_low_confidence_with_sparse_data(tmp_path):
    cm.set_shared_dir(tmp_path)
    try:
        # Only one day of data
        _seed_tuples(tmp_path, "team_bot_a", days_back=0, n=2, noun="fitness", verb="tracking")
        v = cm.resolve_cluster_engagement_trend_for_cluster(
            "team_bot_a", "fitness", "tracking", _NOW, lookback_days=30
        )
        assert v.confidence < 0.5
    finally:
        cm.set_shared_dir(Path("/Users/Shared/evolve"))


def test_engagement_trend_unscoped_returns_placeholder():
    v = resolve("cluster.engagement_trend", "team_bot_a", _NOW)
    assert v.confidence < 0.5
    assert "requires" in v.source_note

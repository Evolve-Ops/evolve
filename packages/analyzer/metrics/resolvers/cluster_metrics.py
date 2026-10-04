"""Cluster + app metric resolvers (L4).

Functional:
  - ``app.invocations_per_week``: count tuples whose noun matches the app
    display_name or capability_tag in the last 7 days
  - ``app.session_count``: count distinct sessions touching the app
  - ``cluster.engagement_trend``: linear slope of engagement over time

Scaffolded (raise NotImplementedError, filled in by later layers):
  - ``cluster.last_activity_days``
  - ``user.positive_action_rate``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register


_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


# App name resolution — for L4 this reads from an in-memory override.
# Production replaces ``_resolve_app_nouns`` with a function that reads
# the bot's manifests.

_AppNounsFn = Callable[[str, str], list[str]]


def _default_app_nouns(bot_id: str, app_id: str) -> list[str]:  # noqa: ARG001
    """Default: use app_id as the only noun candidate."""
    return [app_id.lower()]


_app_nouns_fn: _AppNounsFn = _default_app_nouns


def set_app_nouns_resolver(fn: _AppNounsFn) -> None:
    global _app_nouns_fn
    _app_nouns_fn = fn


def _app_nouns(bot_id: str, app_id: str) -> set[str]:
    return {n.lower() for n in _app_nouns_fn(bot_id, app_id)}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _iter_tuples(bot_id: str, start: datetime, end: datetime):
    """Stream ObservationTuples for bot in [start, end]."""
    from observations.tuples import read_tuples_range

    yield from read_tuples_range(_SHARED_DIR, bot_id, start, end)


# ─────────────────────────────────────────────────────────────────────────────
# app.invocations_per_week
# ─────────────────────────────────────────────────────────────────────────────


def resolve_app_invocations_per_week(
    bot_id: str, as_of: datetime
) -> MetricValue:
    """Count tuples whose noun matches the target app in the last 7 days.

    Tuples don't carry an explicit app_id; the resolver looks up the
    app's candidate nouns (default: just the app_id) and matches against
    each tuple's noun. Requires the caller to have set
    ``app_id`` via a separate context hook — L4's scaffold uses the
    metric's implicit expectation that the caller scoped the app.

    Since the MetricResolver signature is (bot_id, as_of) without an app
    selector, L4 treats the last-touched app as a naive heuristic: this
    resolver is not meant to be called without additional context. It
    returns a low-confidence MetricValue indicating the limitation.
    """
    return MetricValue(
        value=0.0,
        confidence=0.3,
        source_note=(
            "app.invocations_per_week requires an app_id context; "
            "callers should use resolve_app_invocations_per_week_for_app"
        ),
    )


def resolve_app_invocations_per_week_for_app(
    bot_id: str, app_id: str, as_of: datetime
) -> MetricValue:
    """App-scoped variant — what Adjacency Explorer / Gap Filler actually use."""
    since = as_of - timedelta(days=7)
    candidates = _app_nouns(bot_id, app_id)
    if not candidates:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note=f"no noun candidates for app {app_id!r}",
        )

    count = 0
    for t in _iter_tuples(bot_id, since, as_of):
        if t.noun in candidates:
            count += 1

    return MetricValue(
        value=float(count),
        confidence=1.0,
        source_note=(
            f"counted {count} tuples with noun matching "
            f"{sorted(candidates)!r} in last 7 days"
        ),
    )


register(
    MetricSpec(
        name="app.invocations_per_week",
        description="Count of observation tuples matching the app over the last 7 days.",
        unit="count",
        source="observation tuples; app noun resolved from manifest",
    ),
    resolve_app_invocations_per_week,
)


# ─────────────────────────────────────────────────────────────────────────────
# app.session_count
# ─────────────────────────────────────────────────────────────────────────────


def resolve_app_session_count(bot_id: str, as_of: datetime) -> MetricValue:
    """Scaffold (same context limitation as invocations_per_week)."""
    return MetricValue(
        value=0.0,
        confidence=0.3,
        source_note="app.session_count requires an app_id context",
    )


def resolve_app_session_count_for_app(
    bot_id: str, app_id: str, as_of: datetime
) -> MetricValue:
    """App-scoped variant — number of distinct sessions touching the app in 14d."""
    since = as_of - timedelta(days=14)
    candidates = _app_nouns(bot_id, app_id)
    sessions: set[str] = set()
    for t in _iter_tuples(bot_id, since, as_of):
        if t.noun in candidates:
            sessions.add(t.session_id)
    return MetricValue(
        value=float(len(sessions)),
        confidence=1.0,
        source_note=f"{len(sessions)} distinct sessions touched app {app_id!r}",
    )


register(
    MetricSpec(
        name="app.session_count",
        description="Distinct sessions touching this app over the last 14 days.",
        unit="count",
        source="observation tuples",
    ),
    resolve_app_session_count,
)


# ─────────────────────────────────────────────────────────────────────────────
# cluster.engagement_trend
# ─────────────────────────────────────────────────────────────────────────────


def resolve_cluster_engagement_trend(
    bot_id: str,
    as_of: datetime,
    *,
    noun: str | None = None,
    verb: str | None = None,
    lookback_days: int = 30,
) -> MetricValue:
    """Engagement-trend slope for a (noun, verb) cluster cell.

    The cluster identity rides as metric params (e.g.
    ``cluster.engagement_trend?noun=scheduling&verb=asked``); the metric
    registry parses them and forwards them here. Without them the metric is
    unresolvable — low confidence so the verify daemon leaves the claim
    unresolved/retry rather than acting on a meaningless 0.0.
    """
    if not noun or not verb:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note="cluster.engagement_trend requires noun+verb params",
        )
    return resolve_cluster_engagement_trend_for_cluster(
        bot_id, noun, verb, as_of, lookback_days=lookback_days
    )


def resolve_cluster_engagement_trend_for_cluster(
    bot_id: str, noun: str, verb: str, as_of: datetime, lookback_days: int = 30
) -> MetricValue:
    """Linear slope of daily engagement for a (noun, verb) cell over lookback.

    Simple least-squares fit: slope > 0 is "rising trend"; slope < 0 is
    "declining"; ~0 is stable.
    """
    since = as_of - timedelta(days=lookback_days)
    # Bucket daily engagement
    daily: dict[int, int] = {}
    for t in _iter_tuples(bot_id, since, as_of):
        if t.noun != noun.lower() or t.verb != verb.lower():
            continue
        # Day index from since
        ts = _parse_iso(t.timestamp_start)
        if ts is None:
            continue
        day_idx = (ts - since).days
        daily[day_idx] = daily.get(day_idx, 0) + t.engagement

    if len(daily) < 3:
        return MetricValue(
            value=0.0,
            confidence=0.4,
            source_note=f"too few days with activity ({len(daily)}); no trend",
        )

    xs = sorted(daily.keys())
    ys = [daily[x] for x in xs]
    slope = _least_squares_slope(xs, ys)
    return MetricValue(
        value=float(slope),
        confidence=0.9,
        source_note=(
            f"slope={slope:.3f} over {len(xs)} active days for "
            f"({noun},{verb})"
        ),
    )


register(
    MetricSpec(
        name="cluster.engagement_trend",
        description="Linear slope of daily engagement for a (noun, verb) cell.",
        unit="signed rate",
        source="observation tuples; fit over recent active days",
    ),
    resolve_cluster_engagement_trend,
)


# ─────────────────────────────────────────────────────────────────────────────
# cluster.engagement_level
# ─────────────────────────────────────────────────────────────────────────────

# Trailing window for the cluster engagement-LEVEL metric. Matches the
# window_days on Efficiency Hawk's streamline claim so baseline (pre) and verify
# (post) read symmetric windows.
CLUSTER_LEVEL_LOOKBACK_DAYS: int = 14


def resolve_cluster_engagement_level_for_cluster(
    bot_id: str,
    noun: str,
    verb: str,
    as_of: datetime,
    lookback_days: int = CLUSTER_LEVEL_LOOKBACK_DAYS,
) -> MetricValue:
    """Average engagement (turns) per session for a (noun, verb) cluster cell.

    Computed as ``engagement_total / distinct_sessions`` over the trailing
    window — identical to Efficiency Hawk's detection-time ``ratio``
    (generators/efficiency_hawk/observe.py). A streamline claim with
    ``baseline=ratio`` therefore compares like-for-like: did the cluster's
    turns/session fall after the change? This is the LEVEL the claim targets —
    distinct from ``cluster.engagement_trend`` (the slope), which is the wrong
    shape for a level baseline.
    """
    since = as_of - timedelta(days=lookback_days)
    engagement_total = 0
    sessions: set[str] = set()
    for t in _iter_tuples(bot_id, since, as_of):
        if t.noun != noun.lower() or t.verb != verb.lower():
            continue
        engagement_total += t.engagement
        sessions.add(t.session_id)
    if not sessions:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note=(
                f"no sessions for ({noun},{verb}) in trailing {lookback_days}d; "
                "insufficient data"
            ),
        )
    level = engagement_total / len(sessions)
    return MetricValue(
        value=round(level, 4),
        confidence=1.0,
        source_note=(
            f"{engagement_total} engagement / {len(sessions)} sessions "
            f"= {level:.2f} turns/session for ({noun},{verb})"
        ),
    )


def resolve_cluster_engagement_level(
    bot_id: str,
    as_of: datetime,
    *,
    noun: str | None = None,
    verb: str | None = None,
    lookback_days: int = CLUSTER_LEVEL_LOOKBACK_DAYS,
) -> MetricValue:
    """Registered entry: requires (noun, verb) carried as metric params
    (``cluster.engagement_level?noun=...&verb=...``). Without them the metric is
    unresolvable (low confidence → verify daemon leaves the claim unresolved)."""
    if not noun or not verb:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note="cluster.engagement_level requires noun+verb params",
        )
    return resolve_cluster_engagement_level_for_cluster(
        bot_id, noun, verb, as_of, lookback_days=lookback_days
    )


register(
    MetricSpec(
        name="cluster.engagement_level",
        description="Average engagement (turns) per session for a (noun, verb) cell.",
        unit="turns_per_session",
        source="observation tuples; engagement_total / distinct_sessions",
    ),
    resolve_cluster_engagement_level,
)


# ─────────────────────────────────────────────────────────────────────────────
# Scaffolded (L5+)
# ─────────────────────────────────────────────────────────────────────────────


def resolve_cluster_last_activity_days(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    raise NotImplementedError(
        "cluster.last_activity_days is scaffolded for L5+. "
        "Add a per-cluster context variant when the first consumer ships."
    )


register(
    MetricSpec(
        name="cluster.last_activity_days",
        description="Days since last observation in a cluster (L5+).",
        unit="count",
        source="observation tuples",
    ),
    resolve_cluster_last_activity_days,
)


def resolve_user_positive_action_rate(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    raise NotImplementedError(
        "user.positive_action_rate is scaffolded for L5+. "
        "Requires proposal history aggregation that lands with L5 calibration."
    )


register(
    MetricSpec(
        name="user.positive_action_rate",
        description="Fraction of past proposals the user accepted (L5+).",
        unit="ratio",
        source="proposal history",
    ),
    resolve_user_positive_action_rate,
)


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────


def _least_squares_slope(xs: list[int], ys: list[int]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return num / den


def _parse_iso(raw: str) -> datetime | None:
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

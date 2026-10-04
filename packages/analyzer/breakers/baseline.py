"""breakers.baseline — rolling per-bot baseline for the detector.

A Baseline is the bot's "normal" activity shape over a recent window
(default 7 days, excluding the most recent N hours so an in-progress
spike doesn't pollute its own comparison point).

Three numbers we need:

  - auto_rate_per_hr           — turns/hr where classify_turn() == "auto"
  - human_rate_per_hr          — turns/hr where classify_turn() == "human"
  - auto_high_tier_share       — of auto turns, fraction running on a
                                 high-tier model (sonnet/opus/gpt-4/grok)

Cold-start (< MIN_BASELINE_DAYS of data): we return a Baseline with
``cold_start=True`` and the rates derived from whatever we have. The
detector switches to absolute floors instead of multiplicative
thresholds when cold_start is True (see detector.evaluate_window).

Baseline computation is pure-Python over an iterable of turn dicts.
No I/O here — the backtest harness handles loading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from breakers.classify import classify_turn, parse_ts


# Default window: 7 days of history, excluding the most recent 1 hour
# so a fresh spike doesn't get baked into its own baseline.
DEFAULT_BASELINE_DAYS: int = 7
DEFAULT_EXCLUDE_RECENT_HOURS: int = 1

# Below this many days of populated data, treat the bot as cold-start.
# Three days is enough to see a heartbeat-cron rhythm but not enough
# to compute a stable multiplier.
MIN_BASELINE_DAYS: int = 3


@dataclass(frozen=True)
class Baseline:
    """A bot's "normal" activity-shape over the baseline window."""

    bot_id: str
    as_of: datetime               # window end (exclusive)
    window_start: datetime        # window start (inclusive)
    window_end: datetime          # excludes the recent-hours buffer
    auto_rate_per_hr: float
    human_rate_per_hr: float
    auto_high_tier_share: float   # 0.0–1.0; 0.0 when no auto turns observed
    auto_turns: int               # absolute count over the window
    human_turns: int              # absolute count over the window
    days_with_data: int           # how many distinct UTC days had ≥1 turn
    cold_start: bool


def compute_baseline(
    *,
    bot_id: str,
    turns: Iterable[dict],
    as_of: datetime,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
    exclude_recent_hours: int = DEFAULT_EXCLUDE_RECENT_HOURS,
) -> Baseline:
    """Compute the rolling baseline for ``bot_id`` as of ``as_of``.

    The window is ``[as_of - baseline_days, as_of - exclude_recent_hours)``.
    Turns outside this window are ignored. Untimestamped turns are
    dropped (parse_ts returns None).
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    window_end = as_of - timedelta(hours=exclude_recent_hours)
    window_start = as_of - timedelta(days=baseline_days)

    auto_count = 0
    human_count = 0
    auto_high_tier = 0
    days_seen: set[str] = set()

    for t in turns:
        ts = parse_ts(t)
        if ts is None:
            continue
        if not (window_start <= ts < window_end):
            continue

        days_seen.add(ts.date().isoformat())
        c = classify_turn(t)
        if c.bucket == "auto":
            auto_count += 1
            if c.model_tier == "high":
                auto_high_tier += 1
        elif c.bucket == "human":
            human_count += 1

    window_hours = max(
        (window_end - window_start).total_seconds() / 3600.0,
        1.0,  # floor to 1h to avoid divide-by-zero on degenerate inputs
    )
    auto_rate = auto_count / window_hours
    human_rate = human_count / window_hours
    high_tier_share = (
        auto_high_tier / auto_count if auto_count > 0 else 0.0
    )

    cold_start = len(days_seen) < MIN_BASELINE_DAYS

    return Baseline(
        bot_id=bot_id,
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        auto_rate_per_hr=auto_rate,
        human_rate_per_hr=human_rate,
        auto_high_tier_share=high_tier_share,
        auto_turns=auto_count,
        human_turns=human_count,
        days_with_data=len(days_seen),
        cold_start=cold_start,
    )

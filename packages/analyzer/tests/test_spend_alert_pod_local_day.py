"""The daily spend ladder buckets turns by the POD-LOCAL day.

Turn JSONL is UTC on both axes — ``TurnObserver`` writes
``turns-${new Date().toISOString().slice(0, 10)}.jsonl`` and every ``ts`` is a
``Z`` instant — while caps, thresholds and dedup keys roll at pod-local
midnight (``pod_time``). ``load_today_spend_detail`` sits exactly on that
boundary.

It used to load ONE UTC day and compare ``ts[:10]`` to the pod-local date.
West of UTC the two stop intersecting the moment UTC rolls over: on the
Pacific pod, every evening from 17:00 local until local midnight, the filter
selected zero turns and the whole daily ladder — threshold alert,
tier-downgrade, per-bot cap, velocity forecast — read a *measurable* $0.00 and
logged "OK". Observed live 2026-09-03: nine bots reporting
``OK ($0.0000 <= $5.00)`` every 5 minutes while one of them was at $30.49.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import spend_alert  # noqa: E402
import live_spend  # noqa: E402


PACIFIC = timezone(timedelta(hours=-7))   # PDT
TOKYO = timezone(timedelta(hours=9))


def _turn(ts: str, cost: float) -> dict:
    return {
        "ts": ts,
        "cost": cost,
        "source": "human",
        "channel": "telegram",
        "model": "anthropic/claude-sonnet-4-6",
    }


@pytest.fixture
def pacific(monkeypatch):
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: PACIFIC)


def test_evening_turns_count_toward_the_local_day_that_is_still_running(
    pacific, monkeypatch, tmp_path,
):
    """21:00 PDT on the 3rd is 04:00Z on the 4th — still local Sep 3."""
    turns = [
        _turn("2026-09-03T16:00:00Z", 1.0),   # 09:00 PDT Sep 3
        _turn("2026-09-04T01:00:00Z", 4.0),   # 18:00 PDT Sep 3
        _turn("2026-09-04T04:00:00Z", 25.0),  # 21:00 PDT Sep 3
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)

    detail = spend_alert.load_today_spend_detail(
        tmp_path, "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(30.0)
    assert detail.priced_turns == 3
    assert detail.measurable


def test_turns_before_local_midnight_belong_to_the_previous_local_day(
    pacific, monkeypatch, tmp_path,
):
    """The mirror case: 2026-09-03T04:00Z is 21:00 PDT on Sep *2*.

    Without it the fix would over-count instead of under-counting — the same
    calendar error with the opposite sign.
    """
    turns = [
        _turn("2026-09-03T04:00:00Z", 99.0),  # 21:00 PDT Sep 2
        _turn("2026-09-03T16:00:00Z", 1.0),   # 09:00 PDT Sep 3
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)

    detail = spend_alert.load_today_spend_detail(
        tmp_path, "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(1.0)


def test_east_of_utc_pod_buckets_by_its_own_local_day(monkeypatch, tmp_path):
    """A UTC+9 pod's local day opens on the PREVIOUS UTC date."""
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: TOKYO)
    turns = [
        _turn("2026-09-02T16:00:00Z", 7.0),   # 01:00 JST Sep 3
        _turn("2026-09-03T02:00:00Z", 3.0),   # 11:00 JST Sep 3
        _turn("2026-09-03T15:30:00Z", 50.0),  # 00:30 JST Sep 4 — not today
    ]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)

    detail = spend_alert.load_today_spend_detail(
        tmp_path, "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(10.0)


def test_loads_two_utc_days_so_the_local_day_is_not_truncated(
    pacific, monkeypatch, tmp_path,
):
    """A pod-local day straddles two UTC files; ``days=1`` can only see one.

    This is the load-side half of the bug — the filter cannot select a turn
    that was never read off disk.
    """
    seen: dict = {}

    def _spy(bot_id, *, days, end, log=None):
        seen["days"] = days
        return []

    monkeypatch.setattr(live_spend, "load_live_turns", _spy)
    spend_alert.load_today_spend_detail(
        tmp_path, "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert seen["days"] == 2


def test_unparseable_ts_drops_out_rather_than_landing_in_today(
    pacific, monkeypatch, tmp_path,
):
    turns = [_turn("not-a-timestamp", 5.0), _turn("", 5.0), _turn("2026-09-03T16:00:00Z", 1.0)]
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)

    detail = spend_alert.load_today_spend_detail(
        tmp_path, "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(1.0)

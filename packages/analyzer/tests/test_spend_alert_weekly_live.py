"""The Monday weekly summary reads live turns, not the daily rollup files.

``_weekly_spend`` used to sum seven ``metrics/<date>/<bot>.json`` files.
``measure.py`` writes each of those at 01:00 pod-local for the day it names
and never regenerates it, so each holds roughly the first hour of its day.

The partiality is UNIFORM across the seven days, and that is precisely what
kept it hidden: a uniformly-scaled total still looks like a plausible dollar
figure. It is then compared against a real threshold
(``weeklySpendAlertUsd``, default $20) that a 3.8%-scaled number can
essentially never cross. Measured on the mini for 2026-09-03: $0.95 captured
of $55.52 actually spent, pod-wide, in one day — 1.7%.
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


PACIFIC = timezone(timedelta(hours=-7))
NOW = datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc)   # 21:30 PDT Sep 3
TODAY = date(2026, 9, 3)


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


def _serve(monkeypatch, per_bot: dict[str, list[dict]]):
    monkeypatch.setattr(
        live_spend, "load_live_turns",
        lambda bot_id, **kw: per_bot.get(bot_id, []),
    )


def test_weekly_total_sums_the_live_local_days(pacific, monkeypatch, tmp_path):
    _serve(monkeypatch, {
        "team_bot_a": [
            _turn("2026-08-29T16:00:00Z", 2.0),
            _turn("2026-09-03T16:00:00Z", 3.0),
        ],
        "team_bot_b": [_turn("2026-09-04T04:00:00Z", 10.0)],   # 21:00 PDT Sep 3
    })
    total, per_bot = spend_alert._weekly_spend(
        tmp_path, ["team_bot_a", "team_bot_b"], TODAY, now=NOW,
    )
    assert per_bot["team_bot_a"] == pytest.approx(5.0)
    assert per_bot["team_bot_b"] == pytest.approx(10.0)
    assert total == pytest.approx(15.0)


def test_a_stale_metrics_file_no_longer_moves_the_total(
    pacific, monkeypatch, tmp_path,
):
    """The regression pin: the near-blind rollup must not be consulted.

    A metrics file claiming a wildly different number is staged for every
    day of the window. If the total still reflects only the live turns, the
    old reader is genuinely gone.
    """
    import json
    for i in range(7):
        d = TODAY - timedelta(days=i)
        p = tmp_path / "metrics" / d.isoformat() / "team_bot_a.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"total_cost_estimated": 999.0}))

    _serve(monkeypatch, {"team_bot_a": [_turn("2026-09-03T16:00:00Z", 3.0)]})
    total, per_bot = spend_alert._weekly_spend(
        tmp_path, ["team_bot_a"], TODAY, now=NOW,
    )
    assert per_bot["team_bot_a"] == pytest.approx(3.0)
    assert total == pytest.approx(3.0)


def test_turns_outside_the_seven_local_days_are_excluded(
    pacific, monkeypatch, tmp_path,
):
    _serve(monkeypatch, {
        "team_bot_a": [
            _turn("2026-08-20T16:00:00Z", 50.0),   # two weeks back
            _turn("2026-09-03T16:00:00Z", 3.0),
        ],
    })
    total, _ = spend_alert._weekly_spend(tmp_path, ["team_bot_a"], TODAY, now=NOW)
    assert total == pytest.approx(3.0)


def test_a_bot_whose_turns_cannot_be_read_is_none_not_zero(
    pacific, monkeypatch, tmp_path,
):
    """"I could not look" must not be folded into the total as $0.00."""
    def _load(bot_id, **kw):
        if bot_id == "broken_bot":
            return live_spend.LIVE_LOAD_FAILED
        return [_turn("2026-09-03T16:00:00Z", 4.0)]

    monkeypatch.setattr(live_spend, "load_live_turns", _load)
    total, per_bot = spend_alert._weekly_spend(
        tmp_path, ["team_bot_a", "broken_bot"], TODAY, now=NOW,
    )
    assert per_bot["broken_bot"] is None
    assert per_bot["team_bot_a"] == pytest.approx(4.0)
    # The headline is a floor over the bots that COULD be read, not a
    # confident understatement that silently includes the broken one as $0.
    assert total == pytest.approx(4.0)


def test_an_idle_bot_is_zero_not_none(pacific, monkeypatch, tmp_path):
    _serve(monkeypatch, {})
    _total, per_bot = spend_alert._weekly_spend(
        tmp_path, ["quiet_bot"], TODAY, now=NOW,
    )
    assert per_bot["quiet_bot"] == pytest.approx(0.0)


def test_unreadable_bots_render_as_na_in_the_breakdown(
    pacific, monkeypatch, tmp_path,
):
    """The operator must be able to tell a quiet bot from an invisible one."""
    sent: dict = {}
    monkeypatch.setattr(
        spend_alert, "_weekly_spend",
        lambda *_a, **_kw: (4.0, {"team_bot_a": 4.0, "broken_bot": None}),
    )

    def _fake_dispatch(**kwargs):
        sent.update(kwargs)
        return False   # don't write the once-per-week flag file

    monkeypatch.setattr(spend_alert, "_dispatch", _fake_dispatch)
    spend_alert._maybe_send_weekly_summary(
        tmp_path, ["team_bot_a", "broken_bot"], TODAY,
        weekly_threshold=20.0, network={},
    )
    breakdown = sent["payload"]["per_bot_breakdown"]
    assert "team_bot_a: $4.00" in breakdown
    assert "broken_bot: n/a (could not read turns)" in breakdown

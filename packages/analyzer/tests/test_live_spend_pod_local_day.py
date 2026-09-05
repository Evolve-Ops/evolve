"""``live_spend`` — the shared pod-local-day spend reader.

Two surfaces used to answer "what has this bot spent today?" from
``{shared_dir}/metrics/<date>/<bot>.json``. That file cannot answer it:
``measure.py`` runs from launchd at 01:00 pod-local with ``--date``
defaulting to ``date.today()``, so the file named for day D is written one
hour into D and never regenerated. Measured on the mini for 2026-09-03, the
pod's files held **$0.95 against $55.52** of real pod-local-day spend —
**1.7%** — and the heaviest bot's said ``$0.0078`` where the day's live
total was ``$51.45``.

The surfaces that read it as "today's spend" were therefore comparing a
floor of roughly zero to a real dollar threshold:

  * ``spend_caps.get_cap_warnings`` — the Cost Measures chip that is meant
    to warn at 80% of the daily cap, and so could not fire;
  * ``routes_trust``'s heartbeat spend reason, same 80% shape;
  * ``spend_alert._weekly_spend`` — every weekly summary a fixed fraction
    of real spend, which is what made it invisible: uniformly-scaled
    numbers still look plausible.

This module is the reader they moved onto. Its whole job is the UTC-storage
/ pod-local-policy boundary, so that is what these tests pin.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

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


def _serve(monkeypatch, turns):
    monkeypatch.setattr(live_spend, "load_live_turns", lambda *a, **kw: turns)


# ── bucketing on the instant, not the ts prefix ──────────────────────────────


def test_evening_turns_belong_to_the_local_day_still_running(pacific, monkeypatch):
    """21:00 PDT Sep 3 is 04:00Z Sep 4 — still pod-local Sep 3."""
    _serve(monkeypatch, [
        _turn("2026-09-03T16:00:00Z", 1.0),   # 09:00 PDT Sep 3
        _turn("2026-09-04T04:00:00Z", 25.0),  # 21:00 PDT Sep 3
    ])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(26.0)


def test_a_turn_before_local_midnight_stays_on_the_previous_local_day(
    pacific, monkeypatch,
):
    """The mirror case — the fix must not over-count into today."""
    _serve(monkeypatch, [
        _turn("2026-09-03T06:00:00Z", 9.0),   # 23:00 PDT Sep 2
        _turn("2026-09-03T16:00:00Z", 1.0),   # 09:00 PDT Sep 3
    ])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(1.0)


def test_east_of_utc_local_day_opens_on_the_previous_utc_date(monkeypatch):
    """A UTC+9 pod's local day starts at 15:00Z the day before."""
    monkeypatch.setattr(live_spend, "pod_tz_or_local", lambda: TOKYO)
    _serve(monkeypatch, [
        _turn("2026-09-02T16:00:00Z", 4.0),   # 01:00 JST Sep 3
        _turn("2026-09-03T02:00:00Z", 2.0),   # 11:00 JST Sep 3
        _turn("2026-09-03T16:00:00Z", 8.0),   # 01:00 JST Sep 4 — NOT Sep 3
    ])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(6.0)


def test_unparseable_ts_drops_out_rather_than_landing_in_today(pacific, monkeypatch):
    _serve(monkeypatch, [
        _turn("not-a-timestamp", 5.0),
        _turn("", 5.0),
        _turn("2026-09-03T16:00:00Z", 1.0),
    ])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(1.0)


# ── the load-side half ───────────────────────────────────────────────────────


def test_one_local_day_loads_two_utc_files(pacific, monkeypatch):
    """You cannot bucket a turn that was never read off disk."""
    seen: dict = {}

    def _spy(bot_id, *, days, end, log=None):
        seen["days"] = days
        return []

    monkeypatch.setattr(live_spend, "load_live_turns", _spy)
    live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert seen["days"] == 2


def test_seven_local_days_load_eight_utc_files(pacific, monkeypatch):
    seen: dict = {}

    def _spy(bot_id, *, days, end, log=None):
        seen["days"] = days
        return []

    monkeypatch.setattr(live_spend, "load_live_turns", _spy)
    live_spend.total_over_local_days(
        "team_bot_c", days=7,
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert seen["days"] == 8


def test_end_day_moves_the_load_window_not_just_the_selection(pacific, monkeypatch):
    """Selecting on one day while loading around another yields a confident
    $0.00 from a bucket nothing was read into."""
    seen: dict = {}

    def _spy(bot_id, *, days, end, log=None):
        seen["end"] = end
        return []

    monkeypatch.setattr(live_spend, "load_live_turns", _spy)
    live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 8, 20),
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    # End of pod-local Aug 20 (PDT) is 07:00Z Aug 21 — not "now".
    assert seen["end"] == datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)


def test_the_anchor_never_runs_past_now(pacific, monkeypatch):
    seen: dict = {}

    def _spy(bot_id, *, days, end, log=None):
        seen["end"] = end
        return []

    monkeypatch.setattr(live_spend, "load_live_turns", _spy)
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    live_spend.load_day_spend_detail("team_bot_c", date(2026, 9, 3), now=now)
    assert seen["end"] == now


# ── the "did not run" contract ───────────────────────────────────────────────


def test_discovery_failure_returns_none_not_zero(pacific, monkeypatch):
    monkeypatch.setattr(
        live_spend, "load_live_turns", lambda *a, **kw: live_spend.LIVE_LOAD_FAILED,
    )
    assert live_spend.load_day_spend_detail("team_bot_c", date(2026, 9, 3)) is None
    assert live_spend.load_day_spend("team_bot_c", date(2026, 9, 3)) is None
    assert live_spend.total_over_local_days("team_bot_c", days=7) is None


def test_an_idle_bot_is_a_measured_zero(pacific, monkeypatch):
    """``{}`` (no turns) and ``None`` (could not look) are different answers."""
    _serve(monkeypatch, [])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == 0.0
    assert detail.measurable is True


def test_unpriced_turns_are_counted_never_summed_as_zero(pacific, monkeypatch):
    """audit B6 — a total with unpriced turns beside it is a floor."""
    unpriceable = {"ts": "2026-09-03T16:00:00Z", "model": "who/knows-what"}
    _serve(monkeypatch, [_turn("2026-09-03T16:00:00Z", 2.0), unpriceable])
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3),
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(2.0)
    assert detail.unpriced_turns == 1
    assert detail.measurable is False


# ── multi-day windows ────────────────────────────────────────────────────────


def test_spend_is_attributed_to_the_local_day_it_happened_on(pacific, monkeypatch):
    _serve(monkeypatch, [
        _turn("2026-09-02T16:00:00Z", 3.0),   # 09:00 PDT Sep 2
        _turn("2026-09-04T04:00:00Z", 5.0),   # 21:00 PDT Sep 3
    ])
    by_day = live_spend.spend_by_local_day(
        "team_bot_c", days=7,
        now=datetime(2026, 9, 4, 4, 30, tzinfo=timezone.utc),
    )
    assert by_day is not None
    assert by_day["2026-09-02"].usd == pytest.approx(3.0)
    assert by_day["2026-09-03"].usd == pytest.approx(5.0)
    assert "2026-09-04" not in by_day   # local Sep 4 has not started


def test_days_outside_the_window_are_excluded(pacific, monkeypatch):
    _serve(monkeypatch, [
        _turn("2026-08-01T16:00:00Z", 99.0),  # far outside a 7-day window
        _turn("2026-09-03T16:00:00Z", 1.0),
    ])
    out = live_spend.total_over_local_days(
        "team_bot_c", days=7,
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert out is not None
    usd, measurable = out
    assert usd == pytest.approx(1.0)
    assert measurable is True


def test_total_is_marked_unmeasurable_when_any_day_has_unpriced_turns(
    pacific, monkeypatch,
):
    _serve(monkeypatch, [
        _turn("2026-09-03T16:00:00Z", 1.0),
        {"ts": "2026-09-02T16:00:00Z", "model": "who/knows-what"},
    ])
    out = live_spend.total_over_local_days(
        "team_bot_c", days=7,
        now=datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc),
    )
    assert out is not None
    usd, measurable = out
    assert usd == pytest.approx(1.0)
    assert measurable is False


def test_a_naive_now_is_read_as_utc(pacific, monkeypatch):
    """Left naive, ``.astimezone()`` would reinterpret it as system-local and
    shift the whole window by the host's offset."""
    _serve(monkeypatch, [_turn("2026-09-04T04:00:00Z", 5.0)])   # 21:00 PDT Sep 3
    detail = live_spend.load_day_spend_detail(
        "team_bot_c", date(2026, 9, 3), now=datetime(2026, 9, 4, 4, 30),
    )
    assert detail is not None
    assert detail.usd == pytest.approx(5.0)

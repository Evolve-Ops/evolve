"""The tile reports POD-LOCAL days, sourced from UTC-dated turn files.

The tile mixes two data sources that date themselves differently:

  * ``metrics/{date}/{bot}.json`` — measure.py's ``--date`` defaults to
    ``date.today()``. **POD-LOCAL.**
  * ``turns-{date}.jsonl``        — TurnObserver writes
    ``new Date().toISOString().slice(0, 10)``, and every ``ts`` is ``Z``.
    **UTC.**

``today`` is the pod-local day — it is the axis the metrics files already use
and the day the operator's wall clock shows — so the JSONL side converts.

Before this, ``_live_today_overlay`` compared a UTC ``ts[:10]`` prefix against
that local date. West of UTC the whole local evening has already rolled into
tomorrow's UTC date, so those turns matched neither "today" nor "yesterday"
and simply vanished from the tile: on a US/Pacific pod the 1d cost went stale
from 17:00 local until midnight, every day.

These tests pin a **non-UTC** zone on purpose. The suite's other tile tests pin
UTC (see the ``live_turns`` fixture in test_turn_cost_catalog_pricing.py) so
their day arithmetic is trivially stable; these exist to exercise the
conversion those cannot see.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_time  # noqa: E402
import tile_metrics  # noqa: E402
import turn_cost as tc  # noqa: E402
import usage_analytics as ua  # noqa: E402

# US/Pacific in August. Fixed offset rather than ZoneInfo so the test states
# its own arithmetic and cannot drift with the tz database.
PACIFIC = timezone(timedelta(hours=-7))
ON_TABLE_MODEL = "anthropic/claude-sonnet-4-5"

# The operator's local day under test, and the two UTC dates it spans:
# 2026-08-26 00:00 PDT = 2026-08-26 07:00Z ; 23:59 PDT = 2026-08-27 06:59Z.
LOCAL_TODAY = date(2026, 8, 26)


def _turn(ts: str, session_id: str = "s-1") -> dict:
    return {
        "ts": ts, "model": ON_TABLE_MODEL, "provider": "anthropic",
        "cost": 0.0, "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "session_id": session_id, "instance": "placeholder_bot",
        "source": "human", "channel": "placeholder-channel",
    }


@pytest.fixture
def pacific_pod(tmp_path, monkeypatch):
    """A pod on US/Pacific whose turn files are UTC-dated, as on a real pod."""
    monkeypatch.setenv("EVOLVE_SHARED", str(tmp_path))
    tc.reset_pricing_catalog_cache()
    turns_dir = tmp_path / "placeholder_bot" / "turns"
    turns_dir.mkdir(parents=True)
    monkeypatch.setattr(
        ua, "_find_turns_dirs",
        lambda bot_id, network_path=None: [turns_dir],
    )
    monkeypatch.setattr(tile_metrics, "_pod_tz", lambda: PACIFIC)
    tile_metrics._LIVE_OVERLAY_CACHE.clear()
    tile_metrics._LIVE_WINDOW_CACHE.clear()
    yield turns_dir
    tc.reset_pricing_catalog_cache()
    tile_metrics._LIVE_OVERLAY_CACHE.clear()
    tile_metrics._LIVE_WINDOW_CACHE.clear()


def _stage(turns_dir: Path, utc_day: str, turns: list[dict]) -> None:
    (turns_dir / f"turns-{utc_day}.jsonl").write_text(
        "\n".join(json.dumps(t) for t in turns) + "\n"
    )


# ── The finding ──────────────────────────────────────────────────────────────


def test_the_local_evening_counts_as_today_not_as_a_lost_day(pacific_pod):
    """THE regression, stated as the operator experiences it.

    20:00 and 22:00 Pacific on Aug 26 are 03:00 and 05:00 UTC on Aug 27, so
    the writer files them under ``turns-2026-08-27.jsonl``. They are still
    the operator's *today*. Before the fix they matched neither bucket and
    the evening's spend disappeared from the tile.
    """
    _stage(pacific_pod, "2026-08-27", [
        _turn("2026-08-27T03:00:00Z"),   # 20:00 PDT Aug 26
        _turn("2026-08-27T05:00:00Z"),   # 22:00 PDT Aug 26
    ])

    out = tile_metrics._live_today_overlay("placeholder_bot", LOCAL_TODAY)

    assert out["turns_today"] == 2, (
        "the local evening was dropped — turns after 17:00 Pacific live in "
        "tomorrow's UTC file and must still count as today"
    )
    assert out["cost_today"] > 0.0
    assert out["turns_yesterday"] == 0


def test_a_turn_before_local_midnight_utc_is_yesterday_not_today(pacific_pod):
    """The boundary in the other direction.

    2026-08-26T06:00Z is 23:00 PDT on Aug **25** — still yesterday locally,
    even though its UTC date matches ``today``. A prefix compare would have
    called this today.
    """
    _stage(pacific_pod, "2026-08-26", [_turn("2026-08-26T06:00:00Z")])

    out = tile_metrics._live_today_overlay("placeholder_bot", LOCAL_TODAY)

    assert out["turns_today"] == 0, (
        "a turn at 23:00 local yesterday was counted as today — the UTC date "
        "was compared instead of the pod-local one"
    )
    assert out["turns_yesterday"] == 1


def test_both_utc_files_of_one_local_day_are_read(pacific_pod):
    """A local day straddles two UTC files; the window must cover both."""
    _stage(pacific_pod, "2026-08-26", [_turn("2026-08-26T16:00:00Z")])   # 09:00 PDT
    _stage(pacific_pod, "2026-08-27", [_turn("2026-08-27T03:00:00Z")])   # 20:00 PDT

    out = tile_metrics._live_today_overlay("placeholder_bot", LOCAL_TODAY)

    assert out["turns_today"] == 2, (
        "only one of the two UTC files covering the local day was read"
    )


def test_local_yesterdays_morning_is_not_lost_off_the_back_of_the_window(
    pacific_pod,
):
    """The ``days + 1`` widening, pinned where it actually bites.

    Local yesterday (Aug 25) spans UTC Aug 25 **and** Aug 26. Anchoring a
    2-day window at the end of local today reaches UTC {08-26, 08-27} — so
    09:00 PDT on Aug 25, which lives in ``turns-2026-08-25.jsonl``, falls off
    the back and yesterday's tile silently loses its morning.

    Caught by a mutation check: without this test, reverting the widening to
    ``days=2`` passed the whole suite.
    """
    _stage(pacific_pod, "2026-08-25", [
        _turn("2026-08-25T16:00:00Z"),   # 09:00 PDT Aug 25 — local yesterday
    ])

    out = tile_metrics._live_today_overlay("placeholder_bot", LOCAL_TODAY)

    assert out["turns_yesterday"] == 1, (
        "local yesterday's morning fell off the back of the window — a window "
        "of N local days must read N+1 UTC days"
    )


def test_window_costs_reaches_the_oldest_local_day_it_reports(pacific_pod):
    """Same widening, on the per-date window.

    ``days=2`` means local {Aug 25, Aug 26}. The oldest of those starts in
    UTC Aug 25, so the read has to extend there or the day is reported empty.
    """
    _stage(pacific_pod, "2026-08-25", [
        _turn("2026-08-25T16:00:00Z"),   # 09:00 PDT Aug 25
    ])

    out = tile_metrics._live_window_costs("placeholder_bot", LOCAL_TODAY, days=2)

    assert "2026-08-25" in out["per_date"], (
        f"the oldest local day in the window was not read: "
        f"{sorted(out['per_date'])}"
    )
    assert out["per_date"]["2026-08-25"]["turns"] == 1


def test_window_costs_keys_are_pod_local_dates(pacific_pod):
    """``per_date`` must key on the same axis as the metrics files."""
    _stage(pacific_pod, "2026-08-27", [
        _turn("2026-08-27T03:00:00Z"),   # 20:00 PDT Aug 26
    ])

    out = tile_metrics._live_window_costs("placeholder_bot", LOCAL_TODAY, days=2)

    assert "2026-08-26" in out["per_date"], (
        f"per_date is keyed on UTC dates, not pod-local ones: "
        f"{sorted(out['per_date'])}"
    )
    assert "2026-08-27" not in out["per_date"]
    assert out["per_date"]["2026-08-26"]["turns"] == 1


def test_sessions_are_counted_on_the_local_day(pacific_pod):
    """Session sets follow the same axis as costs and turns."""
    _stage(pacific_pod, "2026-08-27", [
        _turn("2026-08-27T03:00:00Z", session_id="evening-a"),
        _turn("2026-08-27T04:00:00Z", session_id="evening-b"),
    ])

    out = tile_metrics._live_today_overlay("placeholder_bot", LOCAL_TODAY)

    assert out["sessions_today"] == 2
    assert out["sessions_yesterday"] == 0


# ── Helper-level contracts ───────────────────────────────────────────────────


def test_a_local_day_spans_two_utc_days_off_utc_and_one_on_it():
    assert tile_metrics._utc_days_for_local_day(LOCAL_TODAY, PACIFIC) == [
        "2026-08-26", "2026-08-27",
    ]
    assert tile_metrics._utc_days_for_local_day(LOCAL_TODAY, timezone.utc) == [
        "2026-08-26",
    ]


def test_the_window_anchor_is_the_end_of_the_local_day():
    """Anchoring at the START would exclude the file being written."""
    end = tile_metrics._utc_end_for_local_day(LOCAL_TODAY, PACIFIC)
    assert end.strftime("%Y-%m-%d") == "2026-08-27"
    assert end.tzinfo is not None


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, "2026-13-45T00:00:00Z"])
def test_an_unparseable_ts_is_dropped_rather_than_bucketed(bad):
    """A malformed ``ts`` must fall out, never land in an arbitrary day."""
    assert tile_metrics._local_day_iso(bad, PACIFIC) is None


def test_a_ts_without_an_offset_is_read_as_utc():
    """The writer's contract is UTC; a bare timestamp is not local."""
    assert tile_metrics._local_day_iso("2026-08-27T03:00:00", PACIFIC) == "2026-08-26"


# ── pod_time.pod_tz ──────────────────────────────────────────────────────────


def test_pod_tz_honours_an_explicit_pod_timezone(tmp_path):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"pod": {"timezone": "America/Los_Angeles"}}))
    tz = pod_time.pod_tz(net)
    # Same instant, expressed in the configured zone.
    stamp = datetime(2026, 8, 26, 20, 0, tzinfo=PACIFIC)
    assert stamp.astimezone(tz).date() == date(2026, 8, 26)


def test_pod_tz_falls_back_to_system_local_when_unset(tmp_path):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"pod": {}}))
    assert pod_time.pod_tz(net) is not None


def test_pod_tz_falls_back_when_the_network_file_is_absent(tmp_path):
    assert pod_time.pod_tz(tmp_path / "nope.json") is not None

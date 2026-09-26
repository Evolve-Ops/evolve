"""tests/test_delivery_status.py — delivery-status ledger reader.

Covers the honest-usage-reporting aggregation that powers the Apps-page
card: most-recent classified delivery_monitor outcome per (bot, app),
read-only over the per-day JSONL ledger. The classifier itself is tested
in test_delivery_monitor.py — here we only exercise the fold:
  * newest classification ts wins per app_id
  * cross-bot rows are filtered out (privacy)
  * multiple actions of one app collapse to the single newest row
  * last_event_ts prefers delivered_at → window_start → ts
  * _workspace pseudo-rows are not apps
  * corrupt lines / missing dirs degrade to empty, never raise
  * lookback_days bounds the file walk
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import delivery_status as ds  # noqa: E402

NOW = datetime(2026, 6, 13, 18, 0, 0, tzinfo=timezone.utc)


def _write_ledger(shared: Path, day: str, rows: list[dict]) -> None:
    ledger = shared / "delivery_monitor" / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    with (ledger / f"{day}.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(**kw) -> dict:
    base = {
        "ts": "2026-06-13T07:31:00+00:00",
        "bot_id": "admin_bot",
        "app_id": "morning-briefing",
        "action_id": "act-1",
        "window_start": "2026-06-13T07:00:00-04:00",
        "window_end": "2026-06-13T07:30:00-04:00",
        "outcome": "on_time",
        "diagnosis": None,
        "suspected_cause": None,
        "delivered_at": "2026-06-13T07:12:00-04:00",
        "healed": False,
    }
    base.update(kw)
    return base


# ── Empty / absent ──────────────────────────────────────────────────────────


def test_missing_ledger_dir_returns_empty(tmp_path):
    assert ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW) == {}


def test_empty_ledger_file_returns_empty(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [])
    assert ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW) == {}


# ── Most-recent wins ────────────────────────────────────────────────────────


def test_newest_ts_wins_for_one_app(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        _row(ts="2026-06-13T07:31:00+00:00", outcome="missed",
             diagnosis="did_not_run", delivered_at=None),
        _row(ts="2026-06-13T12:31:00+00:00", outcome="on_time"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "on_time"


def test_newest_wins_across_days(tmp_path):
    _write_ledger(tmp_path, "2026-06-11", [
        _row(ts="2026-06-11T07:31:00+00:00", outcome="on_time"),
    ])
    _write_ledger(tmp_path, "2026-06-13", [
        _row(ts="2026-06-13T07:31:00+00:00", outcome="missed",
             diagnosis="ran_undelivered", delivered_at=None),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "missed"
    assert out["morning-briefing"]["diagnosis"] == "ran_undelivered"


def test_multiple_actions_collapse_to_newest_row(tmp_path):
    # Same app, two scheduled actions — when neither is a miss, newest wins.
    _write_ledger(tmp_path, "2026-06-13", [
        _row(action_id="act-1", ts="2026-06-13T07:31:00+00:00",
             outcome="on_time"),
        _row(action_id="act-2", ts="2026-06-13T17:31:00+00:00",
             outcome="late"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "late"


def test_miss_aware_a_later_success_does_not_mask_an_earlier_miss(tmp_path):
    # The reviewer's edge case: a 07:00 action MISSED, a 17:00 action of the
    # same app delivered on time later. Naive newest-wins would show green and
    # hide the morning miss — miss-aware selection must surface the miss.
    _write_ledger(tmp_path, "2026-06-13", [
        _row(action_id="morning", ts="2026-06-13T07:31:00+00:00",
             outcome="missed", diagnosis="did_not_run", delivered_at=None),
        _row(action_id="evening", ts="2026-06-13T17:31:00+00:00",
             outcome="on_time"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "missed"
    assert out["morning-briefing"]["diagnosis"] == "did_not_run"


def test_miss_aware_uses_newest_miss_when_several(tmp_path):
    # Two missed actions — surface the most-recent miss.
    _write_ledger(tmp_path, "2026-06-13", [
        _row(action_id="a", ts="2026-06-13T07:31:00+00:00", outcome="missed",
             diagnosis="did_not_run", delivered_at=None, suspected_cause="dst"),
        _row(action_id="b", ts="2026-06-13T12:31:00+00:00", outcome="missed",
             diagnosis="ran_undelivered", delivered_at=None,
             suspected_cause="gateway_down"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["diagnosis"] == "ran_undelivered"


def test_miss_aware_per_action_recovery_clears_the_miss(tmp_path):
    # The morning action's OWN newest row recovered to on_time (older miss is
    # superseded per-action) — no stale miss should surface.
    _write_ledger(tmp_path, "2026-06-12", [
        _row(action_id="morning", ts="2026-06-12T07:31:00+00:00",
             outcome="missed", diagnosis="did_not_run", delivered_at=None),
    ])
    _write_ledger(tmp_path, "2026-06-13", [
        _row(action_id="morning", ts="2026-06-13T07:31:00+00:00",
             outcome="on_time"),
        _row(action_id="evening", ts="2026-06-13T17:31:00+00:00",
             outcome="on_time"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "on_time"


# ── Privacy: cross-bot rows filtered ────────────────────────────────────────


def test_cross_bot_rows_filtered(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        _row(bot_id="admin_bot", outcome="on_time"),
        _row(bot_id="team_bot_a", app_id="other-app", outcome="missed",
             diagnosis="did_not_run", delivered_at=None),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert set(out) == {"morning-briefing"}


# ── last_event_ts preference ────────────────────────────────────────────────


def test_last_event_prefers_delivered_at(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        _row(delivered_at="2026-06-13T07:12:00-04:00"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["last_event_ts"] == "2026-06-13T07:12:00-04:00"


def test_last_event_falls_back_to_window_start(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        _row(outcome="missed", diagnosis="did_not_run", delivered_at=None),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["last_event_ts"] == "2026-06-13T07:00:00-04:00"


def test_last_event_falls_back_to_ts(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        _row(outcome="unmeasurable", delivered_at=None, window_start=None,
             window_end=None, ts="2026-06-13T07:31:00+00:00"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["last_event_ts"] == "2026-06-13T07:31:00+00:00"


# ── Pseudo-rows and robustness ──────────────────────────────────────────────


def test_workspace_pseudo_rows_skipped(tmp_path):
    _write_ledger(tmp_path, "2026-06-13", [
        {"ts": "2026-06-13T07:31:00+00:00", "bot_id": "admin_bot",
         "app_id": "_workspace", "action_id": "_all", "outcome": "unmeasurable",
         "diagnosis": None, "delivered_at": None,
         "window_start": None, "window_end": None, "healed": False},
        _row(outcome="on_time"),
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert set(out) == {"morning-briefing"}


def test_corrupt_lines_skipped_not_raised(tmp_path):
    ledger = tmp_path / "delivery_monitor" / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / "2026-06-13.jsonl").write_text(
        "not json\n"
        + json.dumps(_row(outcome="on_time")) + "\n"
        + "{also not valid\n",
        encoding="utf-8",
    )
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["morning-briefing"]["outcome"] == "on_time"


def test_coverage_row_without_suspected_cause(tmp_path):
    # Coverage (unmonitorable) rows omit the suspected_cause key entirely.
    _write_ledger(tmp_path, "2026-06-13", [
        {"ts": "2026-06-13T07:31:00+00:00", "bot_id": "admin_bot",
         "app_id": "heartbeat-app", "action_id": "act-1",
         "outcome": "unmonitorable", "diagnosis": None,
         "window_start": None, "window_end": None,
         "delivered_at": None, "healed": False},
    ])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out["heartbeat-app"]["outcome"] == "unmonitorable"
    assert out["heartbeat-app"]["suspected_cause"] is None


def test_lookback_excludes_old_rows(tmp_path):
    # A row 40 days old is outside the default 30-day window.
    _write_ledger(tmp_path, "2026-05-04", [_row(outcome="on_time")])
    out = ds.latest_status_by_app(tmp_path, "admin_bot", now=NOW)
    assert out == {}
    # ...but a wider lookback finds it.
    out2 = ds.latest_status_by_app(
        tmp_path, "admin_bot", now=NOW, lookback_days=60,
    )
    assert out2["morning-briefing"]["outcome"] == "on_time"

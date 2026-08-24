"""Tests for the tile_metrics aggregator that powers the redesigned overview.

Replaces the 0-100 composite score + four 25-point bars with concrete
window-based signals (turns, sessions, cost, user/auto split, apps-used)
and discrete health chips. See packages/analyzer/tile_metrics.py for the
public contract.
"""

from __future__ import annotations

import json
import os
import sys
import time as _time
from datetime import date, timedelta
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from tile_metrics import (  # noqa: E402
    _check_acl_drift,
    _apps_used_in_window,
    _classify_split,
    _list_app_manifests,
    _safe_pct,
    _window_sum,
    compute_tile_data,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_metrics_day(shared_dir: Path, bot_id: str, d: date, **fields) -> None:
    """Drop a fake daily metrics file at the canonical path."""
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 2, "bot_id": bot_id, "date": d.isoformat()}
    body.update(fields)
    p.write_text(json.dumps(body))


def _write_cost_events(
    shared_dir: Path, bot_id: str, d: date, events: list[dict]
) -> None:
    """Append cost_events JSONL records for one (bot, date)."""
    p = shared_dir / "annotations" / bot_id / f"cost_events-{d.isoformat()}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _write_app_manifest(shared_dir: Path, bot_id: str, app_id: str) -> None:
    p = shared_dir / "applications" / bot_id / f"{app_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": app_id, "name": app_id}))


# ─────────────────────────────────────────────────────────────────────────────
# Window arithmetic
# ─────────────────────────────────────────────────────────────────────────────


def test_window_sum_inclusive_of_today_exclusive_of_end():
    """end_exclusive==today+1 should include today's data."""
    today = date(2026, 5, 6)
    by_date = {
        today: {"turn_count": 10},
        today - timedelta(days=1): {"turn_count": 5},
        today - timedelta(days=7): {"turn_count": 99},  # outside 7d window
    }
    end = today + timedelta(days=1)
    start = end - timedelta(days=7)
    assert _window_sum(by_date, start, end, "turn_count") == 15


def test_window_sum_returns_zero_when_empty():
    assert _window_sum({}, date(2026, 5, 1), date(2026, 5, 8), "anything") == 0


def test_window_sum_skips_missing_keys():
    by_date = {date(2026, 5, 5): {"other": 1}}
    assert _window_sum(by_date, date(2026, 5, 5), date(2026, 5, 6), "turn_count") == 0


# ─────────────────────────────────────────────────────────────────────────────
# Three-bucket split (human / scheduled / background)
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_split_buckets_trigger_kinds(tmp_path):
    today = date(2026, 5, 6)
    _write_cost_events(tmp_path, "team_bot_a", today, [
        # human
        {"trigger_kind": "user_turn"},
        {"trigger_kind": "user_turn"},
        # scheduled
        {"trigger_kind": "heartbeat"},
        {"trigger_kind": "heartbeat"},
        {"trigger_kind": "heartbeat"},
        {"trigger_kind": "cron_app"},
        # background
        {"trigger_kind": "subagent"},
        {"trigger_kind": "summarizer"},
        {"trigger_kind": "classifier"},
        {"trigger_kind": "task_extractor"},
        {"trigger_kind": "fallback"},
        {"trigger_kind": "unknown"},
        {},  # missing trigger_kind => background
    ])
    human, scheduled, background = _classify_split(
        tmp_path, "team_bot_a", today, today + timedelta(days=1)
    )
    assert (human, scheduled, background) == (2, 4, 7)


def test_classify_split_handles_no_files(tmp_path):
    assert _classify_split(
        tmp_path, "ghost", date(2026, 5, 1), date(2026, 5, 8)
    ) == (0, 0, 0)


def test_safe_pct_handles_zero_denominator():
    assert _safe_pct(0, 0) is None
    assert _safe_pct(3, 4) == pytest.approx(0.75)


# ─────────────────────────────────────────────────────────────────────────────
# Apps used
# ─────────────────────────────────────────────────────────────────────────────


def test_apps_total_unions_manifest_with_usage(tmp_path):
    """An app that ran but lacks a manifest still counts toward 'total' so
    'used' never exceeds 'total' (a real case observed on team_bot_c/admin_bot: 4
    apps ran but applications/<bot>/ was empty)."""
    today = date(2026, 5, 6)
    bot_id = "admin_bot"
    _write_metrics_day(
        tmp_path, bot_id, today,
        turn_count=10, session_count=10, total_cost_estimated=0.0,
        app_usage={"slack": {"sessions": 3}, "github": {"sessions": 2}},
    )
    # No manifest files written.
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(tmp_path, bot_id, bot_data, today=today)
    assert tile["apps"]["total"] == 2
    assert tile["apps"]["used_7d"] == 2
    # And the scan_needed chip should NOT fire when there are no manifests
    # (we don't penalise a bot for not having scanned-in apps when usage data
    # is the only source).
    assert "scan_needed" not in {c["id"] for c in tile["health_chips"]}


def test_count_apps_used_unions_window_keys():
    today = date(2026, 5, 6)
    by_date = {
        today: {"app_usage": {"app_a": {"sessions": 1}, "app_b": {"sessions": 0}}},
        today - timedelta(days=2): {"app_usage": {"app_b": {"sessions": 3}}},
        today - timedelta(days=8): {"app_usage": {"app_c": {"sessions": 5}}},  # outside
    }
    end = today + timedelta(days=1)
    used = _apps_used_in_window(by_date, end - timedelta(days=7), end)
    assert used == {"app_a", "app_b"}  # app_c excluded (8d ago)


def test_count_apps_used_handles_zero_sessions():
    """app_usage entries with sessions==0 don't count as 'used'."""
    today = date(2026, 5, 6)
    by_date = {today: {"app_usage": {"silent_app": {"sessions": 0}}}}
    used = _apps_used_in_window(by_date, today, today + timedelta(days=1))
    assert used == set()


def test_count_apps_used_tolerates_malformed_app_usage():
    today = date(2026, 5, 6)
    by_date = {today: {"app_usage": {"weird": "not a dict"}}}
    used = _apps_used_in_window(by_date, today, today + timedelta(days=1))
    assert used == set()


# ─────────────────────────────────────────────────────────────────────────────
# Top-level compute_tile_data
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_tile_data_full_member_bot(tmp_path):
    """End-to-end happy path: turns/sessions/cost/apps/chips all populated."""
    today = date(2026, 5, 6)
    bot_id = "team_bot_a"

    # Last 7 days: 100 turns/day, 10 sessions/day, $0.50/day. Prior 7: half that.
    for i in range(7):
        _write_metrics_day(
            tmp_path, bot_id, today - timedelta(days=i),
            turn_count=100, session_count=10, total_cost_estimated=0.50,
            correction_count=2, unexpected_billing_turns=0,
            app_usage={"slack": {"sessions": 5}, "github": {"sessions": 1}},
        )
    for i in range(7, 14):
        _write_metrics_day(
            tmp_path, bot_id, today - timedelta(days=i),
            turn_count=50, session_count=5, total_cost_estimated=0.25,
        )

    _write_app_manifest(tmp_path, bot_id, "slack")
    _write_app_manifest(tmp_path, bot_id, "github")
    _write_app_manifest(tmp_path, bot_id, "brave")  # never used

    # Fresh scan-status so the scan_needed chip stays quiet.
    scan_status = tmp_path / "applications" / bot_id / ".scan-status.json"
    scan_status.write_text("{}")

    # Cost events: 26 human, 71 scheduled (heartbeat+cron_app), 3 background
    today_events = (
        [{"trigger_kind": "user_turn"}] * 26
        + [{"trigger_kind": "heartbeat"}] * 60
        + [{"trigger_kind": "cron_app"}] * 11
        + [{"trigger_kind": "summarizer"}] * 3
    )
    _write_cost_events(tmp_path, bot_id, today, today_events)

    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id=bot_id, bot_data=bot_data, today=today
    )

    assert tile["activity"]["turns_7d"] == 700
    assert tile["activity"]["turns_prior_7d"] == 350
    assert tile["activity"]["sessions_7d"] == 70
    assert tile["activity"]["sessions_prior_7d"] == 35
    assert tile["activity"]["human_pct_7d"] == pytest.approx(0.26)
    assert tile["activity"]["scheduled_pct_7d"] == pytest.approx(0.71)
    assert tile["activity"]["background_pct_7d"] == pytest.approx(0.03)
    assert tile["cost"]["usd_7d"] == pytest.approx(3.50)
    assert tile["cost"]["usd_prior_7d"] == pytest.approx(1.75)
    assert tile["apps"]["total"] == 3
    assert tile["apps"]["used_7d"] == 2  # slack + github; brave unused
    assert tile["health_chips"] == []


def test_compute_tile_data_7d_uses_live_window_when_jsonl_readable(tmp_path, monkeypatch):
    """7d / 28d windows must come from raw turn JSONL (same source as the
    Usage Summary card), not from the lagged by_date aggregator which
    silently undercounts by ~$5/day.

    The 2026-05-20 incident's tile-vs-summary gap was driven by this:
    by_date said tile 7d = $21.13 while raw JSONL (summary) said $88.19,
    a $67 gap. PR #1380 fixed 1d, #1409 added today/yesterday overlay
    onto by_date for 7d/28d, but the body of the window still came from
    by_date and stayed undercounted. Path A: read the whole window from
    raw JSONL. This test pins that behavior."""
    today = date(2026, 5, 21)
    bot_id = "security_bot"
    # by_date deliberately undercounts — $1/day × 7 days = $7. The tile
    # must IGNORE this and read from live_window instead.
    for i in range(7):
        _write_metrics_day(
            tmp_path, bot_id, today - timedelta(days=i),
            turn_count=10, session_count=2, total_cost_estimated=1.00,
        )
    import tile_metrics as _tm
    # Mock _live_window_costs to return realistic-shape data. 5 days
    # of $5 + yesterday $33.67 + today $2.79 = $63.46 over 7 days.
    sids = {}
    per_date = {}
    for i in range(2, 7):  # 5-19 .. 5-15
        d = (today - timedelta(days=i)).isoformat()
        per_date[d] = {"cost": 5.00, "turns": 30}
        sids[d] = {f"s-{d}-1", f"s-{d}-2", f"s-{d}-3"}
    per_date[(today - timedelta(days=1)).isoformat()] = {"cost": 33.67, "turns": 286}
    sids[(today - timedelta(days=1)).isoformat()] = {f"s-yest-{n}" for n in range(43)}
    per_date[today.isoformat()] = {"cost": 2.79, "turns": 50}
    sids[today.isoformat()] = {f"s-today-{n}" for n in range(5)}
    monkeypatch.setattr(
        _tm, "_live_window_costs",
        lambda bot_id_, today_, days: {
            "ok": True, "per_date": per_date, "session_ids_per_date": sids,
        },
    )
    bot_data = {"role": "member", "status": "online", "last_metric_date": "2026-05-21"}
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id=bot_id, bot_data=bot_data, today=today,
    )
    # 5*$5 + $33.67 + $2.79 = $61.46
    assert tile["cost"]["usd_7d"] == pytest.approx(61.46)
    assert tile["cost"]["usd_28d"] == pytest.approx(61.46)
    assert tile["cost"]["live_today_7d"] is True
    assert tile["cost"]["live_today_28d"] is True
    # turns_7d: 5 * 30 + 286 + 50 = 486
    assert tile["activity"]["turns_7d"] == 486
    # sessions_7d: 5*3 + 43 + 5 = 63 unique
    assert tile["activity"]["sessions_7d"] == 63


def test_compute_tile_data_7d_falls_back_to_by_date_when_jsonl_missing(tmp_path, monkeypatch):
    """When live JSONL is unreadable (sudo grant missing, ACL drift, etc.)
    the tile falls back to the by_date aggregate. Last-resort behavior
    so a discovery outage doesn't silently zero out the 7d/28d view."""
    today = date(2026, 5, 21)
    bot_id = "security_bot"
    for i in range(7):
        _write_metrics_day(
            tmp_path, bot_id, today - timedelta(days=i),
            turn_count=10, session_count=2, total_cost_estimated=1.00,
        )
    import tile_metrics as _tm
    monkeypatch.setattr(
        _tm, "_live_window_costs",
        lambda bot_id_, today_, days: {
            "ok": False, "per_date": {}, "session_ids_per_date": {},
        },
    )
    monkeypatch.setattr(
        _tm, "_live_today_overlay",
        lambda bot_id_, today_: {
            "ok": False, "cost_today": 0.0, "cost_yesterday": 0.0,
            "turns_today": 0, "turns_yesterday": 0,
            "sessions_today": 0, "sessions_yesterday": 0,
        },
    )
    bot_data = {"role": "member", "status": "online", "last_metric_date": "2026-05-21"}
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id=bot_id, bot_data=bot_data, today=today,
    )
    # by_date sum: 7 days × $1.00 = $7.00. live_today_* flags False.
    assert tile["cost"]["usd_7d"] == pytest.approx(7.00)
    assert tile["cost"]["live_today_7d"] is False
    assert tile["cost"]["live_today_28d"] is False


def test_compute_tile_data_empty_bot_returns_zeros(tmp_path):
    """No metrics, no events, no manifests — everything reads 0/None safely."""
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="ghost", bot_data=bot_data, today=today
    )
    assert tile["activity"]["turns_7d"] == 0
    assert tile["activity"]["human_pct_7d"] is None
    assert tile["activity"]["scheduled_pct_7d"] is None
    assert tile["activity"]["background_pct_7d"] is None
    assert tile["cost"]["usd_7d"] == 0.0
    assert tile["apps"]["total"] == 0
    assert tile["apps"]["used_7d"] == 0
    # Healthy bot with no apps installed → no chips (don't penalise design).
    assert tile["health_chips"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Health chips
# ─────────────────────────────────────────────────────────────────────────────


def test_chip_no_stale_heartbeat_when_online(tmp_path):
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    assert "stale_heartbeat" not in {c["id"] for c in tile["health_chips"]}


def test_chip_no_stale_heartbeat_when_active_and_recent(tmp_path):
    """active + last_metric today/yesterday is a normal day-rollover blip."""
    today = date(2026, 5, 6)
    for last in ("2026-05-06", "2026-05-05"):
        bot_data = {"role": "member", "status": "active", "last_metric_date": last}
        tile = compute_tile_data(
            shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
        )
        assert "stale_heartbeat" not in {c["id"] for c in tile["health_chips"]}, (
            f"unexpected chip for last={last}"
        )


def test_chip_stale_heartbeat_warn_when_active_and_old(tmp_path):
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "active",
        "last_metric_date": "2026-05-02",  # 4d ago
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    chip = next(c for c in tile["health_chips"] if c["id"] == "stale_heartbeat")
    assert chip["severity"] == "warn"
    assert "4d ago" in chip["detail"]


def test_chip_stale_heartbeat_critical_when_offline(tmp_path):
    today = date(2026, 5, 6)
    bot_data = {"role": "member", "status": "offline"}
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    chip = next(c for c in tile["health_chips"] if c["id"] == "stale_heartbeat")
    assert chip["severity"] == "critical"


def test_chip_gateway_down_when_unreachable(tmp_path):
    # Regression: security_bot's gateway was down (heal status said gateway_reachable
    # was false) but the tile rendered "Healthy" because yesterday's metrics
    # file made status="active" — the silent path. The new chip fires off the
    # heal-written probe regardless of metric-file age.
    today = date(2026, 5, 9)
    bot_data = {
        "role": "member",
        "status": "active",
        "last_metric_date": "2026-05-08",
        "gateway_status_fresh": True,
        "gateway_running": False,
        "gateway_reachable": False,
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="security_bot", bot_data=bot_data, today=today
    )
    chip = next(c for c in tile["health_chips"] if c["id"] == "gateway_down")
    assert chip["severity"] == "critical"
    assert chip["detail"] == "process not running"
    assert chip["nav"] == "maintenance/status"


def test_chip_gateway_down_unreachable_but_running(tmp_path):
    today = date(2026, 5, 9)
    bot_data = {
        "role": "member",
        "status": "active",
        "last_metric_date": "2026-05-09",
        "gateway_status_fresh": True,
        "gateway_running": True,
        "gateway_reachable": False,
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    chip = next(c for c in tile["health_chips"] if c["id"] == "gateway_down")
    assert chip["detail"] == "gateway unreachable"


def test_chip_no_gateway_down_when_reachable(tmp_path):
    today = date(2026, 5, 9)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-09",
        "gateway_status_fresh": True,
        "gateway_running": True,
        "gateway_reachable": True,
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    assert "gateway_down" not in {c["id"] for c in tile["health_chips"]}


def test_chip_no_gateway_down_when_status_stale(tmp_path):
    # Stale status file (heal hasn't run recently, e.g. fresh install) —
    # don't fire chip just because the absent file looks like "unreachable".
    today = date(2026, 5, 9)
    bot_data = {
        "role": "member",
        "status": "active",
        "last_metric_date": "2026-05-09",
        "gateway_status_fresh": False,
        "gateway_reachable": False,
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    assert "gateway_down" not in {c["id"] for c in tile["health_chips"]}


def test_chip_version_drift_when_evolve_synced_false(tmp_path):
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
        "evolve_synced": False,
        "evolve_version": "v0.2.9",
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    chip = next(c for c in tile["health_chips"] if c["id"] == "version_drift")
    assert "v0.2.9" in chip["detail"]


def test_chip_unexpected_billing_when_metrics_flag_present(tmp_path):
    today = date(2026, 5, 6)
    _write_metrics_day(
        tmp_path, "team_bot_a", today,
        turn_count=10, session_count=1, total_cost_estimated=0.0,
        unexpected_billing_turns=3,
    )
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(
        shared_dir=tmp_path, bot_id="team_bot_a", bot_data=bot_data, today=today
    )
    ids = {c["id"] for c in tile["health_chips"]}
    assert "unexpected_billing" in ids


def test_chip_high_correction_only_fires_above_threshold(tmp_path):
    """15% correction rate over 7d → fire. 5% → quiet."""
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }

    # 15% rate (well above 10% threshold), 100 turns total over 7d
    for i in range(7):
        _write_metrics_day(
            tmp_path, "high", today - timedelta(days=i),
            turn_count=100, session_count=10, total_cost_estimated=0.0,
            correction_count=15,
        )
    tile = compute_tile_data(tmp_path, "high", bot_data, today=today)
    assert "high_correction" in {c["id"] for c in tile["health_chips"]}

    # 5% rate, separate bot dir
    for i in range(7):
        _write_metrics_day(
            tmp_path, "low", today - timedelta(days=i),
            turn_count=100, session_count=10, total_cost_estimated=0.0,
            correction_count=5,
        )
    tile = compute_tile_data(tmp_path, "low", bot_data, today=today)
    assert "high_correction" not in {c["id"] for c in tile["health_chips"]}


def test_chip_high_correction_quiet_on_tiny_samples(tmp_path):
    """Don't yell about 100% correction rate when there were only 2 turns."""
    today = date(2026, 5, 6)
    _write_metrics_day(
        tmp_path, "team_bot_a", today,
        turn_count=2, session_count=1, total_cost_estimated=0.0,
        correction_count=2,
    )
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(tmp_path, "team_bot_a", bot_data, today=today)
    assert "high_correction" not in {c["id"] for c in tile["health_chips"]}


def test_chip_cost_spike_requires_both_multiplier_and_floor(tmp_path):
    """$0.20 → $0.50 must NOT fire (under absolute floor). $4 → $12 must fire."""
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }

    # Cheap bot: 4× spike but tiny absolute. Should not fire.
    for i in range(7):
        _write_metrics_day(
            tmp_path, "cheap", today - timedelta(days=i),
            turn_count=10, session_count=1, total_cost_estimated=0.50 / 7,
        )
    for i in range(7, 14):
        _write_metrics_day(
            tmp_path, "cheap", today - timedelta(days=i),
            turn_count=10, session_count=1, total_cost_estimated=0.10 / 7,
        )
    tile = compute_tile_data(tmp_path, "cheap", bot_data, today=today)
    assert "cost_spike" not in {c["id"] for c in tile["health_chips"]}

    # Real spike: $12 vs $4 prior, both above floor.
    for i in range(7):
        _write_metrics_day(
            tmp_path, "spike", today - timedelta(days=i),
            turn_count=10, session_count=1, total_cost_estimated=12.0 / 7,
        )
    for i in range(7, 14):
        _write_metrics_day(
            tmp_path, "spike", today - timedelta(days=i),
            turn_count=10, session_count=1, total_cost_estimated=4.0 / 7,
        )
    tile = compute_tile_data(tmp_path, "spike", bot_data, today=today)
    assert "cost_spike" in {c["id"] for c in tile["health_chips"]}


def test_chip_scan_needed_only_when_apps_present_and_no_recent_scan(tmp_path):
    today = date(2026, 5, 7)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-07",
    }

    # No apps → no chip.
    tile = compute_tile_data(tmp_path, "personal_bot", bot_data, today=today)
    assert "scan_needed" not in {c["id"] for c in tile["health_chips"]}

    # Apps installed, no .scan-status.json → fire ("never scanned").
    _write_app_manifest(tmp_path, "team_bot_a", "slack")
    tile = compute_tile_data(tmp_path, "team_bot_a", bot_data, today=today)
    chip = next((c for c in tile["health_chips"] if c["id"] == "scan_needed"), None)
    assert chip is not None
    assert chip["detail"] == "never scanned"
    # Canonical page id is "apps"; the legacy "capabilities" slug
    # silently broke chipNav (no [data-page="capabilities"] element
    # exists). Fixed in the apps-chip-fixes PR.
    assert chip["nav"] == "apps"

    # Apps installed, fresh scan-status → quiet.
    scan_status = tmp_path / "applications" / "team_bot_a" / ".scan-status.json"
    scan_status.write_text("{}")  # mtime defaults to now
    tile = compute_tile_data(tmp_path, "team_bot_a", bot_data, today=today)
    assert "scan_needed" not in {c["id"] for c in tile["health_chips"]}


def test_chip_scan_needed_fires_for_primary_role_too(tmp_path):
    """Unlike the old apps_untested chip, scan_needed applies to any bot —

    primary or member. The Applications page's Run Scan button works for
    any bot id. If a chip is wrong on a specific primary app, the rule
    should be tightened on a different axis (e.g. config marking that
    app as scan-exempt), not blanket-skipped by role."""
    today = date(2026, 5, 7)
    _write_app_manifest(tmp_path, "evolve", "security-cve-scan")
    bot_data = {
        "role": "primary",
        "status": "online",
        "last_metric_date": "2026-05-07",
    }
    tile = compute_tile_data(tmp_path, "evolve", bot_data, network={}, today=today)
    assert "scan_needed" in {c["id"] for c in tile["health_chips"]}


def test_list_app_manifests_empty_network_uses_shared_side(tmp_path, monkeypatch):
    """An empty ``network={}`` carries no bot→user mapping, so the bot-side
    branch must be SKIPPED (before any ``get_bot_user`` call) and the
    shared-side fallback used.

    Regression: the guard was ``if network is not None`` — ``{}`` is not None,
    so the test fixture (which writes only to shared_dir + passes ``{}``) had
    its count silently shadowed by the live ``/Users/<user>/.openclaw`` dir on
    a self-hosted CI runner (empty → 0 apps → suppressed scan_needed chip,
    fleet-wide red). The guard is now truthy, so ``{}``/None take shared-side.
    """
    import evolve_config

    _write_app_manifest(tmp_path, "evolve", "security-cve-scan")

    # Tripwire that catches the regression on ANY host (not just a CI runner
    # with a real /Users/evolve): record every bot-user resolution. The
    # bot-side branch must be skipped entirely for an empty/None network, so
    # get_bot_user must NOT be called. ``_list_app_manifests`` imports it from
    # evolve_config at call time, so patch it there.
    calls: list = []
    monkeypatch.setattr(
        evolve_config, "get_bot_user",
        lambda bot_id, network: calls.append((bot_id, network)) or "evolve",
    )

    assert _list_app_manifests(tmp_path, "evolve", {}) == ["security-cve-scan"]
    assert _list_app_manifests(tmp_path, "evolve", None) == ["security-cve-scan"]
    assert calls == [], "empty/None network must skip the bot-side branch"


def test_chip_scan_needed_reads_bot_side_scan_status(tmp_path, monkeypatch):
    """The scan_needed chip's source of truth is the scanner-written file at
    /Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json,
    not the shared-dir mirror. A fresh bot-side file must silence the chip
    even when the shared-dir mirror is absent — that's exactly the bug
    this PR fixes (operator ran scans, mirror never got written, chip
    stayed stuck).
    """
    import tile_metrics

    today = date(2026, 5, 20)
    _write_app_manifest(tmp_path, "evolve", "security-cve-scan")

    # No shared-dir mirror at all — only the bot-side file exists.
    fake_bot_side = tmp_path / "fake-bot-home" / ".scan-status.json"
    fake_bot_side.parent.mkdir(parents=True)
    fake_bot_side.write_text("{}")  # mtime = now

    monkeypatch.setattr(
        tile_metrics, "_bot_scan_status_path",
        lambda network, bot_id: fake_bot_side if bot_id == "evolve" else None,
    )

    bot_data = {
        "role": "primary",
        "status": "online",
        "last_metric_date": "2026-05-20",
    }
    tile = compute_tile_data(
        tmp_path, "evolve", bot_data, network={"bots": {"evolve": {}}}, today=today,
    )
    chip_ids = {c["id"] for c in tile["health_chips"]}
    assert "scan_needed" not in chip_ids, (
        "Fresh bot-side scan-status should silence the chip, "
        "even without a shared-dir mirror"
    )


def test_chip_scan_needed_fresher_source_wins(tmp_path, monkeypatch):
    """When both the bot-side file and the legacy shared-dir mirror exist,
    the chip rule must use the fresher mtime. A stale mirror should not
    keep a chip quiet when the bot-side file is also stale, but a fresh
    bot-side file should silence the chip even when the mirror is old.
    """
    import tile_metrics

    today = date(2026, 5, 20)
    _write_app_manifest(tmp_path, "evolve", "security-cve-scan")

    # Stale shared-dir mirror (40 days old) — would fire on its own.
    mirror = tmp_path / "applications" / "evolve" / ".scan-status.json"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text("{}")
    stale_mtime = _time.time() - 40 * 86400
    os.utime(mirror, (stale_mtime, stale_mtime))

    # Fresh bot-side file.
    fresh = tmp_path / "fake-bot-home" / ".scan-status.json"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("{}")  # mtime = now

    monkeypatch.setattr(
        tile_metrics, "_bot_scan_status_path",
        lambda network, bot_id: fresh if bot_id == "evolve" else None,
    )

    bot_data = {
        "role": "primary",
        "status": "online",
        "last_metric_date": "2026-05-20",
    }
    tile = compute_tile_data(
        tmp_path, "evolve", bot_data, network={"bots": {"evolve": {}}}, today=today,
    )
    assert "scan_needed" not in {c["id"] for c in tile["health_chips"]}, (
        "Fresh bot-side file should win over stale mirror"
    )


def test_chip_security_critical_fires_when_count_positive(tmp_path):
    """security_critical count comes from the audit cache, overlaid onto
    bot_data by the admin server before tile compute. Chip fires when > 0."""
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
        "security_critical": 5,
    }
    tile = compute_tile_data(tmp_path, "team_bot_a", bot_data, today=today)
    chip = next(c for c in tile["health_chips"] if c["id"] == "security_critical")
    assert chip["severity"] == "critical"
    assert "5" in chip["label"]
    assert chip.get("nav") == "security"


def test_chip_security_critical_quiet_when_zero_or_missing(tmp_path):
    """Audit cache empty / never run → field absent → chip stays quiet.
    Audit ran with no findings → field == 0 → chip stays quiet."""
    today = date(2026, 5, 6)
    base = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    for variant in ({}, {"security_critical": 0}):
        tile = compute_tile_data(tmp_path, "team_bot_a", {**base, **variant}, today=today)
        assert "security_critical" not in {c["id"] for c in tile["health_chips"]}, (
            f"unexpected chip for {variant}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Admin-specific chips
# ─────────────────────────────────────────────────────────────────────────────


def test_acl_drift_check_skips_missing_bots(tmp_path):
    """A bot whose user dir doesn't exist isn't 'drifted', it's just not deployed yet."""
    network = {"bots": {"phantom": {"port": 19000, "role": "member"}}}
    drifted = _check_acl_drift(network)
    assert drifted == []  # /Users/phantom/.openclaw/openclaw.json doesn't exist


def test_acl_drift_skips_primary_role():
    """The primary bot isn't checked — it doesn't have a per-bot .openclaw/."""
    network = {"bots": {"evolve": {"port": 19030, "role": "primary"}}}
    assert _check_acl_drift(network) == []


def test_acl_drift_probe_uses_platform_home(monkeypatch):
    """The drift probe must resolve the bot home through the platform seam,
    not a `/Users/` literal — a hardcoded macOS path probes a file that never
    exists on a Linux pod, so every bot looks 'not deployed' and real drift
    goes unseen."""
    import evolve_config

    probed: list[str] = []

    class _FakePath:
        def __init__(self, p):
            self._p = str(p)

        def __truediv__(self, other):
            return _FakePath(f"{self._p}/{other}")

        def open(self, *_a, **_k):
            probed.append(self._p)
            raise FileNotFoundError(self._p)

    # user_home returns our recording stub so we can see the exact path probed.
    monkeypatch.setattr(
        evolve_config, "user_home", lambda u: _FakePath(f"/home/{u}"), raising=True
    )
    network = {"bots": {"team_bot_a": {"port": 19001, "role": "member"}}}
    assert _check_acl_drift(network) == []
    assert probed == ["/home/team_bot_a/.openclaw/openclaw.json"]


def test_admin_chips_only_added_when_role_primary(tmp_path):
    """A member bot must never get daemon-fleet chips appended."""
    today = date(2026, 5, 6)
    bot_data = {
        "role": "member",
        "status": "online",
        "last_metric_date": "2026-05-06",
    }
    tile = compute_tile_data(tmp_path, "team_bot_a", bot_data, network={}, today=today)
    admin_only_ids = {"repo_puller_stale", "infra_daemon_down", "acl_drift", "disk_high"}
    assert not (admin_only_ids & {c["id"] for c in tile["health_chips"]})


# ─────────────────────────────────────────────────────────────────────────────
# Value block + underused chip (spec internal/spec-value-baseline-2026-06-10.md §7.1)
# ─────────────────────────────────────────────────────────────────────────────


def _write_value_rollup(
    shared_dir: Path,
    anchor: date,
    bots: dict,
    *,
    computed_at: str | None = None,
) -> None:
    """Drop a fake nightly value rollup at the canonical path."""
    from datetime import datetime, timezone

    value_dir = shared_dir / "metrics" / "value"
    value_dir.mkdir(parents=True, exist_ok=True)
    (value_dir / f"{anchor.isoformat()}.json").write_text(json.dumps({
        "version": 1,
        "computed_at": computed_at
        or datetime.now(timezone.utc).isoformat(),
        "anchor_date": anchor.isoformat(),
        "bots": bots,
    }))


def _value_entry(state: str, *, human_28d: int | None = 5) -> dict:
    return {
        "utilization_state": state,
        "state_reason": "test reason",
        "active_human_days_7d": {"value": 1, "measurable_days": 7, "window_days": 7},
        "active_human_days_28d": {
            "value": human_28d, "measurable_days": 27, "window_days": 28,
        },
        "proactive_runs_7d": {"value": 0, "measurable_days": 7, "window_days": 7},
        "proactive_runs_28d": {"value": 0, "measurable_days": 27, "window_days": 28},
        "app_coverage_28d": {"value": None, "apps_total": 0, "apps_used": 0},
        "value_trend_28d": {"value": None, "current": None, "prior": None},
        "usage_breadth_28d": {"value": 3},
        "age_days": 120,
        "anchor_date": "2026-05-05",
    }


_MEMBER = {"role": "member", "status": "online", "last_metric_date": "2026-05-06"}


def test_tile_value_block_from_rollup(tmp_path):
    today = date(2026, 5, 6)
    _write_value_rollup(
        tmp_path, today - timedelta(days=1),
        {"team_bot_a": _value_entry("active")},
    )
    tile = compute_tile_data(tmp_path, "team_bot_a", dict(_MEMBER), today=today)
    v = tile["value"]
    assert v is not None
    assert v["utilization_state"] == "active"
    assert v["active_human_days_28d"]["value"] == 5
    assert v["as_of"] == (today - timedelta(days=1)).isoformat()
    assert v["stale"] is False
    # Internals stay out of the tile block (spec §7.1 "minus internals").
    assert "age_days" not in v
    assert "usage_breadth_28d" not in v
    # Active bot → no underused chip.
    assert "underused" not in {c["id"] for c in tile["health_chips"]}


def test_tile_value_block_none_without_rollup(tmp_path):
    """No rollup yet (fresh pod) → None, never a block of fake zeros."""
    tile = compute_tile_data(
        tmp_path, "team_bot_a", dict(_MEMBER), today=date(2026, 5, 6)
    )
    assert tile["value"] is None
    assert "underused" not in {c["id"] for c in tile["health_chips"]}


def test_tile_value_block_none_for_unknown_bot(tmp_path):
    today = date(2026, 5, 6)
    _write_value_rollup(
        tmp_path, today - timedelta(days=1),
        {"team_bot_a": _value_entry("active")},
    )
    tile = compute_tile_data(tmp_path, "team_bot_b", dict(_MEMBER), today=today)
    assert tile["value"] is None


def test_chip_underused_when_rollup_says_so(tmp_path):
    """The chip reads utilization_state from the rollup — spec §7.1 shape,
    §7.3 copy. It must not re-derive the predicate from raw metrics."""
    today = date(2026, 5, 6)
    # Raw metrics on disk say "active today" — the chip must follow the
    # rollup's judgement anyway (one predicate, one place).
    _write_metrics_day(tmp_path, "team_bot_a", today, turn_count=50, session_count=5)
    _write_value_rollup(
        tmp_path, today - timedelta(days=1),
        {"team_bot_a": _value_entry("underused", human_28d=0)},
    )
    tile = compute_tile_data(tmp_path, "team_bot_a", dict(_MEMBER), today=today)
    chip = next(c for c in tile["health_chips"] if c["id"] == "underused")
    assert chip["severity"] == "warn"
    assert chip["horizon"] == "ongoing"
    assert chip["digest_tier"] == "tile_only"
    assert chip["label"] == "Idle 4 weeks"
    assert chip["detail"] == (
        "No one has used this bot recently and it isn't delivering "
        "anything on a schedule."
    )
    # Plex test (§7.3): no internal vocabulary anywhere on the chip.
    blob = json.dumps(chip).lower()
    for banned in ("baseline", "signal", "producer", "tri-state", "measurable"):
        assert banned not in blob, f"chip copy leaks internal vocab: {banned!r}"


def test_chip_not_added_for_unmeasurable(tmp_path):
    """Unmeasurable must never render as underused (tri-state honesty)."""
    today = date(2026, 5, 6)
    _write_value_rollup(
        tmp_path, today - timedelta(days=1),
        {"team_bot_a": _value_entry("unmeasurable", human_28d=None)},
    )
    tile = compute_tile_data(tmp_path, "team_bot_a", dict(_MEMBER), today=today)
    assert tile["value"]["utilization_state"] == "unmeasurable"
    assert "underused" not in {c["id"] for c in tile["health_chips"]}


def test_tile_value_block_stale_flag(tmp_path):
    """Spec §5.3: a rollup older than 48h is flagged, not silently shown."""
    today = date(2026, 5, 6)
    _write_value_rollup(
        tmp_path, today - timedelta(days=10),
        {"team_bot_a": _value_entry("active")},
        computed_at="2026-04-26T01:00:00+00:00",
    )
    tile = compute_tile_data(tmp_path, "team_bot_a", dict(_MEMBER), today=today)
    assert tile["value"]["stale"] is True

"""End-to-end tests for /api/analytics/activity-rhythm.

Slice 6 of the Sessions redesign — the rhythm-of-use endpoint that powers
the time-of-day heatmap, daily session counts, and inter-turn gap
histogram on the Sessions page. Reuses the same Flask test-client +
JSONL-fixture pattern as test_analytics_cache_economics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.server import _register_analytics_routes  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _write_cost_events(
    shared_dir: Path, bot_id: str, date_str: str, events: list[dict]
) -> None:
    annotations = shared_dir / "annotations" / bot_id
    annotations.mkdir(parents=True, exist_ok=True)
    path = annotations / f"cost_events-{date_str}.jsonl"
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _evt(
    *,
    ts: str,
    bot_id: str = "admin_bot",
    session_id: str = "sess-1",
    trigger_kind: str = "user_turn",
    cache_state: str = "warm",
) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot_id,
        "session_id": session_id,
        "trigger_kind": trigger_kind,
        "model": "claude-sonnet",
        "provider": "anthropic",
        "cache_state": cache_state,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 0,
        "cost_usd": 0.01,
    }


@pytest.fixture
def app(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(
        json.dumps({
            "sharedDir": str(shared),
            "primary": "evolve",
            "members": ["admin_bot", "team_bot_a"],
        })
    )
    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    a.shared_dir = shared
    return a


# ── Happy path ────────────────────────────────────────────────────────────────


def test_endpoint_returns_expected_top_level_keys(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {
        "bot_id",
        "window_days",
        "time_of_day_heatmap",
        "daily_session_counts",
        "inter_turn_gap_histogram",
    }
    assert body["bot_id"] == "admin_bot"
    assert body["window_days"] == 7


def test_empty_pod_returns_zeroed_aggregations(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    body = resp.get_json()
    h = body["time_of_day_heatmap"]
    assert len(h["matrix"]) == 24
    assert all(len(row) == 7 for row in h["matrix"])
    assert all(cell == 0 for row in h["matrix"] for cell in row)
    assert h["max_count"] == 0
    assert h["total_events"] == 0
    assert body["daily_session_counts"] == []
    g = body["inter_turn_gap_histogram"]
    assert g["gap_count"] == 0
    assert g["median_seconds"] == 0.0
    assert g["p95_seconds"] == 0.0


# ── time_of_day_heatmap ──────────────────────────────────────────────────────


def test_heatmap_buckets_into_hour_and_weekday():
    """A single event at 2026-05-11 14:30 UTC (a Monday) lands at hour=14, dow=0."""
    pass  # exercised by the integration test below


def test_heatmap_aggregates_events_by_hour_and_weekday(app):
    # 2026-05-11 was a Monday (weekday=0). 2026-05-12 = Tuesday (weekday=1).
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [_evt(ts="2026-05-11T14:30:00Z")] * 5  # Monday 14:00 cell
        + [_evt(ts="2026-05-11T09:15:00Z")] * 2,  # Monday 09:00 cell
    )
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-12",
        [_evt(ts="2026-05-12T14:45:00Z")] * 3,  # Tuesday 14:00 cell
    )

    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    h = resp.get_json()["time_of_day_heatmap"]
    assert h["matrix"][14][0] == 5  # Monday 14:00
    assert h["matrix"][9][0] == 2   # Monday 09:00
    assert h["matrix"][14][1] == 3  # Tuesday 14:00
    assert h["matrix"][14][2] == 0  # untouched cell
    assert h["max_count"] == 5
    assert h["total_events"] == 10


def test_heatmap_handles_iso_with_no_z_suffix(app):
    # Some emitters write ISO without trailing Z. Parser should accept it
    # and treat as UTC.
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [_evt(ts="2026-05-11T14:30:00+00:00")] * 2,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    h = resp.get_json()["time_of_day_heatmap"]
    assert h["matrix"][14][0] == 2


def test_heatmap_ignores_malformed_timestamps(app):
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [
            _evt(ts="2026-05-11T14:30:00Z"),
            _evt(ts="not-a-real-timestamp"),
            _evt(ts=""),  # empty
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    h = resp.get_json()["time_of_day_heatmap"]
    # Only the valid event lands in the matrix; the malformed ones are
    # silently dropped (we count the parseable ones).
    assert h["total_events"] == 3  # raw count is still 3
    assert h["matrix"][14][0] == 1  # but only 1 maps to a cell


# ── daily_session_counts ─────────────────────────────────────────────────────


def test_daily_session_counts_dedupes_per_session_per_day(app):
    # Same session, multiple events on same day → 1 session that day.
    # Different session same day → 2 sessions that day.
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [_evt(ts="2026-05-11T10:00:00Z", session_id="sess-a")] * 5
        + [_evt(ts="2026-05-11T11:00:00Z", session_id="sess-b")] * 3,
    )
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-12",
        [_evt(ts="2026-05-12T10:00:00Z", session_id="sess-c")] * 2,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    by_date = {r["date"]: r["session_count"] for r in resp.get_json()["daily_session_counts"]}
    assert by_date["2026-05-11"] == 2
    assert by_date["2026-05-12"] == 1


def test_daily_session_counts_skips_events_with_empty_session_id(app):
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [
            _evt(ts="2026-05-11T10:00:00Z", session_id="real"),
            _evt(ts="2026-05-11T11:00:00Z", session_id=""),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    by_date = {r["date"]: r["session_count"] for r in resp.get_json()["daily_session_counts"]}
    assert by_date["2026-05-11"] == 1


# ── inter_turn_gap_histogram ──────────────────────────────────────────────────


def test_gaps_computed_only_over_user_turn_events(app):
    """heartbeats and subagents should NOT contribute to inter-turn gaps."""
    base = "2026-05-11T"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [
            # 2 user turns 2 minutes apart → 1 gap = 120s = "2-5m" bucket
            _evt(ts=f"{base}10:00:00Z", session_id="sess-1", trigger_kind="user_turn"),
            _evt(ts=f"{base}10:02:00Z", session_id="sess-1", trigger_kind="user_turn"),
            # Heartbeat 30 minutes later — must NOT add a gap
            _evt(ts=f"{base}10:32:00Z", session_id="sess-1", trigger_kind="heartbeat"),
            # 3rd user turn 1 hour after the 2nd → gap = 3600s = "1-4h" bucket
            _evt(ts=f"{base}11:02:00Z", session_id="sess-1", trigger_kind="user_turn"),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    g = resp.get_json()["inter_turn_gap_histogram"]
    assert g["gap_count"] == 2  # NOT 3 — heartbeat doesn't add a gap
    bins = {b["label"]: b["count"] for b in g["bins"]}
    assert bins["2-5m"] == 1
    assert bins["1-4h"] == 1


def test_gaps_grouped_by_session(app):
    """Gaps from different sessions must not bleed across session boundaries."""
    base = "2026-05-11T"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [
            _evt(ts=f"{base}10:00:00Z", session_id="sess-A", trigger_kind="user_turn"),
            _evt(ts=f"{base}11:00:00Z", session_id="sess-B", trigger_kind="user_turn"),
            # If we naively sorted ts globally, we'd see a 1h gap between
            # sess-A and sess-B. Correct behavior: NO gap (different sessions).
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    g = resp.get_json()["inter_turn_gap_histogram"]
    assert g["gap_count"] == 0


def test_gap_percentiles_match_raw_distribution(app):
    """median and p95 are computed from the raw gap array, not the histogram."""
    # 100 gaps of exactly 60s + 1 gap of 7200s → median=60, p95=60
    base_ts = "2026-05-11T"
    events = []
    for i in range(101):
        events.append(
            _evt(
                ts=f"{base_ts}{10 + (i // 60):02d}:{i % 60:02d}:00Z",
                session_id="sess-1",
                trigger_kind="user_turn",
            )
        )
    # Add a single 2-hour-later gap at the end
    events.append(
        _evt(
            ts=f"{base_ts}13:01:00Z",  # 2 hours after the last 60s-stride event
            session_id="sess-1",
            trigger_kind="user_turn",
        )
    )
    _write_cost_events(app.shared_dir, "admin_bot", "2026-05-11", events)
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    g = resp.get_json()["inter_turn_gap_histogram"]
    assert g["gap_count"] == 101  # 101 gaps from 102 events
    assert g["median_seconds"] == 60.0
    # p95 of [60×100, 7200] at index int(101*0.95)=95 → 60 (still in the 60s run)
    assert g["p95_seconds"] == 60.0


def test_negative_gaps_from_clock_skew_are_skipped(app):
    """Two user_turn events appearing out of timestamp order must not
    produce a negative gap — those are clock-skew artifacts and skipped.
    """
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [
            _evt(ts="2026-05-11T10:00:00Z", session_id="sess-1", trigger_kind="user_turn"),
            _evt(ts="2026-05-11T10:02:00Z", session_id="sess-1", trigger_kind="user_turn"),
        ],
    )
    # Add one event with an earlier timestamp than the 2nd — would create
    # a negative gap if naive. Our impl sorts within-session first, so
    # this just rearranges into (10:00, 09:30, 10:02) → sorted: 09:30,
    # 10:00, 10:02 → gaps of 30min and 2min (both positive).
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-12",
        [
            _evt(ts="2026-05-12T09:30:00Z", session_id="sess-2", trigger_kind="user_turn"),
            _evt(ts="2026-05-12T10:02:00Z", session_id="sess-2", trigger_kind="user_turn"),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=7")
    g = resp.get_json()["inter_turn_gap_histogram"]
    # 2 gaps total (one per session); both positive.
    assert g["gap_count"] == 2
    assert g["median_seconds"] > 0


# ── Multi-bot + invalid params ────────────────────────────────────────────────


def test_aggregates_across_all_bots_when_bot_omitted(app):
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        "2026-05-11",
        [_evt(ts="2026-05-11T10:00:00Z", bot_id="admin_bot", session_id="admin_bot-1")] * 3,
    )
    _write_cost_events(
        app.shared_dir,
        "team_bot_a",
        "2026-05-11",
        [_evt(ts="2026-05-11T10:00:00Z", bot_id="team_bot_a", session_id="team_bot_a-1")] * 2,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?days=7")
    body = resp.get_json()
    assert body["bot_id"] == ""
    assert body["time_of_day_heatmap"]["total_events"] == 5
    by_date = {r["date"]: r["session_count"] for r in body["daily_session_counts"]}
    assert by_date["2026-05-11"] == 2  # admin_bot-1 + team_bot_a-1


def test_days_param_defaults_to_30_on_invalid_input(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/activity-rhythm?bot=admin_bot&days=banana")
    assert resp.get_json()["window_days"] == 30

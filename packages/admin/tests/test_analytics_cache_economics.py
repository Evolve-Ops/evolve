"""End-to-end tests for /api/analytics/cache-economics.

Slice 3 of the Sessions redesign — the decision-support endpoint that
powers the new Cache & Cost Economics panel. Uses Flask's test_client
against a tiny app that registers only the analytics routes, with a
real cost_event JSONL fixture written to a tmp shared_dir.
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
    """Write a cost_events-<date>.jsonl file the analytics route will read."""
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
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 5000,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.01,
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
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
    }


@pytest.fixture
def app(tmp_path: Path):
    """Minimal Flask app with the analytics routes pointed at tmp shared_dir."""
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
    a.shared_dir = shared  # expose for tests to populate
    return a


# ── Happy path ────────────────────────────────────────────────────────────────


def test_endpoint_returns_expected_top_level_keys(app, tmp_path: Path):
    """Empty pod → endpoint still returns the full response shape."""
    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) >= {
        "bot_id",
        "window_days",
        "cache_health_by_day",
        "token_totals",
        "total_cost_usd",
        "cost_per_session",
        "context_trajectory_by_day",
        "summary",
    }
    assert body["bot_id"] == "admin_bot"
    assert body["window_days"] == 7
    assert body["cache_health_by_day"] == []
    assert body["total_cost_usd"] == 0.0
    assert body["cost_per_session"]["session_count"] == 0


def test_cache_health_by_day_aggregates_states(app):
    """Per-day cache_state counts roll up correctly across events."""
    today = "2026-05-11"
    events = (
        [_evt(ts=f"{today}T10:00:00Z", cache_state="warm")] * 5
        + [_evt(ts=f"{today}T11:00:00Z", cache_state="invalidated")] * 2
        + [_evt(ts=f"{today}T12:00:00Z", cache_state="fresh")] * 3
        + [_evt(ts=f"{today}T13:00:00Z", cache_state="unknown")]
    )
    _write_cost_events(app.shared_dir, "admin_bot", today, events)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    days = body["cache_health_by_day"]
    assert len(days) == 1
    assert days[0]["date"] == today
    assert days[0]["warm"] == 5
    assert days[0]["invalidated"] == 2
    assert days[0]["fresh"] == 3
    assert days[0]["unknown"] == 1


def test_token_totals_sum_across_events(app):
    today = "2026-05-11"
    events = [
        _evt(
            ts=f"{today}T10:00:00Z",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=5000,
            cache_write_tokens=250,
        )
    ] * 4
    _write_cost_events(app.shared_dir, "admin_bot", today, events)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    assert body["token_totals"] == {
        "cache_read": 20000,
        "cache_write": 1000,
        "fresh_input": 4000,
        "output": 800,
    }


def test_total_cost_usd_sums_cost_field(app):
    today = "2026-05-11"
    events = [
        _evt(ts=f"{today}T10:00:00Z", cost_usd=0.12),
        _evt(ts=f"{today}T11:00:00Z", cost_usd=0.34),
        _evt(ts=f"{today}T12:00:00Z", cost_usd=0.56),
    ]
    _write_cost_events(app.shared_dir, "admin_bot", today, events)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    assert body["total_cost_usd"] == pytest.approx(1.02, abs=1e-6)


def test_cost_per_session_aggregates_and_ranks(app):
    today = "2026-05-11"
    events = (
        # Cheap session: 5 events × $0.01 = $0.05
        [_evt(ts=f"{today}T10:00:00Z", session_id="sess-cheap", cost_usd=0.01)] * 5
        # Medium session: 3 events × $0.10 = $0.30
        + [_evt(ts=f"{today}T11:00:00Z", session_id="sess-medium", cost_usd=0.10)] * 3
        # Expensive session: 2 events × $2.00 = $4.00 (the outlier we want at top)
        + [_evt(ts=f"{today}T12:00:00Z", session_id="sess-spike", cost_usd=2.00)] * 2
    )
    _write_cost_events(app.shared_dir, "admin_bot", today, events)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    cps = body["cost_per_session"]
    assert cps["session_count"] == 3
    # Top of the top_n must be the expensive session
    assert cps["top_n"][0]["session_id"] == "sess-spike"
    assert cps["top_n"][0]["cost_usd"] == pytest.approx(4.0, abs=1e-4)
    assert cps["top_n"][0]["event_count"] == 2
    assert cps["top_n"][0]["bot_id"] == "admin_bot"
    # Histogram bins should classify each session into the right bin
    bins = {b["label"]: b["count"] for b in cps["histogram"]}
    assert bins["$0.01-$0.05"] == 0  # $0.05 falls into next bucket due to half-open
    assert bins["$0.05-$0.25"] == 1  # the $0.05 cheap session
    assert bins["$0.25-$1"] == 1     # the $0.30 medium session
    assert bins["$1-$5"] == 1        # the $4.00 expensive session


def test_context_trajectory_per_day(app):
    """avg_prompt_tokens = mean (input + cache_read + cache_write) per event per day."""
    day1 = "2026-05-10"
    day2 = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        day1,
        [
            _evt(
                ts=f"{day1}T10:00:00Z",
                input_tokens=1000,
                cache_read_tokens=4000,
                cache_write_tokens=0,
            )
        ] * 4,  # 4 events × 5000 prompt = avg 5000
    )
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        day2,
        [
            _evt(
                ts=f"{day2}T10:00:00Z",
                input_tokens=2000,
                cache_read_tokens=8000,
                cache_write_tokens=0,
            )
        ] * 2,  # 2 events × 10000 prompt = avg 10000
    )

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    traj = {d["date"]: d for d in body["context_trajectory_by_day"]}
    assert traj[day1]["avg_prompt_tokens"] == 5000
    assert traj[day1]["event_count"] == 4
    assert traj[day2]["avg_prompt_tokens"] == 10000
    assert traj[day2]["event_count"] == 2


def test_summary_invalidated_ratio_matches_session_economics(app):
    """Headline matches session_economics monitor's computation exactly.

    The whole point of showing this on the page is so operators can
    cross-check the alert against the visible data. Drift would erode trust.
    """
    today = "2026-05-11"
    events = (
        [_evt(ts=f"{today}T10:00:00Z", cache_state="invalidated")] * 30
        + [_evt(ts=f"{today}T11:00:00Z", cache_state="warm")] * 70
        + [_evt(ts=f"{today}T12:00:00Z", cache_state="fresh")] * 100  # excluded
        + [_evt(ts=f"{today}T13:00:00Z", cache_state="unknown")] * 50  # excluded
    )
    _write_cost_events(app.shared_dir, "admin_bot", today, events)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    summary = resp.get_json()["summary"]
    # invalidated / (warm + invalidated) over the participating set.
    # Fresh and unknown are excluded — must match session_economics.
    assert summary["participating_events"] == 100
    assert summary["invalidated_ratio"] == 0.30
    assert summary["total_events"] == 250


def test_summary_hit_rate_excludes_non_participating_events(app):
    today = "2026-05-11"
    # All warm, with cache_read=4000 + cache_write=0 + input=1000 per event.
    # Blended hit rate = 4000 / 5000 = 0.80.
    warm = [
        _evt(
            ts=f"{today}T10:00:00Z",
            cache_state="warm",
            cache_read_tokens=4000,
            cache_write_tokens=0,
            input_tokens=1000,
        )
    ] * 50
    # Fresh events with huge inputs must NOT tank the realized hit rate —
    # they're excluded by the cache-participating filter.
    fresh = [
        _evt(
            ts=f"{today}T11:00:00Z",
            cache_state="fresh",
            cache_read_tokens=0,
            cache_write_tokens=0,
            input_tokens=100000,
        )
    ] * 50
    _write_cost_events(app.shared_dir, "admin_bot", today, warm + fresh)

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    summary = resp.get_json()["summary"]
    assert summary["realized_hit_rate"] == 0.80


def test_multi_bot_when_bot_param_omitted(app):
    """When bot is empty, results aggregate across all members + evolve."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(
            ts=f"{today}T10:00:00Z",
            bot_id="admin_bot",
            session_id="admin_bot-sess-1",
            cost_usd=0.50,
        )],
    )
    _write_cost_events(
        app.shared_dir,
        "team_bot_a",
        today,
        [_evt(
            ts=f"{today}T10:00:00Z",
            bot_id="team_bot_a",
            session_id="team_bot_a-sess-1",
            cost_usd=0.30,
        )],
    )

    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?days=7")
    body = resp.get_json()
    assert body["bot_id"] == ""
    assert body["total_cost_usd"] == pytest.approx(0.80, abs=1e-6)
    # Both bots' sessions should appear in the top-N
    bots_in_top = {row["bot_id"] for row in body["cost_per_session"]["top_n"]}
    assert "admin_bot" in bots_in_top
    assert "team_bot_a" in bots_in_top


def test_days_param_defaults_to_30_when_invalid(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=garbage")
    body = resp.get_json()
    assert body["window_days"] == 30


def test_unknown_cache_state_routed_to_unknown_bucket(app):
    """A cache_state value the schema doesn't enumerate must land in 'unknown'."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", cache_state="weird_new_state")] * 3,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/cache-economics?bot=admin_bot&days=7")
    body = resp.get_json()
    day = body["cache_health_by_day"][0]
    assert day["unknown"] == 3
    assert day["warm"] == 0
    assert day["invalidated"] == 0
    assert day["fresh"] == 0

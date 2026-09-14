"""End-to-end tests for /api/analytics/sessions (Slice 8 enrichment + filters).

Covers the Slice 8 additions to the existing endpoint:
  - cache_state_counts populated from cost_event JSONL
  - total_cost_usd summed across the session
  - peak_prompt_tokens = max(input + cache_read + cache_write) per event
  - multi_turn=true filter (turn_count >= 2)
  - cache_invalidated=true filter (any invalidated event in session)

The legacy class / corrections / efficiency filters remain accepted for
backward compatibility; their behavior is unchanged and not re-tested here.
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


def _write_session_summary(
    shared_dir: Path, bot_id: str, date_str: str, summaries: list[dict]
) -> None:
    """Drop session_summary records into the bot's annotation JSONL."""
    annotations = shared_dir / "annotations" / bot_id
    annotations.mkdir(parents=True, exist_ok=True)
    path = annotations / f"{date_str}.jsonl"
    with path.open("a") as f:
        for s in summaries:
            rec = {"type": "session_summary", **s}
            f.write(json.dumps(rec) + "\n")


def _evt(
    *,
    ts: str,
    bot_id: str = "admin_bot",
    session_id: str = "sess-1",
    cache_state: str = "warm",
    cost_usd: float = 0.01,
    input_tokens: int = 1000,
    cache_read_tokens: int = 5000,
    cache_write_tokens: int = 0,
    output_tokens: int = 200,
) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": ts,
        "bot_id": bot_id,
        "session_id": session_id,
        "trigger_kind": "user_turn",
        "model": "claude-sonnet",
        "provider": "anthropic",
        "cache_state": cache_state,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
    }


def _summary(
    *,
    session_id: str,
    turn_count: int = 1,
    ts: str = "2026-05-11T10:00:00Z",
) -> dict:
    return {
        "session_id": session_id,
        "ts": ts,
        "turn_count": turn_count,
        "tier": "ambiguous",
        "outcome": "",
        "correction_count": 0,
        "efficiency_flag": False,
        "first_response_resolution": (turn_count == 1),
        "applications_invoked": [],
        "promises_made": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
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


# ── Enrichment ────────────────────────────────────────────────────────────────


def test_cache_state_counts_populated_from_cost_events(app):
    """Each session row gets a cache_state_counts dict derived from cost_events."""
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir,
        "admin_bot",
        date_str,
        [_summary(session_id="sess-1", turn_count=5, ts=f"{date_str}T12:00:00Z")],
    )
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        date_str,
        [_evt(ts=f"{date_str}T10:00:00Z", session_id="sess-1", cache_state="warm")] * 3
        + [_evt(ts=f"{date_str}T11:00:00Z", session_id="sess-1", cache_state="invalidated")] * 2
        + [_evt(ts=f"{date_str}T11:30:00Z", session_id="sess-1", cache_state="fresh")],
    )

    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7")
    sessions = resp.get_json()["sessions"]
    assert len(sessions) == 1
    s = sessions[0]
    assert s["cache_state_counts"] == {
        "warm": 3, "invalidated": 2, "fresh": 1, "unknown": 0,
    }


def test_total_cost_and_peak_prompt_populated(app):
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [_summary(session_id="sess-1", turn_count=2)],
    )
    _write_cost_events(
        app.shared_dir, "admin_bot", date_str,
        [
            _evt(
                ts=f"{date_str}T10:00:00Z", session_id="sess-1",
                cost_usd=0.50,
                input_tokens=500, cache_read_tokens=2000, cache_write_tokens=0,
            ),
            _evt(
                ts=f"{date_str}T10:05:00Z", session_id="sess-1",
                cost_usd=1.25,
                input_tokens=1000, cache_read_tokens=20000, cache_write_tokens=0,
            ),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7")
    s = resp.get_json()["sessions"][0]
    assert s["total_cost_usd"] == pytest.approx(1.75, abs=1e-4)
    # peak = max(500+2000+0, 1000+20000+0) = 21000
    assert s["peak_prompt_tokens"] == 21000


def test_session_with_no_cost_events_gets_zero_enrichment(app):
    """Sessions that exist via session_summary but have no cost_event records
    (e.g. older bots that didn't ship the cost-event sidecar) get a zeroed
    enrichment payload — frontend can render without null checks."""
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [_summary(session_id="sess-orphan", turn_count=3)],
    )
    # NO cost_events written for this session.
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7")
    s = resp.get_json()["sessions"][0]
    assert s["cache_state_counts"] == {
        "warm": 0, "invalidated": 0, "fresh": 0, "unknown": 0,
    }
    assert s["total_cost_usd"] == 0.0
    assert s["peak_prompt_tokens"] == 0


def test_unknown_cache_state_routed_to_unknown_bucket(app):
    """A cache_state value the schema doesn't enumerate lands in 'unknown'."""
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [_summary(session_id="sess-1")],
    )
    _write_cost_events(
        app.shared_dir, "admin_bot", date_str,
        [_evt(ts=f"{date_str}T10:00:00Z", session_id="sess-1", cache_state="weird_new_state")] * 3,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7")
    s = resp.get_json()["sessions"][0]
    assert s["cache_state_counts"]["unknown"] == 3
    assert s["cache_state_counts"]["warm"] == 0


# ── multi_turn filter ─────────────────────────────────────────────────────────


def test_multi_turn_filter_excludes_single_turn_sessions(app):
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [
            _summary(session_id="sess-single", turn_count=1, ts=f"{date_str}T09:00:00Z"),
            _summary(session_id="sess-multi",  turn_count=5, ts=f"{date_str}T10:00:00Z"),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7&multi_turn=true")
    sids = [s["session_id"] for s in resp.get_json()["sessions"]]
    assert sids == ["sess-multi"]


def test_multi_turn_filter_default_off_returns_all(app):
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [
            _summary(session_id="sess-single", turn_count=1),
            _summary(session_id="sess-multi",  turn_count=5),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7")
    assert len(resp.get_json()["sessions"]) == 2


# ── cache_invalidated filter ──────────────────────────────────────────────────


def test_cache_invalidated_filter_keeps_only_sessions_with_invalidated_events(app):
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [
            _summary(session_id="sess-clean", ts=f"{date_str}T09:00:00Z"),
            _summary(session_id="sess-dirty", ts=f"{date_str}T10:00:00Z"),
        ],
    )
    _write_cost_events(
        app.shared_dir, "admin_bot", date_str,
        [_evt(ts=f"{date_str}T09:00:00Z", session_id="sess-clean", cache_state="warm")] * 5
        + [_evt(ts=f"{date_str}T10:00:00Z", session_id="sess-dirty", cache_state="invalidated")] * 1
        + [_evt(ts=f"{date_str}T10:05:00Z", session_id="sess-dirty", cache_state="warm")] * 9,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7&cache_invalidated=true")
    sids = [s["session_id"] for s in resp.get_json()["sessions"]]
    assert sids == ["sess-dirty"]


def test_cache_invalidated_combines_with_multi_turn(app):
    """Both filters are AND-composed."""
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [
            _summary(session_id="single-invalidated", turn_count=1, ts=f"{date_str}T09:00:00Z"),
            _summary(session_id="multi-clean",        turn_count=4, ts=f"{date_str}T10:00:00Z"),
            _summary(session_id="multi-invalidated",  turn_count=4, ts=f"{date_str}T11:00:00Z"),
        ],
    )
    _write_cost_events(
        app.shared_dir, "admin_bot", date_str,
        [
            _evt(ts=f"{date_str}T09:00:00Z", session_id="single-invalidated", cache_state="invalidated"),
            _evt(ts=f"{date_str}T10:00:00Z", session_id="multi-clean", cache_state="warm"),
            _evt(ts=f"{date_str}T11:00:00Z", session_id="multi-invalidated", cache_state="invalidated"),
        ],
    )
    with app.test_client() as c:
        resp = c.get(
            "/api/analytics/sessions?bot=admin_bot&days=7"
            "&multi_turn=true&cache_invalidated=true"
        )
    sids = [s["session_id"] for s in resp.get_json()["sessions"]]
    assert sids == ["multi-invalidated"]


# ── Backward-compat: legacy filters still accepted ────────────────────────────


def test_legacy_class_filter_still_accepted(app):
    """The class filter is no longer surfaced in the UI but remains
    functional so any old bookmark or external caller keeps working."""
    date_str = "2026-05-11"
    _write_session_summary(
        app.shared_dir, "admin_bot", date_str,
        [
            {**_summary(session_id="sess-prod"), "tier": "productive"},
            {**_summary(session_id="sess-maint"), "tier": "maintenance"},
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/sessions?bot=admin_bot&days=7&class=productive")
    sids = [s["session_id"] for s in resp.get_json()["sessions"]]
    assert sids == ["sess-prod"]

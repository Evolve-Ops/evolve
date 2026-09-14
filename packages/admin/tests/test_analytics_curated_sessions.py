"""End-to-end tests for /api/analytics/curated-sessions.

Slice 7 of the Sessions redesign — the "go read these" surface.
Each of the four categories (most_expensive, longest_by_turns,
biggest_context, most_invalidated) is exercised with a hand-rolled
JSONL fixture and an assertion on the chosen session.
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
    input_tokens: int = 1000,
    cache_read_tokens: int = 5000,
    cache_write_tokens: int = 0,
    output_tokens: int = 200,
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


# ── Happy path / shape ────────────────────────────────────────────────────────


def test_endpoint_returns_expected_shape_when_empty(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"bot_id", "window_days", "session_count", "curated"}
    assert body["bot_id"] == "admin_bot"
    assert body["window_days"] == 7
    assert body["session_count"] == 0
    assert body["curated"] == []


def _by_category(curated: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in curated:
        out.setdefault(c["category"], []).append(c)
    return out


# ── Category 1: most_expensive ────────────────────────────────────────────────


def test_most_expensive_picks_top_cost_sessions(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", session_id="cheap", cost_usd=0.01)] * 3
        + [_evt(ts=f"{today}T11:00:00Z", session_id="medium", cost_usd=0.50)] * 2
        + [_evt(ts=f"{today}T12:00:00Z", session_id="expensive", cost_usd=2.00)] * 5,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    me = cats.get("most_expensive", [])
    assert me, "most_expensive category should be present"
    assert me[0]["session_id"] == "expensive"
    assert me[0]["metric_value"] == pytest.approx(10.0, abs=1e-4)  # 5 × $2.00
    assert me[0]["metric_label"] == "$10.00"


def test_most_expensive_skips_zero_cost_sessions(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", session_id="free", cost_usd=0.0)] * 5,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    assert cats.get("most_expensive", []) == []


# ── Category 2: longest_by_turns ──────────────────────────────────────────────


def test_longest_by_turns_picks_highest_event_count(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", session_id="short")] * 2
        + [_evt(ts=f"{today}T11:00:00Z", session_id="long")] * 47,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    lt = cats.get("longest_by_turns", [])
    assert lt
    assert lt[0]["session_id"] == "long"
    assert lt[0]["event_count"] == 47
    assert lt[0]["metric_label"] == "47 events"


def test_longest_by_turns_skips_single_event_sessions(app):
    """A session with 1 event isn't 'long' — exclude from this category."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", session_id=f"sess-{i}") for i in range(5)],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    assert cats.get("longest_by_turns", []) == []


# ── Category 3: biggest_context ───────────────────────────────────────────────


def test_biggest_context_uses_peak_prompt_tokens_per_session(app):
    """peak_prompt_tokens = max(input + cache_read + cache_write) over events."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [
            # Compact session: all events have ~1k prompt tokens.
            _evt(
                ts=f"{today}T10:00:00Z",
                session_id="compact",
                input_tokens=1000,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
        ] * 5 + [
            # Bloated session: one event has a huge cache_read at the end.
            _evt(
                ts=f"{today}T11:00:00Z",
                session_id="bloated",
                input_tokens=500,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            _evt(
                ts=f"{today}T11:01:00Z",
                session_id="bloated",
                input_tokens=500,
                cache_read_tokens=80000,  # the peak
                cache_write_tokens=0,
            ),
        ],
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    bc = cats.get("biggest_context", [])
    assert bc
    assert bc[0]["session_id"] == "bloated"
    assert bc[0]["peak_prompt_tokens"] == 80500  # 500 + 80000


# ── Category 4: most_invalidated ──────────────────────────────────────────────


def test_most_invalidated_picks_highest_invalidation_ratio(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        # Healthy: 10 warm
        [_evt(ts=f"{today}T10:00:00Z", session_id="healthy", cache_state="warm")] * 10
        # Bad: 4 warm + 8 invalidated (67% invalidated, ≥5 participating)
        + [_evt(ts=f"{today}T11:00:00Z", session_id="bad", cache_state="warm")] * 4
        + [_evt(ts=f"{today}T11:01:00Z", session_id="bad", cache_state="invalidated")] * 8,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    mi = cats.get("most_invalidated", [])
    assert mi
    assert mi[0]["session_id"] == "bad"
    assert mi[0]["metric_value"] == pytest.approx(8/12, abs=1e-4)
    assert "67% invalidated" in mi[0]["metric_label"]


def test_most_invalidated_floors_at_5_participating_events(app):
    """A 1-of-1 invalidated session is 100% but uselessly small — skip it."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        # Tiny: 1 invalidated event. Ratio = 100% but only 1 participating.
        [_evt(ts=f"{today}T10:00:00Z", session_id="tiny", cache_state="invalidated")]
        # Real: 5 invalidated + 3 warm = 5/8 = 62.5%, ≥5 participating ✓
        + [_evt(ts=f"{today}T11:00:00Z", session_id="real", cache_state="invalidated")] * 5
        + [_evt(ts=f"{today}T11:01:00Z", session_id="real", cache_state="warm")] * 3,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    mi = cats.get("most_invalidated", [])
    assert mi
    # "real" should win — "tiny" is excluded by the min-participating floor.
    assert mi[0]["session_id"] == "real"
    sids = [e["session_id"] for e in mi]
    assert "tiny" not in sids


def test_most_invalidated_ignores_fresh_and_unknown_in_denominator(app):
    """Only warm + invalidated count as cache-participating."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        # 5 invalidated + 0 warm = 100% invalidation ratio over 5 participating.
        # The fresh and unknown events should not affect the ratio.
        [_evt(ts=f"{today}T10:00:00Z", session_id="hot", cache_state="invalidated")] * 5
        + [_evt(ts=f"{today}T11:00:00Z", session_id="hot", cache_state="fresh")] * 100
        + [_evt(ts=f"{today}T12:00:00Z", session_id="hot", cache_state="unknown")] * 100,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    mi = cats.get("most_invalidated", [])
    assert mi
    assert mi[0]["session_id"] == "hot"
    assert mi[0]["metric_value"] == 1.0  # 5/5 = 100%


# ── Multi-bot + entry payload ─────────────────────────────────────────────────


def test_multi_bot_when_bot_omitted(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(
            ts=f"{today}T10:00:00Z", bot_id="admin_bot", session_id="admin_bot-1", cost_usd=5.00,
        )] * 3,
    )
    _write_cost_events(
        app.shared_dir,
        "team_bot_a",
        today,
        [_evt(
            ts=f"{today}T10:00:00Z", bot_id="team_bot_a", session_id="team_bot_a-1", cost_usd=0.50,
        )] * 3,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?days=7")
    body = resp.get_json()
    assert body["bot_id"] == ""
    me = _by_category(body["curated"]).get("most_expensive", [])
    assert me[0]["session_id"] == "admin_bot-1"
    assert me[0]["bot_id"] == "admin_bot"


def test_entry_includes_diagnostic_payload(app):
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(
            ts=f"{today}T10:00:00Z",
            session_id="spike",
            cost_usd=2.00,
            trigger_kind="user_turn",
        )] * 5,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    me = _by_category(resp.get_json()["curated"])["most_expensive"][0]
    # All fields required by the frontend renderer should be present.
    assert me["session_id"] == "spike"
    assert me["bot_id"] == "admin_bot"
    assert me["event_count"] == 5
    assert me["cost_usd"] == pytest.approx(10.0, abs=1e-4)
    assert me["trigger_kinds"] == ["user_turn"]
    assert me["first_ts"] == "2026-05-11T10:00:00Z"
    assert me["last_ts"] == "2026-05-11T10:00:00Z"
    assert me["rationale"]  # non-empty


def test_same_session_may_appear_in_multiple_categories(app):
    """A pathological session that's expensive AND long is informative
    when it shows up in both — duplication is intended, not a bug."""
    today = "2026-05-11"
    _write_cost_events(
        app.shared_dir,
        "admin_bot",
        today,
        [_evt(ts=f"{today}T10:00:00Z", session_id="problem", cost_usd=1.00)] * 30,
    )
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=7")
    cats = _by_category(resp.get_json()["curated"])
    assert cats["most_expensive"][0]["session_id"] == "problem"
    assert cats["longest_by_turns"][0]["session_id"] == "problem"


def test_days_param_defaults_to_30_on_invalid_input(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/curated-sessions?bot=admin_bot&days=banana")
    assert resp.get_json()["window_days"] == 30

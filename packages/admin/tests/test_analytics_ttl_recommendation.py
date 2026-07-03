"""End-to-end tests for /api/analytics/ttl-recommendation.

Slice 9 of the Sessions redesign — the synthesis step that turns raw
cache/gap data into a concrete "raise to N" / "lower to N" / "hold" call.

Tests cover the decision tree (each verdict path), tier mapping,
confidence gating, and the impact estimate. The openclaw.json read
is monkey-patched so tests don't depend on /Users/<bot>/ existing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

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


def _generate_user_turns(
    *,
    bot_id: str,
    session_id: str,
    base_iso: str,  # "YYYY-MM-DDThh:mm:ss"
    gap_seconds: int,
    n: int,
    cache_state: str = "warm",
    cache_write_tokens: int = 0,
) -> list[dict]:
    """Generate n user_turn events at fixed gap_seconds intervals."""
    from datetime import datetime, timedelta, timezone

    start = datetime.fromisoformat(base_iso).replace(tzinfo=timezone.utc)
    out = []
    for i in range(n):
        ts = (start + timedelta(seconds=gap_seconds * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(_evt(
            ts=ts,
            bot_id=bot_id,
            session_id=session_id,
            trigger_kind="user_turn",
            cache_state=cache_state,
            cache_write_tokens=cache_write_tokens,
        ))
    return out


# ── Shape ─────────────────────────────────────────────────────────────────────


def test_endpoint_returns_expected_shape(app):
    """Empty pod returns valid recommendation entries (one per bot)."""
    with patch("evolve_admin.web.server.load_network") as ln:
        ln.return_value = json.loads((app.shared_dir.parent / "network.json").read_text())
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"bot_id", "window_days", "recommendations"}
    # All members + evolve, each gets a "not_enough_data" verdict (no events).
    bots_seen = [r["bot_id"] for r in body["recommendations"]]
    assert "evolve" in bots_seen
    assert "admin_bot" in bots_seen
    assert all(r["verdict"] == "not_enough_data" for r in body["recommendations"])


def test_specific_bot_filter(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    body = resp.get_json()
    assert body["bot_id"] == "admin_bot"
    assert len(body["recommendations"]) == 1
    assert body["recommendations"][0]["bot_id"] == "admin_bot"


# ── Confidence gating ────────────────────────────────────────────────────────


def test_not_enough_data_when_gaps_below_50(app, tmp_path):
    """≥50 multi-turn gaps required for any recommendation."""
    date_str = "2026-05-11"
    # 40 user_turn events on one session → 39 gaps (below 50)
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T10:00:00", gap_seconds=120, n=40,
    )
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)
    with app.test_client() as c:
        resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "not_enough_data"
    assert rec["confidence"] == "low"
    assert rec["target_ttl"] is None


def test_not_enough_data_when_window_below_3_days(app):
    """Even with abundant gaps, sub-3d window gates to not_enough_data."""
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso="2026-05-11T10:00:00", gap_seconds=60, n=300,
    )
    _write_cost_events(app.shared_dir, "admin_bot", "2026-05-11", events)
    with app.test_client() as c:
        resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=2")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "not_enough_data"


# ── Verdict: raise (strong signal) ───────────────────────────────────────────


def test_raise_when_p95_far_over_ttl_and_invalidation_elevated(app):
    """p95 gap = 30min, TTL = 15min, invalidation = 31% → raise to next tier."""
    date_str = "2026-05-11"
    # 60 user turns with 30-min gaps in one session = 59 gaps of 1800s
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=1800, n=60,
    )
    # Plus 100 cache-participating events: 50 invalidated + 50 warm
    for i in range(50):
        events.append(_evt(
            ts=f"{date_str}T09:{i:02d}:00Z",
            session_id=f"cache-{i}",
            cache_state="invalidated",
            cache_write_tokens=2000,
        ))
    for i in range(50):
        events.append(_evt(
            ts=f"{date_str}T10:{i:02d}:00Z",
            session_id=f"cache-{i+50}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    # Drop a minimal openclaw.json at the bot's home for the read.
    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"mode": "cache-ttl", "ttl": "15m"}}}
    }))

    # Monkey-patch _bot_home so it points at our fake user tree.
    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert rec["current_ttl"] == "15m"
    assert rec["current_ttl_seconds"] == 900
    # p95 is 1800s; smallest tier ≥ that is 30m (1800s)
    assert rec["target_ttl"] == "30m"
    assert rec["target_ttl_seconds"] == 1800
    # 59 gaps + 100 participating ≥ medium; high requires ≥200 of each.
    assert rec["confidence"] in ("medium", "high")
    # Reasoning should call out the invalidation pattern.
    assert "invalidat" in rec["reasoning"].lower()
    assert "p95" in rec["reasoning"] or "30m" in rec["reasoning"]


# ── Verdict: raise (soft signal) ─────────────────────────────────────────────


def test_raise_when_median_past_ttl_even_if_invalidation_moderate(app):
    """Median gap past TTL → raise, even at lower invalidation rate."""
    date_str = "2026-05-11"
    # 80 user turns with 20-min gaps → median is 20m, well past 15m TTL.
    # Invalidation kept low (8%) so the strong-raise path doesn't fire —
    # this exercises the median-gap-only path.
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=1200, n=80,
    )
    for i in range(8):
        events.append(_evt(
            ts=f"{date_str}T15:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="invalidated", cache_write_tokens=1000,
        ))
    for i in range(92):
        events.append(_evt(
            ts=f"{date_str}T16:{i:02d}:00Z", session_id=f"c{i+8}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "15m"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert "Median" in rec["reasoning"]


# ── Verdict: lower ────────────────────────────────────────────────────────────


def test_lower_when_cache_warm_and_gaps_short_and_ttl_oversized(app):
    """All warm, gaps under 1m, TTL is 4h → recommend stepping down to 1h."""
    date_str = "2026-05-11"
    # 80 user turns with 30s gaps = 30s median, p95 well under 1h
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T10:00:00", gap_seconds=30, n=80,
    )
    # 100 warm cache events, no invalidation
    for i in range(100):
        events.append(_evt(
            ts=f"{date_str}T11:{i // 60:02d}:{i % 60:02d}Z",
            session_id=f"c{i}", cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "4h"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "lower"
    assert rec["current_ttl"] == "4h"
    # Previous tier below 4h is 1h
    assert rec["target_ttl"] == "1h"


# ── Verdict: hold ─────────────────────────────────────────────────────────────


def test_hold_when_ttl_well_matched(app):
    """Gaps centered around current TTL with moderate invalidation → hold."""
    date_str = "2026-05-11"
    # 80 user turns at 2-min gaps; TTL 1h → well within
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T10:00:00", gap_seconds=120, n=80,
    )
    # 10% invalidation, 90% warm — below the strong-raise threshold
    for i in range(10):
        events.append(_evt(
            ts=f"{date_str}T15:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="invalidated", cache_write_tokens=500,
        ))
    for i in range(90):
        events.append(_evt(
            ts=f"{date_str}T16:{i:02d}:00Z", session_id=f"c{i+10}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "1h"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "hold"
    assert rec["target_ttl"] is None
    assert "well-matched" in rec["reasoning"]


# ── Verdict: hold (no TTL config) ─────────────────────────────────────────────


def test_hold_when_openclaw_json_missing(app):
    """Can't read TTL → hold with low confidence; no spurious recommendation."""
    date_str = "2026-05-11"
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T10:00:00", gap_seconds=120, n=80,
    )
    for i in range(60):
        events.append(_evt(
            ts=f"{date_str}T15:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    # _bot_home points at a path that doesn't exist → openclaw.json read fails
    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "nope" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "hold"
    assert rec["current_ttl"] is None
    assert rec["confidence"] == "low"
    assert "Could not read" in rec["reasoning"]


# ── Impact estimate ──────────────────────────────────────────────────────────


def test_impact_estimate_scales_with_cache_write_tokens(app):
    """Estimated monthly savings should be > 0 when raise is recommended
    and there's a meaningful cacheWrite-token footprint to reclaim."""
    date_str = "2026-05-11"
    # Strong-raise pattern + big cache_write_tokens on each invalidated event
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=900, n=60,
    )
    for i in range(50):
        events.append(_evt(
            ts=f"{date_str}T09:{i:02d}:00Z",
            session_id=f"c{i}",
            cache_state="invalidated",
            cache_write_tokens=50_000,  # heavy cacheWrite footprint
        ))
    for i in range(50):
        events.append(_evt(
            ts=f"{date_str}T10:{i:02d}:00Z",
            session_id=f"c{i+50}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "15m"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert rec["estimated_monthly_savings_usd"] is not None
    assert rec["estimated_monthly_savings_usd"] > 0


# ── Slice 10: sparse-gap heartbeat-heavy bot path ────────────────────────────


def test_raise_when_invalidation_high_and_gaps_sparse(app):
    """Heartbeat-heavy bot — plenty of cache_state evidence, few user-turn pairs.

    Real-world case (team_bot_a, team_bot_c on the mini): 60-90% invalidation across
    hundreds of cache events, but only a handful of multi-turn user sessions.
    Before Slice 10 the verdict was "not_enough_data" because gap_count < 50.
    Now the recommendation fires from invalidation evidence alone — bumping
    one tier as a conservative starting point.
    """
    date_str = "2026-05-11"
    # ONLY 5 user_turn gaps (well below the old 50 threshold) but…
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=300, n=6,
    )
    # …a strong invalidation signal from heartbeat-driven cache events.
    for i in range(150):
        events.append(_evt(
            ts=f"{date_str}T{10 + i // 60:02d}:{i % 60:02d}:00Z",
            session_id=f"hb-{i}",
            trigger_kind="heartbeat",
            cache_state="invalidated",
            cache_write_tokens=2000,
        ))
    for i in range(50):
        events.append(_evt(
            ts=f"{date_str}T15:{i:02d}:00Z",
            session_id=f"warm-{i}",
            trigger_kind="heartbeat",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "15m"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    # Conservative one-tier bump: 15m → 30m
    assert rec["target_ttl"] == "30m"
    assert rec["target_ttl_seconds"] == 1800
    # Reasoning should call out the sparse-gap nature explicitly.
    assert "invalidat" in rec["reasoning"].lower()


# ── Slice 10: motivating signal id for snooze ─────────────────────────────────


def test_motivating_signal_id_set_when_firing_signal_exists(app):
    """When a session_economics signal is firing for the bot, the
    recommendation carries its id so the frontend can snooze it inline."""
    from signals import store as signals_store
    from schema.signal import make_signature

    # Drop a firing cache_invalidation_elevated signal in the store.
    signals_store.observe(
        app.shared_dir,
        signature=make_signature(
            "session_economics", "cache_invalidation_elevated", "admin_bot",
        ),
        producer="session_economics",
        type="cache_invalidation_elevated",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="admin_bot: invalidation elevated",
        details={
            "bot_id": "admin_bot", "window_days": 7,
            "invalidated_count": 60, "participating_count": 100,
            "invalidated_ratio": 0.60, "threshold_ratio": 0.15,
        },
    )

    # Plus enough cost_event evidence to actually fire a "raise" verdict.
    date_str = "2026-05-11"
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=1800, n=60,
    )
    for i in range(40):
        events.append(_evt(
            ts=f"{date_str}T09:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="invalidated", cache_write_tokens=5000,
        ))
    for i in range(60):
        events.append(_evt(
            ts=f"{date_str}T10:{i:02d}:00Z", session_id=f"c{i+40}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "15m"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert rec["motivating_signal_id"], (
        "motivating_signal_id should be populated when a firing signal exists"
    )


def test_motivating_signal_id_null_when_no_signal_firing(app):
    """No signal in the store → null id; rec still computed from cost_events."""
    date_str = "2026-05-11"
    events = _generate_user_turns(
        bot_id="admin_bot", session_id="s1",
        base_iso=f"{date_str}T08:00:00", gap_seconds=1800, n=60,
    )
    for i in range(40):
        events.append(_evt(
            ts=f"{date_str}T09:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="invalidated", cache_write_tokens=5000,
        ))
    for i in range(60):
        events.append(_evt(
            ts=f"{date_str}T10:{i:02d}:00Z", session_id=f"c{i+40}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "15m"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert rec["motivating_signal_id"] is None


# ── Slice 10: tier ceiling — UI doesn't support beyond 4h ─────────────────────


def test_target_caps_at_4h_even_when_p95_demands_more(app):
    """p95 gap of 6h would suggest 12h, but the Cost Measures UI tops out
    at 4h. Target should saturate at 4h rather than recommend a value the
    operator can't actually select."""
    date_str = "2026-05-11"
    # 80 user turns with 6-hour gaps spread across multiple sessions
    events = []
    for sess_idx in range(20):
        events.extend(_generate_user_turns(
            bot_id="admin_bot", session_id=f"s-{sess_idx}",
            base_iso=f"{date_str}T0{sess_idx % 10}:00:00",
            gap_seconds=21600, n=4,
        ))
    for i in range(80):
        events.append(_evt(
            ts=f"{date_str}T20:{i:02d}:00Z", session_id=f"c{i}",
            cache_state="invalidated", cache_write_tokens=2000,
        ))
    for i in range(20):
        events.append(_evt(
            ts=f"{date_str}T21:{i:02d}:00Z", session_id=f"c{i+80}",
            cache_state="warm",
        ))
    _write_cost_events(app.shared_dir, "admin_bot", date_str, events)

    openclaw_dir = app.shared_dir / "fake-users" / "admin_bot" / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    (openclaw_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"contextPruning": {"ttl": "1h"}}}
    }))

    from evolve_admin.web import server as _server
    with patch.object(_server, "_bot_home", lambda b, n=None: app.shared_dir / "fake-users" / b):
        with app.test_client() as c:
            resp = c.get("/api/analytics/ttl-recommendation?bot=admin_bot&days=7")
    rec = resp.get_json()["recommendations"][0]
    assert rec["verdict"] == "raise"
    assert rec["target_ttl"] == "4h"  # saturates at UI ceiling, NOT 12h
    assert rec["target_ttl_seconds"] == 14400


# ── Multi-bot iteration ───────────────────────────────────────────────────────


def test_multi_bot_returns_one_entry_per_bot(app):
    """When ``bot`` is empty, return one recommendation per member + evolve."""
    with app.test_client() as c:
        resp = c.get("/api/analytics/ttl-recommendation?days=7")
    body = resp.get_json()
    bots_seen = {r["bot_id"] for r in body["recommendations"]}
    # _oc_members() returns members + evolve
    assert "admin_bot" in bots_seen
    assert "team_bot_a" in bots_seen
    assert "evolve" in bots_seen

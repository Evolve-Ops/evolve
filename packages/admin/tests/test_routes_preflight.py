"""Tests for /api/preflight/health.

Verifies the endpoint reads cascade-telemetry spans from on-disk and
computes the expected per-bot pre-flight stats. Test fixtures mirror
the production span path layout (per-bot ``{shared}/{bot}/spans/``)
so the iter_turn_spans cross-location merge resolves correctly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
# packages/analyzer is required because routes_preflight.py lazily
# imports observability.session_rollup AND cascade.preflight_audit.
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.routes_preflight import (  # noqa: E402
    _compute_preflight_health,
    register_preflight_routes,
)


# ── Span fixture helper ─────────────────────────────────────────────────────


def _write_span(
    shared: Path,
    *,
    bot_id: str = "team_bot_a",
    day: str = "2026-06-07",
    session_id: str = "sess-1",
    turn_index: int = 1,
    preflight_tier: str | None = "tier1",
    preflight_reason: str = "regex:design_imperative",
    preflight_layer: str | None = "regex",
    preflight_confidence: float = 1.0,
    preflight_latency_ms: float = 0.5,
    tier_used: str | None = None,
    tier_chosen_by: str = "preflight",
    success: bool = True,
    total_cost: float = 0.01,
    output_tokens: int = 100,
    tool_count: int = 0,
    struggle_score: float = 0.0,
) -> None:
    """Append a synthetic cascade-telemetry span with pre-flight attrs.

    Matches the path + schema the plugin's CascadeTelemetry.ts writes
    to in production: {shared}/{bot}/spans/spans-{day}.jsonl with
    producer='cascade_telemetry'. The iter_turn_spans merge helper
    requires that producer tag to surface the span.
    """
    spans_dir = shared / bot_id / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)

    attrs: dict = {
        "session_id": session_id,
        "turn_index": turn_index,
        "cascade.tier_used": tier_used if tier_used else preflight_tier,
        "cascade.tier_chosen_by": tier_chosen_by,
        "cascade.success": success,
        "cascade.struggle.score": struggle_score,
        "cascade.struggle.raw.tool_count_per_turn": tool_count,
    }
    if preflight_layer is not None:
        attrs["cascade.preflight.tier"] = preflight_tier
        attrs["cascade.preflight.reason"] = preflight_reason
        attrs["cascade.preflight.layer"] = preflight_layer
        attrs["cascade.preflight.confidence"] = preflight_confidence
        attrs["cascade.preflight.latency_ms"] = preflight_latency_ms

    span = {
        "name": "bot_session_turn",
        "producer": "cascade_telemetry",
        "bot_id": bot_id,
        "trace_id": session_id,
        "end_time": f"{day}T12:00:00+00:00",
        "start_time": f"{day}T12:00:00+00:00",
        "attributes": attrs,
        "usage": {"output_tokens": output_tokens},
        "total_cost": total_cost,
    }
    with (spans_dir / f"spans-{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(span) + "\n")


def _now() -> datetime:
    return datetime(2026, 6, 7, 23, 59, 0, tzinfo=timezone.utc)


# ── _compute_preflight_health ───────────────────────────────────────────────


def test_empty_shared_dir_returns_empty_by_bot(tmp_path: Path):
    """Brand-new pod / no spans yet — response is empty but well-formed."""
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    assert snap["window_days"] == 7
    assert snap["by_bot"] == {}
    assert snap["totals"]["spans_seen"] == 0
    # Thresholds always present so UI can color-code from response alone
    assert snap["thresholds"]["min_decisions"] == 30
    assert snap["thresholds"]["over_escalation_pct"] == 15.0


def test_single_span_produces_per_bot_stats(tmp_path: Path):
    """One regex-tier1 span with a meaningful turn → 1 agreement on team_bot_a."""
    _write_span(
        tmp_path,
        bot_id="team_bot_a",
        preflight_tier="tier1",
        preflight_reason="regex:design_imperative",
        total_cost=2.0,
        output_tokens=3000,
        tool_count=5,
    )
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    a = snap["by_bot"]["team_bot_a"]
    assert a["decisions"] == 1
    assert a["categories"]["agreement"] == 1
    assert a["rates"]["agreement_rate"] == 1.0
    # by_reason breakdown surfaced
    assert "regex:design_imperative" in a["by_reason"]


def test_over_escalation_surfaced_with_top_reasons(tmp_path: Path):
    """Multiple trivial tier1 turns should show in by_reason breakdown."""
    for i in range(5):
        _write_span(
            tmp_path,
            session_id=f"oe-{i}",
            preflight_tier="tier1",
            preflight_reason="regex:design_imperative",
            total_cost=0.01,
            output_tokens=50,
            tool_count=0,
        )
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    a = snap["by_bot"]["team_bot_a"]
    assert a["categories"]["over_escalation"] == 5
    # Top firing reason tracking — Phase 4 RSI consumption point
    assert a["by_reason"]["regex:design_imperative"]["over_escalation"] == 5


def test_haiku_cost_projection_in_response(tmp_path: Path):
    """Haiku-driven spans should accumulate call_count + estimated cost."""
    for i in range(10):
        _write_span(
            tmp_path,
            session_id=f"h-{i}",
            preflight_tier="tier2",
            preflight_layer="haiku",
            preflight_reason="haiku:tier2",
            preflight_latency_ms=150 + i,
            tier_used="tier2",
        )
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    a = snap["by_bot"]["team_bot_a"]
    assert a["haiku_call_count"] == 10
    # 10 calls × $0.00015/call = $0.0015
    assert a["haiku_estimated_cost_usd"] == 0.0015
    assert a["haiku_latency_ms_p50"] is not None
    # Totals roll up
    assert snap["totals"]["haiku_calls"] == 10


def test_per_bot_isolation(tmp_path: Path):
    """Spans from different bots stay in separate buckets."""
    _write_span(tmp_path, bot_id="bot_a", session_id="a-1",
                preflight_tier="tier1", total_cost=2.0, output_tokens=3000, tool_count=5)
    _write_span(tmp_path, bot_id="bot_b", session_id="b-1",
                preflight_tier="tier3", tier_used="tier3", total_cost=0.005)
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    assert "bot_a" in snap["by_bot"]
    assert "bot_b" in snap["by_bot"]
    assert snap["by_bot"]["bot_a"]["decisions"] == 1
    assert snap["by_bot"]["bot_b"]["decisions"] == 1


def test_window_param_clamps_to_30_days(tmp_path: Path):
    """Days query param is clamped to [1, 30] — too-wide windows hit
    span-retention boundaries and would silently produce sparse data."""
    # 100 days requested → clamped down to 30
    snap = _compute_preflight_health(tmp_path, days=100, now=_now())
    # The route handler does the clamp; _compute_preflight_health itself
    # honors whatever it's given, so test the clamping via the route below.
    assert snap["window_days"] == 100  # _compute respects given days
    # The clamp happens in the Flask endpoint wrapper — tested below.


def test_thresholds_block_matches_audit_runner_constants(tmp_path: Path):
    """UI color-codes from the response thresholds — if they drift from
    the Signal-emission thresholds in audit_runner, operators see
    inconsistent thresholds. Pin them."""
    from cascade.preflight_audit import (
        PREFLIGHT_MIN_DECISIONS,
        PREFLIGHT_OVER_ESCALATION_THRESHOLD,
        PREFLIGHT_UNDER_ESCALATION_THRESHOLD,
        PREFLIGHT_CASCADE_CORRECTED_THRESHOLD,
    )
    snap = _compute_preflight_health(tmp_path, days=7, now=_now())
    t = snap["thresholds"]
    assert t["min_decisions"] == PREFLIGHT_MIN_DECISIONS
    assert t["over_escalation_pct"] == PREFLIGHT_OVER_ESCALATION_THRESHOLD * 100
    assert t["under_escalation_pct"] == PREFLIGHT_UNDER_ESCALATION_THRESHOLD * 100
    assert t["cascade_corrected_pct"] == PREFLIGHT_CASCADE_CORRECTED_THRESHOLD * 100


# ── End-to-end Flask smoke ──────────────────────────────────────────────────


def test_route_returns_200_and_json(tmp_path: Path):
    """Smoke test: register route on a Flask app, hit it, get JSON back."""
    # network.json with sharedDir pointing at the tmp dir
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))

    _write_span(tmp_path, preflight_tier="tier1", total_cost=2.0,
                output_tokens=3000, tool_count=5)

    app = Flask(__name__)
    register_preflight_routes(app, network_path)
    client = app.test_client()
    resp = client.get("/api/preflight/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "by_bot" in data
    assert "thresholds" in data


def test_route_clamps_days_param(tmp_path: Path):
    """A days=100 query param should be clamped down by the endpoint
    to 30 — too-wide windows hit span retention + serve sparse data."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    app = Flask(__name__)
    register_preflight_routes(app, network_path)
    client = app.test_client()
    resp = client.get("/api/preflight/health?days=100")
    assert resp.status_code == 200
    assert resp.get_json()["window_days"] == 30


def test_route_clamps_days_param_lower_bound(tmp_path: Path):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    app = Flask(__name__)
    register_preflight_routes(app, network_path)
    client = app.test_client()
    resp = client.get("/api/preflight/health?days=0")
    assert resp.status_code == 200
    assert resp.get_json()["window_days"] == 1


def test_route_handles_garbage_days_param(tmp_path: Path):
    """Non-int days → fall back to default 7. Routes should never
    500 on operator-supplied query params."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))
    app = Flask(__name__)
    register_preflight_routes(app, network_path)
    client = app.test_client()
    resp = client.get("/api/preflight/health?days=banana")
    assert resp.status_code == 200
    assert resp.get_json()["window_days"] == 7

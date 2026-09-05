"""End-to-end test for the v2 PERFORMANCE block on /api/analytics/model-economics.

Writes real cascade turn-spans to the per-bot spans/ dir and stubs load_turns, so
the route reads spans for the window via iter_turn_spans and merges the per-model
`perf` block onto the cost rows. Asserts the join (perf lands on the matching cost
identity), the metric math, coverage honesty (spans-subset vs all-turns), and
fail-safe additivity (a cost model with no spans gets perf=None).

Placeholder bot/model/provider names only — no real pod names — so the scrub
guard stays green.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.server import _register_analytics_routes  # noqa: E402
from observability.opik_client import OpikSpan  # noqa: E402


def _turn(model, *, cost, inp, out, instance, ts):
    return {
        "ts": ts,
        "model": model,
        "provider": model.split("/")[0],
        "source": "human",
        "cost": cost,
        "input_tokens": inp,
        "output_tokens": out,
        "session_id": f"s-{instance}-{ts}",
        "instance": instance,
        "auth_mode": "api_key",
    }


def _write_span(spans_dir: Path, *, model, provider, end, duration_ms,
                struggle, success, error, idx):
    st = end - timedelta(milliseconds=duration_ms)
    attrs = {
        "session_id": f"sess-{idx}",
        "turn_index": idx,
        "cascade.struggle.score": struggle,
    }
    if success is not None:
        attrs["cascade.success"] = success
    span = OpikSpan(
        name="bot_session_turn",
        start_time=st,
        end_time=end,
        type="general",
        model=model,
        provider=provider,
        producer="cascade_telemetry",
        error_info={"message": "boom"} if error else None,
        attributes=attrs,
    )
    day = end.astimezone(timezone.utc).date().isoformat()
    f = spans_dir / f"spans-{day}.jsonl"
    with f.open("a") as fh:
        fh.write(json.dumps(span.to_dict()) + "\n")


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "evolve",
        "members": ["bot_one", "bot_two"],
    }))

    # Cost side: used-lo runs 4 turns (2 bots); beta-2 runs 2 turns (no spans).
    pooled = [
        _turn("provx/used-lo", cost=4.0, inp=400_000, out=100_000,
              instance="bot_one", ts="2026-06-12T10:00:00Z"),
        _turn("provx/used-lo", cost=4.0, inp=400_000, out=100_000,
              instance="bot_one", ts="2026-06-12T10:05:00Z"),
        _turn("provx/used-lo", cost=4.0, inp=400_000, out=100_000,
              instance="bot_two", ts="2026-06-12T11:00:00Z"),
        _turn("provx/used-lo", cost=4.0, inp=400_000, out=100_000,
              instance="bot_two", ts="2026-06-12T11:05:00Z"),
        _turn("provy/beta-2", cost=2.0, inp=300_000, out=80_000,
              instance="bot_one", ts="2026-06-11T09:00:00Z"),
        _turn("provy/beta-2", cost=2.0, inp=300_000, out=80_000,
              instance="bot_one", ts="2026-06-11T09:05:00Z"),
    ]

    import usage_analytics as ua
    monkeypatch.setattr(
        ua, "load_turns",
        lambda bot_id, **kw: (
            list(pooled) if bot_id is None
            else [t for t in pooled if t.get("instance") == bot_id]
        ),
    )

    # Perf side: 2 spans for used-lo (qualified + bare twin), 0 for beta-2. End
    # time is recent so it lands inside the default 30d window.
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    spans_dir = shared / "bot_one" / "spans"
    spans_dir.mkdir(parents=True)
    _write_span(spans_dir, model="provx/used-lo", provider="provx", end=end,
                duration_ms=1000, struggle=0.2, success=True, error=False, idx=0)
    # The bare twin (model without provider prefix, clean provider field) must
    # collapse onto the SAME identity as the qualified span.
    _write_span(spans_dir, model="used-lo", provider="provx", end=end,
                duration_ms=3000, struggle=0.6, success=False, error=False, idx=1)

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def test_perf_block_joins_cost_rows(app):
    with app.test_client() as c:
        body = c.get("/api/analytics/model-economics?days=30").get_json()

    by_id = {r["model_id"]: r for r in body["rows"]}

    # used-lo: 2 spans (qualified + bare) collapsed onto one identity, joined to
    # its cost row (4 turns).
    perf = by_id["used-lo"]["perf"]
    assert perf is not None
    assert perf["span_count"] == 2
    # latency p50 = midpoint(1000, 3000) = 2000; p95 = 1000 + 0.95*2000 = 2900.
    assert perf["latency_p50_ms"] == 2000.0
    assert perf["latency_p95_ms"] == 2900.0
    # struggle_avg = mean(0.2, 0.6) = 0.4.
    assert perf["struggle_avg"] == 0.4
    # success present on both; 1 true → 0.5 over the present subset of 2.
    assert perf["success_rate"] == 0.5
    assert perf["success_n"] == 2
    assert perf["error_rate"] == 0.0
    # coverage = 2 spans / 4 cost-turns = 0.5; thin sample → low_coverage.
    assert perf["coverage_pct"] == 0.5
    assert perf["low_coverage"] is True

    # beta-2 has cost-turns but NO spans → perf is None (route fail-safe), the
    # cost row is intact and never hidden.
    assert by_id["beta-2"]["perf"] is None
    assert by_id["beta-2"]["turns"] == 2

    # Pod-level coverage = 2 spans / 6 total turns ≈ 0.3333.
    assert body["perf_coverage_pct"] == pytest.approx(round(2 / 6, 4))
    assert body["perf_total_spans"] == 2
    # Sibling identity map present (so perf-only models stay reachable in Bite B).
    assert "provx/used-lo" in body["performance"]


def test_cost_payload_intact_alongside_perf(app):
    # Additive: v1.5 cost fields all still present + correct.
    with app.test_client() as c:
        body = c.get("/api/analytics/model-economics?days=30").get_json()
    assert set(body.keys()) >= {
        "rows", "unused", "total_cost", "total_turns", "has_pricing", "days",
        "perf_coverage_pct", "performance",
    }
    by_id = {r["model_id"]: r for r in body["rows"]}
    assert by_id["used-lo"]["cost_per_turn"] == 4.0   # 16.0 / 4 turns
    assert by_id["used-lo"]["bot_count"] == 2

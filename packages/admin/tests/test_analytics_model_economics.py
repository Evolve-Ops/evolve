"""End-to-end test for /api/analytics/model-economics (Phase 13 v1).

The pod-wide, model-centric cost leaderboard route. We stub load_turns (so the
test doesn't depend on /Users/Shared turn files) and let the real assembly +
compute_summary run, asserting the payload SHAPE and the headline invariants
(pod-wide pooling, $/turn default sort, configured-but-unused inclusion,
low-confidence passthrough).

Placeholder bot/model/provider names only — no real pod bot names — so the
scrub guard stays green.
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


def _turn(model, *, cost, inp, out, instance, ts, source="human"):
    return {
        "ts": ts,
        "model": model,
        "provider": model.split("/")[0],
        "source": source,
        "cost": cost,
        "input_tokens": inp,
        "output_tokens": out,
        "session_id": f"s-{instance}-{ts}",
        "instance": instance,
        "auth_mode": "api_key",
    }


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "evolve",
        "members": ["bot_one", "bot_two"],
        # Pod catalog: one used model + one configured-but-unused model.
        "models": {
            "rungs": [
                {"id": "lo", "costClass": "low",
                 "models": ["provx/used-lo", "provx/unused-lo"]},
            ],
            "roles": {"fast": "lo"},
        },
    }))

    # Pod-wide pool: two bots, two models. Stub load_turns so the route runs
    # without touching /Users/Shared. assemble_model_economics + compute_summary
    # run for real on the stubbed turns.
    pooled = [
        # pricey model: $0.30/turn, two bots.
        _turn("provx/used-lo", cost=15.0, inp=400_000, out=100_000,
              instance="bot_one", ts="2026-06-12T10:00:00Z"),
        _turn("provx/used-lo", cost=15.0, inp=400_000, out=100_000,
              instance="bot_two", ts="2026-06-12T11:00:00Z"),
        # cheaper model: lower $/turn, one bot, many turns. low sample.
        _turn("provy/beta-2", cost=0.02, inp=300, out=80,
              instance="bot_one", ts="2026-06-11T09:00:00Z"),
    ]

    import usage_analytics as ua

    def _fake_load_turns(bot_id, **kw):
        # Default (bot_id=None) pools pod-wide — total_turns==3 across two bots
        # below proves the pooling. ?bot= re-scopes to one bot's turns.
        if bot_id is None:
            return list(pooled)
        return [t for t in pooled if t.get("instance") == bot_id]

    monkeypatch.setattr(ua, "load_turns", _fake_load_turns)

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def test_payload_shape_and_pod_wide(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/model-economics?days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) >= {
        "rows", "unused", "total_cost", "total_turns", "has_pricing", "days",
    }
    assert body["days"] == 7
    assert body["total_turns"] == 3

    rows = body["rows"]
    # Both used models present; default sort = $/turn desc.
    ids = [r["model_id"] for r in rows]
    assert ids[0] == "used-lo"  # $0.30/turn > $0.02/turn
    by_id = {r["model_id"]: r for r in rows}

    # $/turn honest math: 30.0 over 2 turns = 15.0.
    assert by_id["used-lo"]["cost_per_turn"] == 15.0
    # bot-count: used-lo ran on both bots.
    assert by_id["used-lo"]["bot_count"] == 2
    assert by_id["beta-2"]["bot_count"] == 1
    # recency passthrough.
    assert by_id["used-lo"]["last_used_ts"] == "2026-06-12T11:00:00Z"
    # low-confidence row (tiny sample) is PRESENT, not hidden, and flagged.
    assert by_id["beta-2"]["low_confidence"] is True
    # configured model flagged.
    assert by_id["used-lo"]["configured"] is True


def test_configured_but_unused_included(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/model-economics")
    body = resp.get_json()
    unused_ids = {r["model_id"] for r in body["unused"]}
    assert "unused-lo" in unused_ids
    row = next(r for r in body["unused"] if r["model_id"] == "unused-lo")
    assert row["turns"] == 0
    assert row["total_cost"] == 0.0
    assert "fast" in row["roles"]


# ── v1.5 — additive payload (rollups + matrix) ───────────────────────────────


def test_rollups_and_matrix_present(app):
    with app.test_client() as c:
        body = c.get("/api/analytics/model-economics").get_json()
    assert "rollups" in body and "bot_model_matrix" in body
    assert {"by_band", "by_role"} <= set(body["rollups"].keys())
    # The pod points the fast role at the "lo" rung — it shows in the role view.
    assert "fast" in {r["role"] for r in body["rollups"]["by_role"]}
    # The matrix carries per-(bot × model) legs — used-lo ran on both bots.
    cells = {(c["bot_id"], c["model_id"]) for c in body["bot_model_matrix"]}
    assert ("bot_one", "used-lo") in cells
    assert ("bot_two", "used-lo") in cells


# ── v1.5 — facet filter params ───────────────────────────────────────────────


def test_endpoint_provider_band_audience_bot_params(app):
    with app.test_client() as c:
        # provider filter keeps only provx rows + echoes the facet.
        b1 = c.get("/api/analytics/model-economics?provider=provx").get_json()
        assert b1["rows"] and all(r["provider"] == "provx" for r in b1["rows"])
        assert b1["filters"]["provider"] == "provx"

        # bot filter re-scopes the load to one bot — bot_count collapses to 1.
        b2 = c.get("/api/analytics/model-economics?bot=bot_one").get_json()
        assert b2["filters"]["bot"] == "bot_one"
        assert all(r["bot_count"] == 1 for r in b2["rows"])
        # bot_two's used-lo turn is excluded, so used-lo shows a single turn.
        used = next(r for r in b2["rows"] if r["model_id"] == "used-lo")
        assert used["turns"] == 1

        # audience filter nulls Eff. cost/1k (no per-audience token volume).
        b3 = c.get("/api/analytics/model-economics?audience=human").get_json()
        assert b3["filters"]["audience"] == "human"
        assert b3["rows"] and all(
            r["usd_per_1k_blended"] is None for r in b3["rows"]
        )

        # band filter echoes + keeps only that band.
        b4 = c.get("/api/analytics/model-economics?band=low").get_json()
        assert b4["filters"]["band"] == "low"


# ── v1.5 — unexpected-billing folds into one identity (end-to-end) ───────────


@pytest.fixture
def app_ub(tmp_path: Path, monkeypatch):
    """A pod where one model drifts to API-key billing on one bot — so
    compute_summary splits it into a `:unexpected_billing` sub-series that the
    v1.5 merge must fold back into a single identity row."""
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "evolve",
        "members": ["bot_one", "bot_two"],
    }))

    pooled = [
        _turn("provx/alpha-8", cost=10.0, inp=400_000, out=100_000,
              instance="bot_one", ts="2026-06-12T10:00:00Z"),
        # Same model, same provider — but drifted to API-key billing on bot_two.
        {**_turn("provx/alpha-8", cost=5.0, inp=80_000, out=20_000,
                 instance="bot_two", ts="2026-06-12T11:00:00Z"),
         "unexpected_billing_mode": True},
    ]

    import usage_analytics as ua
    monkeypatch.setattr(
        ua, "load_turns",
        lambda bot_id, **kw: (
            list(pooled) if bot_id is None
            else [t for t in pooled if t.get("instance") == bot_id]
        ),
    )

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def test_unexpected_billing_folds_into_one_identity(app_ub):
    with app_ub.test_client() as c:
        body = c.get("/api/analytics/model-economics?days=30").get_json()
    rows = [r for r in body["rows"] if r["model_id"] == "alpha-8"]
    assert len(rows) == 1                    # collapsed despite the billing split
    r = rows[0]
    assert r["unexpected_billing"] is True   # the UB sub-series flag carries
    assert r["turns"] == 2                    # 1 normal + 1 unexpected
    assert r["total_cost"] == 15.0           # 10 + 5
    assert r["bot_count"] == 2               # distinct bots, from the matrix

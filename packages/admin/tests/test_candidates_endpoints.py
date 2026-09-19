"""tests/test_candidates_endpoints.py — /api/candidates/* read endpoints.

Phase 2 of internal/spec-proposal-synthesizer-2026-05-10.md adds the
Tracked-candidates surface to the Alerts page. These endpoints back
it:

  GET /api/candidates/watchlist     — concreteness-demoted entries
  GET /api/candidates/synthesizing  — substrate aggregates awaiting LLM
  GET /api/candidates/dropped       — last N days of gate drops

Read-only in Phase 2; mutation endpoints land when the UI grows
actions on these surfaces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network = {"members": [], "bots": {}, "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app


def _make_candidate(
    bot_id: str = "admin_bot",
    variant: str = "cron_wakes_agent",
    state: str = "watchlist",
):
    from proposal_synthesizer import store as cstore
    from schema.candidate_proposal import (
        CandidateProposal,
        Magnitude,
        new_candidate_id,
    )
    from schema.proposal import Investigation, RiskTag
    from schema.provenance import Provenance

    return CandidateProposal(
        id=new_candidate_id(),
        bot_id=bot_id,
        state=state,
        generator_id="efficiency_hawk",
        dimension="efficiency",
        variant=variant,
        trigger_observations=[f"{variant}:{bot_id}"],
        provenance=Provenance(
            technique=f"efficiency_hawk.{variant}",
            signals={"sample": "value"},
            confidence=0.85,
        ),
        motivating_signals=[f"sig-{bot_id}-1"],
        magnitude=Magnitude(unit="usd/week", value=5.0),
        draft_problem=f"{bot_id}: vague observation",
        draft_headline=f"Inspect {bot_id} something",
        draft_action=Investigation(context="Look around."),
        draft_risk_tag=RiskTag(
            blast_radius="bot", reversibility="manual", touches=[]
        ),
        draft_urgency="hygiene",
        draft_approval_audience="pod_operator",
        confidence=0.85,
    )


def test_watchlist_endpoint_empty(app):
    client = app.test_client()
    resp = client.get("/api/candidates/watchlist")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload == {"candidates": [], "total": 0}


def test_watchlist_endpoint_returns_candidates(app, tmp_path):
    from proposal_synthesizer import store as cstore

    shared_dir = tmp_path / "evolve"
    cands = [
        _make_candidate(bot_id="admin_bot", state="watchlist"),
        _make_candidate(bot_id="team_bot_c", state="watchlist"),
    ]
    for c in cands:
        cstore.write_candidate(c, shared_dir)

    client = app.test_client()
    resp = client.get("/api/candidates/watchlist")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 2
    ids = {c["bot_id"] for c in payload["candidates"]}
    assert ids == {"admin_bot", "team_bot_c"}


def test_synthesizing_endpoint_returns_substrate_aggregates(app, tmp_path):
    from proposal_synthesizer import store as cstore

    shared_dir = tmp_path / "evolve"
    c = _make_candidate(state="synthesizing")
    c.aggregation = "substrate"
    c.bot_id = "<pod>"
    cstore.write_candidate(c, shared_dir)

    client = app.test_client()
    resp = client.get("/api/candidates/synthesizing")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 1
    assert payload["candidates"][0]["aggregation"] == "substrate"
    assert payload["candidates"][0]["bot_id"] == "<pod>"


def test_dropped_endpoint_reads_jsonl_log(app, tmp_path):
    from proposal_synthesizer import store as cstore

    shared_dir = tmp_path / "evolve"
    c = _make_candidate()
    cstore.record_drop(
        shared_dir, c, reason="below_magnitude_floor", note="0.01 < 1.0"
    )

    client = app.test_client()
    resp = client.get("/api/candidates/dropped?days=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["window_days"] == 1
    assert payload["total"] == 1
    assert payload["drops"][0]["reason"] == "below_magnitude_floor"


def test_view_slims_provenance_signals(app, tmp_path):
    """The list-view drops the full provenance.signals payload but
    keeps technique + confidence so the UI can render the source."""
    from proposal_synthesizer import store as cstore

    shared_dir = tmp_path / "evolve"
    c = _make_candidate(state="watchlist")
    cstore.write_candidate(c, shared_dir)

    client = app.test_client()
    resp = client.get("/api/candidates/watchlist")
    payload = resp.get_json()
    prov = payload["candidates"][0]["provenance"]
    assert prov["technique"] == "efficiency_hawk.cron_wakes_agent"
    assert prov["confidence"] == 0.85
    # The full signals dict is stripped from the list view.
    assert "signals" not in prov

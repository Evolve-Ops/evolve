"""tests/test_arbiter_adopt_group — Bite-2 per-model + batch adoption.

Spec: docs/design-recommendation-legibility-2026-06-12.md (Bite 2).

Drives the new coalesced-group adopt endpoints through Flask's test client:

  POST /api/arbiter/proposals/<id>/adopt-model        (one model)
  POST /api/arbiter/proposals/<id>/adopt-all-dormant  (the whole group)

A coalesced ``model_discovery`` parent carries its own head AdoptModel plus N
folded sub-findings. These endpoints make every model independently adoptable
and reconcile the parent container (drop a sub / promote a sibling into the
head / archive when drained) keeping the parent id so card-level
snooze/dismiss survives partial adoption.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from arbiter.appliers import adopt_model  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import find_proposal, iter_proposals, write_proposal  # noqa: E402
from schema.proposal import (  # noqa: E402
    AdoptModel,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def arbiter_app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {"members": ["team_bot_a"], "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


@pytest.fixture
def network_models():
    """Capture AdoptModel applier writes in-memory so the real network.json is
    never touched. Yields the live models block."""
    state = {"models": {"rungs": [], "roles": {}}}

    def read_fn():
        return {"models": state["models"], "bots": {}}

    def write_fn(net):
        state["models"] = net.get("models") or {}

    adopt_model.set_network_io(read_fn, write_fn)
    try:
        yield state
    finally:
        adopt_model.set_network_io(None, None)


def _adopt_prop(provider: str, model_id: str) -> Proposal:
    qualified = f"{provider}/{model_id}"
    return Proposal(
        id=new_proposal_id(),
        bot_id="<pod>",
        generator_id="model_discovery",
        dimension="substrate_health",
        trigger_observations=[f"model_discovery:{provider}:{model_id}"],
        provenance=Provenance(
            technique="model_discovery.listing_diff",
            signals={
                "provider": provider,
                "qualified_id": qualified,
                "suggested_rung_slug": f"{model_id}-rung",
                "suggested_cost_class": "medium",
                "suggested_position": 0,
                "evidence": {"context_window": 1000},
            },
            confidence=0.9,
        ),
        problem=f"{qualified} is in no rung.",
        action=AdoptModel(
            provider=provider, model_id=model_id, rung_slug=f"{model_id}-rung"
        ),
        risk_tag=RiskTag(
            blast_radius="pod", reversibility="manual", touches=["models.rungs"]
        ),
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=f"New model from {provider}: {qualified}",
        summary=f"New model from {provider}: {qualified}",
        coalesce_key=f"model_discovery:{provider}",
        human_title=f"New models available from {provider}",
    )


def _seed_group(shared_dir: Path, provider: str, model_ids: list[str]) -> Proposal:
    for mid in model_ids:
        p = _adopt_prop(provider, mid)
        transition(p, "pending", actor="test", reason="seed")
        write_proposal(p, shared_dir)
    pending = [
        p
        for p in iter_proposals(shared_dir, subdirs=("pending",))
        if p.coalesce_key == f"model_discovery:{provider}"
    ]
    assert len(pending) == 1
    return pending[0]


def _rung_ids(models: dict) -> set:
    return {r.get("id") for r in (models.get("rungs") or [])}


# ─────────────────────────────────────────────────────────────────────────────
# adopt-model (one model)
# ─────────────────────────────────────────────────────────────────────────────


def test_adopt_one_sub_keeps_group_and_writes_rung(arbiter_app, network_models):
    """Adopting a folded sub-finding adopts only that model (default dormant)
    and leaves the rest of the group pending."""
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5", "grok-5-fast"])
    sub_key = "model_discovery:xai:grok-5-fast"

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={"model_key": sub_key},
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["group_disposition"] == "kept"

    # The adopted model's rung landed in the catalog.
    assert "grok-5-fast-rung" in _rung_ids(network_models["models"])
    # Parent still pending, head unchanged, sub gone.
    located = find_proposal(shared, parent.id)
    assert located is not None and located[2] == "pending"
    parent2 = located[0]
    assert parent2.trigger_observations == ["model_discovery:xai:grok-5"]
    assert parent2.sub_findings == []
    # The child adoption is an archived succeeded record.
    child = find_proposal(shared, data["child_id"])
    assert child is not None and child[2] == "archived"
    assert child[0].status == "succeeded"


def test_adopt_head_promotes_sibling_into_head(arbiter_app, network_models):
    """Adopting the parent's own head model promotes a surviving sibling into
    the head — keeping the parent id (and its operator state)."""
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5", "grok-5-fast"])
    head_key = "model_discovery:xai:grok-5"

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={"model_key": head_key},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["group_disposition"] == "kept"

    assert "grok-5-rung" in _rung_ids(network_models["models"])
    located = find_proposal(shared, parent.id)
    assert located is not None and located[2] == "pending"
    parent2 = located[0]
    # The sibling was promoted into the head; no subs remain.
    assert parent2.trigger_observations == ["model_discovery:xai:grok-5-fast"]
    assert parent2.action.model_id == "grok-5-fast"
    assert parent2.sub_findings == []
    # human_title / coalesce_key / id preserved across the promotion.
    assert parent2.id == parent.id
    assert parent2.coalesce_key == "model_discovery:xai"
    assert parent2.human_title == "New models available from xai"


def test_adopt_last_model_archives_parent(arbiter_app, network_models):
    """Adopting the only model in a single-model group archives the card."""
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5"])

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={"model_key": "model_discovery:xai:grok-5"},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["group_disposition"] == "drained"

    located = find_proposal(shared, parent.id)
    assert located is not None and located[2] == "archived"
    assert located[0].status == "superseded"


def test_adopt_one_maps_role_and_cap(arbiter_app, network_models):
    """A role + cap in the body is applied to the adopted model's rung."""
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5", "grok-5-fast"])

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={
                "model_key": "model_discovery:xai:grok-5",
                "role": "power",
                "cap": 9,
            },
        )
        assert resp.status_code == 200, resp.get_json()

    models = network_models["models"]
    assert models.get("roles", {}).get("power") == "grok-5-rung"
    assert models.get("roleCaps", {}).get("power", {}).get("maxPerDayPerBot") == 9


def test_adopt_one_default_is_dormant_no_role(arbiter_app, network_models):
    """Default (no role) adopts as a dormant catalog entry — rung created, no
    role mapped. This is the low-risk default the bite must preserve."""
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5", "grok-5-fast"])

    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={"model_key": "model_discovery:xai:grok-5"},
        )
        assert resp.status_code == 200, resp.get_json()

    models = network_models["models"]
    assert "grok-5-rung" in _rung_ids(models)
    assert models.get("roles", {}) == {}  # nothing mapped


def test_adopt_one_404_on_unknown_model(arbiter_app, network_models):
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5"])
    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model",
            json={"model_key": "model_discovery:xai:not-a-model"},
        )
        assert resp.status_code == 404


def test_adopt_one_400_without_model_key(arbiter_app, network_models):
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5"])
    with app.test_client() as c:
        resp = c.post(
            f"/api/arbiter/proposals/{parent.id}/adopt-model", json={}
        )
        assert resp.status_code == 400


def test_adopt_one_404_on_missing_proposal(arbiter_app, network_models):
    app, _ = arbiter_app
    with app.test_client() as c:
        resp = c.post(
            "/api/arbiter/proposals/no-such-id/adopt-model",
            json={"model_key": "x"},
        )
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# adopt-all-dormant (the whole group)
# ─────────────────────────────────────────────────────────────────────────────


def test_adopt_all_dormant_adopts_every_model_and_archives(arbiter_app, network_models):
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5", "grok-5-fast", "grok-5-mini"])

    with app.test_client() as c:
        resp = c.post(f"/api/arbiter/proposals/{parent.id}/adopt-all-dormant")
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()
        assert data["ok"] is True
        assert data["adopted"] == 3
        assert data["failed"] == 0
        assert data["group_disposition"] == "drained"

    # Every model's rung landed, all dormant (no roles mapped).
    models = network_models["models"]
    assert {"grok-5-rung", "grok-5-fast-rung", "grok-5-mini-rung"} <= _rung_ids(models)
    assert models.get("roles", {}) == {}
    # Parent archived.
    located = find_proposal(shared, parent.id)
    assert located is not None and located[2] == "archived"
    assert located[0].status == "superseded"


def test_adopt_all_dormant_409_on_non_pending(arbiter_app, network_models):
    app, shared = arbiter_app
    parent = _seed_group(shared, "xai", ["grok-5"])
    # Move it out of pending first.
    with app.test_client() as c:
        c.post(f"/api/arbiter/proposals/{parent.id}/dismiss")
        resp = c.post(f"/api/arbiter/proposals/{parent.id}/adopt-all-dormant")
        assert resp.status_code == 409

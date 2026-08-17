"""tests/test_coalesce_group_survival — Bite-2 store/sweep behavior.

Spec: docs/design-recommendation-legibility-2026-06-12.md (Bite 2).

Two store-layer fixes that reconcile coalescing with N-independently-actionable
folded items (model_discovery's AdoptModel proposals):

  1. A folded sub-finding record now carries the proposal's serialized
     ``action`` so it stays adoptable from the drill-down (not display-only).

  2. ``sweep_resolve_proposals`` is group-aware: a coalesced parent survives
     while ANY model in its group is still emitted (keyed on ``coalesce_key``),
     so a card whose own head model went silent isn't flickered out — which
     would otherwise drop the operator's snooze/dismiss state for one cycle.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import (  # noqa: E402
    find_proposal,
    iter_proposals,
    sweep_resolve_proposals,
    write_proposal,
)
from schema.proposal import (  # noqa: E402
    AdoptModel,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


def _adopt_prop(provider: str, model_id: str) -> Proposal:
    """A pod-wide AdoptModel proposal shaped like model_discovery's output,
    coalescing per provider."""
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
                "suggested_rung_slug": "new-rung",
                "suggested_cost_class": "medium",
                "suggested_position": 0,
                "evidence": {"context_window": 1000},
            },
            confidence=0.9,
        ),
        problem=f"{qualified} is in no rung.",
        action=AdoptModel(provider=provider, model_id=model_id, rung_slug="new-rung"),
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
    """Write one AdoptModel proposal per model; they fold into one parent.
    Returns the parent."""
    for mid in model_ids:
        p = _adopt_prop(provider, mid)
        transition(p, "pending", actor="test", reason="seed")
        write_proposal(p, shared_dir)
    pending = [
        p for p in iter_proposals(shared_dir, subdirs=("pending",))
        if p.coalesce_key == f"model_discovery:{provider}"
    ]
    assert len(pending) == 1, "models from one provider fold into one parent"
    return pending[0]


# ── 1. Sub-finding record carries the action ─────────────────────────────────


def test_folded_sub_finding_carries_action(tmp_path):
    """A folded sub-finding records the proposal's serialized action so it
    stays independently adoptable from the drill-down."""
    parent = _seed_group(tmp_path, "xai", ["grok-5", "grok-5-fast"])
    assert len(parent.sub_findings) == 1
    sf = parent.sub_findings[0]
    assert sf["action"]["kind"] == "AdoptModel"
    assert sf["action"]["model_id"] == "grok-5-fast"
    assert sf["action"]["provider"] == "xai"
    # And the existing display fields are unchanged.
    assert sf["trigger_observation"] == "model_discovery:xai:grok-5-fast"
    assert sf["provenance_signals"]["qualified_id"] == "xai/grok-5-fast"


# ── 2. Group-aware sweep survival ────────────────────────────────────────────


def test_sweep_preserves_coalesced_parent_while_group_fires(tmp_path):
    """The parent's OWN head model goes silent this cycle, but a sibling in the
    same group still fires. Group-aware sweep keeps the parent (and its
    snooze/dismiss state) alive — no flicker."""
    parent = _seed_group(tmp_path, "xai", ["grok-5", "grok-5-fast"])
    # head = grok-5; only the sibling (grok-5-fast) re-fires this cycle.
    sibling_fp = compute_fingerprint(_adopt_prop("xai", "grok-5-fast"))
    assert compute_fingerprint(parent) != sibling_fp  # head fp is NOT emitted

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="model_discovery",
        emissions_by_bot={"": {sibling_fp}},
        visited_bots={""},
        valid_bot_ids=None,
        per_bot=False,
        emitted_coalesce_keys_by_bot={"": {"model_discovery:xai"}},
    )
    assert archived == 0
    located = find_proposal(tmp_path, parent.id)
    assert located is not None and located[2] == "pending"


def test_sweep_flickers_parent_without_group_awareness(tmp_path):
    """Regression pin: WITHOUT the group-aware arg, the same silent-head /
    live-sibling cycle archives the parent — the one-cycle flicker the fix
    removes."""
    parent = _seed_group(tmp_path, "xai", ["grok-5", "grok-5-fast"])
    sibling_fp = compute_fingerprint(_adopt_prop("xai", "grok-5-fast"))

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="model_discovery",
        emissions_by_bot={"": {sibling_fp}},
        visited_bots={""},
        valid_bot_ids=None,
        per_bot=False,
        # emitted_coalesce_keys_by_bot omitted (defaults None) → pure
        # per-fingerprint behavior.
    )
    assert archived == 1
    located = find_proposal(tmp_path, parent.id)
    assert located is not None and located[2] == "archived"
    assert located[0].status == "resolved_externally"


def test_sweep_archives_parent_when_whole_group_silent(tmp_path):
    """When NO model in the group re-fires, group-awareness does not pin the
    card — the parent archives as resolved_externally."""
    parent = _seed_group(tmp_path, "xai", ["grok-5", "grok-5-fast"])

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="model_discovery",
        emissions_by_bot={"": set()},
        visited_bots={""},
        valid_bot_ids=None,
        per_bot=False,
        emitted_coalesce_keys_by_bot={"": set()},
    )
    assert archived == 1
    located = find_proposal(tmp_path, parent.id)
    assert located is not None and located[2] == "archived"


def test_sweep_preserves_parent_when_head_model_still_fires(tmp_path):
    """Baseline: the head model itself re-fires → preserved by the plain
    fingerprint path, group-awareness or not."""
    parent = _seed_group(tmp_path, "xai", ["grok-5", "grok-5-fast"])
    head_fp = compute_fingerprint(parent)

    archived = sweep_resolve_proposals(
        tmp_path,
        generator_id="model_discovery",
        emissions_by_bot={"": {head_fp}},
        visited_bots={""},
        valid_bot_ids=None,
        per_bot=False,
        emitted_coalesce_keys_by_bot={"": {"model_discovery:xai"}},
    )
    assert archived == 0
    assert find_proposal(tmp_path, parent.id)[2] == "pending"

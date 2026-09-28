"""Tests for the autonomy_promoter generator, the UpdateAutonomyPosture
applier, and the permanent upward auto-approve carve-outs (spec §3.2)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autonomy import actions_ledger as _ledger
from autonomy import store as _store
from generators.autonomy_promoter.observe import (
    AutonomyPromoterContext,
    observe,
)
from schema.proposal import Proposal, RiskTag, UpdateAutonomyPosture
from signals import store as _signals_store


BOT = "alpha"
IID = "google_workspace"
NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {IID: {"command": "uvx", "args": ["workspace-mcp"]}}},
    }))
    return h


def _fire_candidate(shared_dir: Path, *, bot: str = BOT, iid: str = IID):
    return _signals_store.observe(
        shared_dir,
        signature=f"permission_monitor:autonomy_promotion_candidate:{bot}:{iid}",
        producer="permission_monitor",
        type="autonomy_promotion_candidate",
        flavor="maintenance",
        severity="info",
        scope="bot",
        bot_id=bot,
        title="clean track record",
        body="…",
        details={
            "bot_id": bot, "integration_id": iid, "actions": 12,
            "span_days": 9, "max_actions_per_day": 3,
            "suggested_actions_per_day": 6,
        },
    )


def _ctx(shared_dir: Path) -> AutonomyPromoterContext:
    return AutonomyPromoterContext(bot_ids=[BOT], shared_dir=shared_dir, now=NOW)


def _set_rung2(shared_dir: Path) -> None:
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=_store.ACTOR_OPERATOR_UI,
    )


# ── Generator ────────────────────────────────────────────────────────────────

def test_promoter_emits_typed_promotion_proposal(shared_dir: Path):
    _set_rung2(shared_dir)
    sig = _fire_candidate(shared_dir)
    proposals = observe(_ctx(shared_dir))
    assert len(proposals) == 1
    p = proposals[0]
    assert p.bot_id == BOT
    assert p.generator_id == "autonomy_promoter"
    assert p.action.kind == "UpdateAutonomyPosture"
    assert p.action.rung == "autonomous_within_rules"
    assert p.action.expected_current_rung == "act_with_approval"
    assert p.action.rules == {"actions_per_day": 6}
    assert p.motivating_signals == [sig.id]
    assert p.approval_audience == "pod_operator"
    assert "tools" in p.risk_tag.touches
    # Plex test: no internal vocabulary on the operator-facing strings.
    for text in (p.admin_surface_summary, p.summary):
        for banned in ("rung", "posture", "ladder", "mcp"):
            assert banned not in text.lower(), (banned, text)


def test_promoter_revalidates_against_live_posture(shared_dir: Path):
    _fire_candidate(shared_dir)
    # No posture entry at all → no proposal.
    assert observe(_ctx(shared_dir)) == []
    # Already promoted → no proposal.
    _store.set_posture(
        shared_dir, BOT, IID, rung="autonomous_within_rules",
        rules={"actions_per_day": 5}, actor=_store.ACTOR_OPERATOR_UI,
    )
    assert observe(_ctx(shared_dir)) == []
    # Auto-demoted → the restore path owns it; no re-promotion pitch.
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=f"{_store.ACTOR_PREFIX_AUTO_DEMOTION}sig-1",
    )
    assert observe(_ctx(shared_dir)) == []


def test_promoter_respects_charter_invariants(shared_dir: Path):
    """The emitted proposal passes its own charter's invariants — the
    ingest-time guarantee that the generator only ships what it
    declared (action_kind_allowed + human_approval_for)."""
    from arbiter.ingest import _check_invariants
    from registry.charter_loader import load_charter_from_yaml

    _set_rung2(shared_dir)
    _fire_candidate(shared_dir)
    proposals = observe(_ctx(shared_dir))
    charter, _fp = load_charter_from_yaml(
        Path(__file__).parent.parent / "generators" / "autonomy_promoter" / "charter.yaml",
    )
    assert charter.subscribes_to == ["autonomy_promotion_candidate"]
    _check_invariants(proposals[0], charter, known_metrics=None)  # no raise


# ── Applier ──────────────────────────────────────────────────────────────────

def _applier(shared_dir: Path, home: Path):
    from arbiter.appliers import get_applier
    applier = get_applier("UpdateAutonomyPosture")
    applier.shared_override = shared_dir
    applier.home_override = home
    return applier


def _promotion_action() -> UpdateAutonomyPosture:
    return UpdateAutonomyPosture(
        bot_id=BOT, integration_id=IID,
        rung="autonomous_within_rules",
        rules={"actions_per_day": 6},
        expected_current_rung="act_with_approval",
    )


def test_applier_writes_proposal_provenance_and_renders(
    shared_dir: Path, home: Path,
):
    _set_rung2(shared_dir)
    applier = _applier(shared_dir, home)
    action = _promotion_action()
    snapshot = applier.capture_snapshot(action, BOT)
    result = applier.apply(action, BOT, proposal_id="prop-123")
    assert result.ok, result.message
    posture = _store.load(shared_dir, BOT).integrations[IID]
    assert posture.rung == "autonomous_within_rules"
    assert posture.set_by["actor"] == "proposal:prop-123"
    assert posture.rules == {"actions_per_day": 6}

    # Revert restores the snapshot.
    revert = applier.revert(snapshot, BOT)
    assert revert.ok, revert.message
    posture = _store.load(shared_dir, BOT).integrations[IID]
    assert posture.rung == "act_with_approval"


def test_applier_cas_fails_loudly_on_moved_posture(shared_dir: Path, home: Path):
    _set_rung2(shared_dir)
    applier = _applier(shared_dir, home)
    # Posture moves between authoring and apply.
    _store.set_posture(
        shared_dir, BOT, IID, rung="draft_only", actor=_store.ACTOR_OPERATOR_UI,
    )
    result = applier.apply(_promotion_action(), BOT, proposal_id="prop-123")
    assert not result.ok
    assert result.details["error"] == "stale_posture"
    assert _store.load(shared_dir, BOT).integrations[IID].rung == "draft_only"


def test_applier_requires_cas_witness(shared_dir: Path, home: Path):
    _set_rung2(shared_dir)
    applier = _applier(shared_dir, home)
    action = UpdateAutonomyPosture(
        bot_id=BOT, integration_id=IID, rung="draft_only",
        expected_current_rung=None,
    )
    result = applier.apply(action, BOT, proposal_id="p")
    assert not result.ok
    assert result.details["error"] == "missing_expected_current_rung"


# ── Carve-outs: upward is excluded from EVERY auto-approve lane ──────────────

def _proposal_with(action, *, touches=()) -> Proposal:
    from schema.proposal import new_proposal_id
    from schema.provenance import Provenance
    return Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id="autonomy_promoter",
        dimension="safety",
        trigger_observations=[],
        provenance=Provenance(technique="test", signals={}, confidence=1.0),
        problem="p",
        action=action,
        # Deliberately the friendliest possible risk_tag — the carve-out
        # must hold even when a generator mis-tags the proposal.
        risk_tag=RiskTag(blast_radius="bot", reversibility="auto",
                         touches=list(touches)),
        approval_audience="pod_operator",
        urgency="improvement",
    )


def test_routing_never_autonomous_for_promotion():
    from arbiter.routing import is_autonomous_eligible
    promo = _proposal_with(_promotion_action())
    assert is_autonomous_eligible(promo) is False
    # Fail closed: a promotion with no CAS witness is still a promotion.
    blind = _proposal_with(UpdateAutonomyPosture(
        bot_id=BOT, integration_id=IID, rung="autonomous_within_rules",
        rules={"actions_per_day": 5}, expected_current_rung=None,
    ))
    assert is_autonomous_eligible(blind) is False


def test_eligibility_promotion_always_asks():
    from eligibility import classify_proposal
    promo = _proposal_with(_promotion_action()).to_dict()
    e = classify_proposal(promo)
    assert e.tier_floor == "ask"
    assert e.decidable is False
    assert "permanently" in e.reason
    # Missing direction fields fail closed to "ask" too.
    promo["action"].pop("expected_current_rung", None)
    assert classify_proposal(promo).tier_floor == "ask"


def test_eligibility_demotion_may_use_auto_lanes():
    from eligibility import classify_proposal
    demo = _proposal_with(UpdateAutonomyPosture(
        bot_id=BOT, integration_id=IID, rung="act_with_approval",
        expected_current_rung="autonomous_within_rules",
    )).to_dict()
    # Demotions need the same claim+revert plumbing as any decidable
    # kind; without it they ask (conservative), but they are NOT
    # blocked by the promotion carve-out.
    e = classify_proposal(demo)
    assert "permanently" not in e.reason
    demo["claim"] = {"metric": "m", "direction": "down", "magnitude": 1,
                     "window_days": 7, "baseline": 0}
    demo["revert_on_failure"] = {"x": 1}
    e2 = classify_proposal(demo)
    assert e2.tier_floor in ("auto", "auto-small")


def test_remediation_restore_is_high_risk_never_autofires():
    from eligibility import classify_signal, fix_risk_for_remediation
    assert fix_risk_for_remediation("restore_autonomy_posture") == "high"
    sig = {
        "remediation": {"kind": "restore_autonomy_posture", "params": {}},
        "severity_framework": {},
    }
    assert classify_signal(sig).tier_floor == "ask"

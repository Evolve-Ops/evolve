"""tests/test_rsi_schema.py — RSI schema round-trip and validation tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


from schema import (  # noqa: E402
    Charter,
    Claim,
    ConfigPatch,
    GeneratorRecord,
    GuardianAnnotation,
    Invariant,
    Investigation,
    Proposal,
    Provenance,
    RevertPlan,
    RiskTag,
    TrackRecord,
    WorkflowInstruction,
)
from schema.proposal import (  # noqa: E402
    IRREVERSIBILITY_SURFACES,
    ValueEstimate,
    action_from_dict,
    action_to_dict,
)


# ─────────────────────────────────────────────────────────────────────────────
# Provenance
# ─────────────────────────────────────────────────────────────────────────────


def test_provenance_rejects_empty_technique():
    with pytest.raises(ValueError):
        Provenance(technique="")


def test_provenance_rejects_confidence_out_of_range():
    with pytest.raises(ValueError):
        Provenance(technique="x", confidence=1.5)


def test_provenance_roundtrip():
    p = Provenance(technique="test", signals={"k": "v"}, confidence=0.7)
    assert Provenance.from_dict(p.to_dict()) == p


# ─────────────────────────────────────────────────────────────────────────────
# Claim
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_rejects_empty_metric():
    with pytest.raises(ValueError):
        Claim(metric="", direction="up", magnitude=1.0, window_days=1, baseline=0)


def test_claim_rejects_negative_magnitude():
    with pytest.raises(ValueError):
        Claim(metric="x", direction="up", magnitude=-1.0, window_days=1, baseline=0)


def test_claim_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        Claim(metric="x", direction="up", magnitude=1.0, window_days=0, baseline=0)


def test_claim_roundtrip():
    c = Claim(
        metric="gateway.up",
        direction="up",
        magnitude=1.0,
        window_days=7,
        baseline=0.0,
        fallback="revert",
    )
    assert Claim.from_dict(c.to_dict()) == c


# ─────────────────────────────────────────────────────────────────────────────
# RiskTag
# ─────────────────────────────────────────────────────────────────────────────


def test_risk_tag_normalizes_touches():
    rt = RiskTag(
        blast_radius="bot", reversibility="auto", touches=["Config", "CONFIG", "auth"]
    )
    assert rt.touches == ["auth", "config"]


def test_risk_tag_detects_irreversibility_surface():
    rt = RiskTag(
        blast_radius="bot", reversibility="auto", touches=["config", "auth"]
    )
    assert rt.touches_irreversibility_surface()
    assert "auth" in IRREVERSIBILITY_SURFACES


def test_risk_tag_safe_touches():
    rt = RiskTag(
        blast_radius="bot", reversibility="auto", touches=["memory", "manifest_tags"]
    )
    assert not rt.touches_irreversibility_surface()


# ─────────────────────────────────────────────────────────────────────────────
# Action variants — discriminated union
# ─────────────────────────────────────────────────────────────────────────────


def test_action_roundtrip_each_variant():
    for action in [
        ConfigPatch(target_path="foo::bar", operation="set", value=42),
        Investigation(context="check logs"),
        WorkflowInstruction(bot_id="team_bot_a", path="notes.md", content="x"),
    ]:
        d = action_to_dict(action)
        assert d["kind"] == action.kind
        restored = action_from_dict(d)
        assert restored == action


def test_action_from_dict_rejects_unknown_kind():
    with pytest.raises(ValueError):
        action_from_dict({"kind": "FakeAction"})


# ─────────────────────────────────────────────────────────────────────────────
# Proposal — end-to-end roundtrip
# ─────────────────────────────────────────────────────────────────────────────


def _sample_proposal():
    return Proposal(
        id="prop-123",
        bot_id="team_bot_a",
        generator_id="sysadmin_watchdog",
        dimension="substrate_health",
        trigger_observations=["obs-1", "obs-2"],
        provenance=Provenance(technique="t1", confidence=0.7),
        problem="Test problem",
        action=Investigation(context="see incidents"),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        urgency="operational_urgent",
        admin_surface_summary="test",
    )


def test_proposal_roundtrip():
    p = _sample_proposal()
    d = p.to_dict()
    restored = Proposal.from_dict(d)
    assert restored.id == p.id
    assert restored.action == p.action
    assert restored.provenance == p.provenance
    assert restored.risk_tag == p.risk_tag
    assert restored.urgency == p.urgency
    assert restored.schema_version == 2


def test_proposal_rejects_missing_required():
    with pytest.raises(ValueError):
        Proposal(
            id="",
            bot_id="team_bot_a",
            generator_id="g",
            dimension="d",
            trigger_observations=[],
            provenance=Provenance(technique="t"),
            problem="",
            action=Investigation(context=""),
            risk_tag=RiskTag(blast_radius="bot", reversibility="manual"),
        )


def test_proposal_with_claim_and_revert():
    p = _sample_proposal()
    p.claim = Claim(
        metric="gateway.up",
        direction="up",
        magnitude=1.0,
        window_days=1,
        baseline=0.0,
    )
    p.revert_on_failure = RevertPlan(
        before_snapshot={"key": "old"},
        revert_action=ConfigPatch(target_path="a::b", operation="set", value="old"),
        expires_at="2026-05-01T00:00:00+00:00",
    )
    d = p.to_dict()
    restored = Proposal.from_dict(d)
    assert restored.claim == p.claim
    assert restored.revert_on_failure.before_snapshot == {"key": "old"}


def test_proposal_with_guardian_annotations():
    p = _sample_proposal()
    p.guardian_annotations = [
        GuardianAnnotation(
            guardian_id="security_warden",
            severity="high",
            reason="expands tool scope",
        )
    ]
    d = p.to_dict()
    restored = Proposal.from_dict(d)
    assert len(restored.guardian_annotations) == 1
    assert restored.guardian_annotations[0].severity == "high"


# ─────────────────────────────────────────────────────────────────────────────
# Altitude + ValueEstimate (Fit Reviewer Bite 2)
# ─────────────────────────────────────────────────────────────────────────────


def test_proposal_altitude_defaults_to_l0():
    # Every existing generator stays valid without a change: default L0.
    p = _sample_proposal()
    assert p.altitude == 0
    assert p.value_estimate is None
    # Round-trips through to_dict/from_dict, always serialized.
    d = p.to_dict()
    assert d["altitude"] == 0
    assert d["value_estimate"] is None
    assert Proposal.from_dict(d).altitude == 0


def test_proposal_altitude_accepts_and_roundtrips_l2():
    p = _sample_proposal()
    p.altitude = 2
    p.value_estimate = ValueEstimate(
        tier="high",
        basis="purpose-aligned #1 domain, 69 events, no current coverage",
        evidence_refs=["obs-1", "session:abc123"],
    )
    restored = Proposal.from_dict(p.to_dict())
    assert restored.altitude == 2
    assert restored.value_estimate == p.value_estimate
    assert restored.value_estimate.tier == "high"
    assert restored.value_estimate.evidence_refs == ["obs-1", "session:abc123"]


def test_proposal_backward_compat_without_altitude_keys():
    # A pre-Bite-2 proposal on disk has no altitude / value_estimate keys.
    d = _sample_proposal().to_dict()
    del d["altitude"]
    del d["value_estimate"]
    restored = Proposal.from_dict(d)
    assert restored.altitude == 0  # missing → L0
    assert restored.value_estimate is None


def test_value_estimate_roundtrip():
    ve = ValueEstimate(tier="medium", basis="3+ sessions", evidence_refs=["o1"])
    assert ValueEstimate.from_dict(ve.to_dict()) == ve
    # evidence_refs defaults to [] and reconstructs cleanly when absent.
    ve2 = ValueEstimate.from_dict({"tier": "low", "basis": "thin"})
    assert ve2.evidence_refs == []


# ─────────────────────────────────────────────────────────────────────────────
# Charter
# ─────────────────────────────────────────────────────────────────────────────


def test_charter_rejects_duplicate_invariant_ids():
    with pytest.raises(ValueError):
        Charter(
            id="g",
            type="guardian",
            dimension="d",
            purpose="p",
            cadence="hourly",
            invariants=[
                Invariant(id="x", description="d1", check_kind="claim_required"),
                Invariant(id="x", description="d2", check_kind="claim_required"),
            ],
        )


def test_charter_invariant_by_id():
    inv = Invariant(id="a", description="d", check_kind="claim_required")
    c = Charter(
        id="g",
        type="guardian",
        dimension="d",
        purpose="p",
        cadence="hourly",
        invariants=[inv],
    )
    assert c.invariant_by_id("a") is inv
    assert c.invariant_by_id("missing") is None


def test_charter_roundtrip():
    c = Charter(
        id="g",
        type="guardian",
        dimension="d",
        purpose="p",
        cadence="hourly",
        invariants=[
            Invariant(
                id="ai",
                description="d",
                check_kind="action_kind_allowed",
                params={"allowlist": ["Investigation"]},
            )
        ],
    )
    restored = Charter.from_dict(c.to_dict())
    assert restored.id == c.id
    assert restored.invariants[0].params["allowlist"] == ["Investigation"]


def test_charter_bucket_field():
    # Explicit bucket roundtrips.
    c = Charter(
        id="g", type="guardian", dimension="d", purpose="p", cadence="hourly",
        bucket="operate",
    )
    assert Charter.from_dict(c.to_dict()).bucket == "operate"

    # Default is None (charters predating the field are still valid).
    legacy = Charter(id="g", type="guardian", dimension="d", purpose="p", cadence="hourly")
    assert legacy.bucket is None
    legacy_payload = {k: v for k, v in legacy.to_dict().items() if k != "bucket"}
    assert Charter.from_dict(legacy_payload).bucket is None


def test_charter_altitude_field():
    # Explicit altitude (e.g. app_suggester → L2 capability) roundtrips.
    c = Charter(
        id="g", type="optimizer", dimension="capabilities", purpose="p",
        cadence="weekly", altitude=2,
    )
    assert Charter.from_dict(c.to_dict()).altitude == 2

    # Default is L0; charters predating the field are still valid.
    legacy = Charter(id="g", type="guardian", dimension="d", purpose="p", cadence="hourly")
    assert legacy.altitude == 0
    legacy_payload = {k: v for k, v in legacy.to_dict().items() if k != "altitude"}
    assert Charter.from_dict(legacy_payload).altitude == 0


# ─────────────────────────────────────────────────────────────────────────────
# TrackRecord / GeneratorRecord
# ─────────────────────────────────────────────────────────────────────────────


def test_track_record_roundtrip():
    tr = TrackRecord(
        proposals_emitted=3,
        proposals_applied=2,
        proposals_verified_success=1,
    )
    assert TrackRecord.from_dict(tr.to_dict()) == tr


def test_generator_record_roundtrip():
    gr = GeneratorRecord(
        id="g",
        charter_fingerprint="abc",
        config={"x": 1},
        track_record=TrackRecord(proposals_emitted=5),
    )
    restored = GeneratorRecord.from_dict(gr.to_dict())
    assert restored.config == {"x": 1}
    assert restored.track_record.proposals_emitted == 5

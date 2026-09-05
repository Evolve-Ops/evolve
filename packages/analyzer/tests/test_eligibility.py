"""tests/test_eligibility.py — auto-act eligibility classifier.

Spec: internal/spec-severity-framework-2026-05-18.md §1, §3, §8.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import eligibility as elg  # noqa: E402


# ── fix_risk_for_remediation ─────────────────────────────────────────────────


def test_remediation_fix_risk_known_kinds():
    assert elg.fix_risk_for_remediation("reset_baseline") == "low"
    assert elg.fix_risk_for_remediation("flip_cron_session_target") == "low"
    assert elg.fix_risk_for_remediation("install_infra_jobs") == "medium"
    assert elg.fix_risk_for_remediation("set_exec_allowlist") == "high"
    assert elg.fix_risk_for_remediation("set_exec_security") == "high"


def test_remediation_fix_risk_unknown_kinds_default_to_medium():
    assert elg.fix_risk_for_remediation("brand_new_kind") == "medium"
    assert elg.fix_risk_for_remediation(None) == "medium"
    assert elg.fix_risk_for_remediation("") == "medium"


# ── classify_signal ──────────────────────────────────────────────────────────


def _sig(*, remediation=None, severity_framework=None) -> dict:
    out: dict = {"id": "s1"}
    if remediation is not None:
        out["remediation"] = remediation
    if severity_framework is not None:
        out["severity_framework"] = severity_framework
    return out


def test_signal_without_remediation_asks():
    e = elg.classify_signal(_sig())
    assert e.decidable is False
    assert e.tier_floor == "ask"
    assert "no structured remediation" in e.reason


def test_signal_low_risk_remediation_at_low_magnitude_auto_small():
    e = elg.classify_signal(_sig(
        remediation={"kind": "reset_baseline", "params": {}},
        severity_framework={"vector": "cost", "magnitude": 1},
    ))
    assert e.fix_risk == "low"
    assert e.decidable is True
    assert e.tier_floor == "auto-small"


def test_signal_low_risk_remediation_at_higher_magnitude_auto():
    e = elg.classify_signal(_sig(
        remediation={"kind": "reset_baseline", "params": {}},
        severity_framework={"vector": "cost", "magnitude": 3},
    ))
    assert e.fix_risk == "low"
    assert e.tier_floor == "auto"  # bumps out of auto-small at mag 2+


def test_signal_medium_risk_remediation_auto():
    e = elg.classify_signal(_sig(
        remediation={"kind": "install_infra_jobs", "params": {}},
        severity_framework={"vector": "operations", "magnitude": 2},
    ))
    assert e.fix_risk == "medium"
    assert e.tier_floor == "auto"


def test_signal_high_risk_remediation_asks():
    """Security-policy handlers never auto-fire regardless of severity."""
    e = elg.classify_signal(_sig(
        remediation={"kind": "set_exec_allowlist", "params": {}},
        severity_framework={"vector": "operations", "magnitude": 1},
    ))
    assert e.fix_risk == "high"
    assert e.tier_floor == "ask"


def test_signal_security_critical_asks_even_with_low_risk_fix():
    """A security finding at magnitude ≥ 3 always asks, even when the
    handler is low-risk."""
    e = elg.classify_signal(_sig(
        remediation={"kind": "reset_baseline", "params": {}},
        severity_framework={"vector": "security", "magnitude": 4},
    ))
    assert e.tier_floor == "ask"
    assert "security-critical" in e.reason


def test_signal_remediation_without_kind_asks():
    e = elg.classify_signal(_sig(remediation={"params": {}}))
    assert e.tier_floor == "ask"


# ── classify_proposal ────────────────────────────────────────────────────────


def _prop(
    *,
    action_kind="ConfigPatch",
    urgency="improvement",
    risk_tag=None,
    has_claim=True,
    has_revert=True,
) -> dict:
    out: dict = {
        "id": "p1",
        "action": {"kind": action_kind},
        "urgency": urgency,
    }
    if risk_tag is not None:
        out["risk_tag"] = risk_tag
    if has_claim:
        out["claim"] = {"metric": "x", "target": 0.5}
    if has_revert:
        out["revert_on_failure"] = {"strategy": "rollback"}
    return out


def test_proposal_security_critical_always_asks():
    e = elg.classify_proposal(_prop(urgency="security_critical"))
    assert e.tier_floor == "ask"
    assert "security_critical" in e.reason


def test_proposal_human_only_action_kinds_ask():
    for kind in ("Investigation", "WorkflowInstruction", "SoulEdit", "BuildApp"):
        e = elg.classify_proposal(_prop(action_kind=kind))
        assert e.tier_floor == "ask", kind
        assert "human judgment" in e.reason


def test_memory_curate_is_never_auto_appliable():
    """MemoryCurate deletes or rewrites a bot's own accumulated memory.

    It used to sit in ``_DECIDABLE_ACTION_KINDS`` at base risk "low", so this
    exact shape — hygiene urgency, a bot-scoped auto-reversible risk_tag, a
    claim and a revert plan — classified as **auto-small**: a `rewrite` over
    ``.openclaw/workspace/memory/`` could apply with no person, while a
    one-line AGENTS.md append could not. Nothing emits the kind and no spec
    says what ``target_selector`` selects, so "the action has a computable
    right answer" was never true of it.

    Asserted as the classifier's ANSWER, not as set membership: the reason
    pins *why* it asks, so re-adding the kind to the decidable allowlist and
    dropping it from the human-only set both go red.
    """
    from schema.proposal import MemoryCurate

    action = MemoryCurate(
        bot_id="team_bot_a", operation="rewrite", target_selector="*", replacement="",
    )
    e = elg.classify_proposal(
        _prop(
            action_kind=action.kind,
            urgency="hygiene",
            risk_tag={
                "blast_radius": "bot",
                "reversibility": "auto",
                "touches": ["memory"],
            },
        )
    )
    assert e.decidable is False
    assert e.tier_floor == "ask"
    assert "human judgment" in e.reason


def test_proposal_unknown_action_kind_asks():
    """Conservative default — unrecognized action kinds ask."""
    e = elg.classify_proposal(_prop(action_kind="UnknownThing"))
    assert e.tier_floor == "ask"
    assert "auto-eligible allowlist" in e.reason


def test_proposal_missing_claim_asks():
    e = elg.classify_proposal(_prop(has_claim=False))
    assert e.tier_floor == "ask"
    assert "claim" in e.reason


def test_proposal_missing_revert_asks():
    e = elg.classify_proposal(_prop(has_revert=False))
    assert e.tier_floor == "ask"


def test_proposal_hygiene_low_risk_decidable_auto_small():
    """Canonical tier-b: low-blast + auto-revert + hygiene urgency +
    decidable action → auto-small."""
    e = elg.classify_proposal(_prop(
        action_kind="ConfigPatch",
        urgency="hygiene",
        risk_tag={"blast_radius": "local", "reversibility": "auto", "touches": []},
    ))
    assert e.fix_risk == "low"
    assert e.decidable is True
    assert e.tier_floor == "auto-small"


def test_proposal_improvement_low_risk_decidable_auto():
    """Same shape but improvement urgency → tier 'auto' (decide-for-me)."""
    e = elg.classify_proposal(_prop(
        action_kind="ConfigPatch",
        urgency="improvement",
        risk_tag={"blast_radius": "local", "reversibility": "auto", "touches": []},
    ))
    assert e.tier_floor == "auto"


def test_proposal_irreversibility_surface_forces_high():
    """A ConfigPatch that touches `auth` flips to high regardless of
    blast/reversibility."""
    e = elg.classify_proposal(_prop(
        risk_tag={
            "blast_radius": "local",
            "reversibility": "auto",
            "touches": ["auth"],
        },
    ))
    assert e.fix_risk == "high"
    assert e.tier_floor == "ask"


def test_proposal_unreversible_forces_high():
    e = elg.classify_proposal(_prop(
        risk_tag={"blast_radius": "local", "reversibility": "none", "touches": []},
    ))
    assert e.fix_risk == "high"
    assert e.tier_floor == "ask"


def test_proposal_platform_blast_bumps_risk():
    e = elg.classify_proposal(_prop(
        action_kind="ConfigPatch",   # base: low
        risk_tag={"blast_radius": "platform", "reversibility": "auto", "touches": []},
    ))
    # low base + platform blast → bumped to medium
    assert e.fix_risk == "medium"
    # medium + decidable → "auto" tier
    assert e.tier_floor == "auto"


def test_proposal_manual_reversibility_bumps_risk():
    e = elg.classify_proposal(_prop(
        action_kind="ConfigPatch",
        risk_tag={"blast_radius": "local", "reversibility": "manual", "touches": []},
    ))
    assert e.fix_risk == "medium"


def test_proposal_install_plugin_medium_risk_at_base():
    """EnablePluginEntry has medium base risk even with the cleanest
    risk_tag — enabling a new tool is structurally riskier than a
    config patch."""
    e = elg.classify_proposal(_prop(
        action_kind="EnablePluginEntry",
        risk_tag={"blast_radius": "local", "reversibility": "auto", "touches": []},
    ))
    assert e.fix_risk == "medium"
    assert e.tier_floor == "auto"


def test_proposal_update_agent_defaults_low_risk_decidable_auto_small():
    """UpdateAgentDefaults (PR B, 2026-05-31) — the cacheRetention L2
    applier. Per-bot single-enum knob, fully reversible by re-applying
    the inverse value, bounded blast radius. With hygiene urgency it
    must land at tier_floor=auto-small so the cache_ttl_tuner generator
    (PR F) can auto-apply against low-risk telemetry findings without
    operator click."""
    e = elg.classify_proposal(_prop(
        action_kind="UpdateAgentDefaults",
        urgency="hygiene",
        risk_tag={"blast_radius": "local", "reversibility": "auto",
                  "touches": []},
    ))
    assert e.fix_risk == "low"
    assert e.decidable is True
    assert e.tier_floor == "auto-small"


def test_proposal_update_agent_defaults_improvement_urgency_auto():
    """Same action with improvement urgency lands at tier 'auto' —
    operator still gets a click in tier-b, but tier-c auto-applies."""
    e = elg.classify_proposal(_prop(
        action_kind="UpdateAgentDefaults",
        urgency="improvement",
        risk_tag={"blast_radius": "local", "reversibility": "auto",
                  "touches": []},
    ))
    assert e.tier_floor == "auto"


def test_to_dict_round_trip():
    e = elg.classify_proposal(_prop(
        urgency="hygiene",
        risk_tag={"blast_radius": "local", "reversibility": "auto", "touches": []},
    ))
    d = e.to_dict()
    assert d["fix_risk"] == "low"
    assert d["decidable"] is True
    assert d["tier_floor"] == "auto-small"
    assert "reason" in d

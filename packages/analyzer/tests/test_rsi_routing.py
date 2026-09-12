"""tests/test_rsi_routing.py — autonomy gate + approval-audience routing."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.routing import (  # noqa: E402
    BotRoutingConfig,
    is_autonomous_eligible,
    resolve_audience,
    route,
)
from schema.proposal import (  # noqa: E402
    Claim,
    RevertPlan,
    RiskTag,
    ConfigPatch,
)
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
    make_workflow_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Bot config defaults
# ─────────────────────────────────────────────────────────────────────────────


def test_primary_bot_defaults_to_pod_operator():
    b = BotRoutingConfig(bot_id="evo", role="primary", multi_user=False)
    assert b.resolved_sysadmin_audience() == "pod_operator"


def test_single_user_member_defaults_to_both():
    b = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)
    assert b.resolved_sysadmin_audience() == "both"


def test_multi_user_member_defaults_to_pod_operator():
    b = BotRoutingConfig(bot_id="team", role="member", multi_user=True)
    assert b.resolved_sysadmin_audience() == "pod_operator"


def test_bot_config_explicit_override_wins():
    b = BotRoutingConfig(
        bot_id="team",
        role="member",
        multi_user=True,
        sysadmin_audience="primary_user",
    )
    assert b.resolved_sysadmin_audience() == "primary_user"


# ─────────────────────────────────────────────────────────────────────────────
# Autonomy eligibility
# ─────────────────────────────────────────────────────────────────────────────


def _config_patch_with_claim(target_path, **kwargs):
    p = make_config_patch_proposal(target_path=target_path, **kwargs)
    # harness leaves revert_on_failure=None; auto path requires it to be set,
    # normally filled by snapshot capture. For routing tests, synthesize.
    p.revert_on_failure = RevertPlan(
        before_snapshot={},
        revert_action=p.action,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    return p


def test_investigation_not_autonomous():
    # Investigation has no claim → can't auto-revert
    p = make_investigation_proposal()
    assert not is_autonomous_eligible(p)


def test_reversible_config_patch_is_autonomous():
    p = _config_patch_with_claim(target_path="/tmp/x.json::k", value=1)
    assert is_autonomous_eligible(p)


def test_touches_irreversibility_surface_blocks_autonomy():
    p = _config_patch_with_claim(
        target_path="/tmp/x.json::k", value=1, touches=["auth"]
    )
    assert not is_autonomous_eligible(p)


def test_manual_reversibility_blocks_autonomy():
    p = _config_patch_with_claim(
        target_path="/tmp/x.json::k", value=1, reversibility="manual"
    )
    assert not is_autonomous_eligible(p)


def test_platform_blast_radius_blocks_autonomy():
    p = _config_patch_with_claim(
        target_path="/tmp/x.json::k", value=1, blast_radius="platform"
    )
    assert not is_autonomous_eligible(p)


def test_missing_claim_blocks_autonomy():
    p = _config_patch_with_claim(target_path="/tmp/x.json::k", value=1)
    p.claim = None
    assert not is_autonomous_eligible(p)


def test_missing_revert_plan_blocks_autonomy():
    p = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    # claim present, revert_on_failure None → not autonomous
    assert p.claim is not None
    assert p.revert_on_failure is None
    assert not is_autonomous_eligible(p)


# ─────────────────────────────────────────────────────────────────────────────
# Audience resolution — by dimension + bot config
# ─────────────────────────────────────────────────────────────────────────────


def test_meta_health_always_pod_operator():
    p = make_investigation_proposal(dimension="meta_health")
    b = BotRoutingConfig(
        bot_id="team_bot_a", role="member", multi_user=False, sysadmin_audience="primary_user"
    )
    assert resolve_audience(p, b) == "pod_operator"


def test_improvement_on_personal_bot_routes_to_primary_user():
    p = make_workflow_proposal(dimension="utility")
    b = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)
    assert resolve_audience(p, b) == "bot_primary_user"


def test_improvement_on_team_bot_routes_to_pod_operator():
    p = make_workflow_proposal(dimension="utility")
    b = BotRoutingConfig(bot_id="team", role="member", multi_user=True)
    assert resolve_audience(p, b) == "pod_operator"


def test_improvement_on_primary_bot_routes_to_pod_operator():
    p = make_workflow_proposal(dimension="utility")
    b = BotRoutingConfig(bot_id="evo", role="primary", multi_user=False)
    assert resolve_audience(p, b) == "pod_operator"


def test_sysadmin_proposal_honors_sysadmin_audience_both():
    p = make_investigation_proposal(dimension="substrate_health")
    b = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)
    # default for single-user member is "both"
    assert resolve_audience(p, b) == "both"


def test_security_critical_forces_dual_surface_even_when_primary_user():
    p = make_investigation_proposal(dimension="safety", urgency="security_critical")
    b = BotRoutingConfig(
        bot_id="team_bot_a",
        role="member",
        multi_user=False,
        sysadmin_audience="primary_user",
    )
    # primary_user override → bot_primary_user, but critical elevates to both
    assert resolve_audience(p, b) == "both"


def test_security_non_critical_honors_primary_user_setting():
    p = make_investigation_proposal(dimension="safety", urgency="substrate_warn")
    b = BotRoutingConfig(
        bot_id="team_bot_a",
        role="member",
        multi_user=False,
        sysadmin_audience="primary_user",
    )
    assert resolve_audience(p, b) == "bot_primary_user"


# ─────────────────────────────────────────────────────────────────────────────
# Combined route()
# ─────────────────────────────────────────────────────────────────────────────


def test_route_autonomous_path():
    p = _config_patch_with_claim(target_path="/tmp/x.json::k", value=1)
    b = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)
    decision = route(p, b)
    assert decision.autonomous
    assert decision.audience == "none"
    assert any("autonomous" in r for r in decision.reasons)


def test_route_manual_path_records_reasons():
    p = make_investigation_proposal(dimension="substrate_health")
    b = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)
    decision = route(p, b)
    assert not decision.autonomous
    assert decision.audience in ("pod_operator", "both", "bot_primary_user")
    assert any("not reversible" in r or "no claim" in r for r in decision.reasons)

"""tests/test_rsi_integration.py — L1 end-to-end acceptance walkthrough.

Implements the flows named in spec-rsi-layer-1-foundation-2026-04-18.md §8.6:

  1. Emit a fixture proposal through the synthetic harness
  2. Watch it traverse ingest → routing → apply → scheduled verification hook
  3. Mock the metric response so the claim succeeds; confirm ``succeeded`` (L2
     owns the actual verify daemon; here we just walk through the arbiter path
     that gets us to the ``applied`` state, which is L1's terminus)
  4. Repeat with a failing metric; confirm revert restores state

L1 doesn't ship the verify daemon. These tests exercise the full arbiter
path as far as L1 can take it, then manually call the applier's revert
to prove the revert pathway works end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import apply as arbiter_apply  # noqa: E402
from arbiter import ingest, route  # noqa: E402
from arbiter.appliers import get_applier  # noqa: E402
from arbiter.routing import BotRoutingConfig  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from testing.harness import (  # noqa: E402
    make_charter,
    make_config_patch_proposal,
    make_investigation_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Autonomous happy path — config patch survives through applied
# ─────────────────────────────────────────────────────────────────────────────


def test_autonomous_config_patch_end_to_end(tmp_path):
    """Emit → ingest → route → apply → applied state + snapshot captured."""
    # Seed a config file the patch will target
    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text(json.dumps({"ui": {"theme": "light"}, "keep": 1}))

    # Build proposal
    proposal = make_config_patch_proposal(
        target_path=f"{cfg_path}::ui.theme",
        value="dark",
        # Use a fallback the schema validator accepts without revert_on_failure
        # since the harness doesn't pre-populate revert plans.
    )
    # The claim fallback defaults to "revert"; for L1 we need fallback="flag"
    # to let ingest pass without a revert_on_failure pre-filled (the apply
    # step fills revert_on_failure via snapshot capture).
    # Update: the arbiter.apply step populates revert_on_failure before
    # running the applier, so revert doesn't need to be pre-filled for
    # apply. But ingest wants to see it OR fallback=flag. Let's set flag
    # so ingest passes, then arbiter.apply fills it anyway.
    proposal.claim.fallback = "flag"

    charter = make_charter(
        gen_type="guardian",
        dimension="substrate_health",
        action_allowlist=["ConfigPatch"],
    )
    bot = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)

    # Step 1: ingest
    result = ingest(proposal, charter=charter)
    assert result.accepted
    assert proposal.status == "pending"

    # Step 2: route
    decision = route(proposal, bot)
    # Proposal touches ["config"] which is NOT in irreversibility surfaces,
    # is reversibility=auto, blast_radius=bot, has a claim. But the
    # revert_on_failure is still None at this point, so it's not autonomous
    # until apply captures a snapshot.
    # Spec §3.5 requires claim+revert+reversible+scoped. revert is filled at
    # apply time. So at route() time, autonomous is False.
    # After apply captures the snapshot, the proposal is applied.
    # To model the full autonomous path, we transition manually to
    # approved_auto (simulating that the arbiter has determined it's
    # autonomous-eligible after snapshot capture) and then call apply().
    assert not decision.autonomous  # revert not yet attached
    transition(proposal, "approved_auto", actor="arbiter")

    # Step 3: apply
    outcome = arbiter_apply.apply(proposal)
    assert outcome.ok, outcome.message
    assert proposal.status == "applied"
    # The applier captured a snapshot; revert_on_failure is now populated
    assert proposal.revert_on_failure is not None

    # Verify the config actually changed on disk
    data = json.loads(cfg_path.read_text())
    assert data["ui"]["theme"] == "dark"

    # L1 terminus — verify daemon in L2 would now check the metric at the
    # claim's horizon. Here we simulate success by transitioning directly.
    transition(
        proposal,
        "succeeded",
        actor="verify_daemon",
        reason="claim held (simulated)",
    )
    assert proposal.status == "succeeded"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Failure path — revert restores pre-state
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_failure_revert_restores_state(tmp_path):
    """Apply makes a change; revert restores the exact prior state."""
    cfg_path = tmp_path / "openclaw.json"
    original = {"ui": {"theme": "light", "keep": "me"}, "x": 1}
    cfg_path.write_text(json.dumps(original))

    proposal = make_config_patch_proposal(
        target_path=f"{cfg_path}::ui.theme",
        value="dark",
    )
    proposal.claim.fallback = "flag"

    charter = make_charter(
        action_allowlist=["ConfigPatch"],
    )
    ingest(proposal, charter=charter)
    transition(proposal, "approved_auto", actor="arbiter")

    outcome = arbiter_apply.apply(proposal)
    assert outcome.ok
    # Verify patched
    after_apply = json.loads(cfg_path.read_text())
    assert after_apply["ui"]["theme"] == "dark"

    # Simulate verify daemon deciding the claim failed → revert
    applier = get_applier("ConfigPatch")
    revert_result = applier.revert(
        proposal.revert_on_failure.before_snapshot, proposal.bot_id
    )
    assert revert_result.ok

    # File should match the original exactly
    after_revert = json.loads(cfg_path.read_text())
    assert after_revert == original

    # Transition proposal to terminal failed_reverted state
    transition(
        proposal, "failed_reverted", actor="verify_daemon", reason="simulated"
    )
    assert proposal.status == "failed_reverted"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Human-approval path for non-autonomous proposals
# ─────────────────────────────────────────────────────────────────────────────


def test_human_approval_path_end_to_end():
    """Investigation → ingest → route (pod_operator audience) → approve → apply."""
    proposal = make_investigation_proposal(dimension="substrate_health")
    charter = make_charter(
        action_allowlist=["Investigation"],
    )
    bot = BotRoutingConfig(bot_id="team_bot_a", role="member", multi_user=False)

    ingest(proposal, charter=charter)
    decision = route(proposal, bot)
    assert not decision.autonomous
    # Default sysadmin_audience for single-user member is "both"
    assert decision.audience == "both"

    # Human approves
    transition(proposal, "approved_human", actor="user", reason="accepted via UI")
    outcome = arbiter_apply.apply(proposal)
    assert outcome.ok
    # Investigation is a manual-completion kind: applier no-ops, but the
    # proposal sits in ``applied`` (the In Process queue) awaiting an
    # explicit Mark complete from the operator.
    assert proposal.status == "applied"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Invariant violation quarantines the path
# ─────────────────────────────────────────────────────────────────────────────


def test_invariant_violation_at_ingest():
    """Emit a proposal whose kind isn't allowed by the charter → ingest raises."""
    from arbiter.ingest import InvariantViolation

    proposal = make_investigation_proposal()
    charter = make_charter(action_allowlist=["ConfigPatch"])  # no Investigation

    try:
        ingest(proposal, charter=charter)
    except InvariantViolation as exc:
        assert exc.invariant.check_kind == "action_kind_allowed"
        assert "action.kind" in exc.detail
    else:
        raise AssertionError("expected InvariantViolation")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Snooze → wake → re-process cycle
# ─────────────────────────────────────────────────────────────────────────────


def test_snooze_wake_cycle():
    """User snoozes a proposal; snooze-wake daemon brings it back."""
    from datetime import datetime, timedelta, timezone

    from arbiter.snooze_wake import wake_expired

    proposal = make_investigation_proposal()
    charter = make_charter(action_allowlist=["Investigation"])
    ingest(proposal, charter=charter)

    # User snoozes
    transition(proposal, "snoozed", actor="user")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    proposal.snoozed_until = past.isoformat(timespec="seconds")

    # Snooze-wake daemon runs
    result = wake_expired([proposal])
    assert proposal.id in result.woken
    assert proposal.status == "pending"
    assert proposal.snoozed_until is None

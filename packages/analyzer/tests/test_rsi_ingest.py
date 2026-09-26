"""tests/test_rsi_ingest.py — arbiter.ingest schema + invariant + dedup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.ingest import (  # noqa: E402
    InvariantViolation,
    SchemaError,
    ingest,
)
from schema import (  # noqa: E402
    Charter,
    Invariant,
    MemoryCurate,
)
from schema.proposal import (  # noqa: E402
    Claim,
    Proposal,
)
from testing.harness import (  # noqa: E402
    make_charter,
    make_config_patch_proposal,
    make_investigation_proposal,
    make_workflow_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def test_accept_valid_investigation():
    charter = make_charter()
    p = make_investigation_proposal()
    result = ingest(p, charter=charter)
    assert result.accepted
    assert p.status == "pending"


def test_accept_claim_without_revert_plan_at_ingest():
    """Revert plan is populated at apply time, not emission. Ingest should
    accept a proposal that declares a claim with fallback=revert even
    though revert_on_failure is None."""
    charter = make_charter()
    p = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    assert p.claim is not None
    assert p.claim.fallback == "revert"
    assert p.revert_on_failure is None  # populated at apply time
    # Ingest should pass — the applier is responsible for capturing the
    # snapshot before running the applier's apply().
    result = ingest(p, charter=charter)
    assert result.accepted


def test_reject_schema_version_below_2():
    charter = make_charter()
    p = make_investigation_proposal()
    p.schema_version = 1
    with pytest.raises(SchemaError):
        ingest(p, charter=charter)


# ─────────────────────────────────────────────────────────────────────────────
# Invariants
# ─────────────────────────────────────────────────────────────────────────────


def test_action_kind_allowed_invariant_blocks_forbidden():
    charter = make_charter(action_allowlist=["Investigation"])
    p = make_workflow_proposal()  # action.kind == "WorkflowInstruction"
    # Flip fallback so schema validation passes without a revert plan;
    # we want invariant check to be the thing that blocks, not schema.
    p.claim.fallback = "flag"
    with pytest.raises(InvariantViolation) as exc:
        ingest(p, charter=charter)
    assert exc.value.generator_id == charter.id
    assert "action.kind" in exc.value.detail


def test_touches_forbidden_invariant_blocks():
    charter = make_charter(
        action_allowlist=["ConfigPatch"],
        touches_forbidden=["config"],
    )
    p = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    p.claim = Claim(
        metric="x",
        direction="up",
        magnitude=1.0,
        window_days=1,
        baseline=0.0,
        fallback="flag",
    )
    # touches=["config"] per harness default; forbidden matches
    with pytest.raises(InvariantViolation):
        ingest(p, charter=charter)


def test_claim_required_invariant():
    charter = Charter(
        id="g",
        type="optimizer",
        dimension="utility",
        purpose="p",
        cadence="hourly",
        invariants=[
            Invariant(
                id="needs_claim",
                description="d",
                check_kind="claim_required",
            ),
        ],
    )
    p = make_investigation_proposal()  # no claim
    with pytest.raises(InvariantViolation):
        ingest(p, charter=charter)


def test_claim_metric_known_invariant_passes_when_registered():
    charter = Charter(
        id="g",
        type="optimizer",
        dimension="utility",
        purpose="p",
        cadence="hourly",
        invariants=[
            Invariant(
                id="known_metric",
                description="d",
                check_kind="claim_metric_known",
            ),
        ],
    )
    p = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    p.claim = Claim(
        metric="gateway.up",
        direction="up",
        magnitude=1.0,
        window_days=1,
        baseline=0.0,
        fallback="flag",
    )
    # Passes when metric is in the registry
    result = ingest(p, charter=charter, known_metrics=["gateway.up"])
    assert result.accepted


def test_claim_metric_known_invariant_rejects_unknown():
    charter = Charter(
        id="g",
        type="optimizer",
        dimension="utility",
        purpose="p",
        cadence="hourly",
        invariants=[
            Invariant(
                id="known_metric",
                description="d",
                check_kind="claim_metric_known",
            ),
        ],
    )
    p = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    p.claim = Claim(
        metric="made.up.metric",
        direction="up",
        magnitude=1.0,
        window_days=1,
        baseline=0.0,
        fallback="flag",
    )
    with pytest.raises(InvariantViolation):
        ingest(p, charter=charter, known_metrics=["gateway.up"])


# ─────────────────────────────────────────────────────────────────────────────
# Dedup
# ─────────────────────────────────────────────────────────────────────────────


def test_dedup_records_fingerprint():
    charter = make_charter()
    p = make_investigation_proposal()
    result = ingest(p, charter=charter)
    assert result.dedup is not None
    assert len(result.dedup.fingerprint) == 64  # SHA-256 hex
    assert result.dedup.collisions == []


def test_dedup_collision_merges_into_existing_and_suppresses_new():
    """When the incoming proposal's fingerprint collides with an open proposal,
    ingest merges into the existing one (appends a refresh history entry)
    and signals the caller via merged_into_id NOT to write the new proposal.
    This is the change that collapses 224-card duplicate piles."""
    charter = make_charter()
    p1 = make_investigation_proposal(problem="X", context="Y")
    p2 = make_investigation_proposal(problem="X", context="Y")
    # The fingerprint is keyed on (bot_id, action_kind, target_surface,
    # target_metric, triggers) — identical triggers reproduce the production
    # case where sysadmin_watchdog re-fires the same detector each cycle and
    # writes an identical trigger string like "plugin_missing:admin_bot".
    p1.trigger_observations = ["obs-stable"]
    p2.trigger_observations = ["obs-stable"]

    ingest(p1, charter=charter)
    result = ingest(p2, charter=charter, open_proposals=[p1])

    assert result.accepted
    assert result.merged_into_id == p1.id
    assert result.refreshed_proposal is p1
    assert result.should_write_new is False
    # Same-status refresh leaves a same-state history entry as a "last
    # re-detected at" timestamp without requiring a schema change.
    refresh_entries = [h for h in p1.history if "dedup-refresh" in h.reason]
    assert len(refresh_entries) == 1
    assert refresh_entries[0].from_status == refresh_entries[0].to_status == "pending"


def test_dedup_collision_unions_new_triggers_into_existing():
    """If a future generator emits a trigger overlapping but not identical to
    an existing proposal's, the merge unions new triggers into the existing
    list — preserving the audit trail of every observation that fed the
    same fingerprint class. (The fingerprint primitive currently differentiates
    on full trigger set, so this path is mostly defensive, but exercising it
    pins the contract for future fingerprint variants.)"""
    from arbiter.ingest import _merge_triggers
    assert _merge_triggers(
        ["obs-a", "obs-b"], ["obs-b", "obs-c"]
    ) == ["obs-a", "obs-b", "obs-c"]
    # Empty inputs are a no-op.
    assert _merge_triggers(["obs-a"], []) == ["obs-a"]
    assert _merge_triggers([], ["obs-a"]) == ["obs-a"]


def test_dedup_collision_with_snoozed_proposal_resurfaces_to_pending():
    """A collision against a snoozed proposal re-surfaces it to pending —
    snoozing means 'come back later'; if the issue is firing again, the
    dedup hook should treat that as a real wake signal rather than letting
    the new proposal pile up alongside."""
    charter = make_charter()
    existing = make_investigation_proposal(problem="X", context="Y")
    incoming = make_investigation_proposal(problem="X", context="Y")
    existing.trigger_observations = ["obs-stable"]
    incoming.trigger_observations = ["obs-stable"]

    ingest(existing, charter=charter)
    # Manually move the existing proposal to snoozed state.
    from arbiter.state_machine import transition as st_transition
    st_transition(existing, "snoozed", actor="user", reason="defer")
    assert existing.status == "snoozed"

    result = ingest(incoming, charter=charter, open_proposals=[existing])

    assert result.merged_into_id == existing.id
    assert result.refreshed_from_status == "snoozed"
    assert existing.status == "pending"  # re-surfaced
    assert any(
        h.from_status == "snoozed" and h.to_status == "pending"
        and "dedup-refresh" in h.reason
        for h in existing.history
    )


def test_dedup_collision_without_open_proposals_falls_back_to_log_only():
    """If the caller doesn't supply open_proposals at all, the merge logic
    has nothing to merge into; ingest accepts the new proposal as before
    (back-compat for callers that don't track an open set)."""
    charter = make_charter()
    p = make_investigation_proposal(problem="X", context="Y")
    result = ingest(p, charter=charter)  # no open_proposals
    assert result.accepted
    assert result.merged_into_id is None
    assert result.should_write_new is True


def test_dedup_collision_picks_oldest_when_multiple_match():
    """If two open proposals share a fingerprint (rare; usually a legacy
    artifact), merge into the oldest one so history accumulates on the
    original record rather than fragmenting."""
    charter = make_charter()
    older = make_investigation_proposal(problem="X", context="Y")
    newer = make_investigation_proposal(problem="X", context="Y")
    older.trigger_observations = ["obs-stable"]
    newer.trigger_observations = ["obs-stable"]
    older.created_at = "2026-04-01T00:00:00+00:00"
    newer.created_at = "2026-04-15T00:00:00+00:00"
    ingest(older, charter=charter)
    ingest(newer, charter=charter)  # treated as new because no open_proposals

    incoming = make_investigation_proposal(problem="X", context="Y")
    incoming.trigger_observations = ["obs-stable"]
    result = ingest(incoming, charter=charter, open_proposals=[newer, older])
    assert result.merged_into_id == older.id


def test_dedup_refresh_overwrites_user_facing_framing():
    """A re-detection should overwrite the user-facing fields (problem,
    action, provenance, claim, admin_surface_summary) with the incoming
    proposal's values so the queue shows the *current* numbers — not the
    snapshot at first detection.

    Specifically pins the budget_hawk case: today's spend $4.23 crosses the
    cap, gets emitted; next cycle today's spend is $7.50; the proposal in
    the queue must say $7.50, not $4.23. Trigger key stays the same (that
    is what makes the fingerprint match) but the framing freshens.
    """
    charter = make_charter()
    older = make_investigation_proposal(
        problem="admin_bot daily spend $4.23 crossed warn cap $2.00",
        context="Bot admin_bot's spend ($4.23) exceeded the warn cap ($2.00).",
    )
    newer = make_investigation_proposal(
        problem="admin_bot daily spend $7.50 crossed warn cap $2.00",
        context="Bot admin_bot's spend ($7.50) exceeded the warn cap ($2.00).",
    )
    older.trigger_observations = ["warn_cap_crossed:admin_bot"]
    newer.trigger_observations = ["warn_cap_crossed:admin_bot"]
    older.admin_surface_summary = "admin_bot daily spend $4.23 crossed warn cap $2.00"
    newer.admin_surface_summary = "admin_bot daily spend $7.50 crossed warn cap $2.00"

    ingest(older, charter=charter)
    result = ingest(newer, charter=charter, open_proposals=[older])

    assert result.merged_into_id == older.id
    assert result.should_write_new is False
    # The existing proposal's framing now reflects the LATEST detection.
    assert "$7.50" in older.problem
    assert "$7.50" in older.admin_surface_summary
    assert "$7.50" in older.action.context  # type: ignore[union-attr]
    # Provenance was replaced wholesale (not merged).
    assert older.provenance is newer.provenance


def test_dedup_records_fingerprint_no_collision():
    """Sanity check: with no collision, dedup result is recorded but no
    merge happens (the original L1 pass-through path stays intact)."""
    charter = make_charter()
    p = make_investigation_proposal()
    result = ingest(p, charter=charter)
    assert result.dedup is not None
    assert len(result.dedup.fingerprint) == 64
    assert result.dedup.collisions == []
    assert result.merged_into_id is None
    assert result.should_write_new is True


def test_draft_transitions_to_pending():
    charter = make_charter()
    p = make_investigation_proposal()
    assert p.status == "draft"
    ingest(p, charter=charter)
    assert p.status == "pending"
    assert any(h.from_status == "draft" for h in p.history)

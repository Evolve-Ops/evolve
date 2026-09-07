"""verify.dispatch — Routes ClaimOutcome to the correct terminal state.

For each claim outcome:
  - succeeded → transition proposal to ``succeeded``
  - failed + fallback=revert → call applier.revert; transition to
    ``failed_reverted`` (or ``failed_revert_failed`` if revert blew up)
  - failed + fallback=flag → transition to ``failed_flagged``; emit an
    escalation proposal to pod_operator
  - failed + fallback=escalate → call applier.revert AND emit escalation
  - unresolved → no transition (caller retries)

The escalation proposal is a new Proposal the verify daemon synthesizes
and writes to ``proposals/pending/``. It carries urgency ``operational_urgent``
and ``approval_audience = pod_operator``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arbiter.appliers import get_applier
from arbiter.state_machine import transition
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)
from verify.evaluate import ClaimOutcome


@dataclass
class DispatchResult:
    """Outcome of dispatching a ClaimOutcome against a Proposal."""

    final_status: str  # the proposal's status after dispatch
    revert_ok: bool | None = None  # None if revert wasn't attempted
    revert_message: str = ""
    escalation: Proposal | None = None  # synthesized escalation proposal
    notes: list[str] = field(default_factory=list)


def _synthesize_escalation(
    original: Proposal,
    outcome: ClaimOutcome,
    *,
    reason: str,
) -> Proposal:
    """Build a pod_operator-audience Investigation proposal that escalates
    a verification failure to a human."""
    ctx = (
        f"Verification failed for proposal {original.id}. "
        f"metric={original.claim.metric if original.claim else '?'} "
        f"delta={outcome.delta:.4f} "
        f"direction={original.claim.direction if original.claim else '?'} "
        f"magnitude={original.claim.magnitude if original.claim else 0.0} "
        f"fallback={original.claim.fallback if original.claim else '?'}. "
        f"Reason: {reason}"
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=original.bot_id,
        generator_id="verify_daemon",
        dimension="meta_health",
        trigger_observations=[f"verification_failed:{original.id}"],
        provenance=Provenance(
            technique="verify_daemon.escalation",
            signals={
                "original_proposal_id": original.id,
                "original_generator_id": original.generator_id,
                "outcome": outcome.result,
                "delta": outcome.delta,
            },
            confidence=0.99,
        ),
        problem=reason,
        action=Investigation(context=ctx),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        ),
        approval_audience="pod_operator",
        urgency="operational_urgent",
        admin_surface_summary=f"Verification failed for {original.id}: {reason}"[
            :120
        ],
        status="draft",
    )


def dispatch_outcome(
    proposal: Proposal,
    outcome: ClaimOutcome,
    *,
    actor: str = "verify_daemon",
) -> DispatchResult:
    """Apply the rules described in the module docstring.

    Mutates ``proposal`` (status transition; may clear revert_on_failure).
    Does NOT write escalation proposals to disk — the caller is responsible
    for that. Returns any synthesized escalation in ``result.escalation``.

    For ``unresolved`` outcomes, no mutation occurs and the caller should
    retry on the next cycle.
    """
    result = DispatchResult(final_status=proposal.status)

    if outcome.result == "unresolved":
        result.notes.append(
            f"unresolved: {outcome.message}; leaving proposal in {proposal.status}"
        )
        return result

    if outcome.result == "succeeded":
        transition(
            proposal,
            "succeeded",
            actor=actor,
            reason=outcome.message,
        )
        result.final_status = proposal.status
        result.notes.append("claim held; proposal succeeded")
        return result

    # ── Failure paths ──────────────────────────────────────────────────────
    assert outcome.result == "failed"
    if proposal.claim is None:
        # Shouldn't happen — evaluate_claim requires a claim — but be safe.
        result.notes.append("failed outcome but proposal has no claim; no-op")
        return result

    fallback = proposal.claim.fallback

    if fallback == "flag":
        transition(
            proposal,
            "failed_flagged",
            actor=actor,
            reason=outcome.message,
        )
        result.escalation = _synthesize_escalation(
            proposal,
            outcome,
            reason=f"Claim failed with fallback=flag; change was NOT reverted",
        )
        result.final_status = proposal.status
        result.notes.append("fallback=flag: flagged for human review; escalation emitted")
        return result

    # fallback == "revert" or "escalate" — attempt revert
    revert_ok, revert_msg = _attempt_revert(proposal, actor=actor)
    result.revert_ok = revert_ok
    result.revert_message = revert_msg

    if not revert_ok:
        transition(
            proposal,
            "failed_revert_failed",
            actor=actor,
            reason=f"revert failed: {revert_msg}",
        )
        result.escalation = _synthesize_escalation(
            proposal,
            outcome,
            reason=f"Revert failed — manual intervention required: {revert_msg}",
        )
        result.final_status = proposal.status
        result.notes.append("revert failed; manual intervention proposal emitted")
        return result

    # revert succeeded
    transition(
        proposal,
        "failed_reverted",
        actor=actor,
        reason=outcome.message,
    )
    result.final_status = proposal.status
    result.notes.append("claim failed; revert executed; proposal failed_reverted")

    if fallback == "escalate":
        result.escalation = _synthesize_escalation(
            proposal,
            outcome,
            reason="Claim failed and was reverted (fallback=escalate)",
        )
        result.notes.append("fallback=escalate: escalation proposal emitted")

    return result


def _attempt_revert(
    proposal: Proposal,
    *,
    actor: str,  # noqa: ARG001 — reserved for future hooks
) -> tuple[bool, str]:
    """Call the applier's revert. Returns (ok, message).

    Requires ``proposal.revert_on_failure`` to be populated (snapshot captured
    at apply time). If it's None, we treat revert as impossible and return
    (False, ...).
    """
    if proposal.revert_on_failure is None:
        return False, "revert_on_failure is None; cannot revert without snapshot"

    kind = getattr(
        proposal.revert_on_failure.revert_action,
        "kind",
        type(proposal.revert_on_failure.revert_action).__name__,
    )
    try:
        applier = get_applier(kind)
    except KeyError as e:
        return False, f"no applier for kind {kind!r}: {e}"

    try:
        rr = applier.revert(
            proposal.revert_on_failure.before_snapshot, proposal.bot_id
        )
    except Exception as e:  # noqa: BLE001 — revert errors surface
        return False, f"revert raised: {e}"

    return rr.ok, rr.message

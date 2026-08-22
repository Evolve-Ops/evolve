"""arbiter.state_machine — Proposal lifecycle transitions.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §3.6.

Defines the legal graph of status transitions. Any attempted illegal
transition raises ``IllegalTransitionError`` and the proposal is frozen
pending investigation (never silently corrected).

Transitions are append-only: every accepted transition writes a
``StatusTransition`` entry into ``proposal.history``.
"""

from __future__ import annotations

from evolve_util import now_iso_offset as _utc_now_iso
from schema.proposal import Proposal, Status, StatusTransition


class IllegalTransitionError(Exception):
    """Raised when a status transition is not permitted by the state machine."""

    def __init__(self, from_status: str, to_status: str, reason: str = ""):
        self.from_status = from_status
        self.to_status = to_status
        msg = f"Illegal transition: {from_status} → {to_status}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Transition graph
# ─────────────────────────────────────────────────────────────────────────────

# Each key is the current status; the value is a set of legal next statuses.
# This graph is deliberately strict — adding a transition is a considered
# change, not a reflex.
_TRANSITIONS: dict[Status, frozenset[Status]] = {
    "draft": frozenset({"pending"}),
    "pending": frozenset(
        {
            "approved_auto",
            "approved_human",
            "rejected",
            "dismissed",
            "snoozed",
            "superseded",
            "resolved_externally",
            # Slice 3 (2026-06-04): operator clicked "Have evo fix
            # this" / "Send to {bot_id}" / "Have forge handle this".
            # Proposal hands off to dispatch_target; on result
            # callback it transitions to applied | failed_flagged,
            # or back to pending on cancel.
            "dispatched",
        }
    ),
    # ``failed_flagged`` is reachable from approved_* for the case where
    # the applier refuses to act before any side effects (e.g. BuildApp
    # detecting an existing manifest). The proposal didn't transit
    # ``applied`` because nothing was applied; it goes straight to the
    # operator-review queue with the failure reason.
    "approved_auto": frozenset({"applied", "rejected", "failed_flagged"}),
    "approved_human": frozenset({"applied", "rejected", "failed_flagged"}),
    # ``dismissed`` is reachable from ``applied`` for manual-completion kinds
    # (Investigation, WorkflowInstruction): the operator took the proposal on,
    # then decided not to follow through. The In Process tab exposes a Dismiss
    # button alongside Mark complete for exactly this case.
    "applied": frozenset(
        {
            "succeeded",
            "failed_reverted",
            "failed_flagged",
            "failed_revert_failed",
            "dismissed",
        }
    ),
    "snoozed": frozenset(
        {"pending", "rejected", "dismissed", "superseded", "resolved_externally"}
    ),
    # Slice 3 dispatch lifecycle. dispatched is a transient state
    # between pending and the terminal-ish ones. Three legal exits:
    #   - applied: target reported success (also covers in_process —
    #     manual-completion kinds still need operator Mark complete)
    #   - failed_flagged: target reported failure; operator-review
    #   - pending: operator cancelled (POST /dispatch/cancel)
    # No direct edge to succeeded — that runs through applied + the
    # verify-daemon's check the same as any other applied proposal.
    "dispatched": frozenset({"applied", "failed_flagged", "pending"}),
    # Terminal states (no outgoing transitions in L1)
    "succeeded": frozenset(),
    "failed_reverted": frozenset(),
    "failed_flagged": frozenset({"rejected", "succeeded"}),  # human can resolve
    "failed_revert_failed": frozenset(),  # manual intervention only
    "rejected": frozenset(),
    "dismissed": frozenset(),
    "superseded": frozenset(),
    "resolved_externally": frozenset(),
}


def allowed_transitions(from_status: Status) -> frozenset[Status]:
    """Return the set of statuses ``from_status`` may transition to."""
    return _TRANSITIONS.get(from_status, frozenset())


def is_legal_transition(from_status: Status, to_status: Status) -> bool:
    """True if the transition is in the graph."""
    return to_status in _TRANSITIONS.get(from_status, frozenset())


def transition(
    proposal: Proposal,
    to_status: Status,
    *,
    actor: str,
    reason: str = "",
) -> Proposal:
    """Transition ``proposal`` to ``to_status`` in place, appending history.

    Raises ``IllegalTransitionError`` if the transition is not allowed.
    Returns the same proposal (mutated) for convenience.

    ``actor`` should be a short identifier:
      - ``"arbiter"`` for automated routing decisions
      - ``"user"`` for human actions (act/dismiss/reject/snooze)
      - ``"verify_daemon"`` for verification outcomes
      - ``"snooze_wake"`` for snoozed → pending transitions
      - a generator_id for generator-originated transitions
    """
    if not is_legal_transition(proposal.status, to_status):
        raise IllegalTransitionError(
            proposal.status,
            to_status,
            reason=f"proposal={proposal.id} (actor={actor})",
        )

    entry = StatusTransition(
        from_status=proposal.status,
        to_status=to_status,
        at=_utc_now_iso(),
        actor=actor,
        reason=reason,
    )
    proposal.history.append(entry)
    proposal.status = to_status
    return proposal

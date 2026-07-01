"""edr.bridge.lifecycle — the R1 approval gate + dispatch eligibility.

The lifecycle half of the actuator bridge (design:
docs/edr/design-edr-2026-06-11.md §5.1 + §5.3). Two functions:

  - ``approve(shared_dir, proposal_id)`` — the HUMAN gate. Moves one
    pending, agent-able triage Proposal to ``approved_human`` through
    the real arbiter lifecycle (``arbiter.state_machine.transition`` —
    legal-edge check + append-only history — then
    ``arbiter.store.move_proposal``). Refuses loudly, with NO mutation,
    in every other case (rules below).
  - ``eligible_for_dispatch(shared_dir)`` — what P1.3b may launch:
    approved Proposals whose triage verdict is agent-able AND carries a
    non-empty proof artifact. Defensive by design: anything
    missing/malformed is NOT eligible and the reason is logged.

This module is a TIER INTERFACE (tiered-oversight §4): every rule is
MODULE-LEVEL DATA (status sets, refusal codes, eligibility reasons), so
the contract is inspectable, not buried in branchy prose.

Why ``approved_human`` (the status choice, from the real schema)
─────────────────────────────────────────────────────────────────────────
``schema.proposal.Status`` has exactly two approved spellings:
``approved_auto`` (the arbiter's autonomous lane) and ``approved_human``
(a person decided). EDR v1 lives at the R1 rung — *a human opts every
dispatch in* (design §5.3; operator decision 2026-06-12) — so
``approve()`` writes ``approved_human``, always. ``approved_auto`` is
never produced by EDR v1; ``eligible_for_dispatch`` still accepts both
``approved_*`` statuses because the §5.1 bridge contract is stated over
"status ``approved_*``" and a future R2 lane must not require a
lifecycle rewrite.

Both approved statuses map to the ``pending/`` subdir
(``arbiter.store._STATUS_TO_SUBDIR``): the status FIELD is
authoritative, the subdir is a physical index, and the approve move is
an in-place rewrite handled by ``move_proposal`` — never move files
yourself.

The approve-refusal rules (each refusal raises ``ApprovalRefused``
carrying a machine-readable ``code``; NOTHING is mutated)
─────────────────────────────────────────────────────────────────────────
  REFUSAL_NOT_FOUND       — no proposal with that id in any subdir.
  REFUSAL_NOT_APPROVABLE  — status outside ``APPROVABLE_STATUSES``
                            (already approved / applied / archived /
                            snoozed / …). Approval is a one-way human
                            act on a *pending* item; re-approving or
                            resurrecting an archived verdict is not it.
  REFUSAL_HUMAN_ROUTED    — the triage verdict has ``agent_able`` ≠
                            True (or the machine-readable triage block
                            is missing/malformed, which we treat the
                            same — defensively human). **A human cannot
                            'approve' a human-routed item into the
                            agent lane; that's a triage re-run after
                            relabeling, not an approval.** The agent-
                            able verdict belongs to triage (design
                            §6.1: label + Proof: line on the issue);
                            the bridge's approve() is consent to
                            dispatch an ALREADY agent-able item, never
                            a reclassification surface.
  REFUSAL_NO_PROOF        — agent-able but no non-empty
                            ``proof_artifact``. Triage never produces
                            this shape (its agent-able verdict requires
                            a Proof: line), but approving it would
                            create a permanently un-dispatchable
                            approved item, so the gate refuses
                            defensively (§5.2: no proof, no loop).

Dispatch eligibility (what P1.3b is allowed to pick up)
─────────────────────────────────────────────────────────────────────────
ALL of, evaluated by the pure ``dispatch_eligibility()``:
  1. ``status`` ∈ ``ELIGIBLE_STATUSES`` (``approved_human`` |
     ``approved_auto``);
  2. ``provenance.signals["triage"]["agent_able"] is True``;
  3. ``proof_artifact`` is a non-empty string.
Missing/malformed triage block → NOT eligible; an *approved* proposal
failing 2–3 is the surprising case and is logged at WARNING with the
reason (the boring case — a pending proposal — is silently skipped).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

# The analyzer is the installed evolve-analyzer package (flat layout), so
# its subpackages import by their bare names — the same modules the shipped
# admin server and daemons load. Import, never fork.
import arbiter.state_machine as state_machine
import arbiter.store as arbiter_store
from schema.proposal import Proposal

log = logging.getLogger(__name__)

# ── DATA: status sets (the lifecycle contract; see module docstring) ─────────

# What approve() writes. R1 = a human approves each dispatch, so the
# human spelling — never approved_auto (the arbiter's autonomous lane).
APPROVED_STATUS = "approved_human"

# What approve() accepts as input. Triage emits status="pending"; every
# other status means the item already moved through a human/lifecycle
# decision that approve() must not override.
APPROVABLE_STATUSES: frozenset[str] = frozenset({"pending"})

# What eligible_for_dispatch() accepts — the §5.1 "status approved_*"
# contract. v1 only ever produces approved_human (see docstring).
ELIGIBLE_STATUSES: frozenset[str] = frozenset(
    {"approved_human", "approved_auto"}
)

# Actor recorded in the proposal's append-only history. Per
# arbiter.state_machine.transition's contract: "user" for human actions
# — approve() is exactly that (the R1 human gate).
APPROVAL_ACTOR = "user"
APPROVAL_REASON = "EDR bridge: human-gated dispatch approval (R1)"

# ── DATA: machine-readable refusal codes (ApprovalRefused.code) ──────────────

REFUSAL_NOT_FOUND = "proposal not found"
REFUSAL_NOT_APPROVABLE = "not in an approvable status"
REFUSAL_HUMAN_ROUTED = "human-routed: triage agent_able is not True"
REFUSAL_NO_PROOF = "no falsifiable proof_artifact on the triage verdict"

# ── DATA: machine-readable eligibility reasons (dispatch_eligibility) ────────

ELIGIBLE_OK = "eligible: approved + agent-able + proof artifact"
INELIGIBLE_STATUS = "status not approved_*"
INELIGIBLE_NO_TRIAGE = "missing/malformed triage block"
INELIGIBLE_HUMAN_ROUTED = "human-routed (triage agent_able is not True)"
INELIGIBLE_NO_PROOF = "missing/empty proof_artifact"


class ApprovalRefused(Exception):
    """``approve()`` refused — nothing was mutated.

    ``code`` is one of the ``REFUSAL_*`` constants (machine-readable;
    callers may branch on it); ``str(exc)`` carries the operator-facing
    detail.
    """

    def __init__(self, proposal_id: str, code: str, detail: str = ""):
        self.proposal_id = proposal_id
        self.code = code
        message = f"approve refused for {proposal_id!r}: {code}"
        if detail:
            message += f" — {detail}"
        super().__init__(message)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def triage_verdict(proposal: Proposal) -> dict[str, Any] | None:
    """The machine-readable triage block, or None when absent/malformed.

    The P1.2 contract: ``Proposal.provenance.signals["triage"]`` is a
    dict (see edr.triage.dev_issue's module docstring for the full
    shape). Anything else — missing key, non-dict value — returns None;
    callers treat None as defensively human-routed.
    """
    signals = getattr(proposal.provenance, "signals", None) or {}
    verdict = signals.get("triage")
    if not isinstance(verdict, dict):
        return None
    return verdict


def dispatch_eligibility(proposal: Proposal) -> tuple[bool, str]:
    """Pure eligibility verdict → ``(eligible, reason)``.

    ``reason`` is one of the ``ELIGIBLE_OK`` / ``INELIGIBLE_*`` module
    constants — inspectable data, so tests and P1.3b can assert on the
    exact branch taken.
    """
    if proposal.status not in ELIGIBLE_STATUSES:
        return False, INELIGIBLE_STATUS
    verdict = triage_verdict(proposal)
    if verdict is None:
        return False, INELIGIBLE_NO_TRIAGE
    if verdict.get("agent_able") is not True:
        return False, INELIGIBLE_HUMAN_ROUTED
    proof = verdict.get("proof_artifact")
    if not isinstance(proof, str) or not proof.strip():
        return False, INELIGIBLE_NO_PROOF
    return True, ELIGIBLE_OK


# ── The approval gate ────────────────────────────────────────────────────────


def approve(shared_dir: Path, proposal_id: str) -> Proposal:
    """Human-gated approval: pending → ``approved_human`` (R1).

    Runs the REAL arbiter lifecycle: ``state_machine.transition``
    (legal-edge check + append-only ``StatusTransition`` history entry,
    actor ``"user"``) then ``arbiter_store.move_proposal`` (status
    field authoritative; subdir routing — pending→pending here — is the
    store's job). Returns the updated Proposal.

    Refuses with ``ApprovalRefused`` and NO mutation per the
    refusal-rule table in the module docstring. In particular: a
    human-routed verdict cannot be approved into the agent lane —
    relabel the issue (``edr:agent-able`` + a ``Proof:`` line) and
    re-run triage instead; approval is consent to dispatch, not a
    reclassification surface.
    """
    shared_dir = Path(shared_dir)
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    if located is None:
        raise ApprovalRefused(proposal_id, REFUSAL_NOT_FOUND)
    proposal, _path, subdir = located

    if proposal.status not in APPROVABLE_STATUSES:
        raise ApprovalRefused(
            proposal_id,
            REFUSAL_NOT_APPROVABLE,
            detail=(
                f"status is {proposal.status!r} (subdir {subdir}/); "
                f"approvable: {sorted(APPROVABLE_STATUSES)}"
            ),
        )

    verdict = triage_verdict(proposal)
    if verdict is None or verdict.get("agent_able") is not True:
        raise ApprovalRefused(
            proposal_id,
            REFUSAL_HUMAN_ROUTED,
            detail=(
                "a human cannot 'approve' a human-routed item into the "
                "agent lane; relabel the issue (edr:agent-able + a "
                "Proof: line) and re-run triage — that's a triage "
                "re-run, not an approval"
                + (
                    f" (triage reason: {verdict.get('reason', '')})"
                    if verdict is not None
                    else " (no machine-readable triage block)"
                )
            ),
        )

    proof = verdict.get("proof_artifact")
    if not isinstance(proof, str) or not proof.strip():
        raise ApprovalRefused(
            proposal_id,
            REFUSAL_NO_PROOF,
            detail=(
                "agent-able but no falsifiable proof artifact — "
                "approving would create a permanently un-dispatchable "
                "item (design §5.2: no proof, no loop)"
            ),
        )

    # All gates passed — run the real lifecycle. transition() mutates
    # the in-memory copy (status + history); move_proposal persists it
    # under the store lock. expected_status is the CAS guard for the
    # cross-subdir case; approved_human maps to the same pending/
    # subdir, so this is an in-place rewrite.
    state_machine.transition(
        proposal, APPROVED_STATUS, actor=APPROVAL_ACTOR, reason=APPROVAL_REASON
    )
    arbiter_store.move_proposal(
        proposal, shared_dir, from_subdir=subdir, expected_status="pending"
    )
    log.info("bridge: approved %s for dispatch (R1 human gate)", proposal_id)
    return proposal


# ── The dispatch-eligibility filter ──────────────────────────────────────────


def eligible_for_dispatch(shared_dir: Path) -> list[Proposal]:
    """Every Proposal P1.3b may dispatch, in stable (id-sorted) order.

    Scans the ``pending/`` subdir only — that is where ``approved_*``
    statuses physically live (``arbiter.store._STATUS_TO_SUBDIR``);
    applied/archived/snoozed items are out of the dispatch lifecycle by
    construction. Each candidate runs through the pure
    ``dispatch_eligibility()``; an *approved* proposal that fails the
    triage-block checks is the surprising/defensive case and is logged
    at WARNING with the machine-readable reason.
    """
    eligible: list[Proposal] = []
    for proposal in arbiter_store.iter_proposals(
        Path(shared_dir), subdirs=("pending",)
    ):
        ok, reason = dispatch_eligibility(proposal)
        if ok:
            eligible.append(proposal)
        elif proposal.status in ELIGIBLE_STATUSES:
            log.warning(
                "bridge: %s is approved but NOT dispatchable: %s",
                proposal.id,
                reason,
            )
    return sorted(eligible, key=lambda proposal: proposal.id)

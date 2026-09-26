"""proposal_synthesizer.promote — Mechanical CandidateProposal → Proposal.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §9 Phase 2.

In Phase 2 we don't yet have the LLM synthesizer. Passed candidates
that already carry a complete ``draft_action`` get promoted to
Proposals mechanically — copy the draft fields into a Proposal,
write to the proposal store, and the candidate's job is done.

Substrate aggregates (``aggregation == "substrate"``) intentionally
don't have a ``draft_action`` — the synthesizer is responsible for
drafting a substrate-level action that the per-bot drafts can't
capture. Until Phase 3+ ships that synthesizer, substrate aggregates
sit in ``candidates/synthesizing/`` and are surfaced in the UI as
"awaiting synthesis."
"""

from __future__ import annotations

import logging
from pathlib import Path

from arbiter import store as proposal_store
from arbiter.ingest import _refresh_existing
from arbiter.track_record import bump_proposals_emitted
from schema.candidate_proposal import CandidateProposal
from schema.proposal import Proposal, new_proposal_id


log = logging.getLogger(__name__)


def is_promotable(candidate: CandidateProposal) -> bool:
    """A candidate is promotable when it has everything a Proposal
    record requires. Substrate aggregates intentionally lack
    draft_action; they wait for the LLM synthesizer."""
    if candidate.aggregation == "substrate":
        return False
    if candidate.draft_action is None:
        return False
    if candidate.draft_risk_tag is None:
        return False
    if not candidate.draft_problem:
        return False
    return True


def _build_proposal_from_candidate(candidate: CandidateProposal) -> Proposal:
    """Mechanical projection of a candidate's draft fields into a Proposal."""
    headline = candidate.draft_headline or candidate.draft_problem[:120]
    return Proposal(
        id=new_proposal_id(),
        bot_id=candidate.bot_id,
        generator_id=candidate.generator_id,
        dimension=candidate.dimension,
        trigger_observations=list(candidate.trigger_observations),
        provenance=candidate.provenance,
        problem=candidate.draft_problem,
        action=candidate.draft_action,
        claim=candidate.draft_claim,
        risk_tag=candidate.draft_risk_tag,
        approval_audience=candidate.draft_approval_audience,
        urgency=candidate.draft_urgency,
        admin_surface_summary=headline[:120],
        motivating_signals=list(candidate.motivating_signals),
        status="pending",
    )


def promote_to_proposal(
    candidate: CandidateProposal,
    shared_dir: Path,
) -> Proposal | None:
    """Build a Proposal from a passed candidate and write it to
    ``proposals/pending/``.

    Returns the created Proposal, or None if the candidate isn't
    promotable (substrate aggregate, missing fields, or write error).
    Phase 2 mechanical promotion: no LLM, no transformation — just
    copy the draft fields into a Proposal.

    Fingerprint dedup: if an open Proposal (pending/snoozed) already
    exists with the same fingerprint, the existing one is refreshed
    via the standard ingest helper and the candidate does not produce
    a new Proposal. This prevents the queue from accumulating
    duplicates when an operator hasn't yet acted on a prior
    occurrence.
    """
    if not is_promotable(candidate):
        return None
    try:
        proposal = _build_proposal_from_candidate(candidate)
        # check-then-write under the store lock (7.1 Phase A) so a
        # concurrent emitter can't slip a duplicate in between.
        with proposal_store.locked(shared_dir):
            existing = proposal_store.find_open_duplicate(proposal, shared_dir)
            if existing is not None:
                # Refresh the open proposal in-place rather than writing a
                # duplicate. The refresh helper updates prose + motivating
                # signals and records the re-detection on the existing
                # history.
                _refresh_existing(existing, incoming=proposal, actor="proposal_synthesizer")
                located = proposal_store.find_proposal(shared_dir, existing.id)
                if located is not None:
                    _, _, src_subdir = located
                    proposal_store.move_proposal(
                        existing, shared_dir, from_subdir=src_subdir
                    )
                else:
                    proposal_store.write_proposal(existing, shared_dir)
                return existing
            proposal_store.write_proposal(proposal, shared_dir)
        bump_proposals_emitted(shared_dir, proposal.generator_id)
        return proposal
    except Exception:
        log.warning(
            "promote_to_proposal failed for candidate %s",
            candidate.id,
            exc_info=True,
        )
        return None

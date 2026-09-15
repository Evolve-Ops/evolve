"""schema.candidate_proposal — CandidateProposal dataclass.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §3.

A CandidateProposal is a structured observation that feeds the
proposal synthesizer. It is NOT a draft Proposal awaiting promotion —
the relationship between candidates and proposals is many-to-many.
The synthesizer may collapse N candidates into one Proposal, split
one candidate into two, or produce nothing at all.

The candidate carries the data a synthesizer needs to reason:
  - signal lineage (`motivating_signals`)
  - an aggregation fingerprint (for the gate's dedup pass)
  - a magnitude estimate (what's at stake, in unit-specific terms)
  - a draft of what an action *might* look like (action, claim,
    risk_tag, urgency, approval_audience) — the synthesizer may
    revise any of these.

State lives under ``{shared_dir}/candidates/{subdir}/<id>.json`` where
subdir ∈ {pending, synthesizing, watchlist}. Dropped candidates write
to ``candidates/dropped/<YYYY-MM-DD>.jsonl`` instead — they are not
preserved as individual files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from evolve_util import now_iso_offset as _utc_now_iso

from schema.proposal import (
    Action,
    ApprovalAudience,
    Claim,
    RiskTag,
    Urgency,
    action_from_dict,
    action_to_dict,
)
from schema.provenance import Provenance


CANDIDATE_SCHEMA_VERSION = 1


# State a candidate occupies on disk.
#
#  - ``pending``      — generator emitted, gate has not yet processed
#  - ``synthesizing`` — passed the gate, awaiting synthesizer
#  - ``watchlist``    — gate (or synthesizer) demoted; tracking, no
#                       operator action
CandidateState = Literal["pending", "synthesizing", "watchlist"]


# How the gate aggregated this candidate at the moment it passed
# through. ``none`` means no aggregation; ``bot_pattern`` means K
# occurrences on the same bot folded together; ``substrate`` means the
# same condition on ≥3 bots collapsed into one substrate-level
# candidate.
AggregationKind = Literal["none", "bot_pattern", "substrate"]


def new_candidate_id() -> str:
    """Generate a fresh CandidateProposal id."""
    return str(uuid4())


@dataclass
class Magnitude:
    """What's at stake for this candidate.

    ``unit`` is a free-form string the gate matches against its
    per-unit floor table (see proposal_synthesizer.config). Examples:
    ``usd/week`` (saved), ``usd/session`` (outlier), ``pct/share``
    (background or tier dominance), ``kb`` (context bloat), or
    ``sessions/week`` (frequency).

    The synthesizer is allowed to re-estimate magnitude after
    investigation; the value here is the generator's best initial
    estimate.
    """

    unit: str
    value: float

    def to_dict(self) -> dict:
        return {"unit": self.unit, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict) -> "Magnitude":
        return cls(unit=str(data["unit"]), value=float(data["value"]))


@dataclass
class CandidateProposal:
    """A structured observation fed to the proposal synthesizer."""

    # ── Identity ────────────────────────────────────────────────────────────
    id: str
    bot_id: str
    state: CandidateState = "pending"

    # ── Provenance ──────────────────────────────────────────────────────────
    generator_id: str = ""
    dimension: str = ""
    variant: str = ""  # e.g. "cron_wakes_agent", "heartbeat_no_model_override"
    trigger_observations: list[str] = field(default_factory=list)
    provenance: Provenance | None = None

    # ── Signal lineage ──────────────────────────────────────────────────────
    motivating_signals: list[str] = field(default_factory=list)

    # ── Aggregation ─────────────────────────────────────────────────────────
    # Stable per (generator_id, variant, bot_id) by default. Substrate
    # variants may use a non-bot fingerprint so cross-bot aggregation
    # collapses to one candidate.
    fingerprint: str = ""
    aggregation: AggregationKind = "none"
    aggregated_from: list[str] = field(default_factory=list)  # Candidate IDs

    # ── Magnitude ───────────────────────────────────────────────────────────
    magnitude: Magnitude | None = None

    # ── Drafted Proposal payload ────────────────────────────────────────────
    # The synthesizer may revise any of these; the draft is the
    # generator's best guess at what a Proposal might look like.
    draft_problem: str = ""
    draft_headline: str = ""  # action-led, ≤120 chars
    draft_action: Action | None = None
    draft_claim: Claim | None = None
    draft_risk_tag: RiskTag | None = None
    draft_urgency: Urgency = "improvement"
    draft_approval_audience: ApprovalAudience = "none"

    # ── Bookkeeping ─────────────────────────────────────────────────────────
    confidence: float = 0.0  # 0-1, generator's self-assessment
    created_at: str = field(default_factory=_utc_now_iso)

    # ── Synthesizer annotations ─────────────────────────────────────────────
    # When the synthesizer (Phase 3+) writes a candidate to watchlist or
    # parks it in synthesizing/, it can leave a one-paragraph note
    # explaining why and what would promote it. Empty for candidates
    # that never went through the synthesizer.
    synthesizer_note: str = ""

    # ── Misc ────────────────────────────────────────────────────────────────
    schema_version: int = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("CandidateProposal.id must be non-empty")
        if not self.bot_id:
            raise ValueError("CandidateProposal.bot_id must be non-empty")
        if not self.generator_id:
            raise ValueError("CandidateProposal.generator_id must be non-empty")
        if not self.variant:
            raise ValueError("CandidateProposal.variant must be non-empty")
        if not self.fingerprint:
            self.fingerprint = self._default_fingerprint()
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(
                f"CandidateProposal.confidence must be in [0,1], got {self.confidence}"
            )

    def _default_fingerprint(self) -> str:
        return f"{self.generator_id}:{self.variant}:{self.bot_id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "bot_id": self.bot_id,
            "state": self.state,
            "generator_id": self.generator_id,
            "dimension": self.dimension,
            "variant": self.variant,
            "trigger_observations": list(self.trigger_observations),
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "motivating_signals": list(self.motivating_signals),
            "fingerprint": self.fingerprint,
            "aggregation": self.aggregation,
            "aggregated_from": list(self.aggregated_from),
            "magnitude": self.magnitude.to_dict() if self.magnitude else None,
            "draft_problem": self.draft_problem,
            "draft_headline": self.draft_headline,
            "draft_action": (
                action_to_dict(self.draft_action) if self.draft_action else None
            ),
            "draft_claim": self.draft_claim.to_dict() if self.draft_claim else None,
            "draft_risk_tag": (
                self.draft_risk_tag.to_dict() if self.draft_risk_tag else None
            ),
            "draft_urgency": self.draft_urgency,
            "draft_approval_audience": self.draft_approval_audience,
            "confidence": self.confidence,
            "synthesizer_note": self.synthesizer_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateProposal":
        prov_raw = data.get("provenance")
        action_raw = data.get("draft_action")
        claim_raw = data.get("draft_claim")
        risk_raw = data.get("draft_risk_tag")
        mag_raw = data.get("magnitude")
        return cls(
            id=data["id"],
            schema_version=int(data.get("schema_version", CANDIDATE_SCHEMA_VERSION)),
            created_at=data.get("created_at", _utc_now_iso()),
            bot_id=data["bot_id"],
            state=data.get("state", "pending"),
            generator_id=data["generator_id"],
            dimension=data.get("dimension", ""),
            variant=data["variant"],
            trigger_observations=list(data.get("trigger_observations") or []),
            provenance=Provenance.from_dict(prov_raw) if prov_raw else None,
            motivating_signals=list(data.get("motivating_signals") or []),
            fingerprint=data.get("fingerprint", ""),
            aggregation=data.get("aggregation", "none"),
            aggregated_from=list(data.get("aggregated_from") or []),
            magnitude=Magnitude.from_dict(mag_raw) if mag_raw else None,
            draft_problem=data.get("draft_problem", ""),
            draft_headline=data.get("draft_headline", ""),
            draft_action=action_from_dict(action_raw) if action_raw else None,
            draft_claim=Claim.from_dict(claim_raw) if claim_raw else None,
            draft_risk_tag=RiskTag.from_dict(risk_raw) if risk_raw else None,
            draft_urgency=data.get("draft_urgency", "improvement"),
            draft_approval_audience=data.get("draft_approval_audience", "none"),
            confidence=float(data.get("confidence", 0.0)),
            synthesizer_note=str(data.get("synthesizer_note", "")),
        )

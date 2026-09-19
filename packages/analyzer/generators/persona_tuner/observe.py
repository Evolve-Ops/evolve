"""generators.persona_tuner.observe — Mood-pattern detection + pitch proposals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from observations.access import ObservationWindow
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.observation import ClusterSpec
from schema.proposal import (
    AgentsAppend,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "persona_tuner"
DIMENSION = "voice_fit"


# Phase A.5 dismiss signature — per-cluster so dismissing tone advice
# for (noun_a, verb_a) doesn't suppress advice for (noun_b, verb_b).
def dismiss_signature_for_cluster(noun: str, verb: str) -> str:
    return f"persona_tuner:tone_frustration:{noun}:{verb}"


@dataclass
class PersonaTunerContext:
    bot_id: str
    window: ObservationWindow
    frustration_mood_threshold: float = 0.25
    min_cluster_engagement: int = 10
    max_proposals_per_run: int = 1
    audience: str = "bot_primary_user"
    # Optional — when set, observe() also emits CandidateProposals
    # to ``candidates/pending/`` for the substantiveness gate.
    shared_dir: Path | None = None


def observe(ctx: PersonaTunerContext) -> list[Proposal]:
    """Find (noun × verb) cells with elevated frustration share."""
    view = ctx.window.clusters(ClusterSpec(group_by=["noun", "verb"]))

    # Collect frustration-heavy cells
    frustrated_cells = []
    for cell in view.cells:
        if cell.engagement_total < ctx.min_cluster_engagement:
            continue
        if cell.frustrated_mood_share >= ctx.frustration_mood_threshold:
            frustrated_cells.append(cell)

    if not frustrated_cells:
        return []

    # Sort by frustration share descending; emit the worst offenders
    frustrated_cells.sort(key=lambda c: -c.frustrated_mood_share)
    frustrated_cells = frustrated_cells[: ctx.max_proposals_per_run]

    # Phase A.5 — preload suppressions for this bot.
    from arbiter.dismissals import preload_suppressed_signatures
    suppressed = preload_suppressed_signatures(ctx.shared_dir, ctx.bot_id)

    for cell in frustrated_cells:
        p = _build_tone_proposal(cell, ctx)
        # Phase A.5 dismiss gate (per (noun, verb) cluster).
        if p.dismiss_signature in suppressed:
            continue
        # Phase 6c cutover: emit as CandidateProposal only; observe()
        # returns []. Tests inspect candidates/pending/.
        emit_from_proposal(
            p,
            variant="tone_frustration",
            magnitude=Magnitude(
                unit="pct/share", value=float(cell.frustrated_mood_share)
            ),
            shared_dir=ctx.shared_dir,
        )
    return []


def _build_tone_proposal(cell, ctx: PersonaTunerContext) -> Proposal:
    noun = str(cell.key[0]) if len(cell.key) >= 1 else "unknown"
    verb = str(cell.key[1]) if len(cell.key) >= 2 else "exploring"
    share_pct = int(round(cell.frustrated_mood_share * 100))

    guidance = (
        f"## Tone note for {noun} interactions\n\n"
        f"Observed {share_pct}% frustrated mood across recent "
        f"{verb} sessions in the {noun} domain. Consider:\n\n"
        "- Asking a clarifying question early rather than making assumptions\n"
        f"- Checking in after the first step in a {verb} task\n"
        "- Keeping responses concise when the user shows signs of frustration\n"
    )

    summary = (
        f"Tone pattern: {share_pct}% frustration in ({noun}, {verb}) — "
        "suggest AGENTS.md guideline"
    )

    # Phase C-8 operator-first content (Tier 1 — auto-apply AgentsAppend)
    operator_summary = (
        f"Recent {verb} conversations about {noun} on this bot have "
        f"shown frustration {share_pct}% of the time. Adding a short "
        f"tone note to AGENTS.md ({verb}-clarify, check-in early, "
        f"stay concise) usually shifts the pattern. Auto-revert if "
        f"the rate doesn't improve."
    )
    operator_explanation = (
        f"AGENTS.md is the bot's standing-instructions doc — guidance "
        f"the model reads at the start of every turn. Small tone "
        f"changes here (\"ask a clarifying question before assuming\", "
        f"\"check in after the first step\") often shift the feel of "
        f"a conversation cluster without changing what the bot can "
        f"do.\n\n"
        f"Diagnosis. Over a recent window, {share_pct}% of {verb} "
        f"sessions about {noun} on this bot showed frustration "
        f"indicators (terse messages, repeats, explicit complaints). "
        f"That's above the threshold for this cluster.\n\n"
        f"What this changes. Appends a tone note to AGENTS.md "
        f"specifically scoped to {noun}/{verb} interactions. The bot "
        f"behaves the same on other clusters; the note shapes only "
        f"the pattern we flagged.\n\n"
        f"What could go wrong. If the frustration was actually about "
        f"the bot getting something factually wrong (not tone), a "
        f"tone change won't help and will look like the bot ducking "
        f"the issue. Watch the cluster's frustrated share over the "
        f"next two weeks — auto-revert kicks in if it doesn't drop."
    )

    # Requires ≥ 3 supporting observation IDs per charter; synthesize
    # from the cluster aggregate (we cite the cluster key as a
    # pseudo-observation in L6; real tuple IDs come when the Persona
    # Tuner has transcript access).
    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"cluster:{noun}:{verb}",
            f"frustrated_share:{share_pct}",
            f"engagement:{cell.engagement_total}",
        ],
        provenance=Provenance(
            technique="persona_tuner.frustration_pattern",
            signals={
                "noun": noun,
                "verb": verb,
                "frustrated_share": cell.frustrated_mood_share,
                "tuple_count": cell.tuple_count,
                "engagement_total": cell.engagement_total,
            },
            confidence=0.75,
        ),
        problem=summary,
        action=AgentsAppend(
            bot_id=ctx.bot_id,
            section=f"Tone — {noun}",
            content=guidance,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["agents_doc"],
        ),
        # Claim: frustration share in this cluster should drop over the
        # next 2 weeks.
        claim=None,  # qualitative — the applier is a no-op style change
        approval_audience=ctx.audience,  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=summary[:120],
        conversational_pitch=(
            f"I notice our {verb} conversations in {noun} have felt "
            "frustrating lately. Want me to adjust how I handle these?"
        ),
        # ── Phase C-8 operator-first content (Tier 1 — auto-apply) ──────
        summary=operator_summary,
        explanation=operator_explanation,
        action_label="Add tone note to AGENTS.md",
        manual_path=f"Bots → {ctx.bot_id} → AGENTS.md",
        dismiss_signature=dismiss_signature_for_cluster(noun, verb),
        dismiss_scope="kind",
    )

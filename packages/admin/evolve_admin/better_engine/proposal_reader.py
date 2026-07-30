"""better_engine.proposal_reader — Arbiter bridge adapter.

Reads v2 Proposal objects emitted by the L1-L6 arbiter from
``{shared_dir}/proposals/{pending,snoozed}/*.json`` and materializes
them as ``Recommendation`` objects that the existing Better Engine UI +
``evo`` keyword surface already know how to display.

Spec: docs/archive/specs/spec-better-engine-arbiter-bridge-2026-04-18.md
Upgrade plan: docs/archive/specs/spec-ui-rationalization-2026-04-18.md (Phase 1)
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

from .model import Recommendation, now_iso

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Dimension → Recommendation.type mapping (§4.4 of the bridge spec)
# ─────────────────────────────────────────────────────────────────────────────

DIMENSION_TO_TYPE: dict[str, str] = {
    "substrate_health": "operational",
    "cost": "cost",
    "safety": "security",
    "utility": "app_quality",
    "capability_growth": "app_quality",
    "efficiency": "app_quality",
    "voice_fit": "app_quality",
    "hygiene": "app_quality",
    "meta_health": "operational",
}


# Urgency → base priority score (the pre-referee ladder in the bridge spec §6)
URGENCY_SCORE: dict[str, int] = {
    "security_critical": 1000,
    "operational_urgent": 700,
    "cost_alert": 500,
    "substrate_warn": 300,
    "improvement": 200,
    "hygiene": 100,
    "whimsy": 10,
}


# Action kind → default action_label text
ACTION_LABELS: dict[str, str] = {
    "ConfigPatch": "Apply fix",
    "ManifestUpdate": "Update manifest",
    "MemoryCurate": "Curate memory",
    "AgentsAppend": "Add note",
    "InstallApp": "Install",
    "WorkflowInstruction": "Add workflow",
    "TierAdjustment": "Adjust tier",
    "Investigation": "Investigate",
    "DeprecateApp": "Deprecate",
    "SoulEdit": "Review edit",
    "ThrottleGenerator": "Throttle",
    "PauseGenerator": "Pause",
    "VetoAnnotation": "Review",
}


# ─────────────────────────────────────────────────────────────────────────────
# Main adapter
# ─────────────────────────────────────────────────────────────────────────────


class ProposalReaderAdapter:
    """Better Engine adapter: Arbiter proposals → Recommendations."""

    source_name = "proposal_reader"

    def generate(self, shared_dir: Path, network: dict) -> list[Recommendation]:
        from arbiter.store import iter_proposals  # deferred import

        recs: list[Recommendation] = []
        try:
            for proposal in iter_proposals(
                shared_dir, subdirs=("pending", "snoozed")
            ):
                try:
                    rec = proposal_to_recommendation(proposal)
                except Exception as exc:  # noqa: BLE001 — one bad proposal shouldn't sink the batch
                    _log.warning(
                        "ProposalReaderAdapter: skipping proposal %s due to mapping error: %s",
                        getattr(proposal, "id", "?"),
                        exc,
                    )
                    continue
                if rec is not None:
                    recs.append(rec)
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "ProposalReaderAdapter: iter_proposals raised: %s", exc, exc_info=True
            )
            return []
        return recs


# ─────────────────────────────────────────────────────────────────────────────
# Proposal → Recommendation mapping (§4.3 of the bridge spec)
# ─────────────────────────────────────────────────────────────────────────────


def proposal_to_recommendation(proposal) -> Optional[Recommendation]:
    """Convert a v2 Proposal to a Recommendation.

    Skips proposals with ``approval_audience == "none"`` — those are the
    autonomous path and nothing for the UI to show.
    """
    audience = proposal.approval_audience
    if audience == "none":
        return None

    # Scope / scope_id routing
    scope, scope_id = _scope_for_audience(audience, proposal.bot_id)

    # Action kind + label
    action_kind = getattr(proposal.action, "kind", type(proposal.action).__name__)
    action_label = ACTION_LABELS.get(action_kind, "Review")

    # Title / detail / context
    title = proposal.admin_surface_summary or proposal.problem[:80]
    detail = proposal.problem or proposal.admin_surface_summary or ""
    context = _render_context(proposal)

    # Scoring
    priority_score = _priority_score(proposal)

    # Tags
    tags = _tags_for(proposal)

    # dedup key — stable across reads
    dedup_key = f"arbiter::{proposal.generator_id}::{proposal.id}"
    rec_id = f"rec_prop_{proposal.id}"

    # Status mapping — proposal lifecycle → Recommendation lifecycle
    status = _normalize_status(proposal.status)

    # Member bot surface
    member_bot_title, member_bot_detail = _member_bot_surface(proposal, audience)

    # Claim + revert summary
    claim_dict = proposal.claim.to_dict() if proposal.claim else None
    revert_summary = _revert_summary(proposal)

    # Guardian annotations + conflicts (serialized)
    guardian_annotations = [a.to_dict() for a in proposal.guardian_annotations]
    conflicts_with = [c.to_dict() for c in proposal.conflicts_with]

    # Verify status (Phase 1: surface "pending" if applied + has claim; else "unknown").
    # The verify daemon updates proposal status on the file itself when it
    # confirms/refutes; the Phase 2 work threads a richer verify record
    # through proposal.history. For now: derive from proposal.status.
    verify_status = _verify_status_from_proposal(proposal)

    return Recommendation(
        id=rec_id,
        dedup_key=dedup_key,
        type=DIMENSION_TO_TYPE.get(proposal.dimension, "app_quality"),
        source=f"generator:{proposal.generator_id}",
        scope=scope,
        scope_id=scope_id,
        title=title,
        detail=detail,
        context=context,
        action_label=action_label,
        action="arbiter_act",
        action_args={"proposal_id": proposal.id},
        bot_executable=(audience in ("bot_primary_user", "both")),
        bridge_strategy=None,
        accept_label=action_label,
        member_bot_title=member_bot_title,
        member_bot_detail=member_bot_detail,
        priority_score=priority_score,
        priority_components={
            "urgency_ladder": URGENCY_SCORE.get(proposal.urgency, 0),
            "age_tiebreak": _age_tiebreak(proposal),
            "annotation_boost": _annotation_boost(proposal),
        },
        learning_weight=1.0,
        tags=tags,
        status=status,
        snooze_count=0,
        snooze_until=proposal.snoozed_until,
        created_at=proposal.created_at,
        updated_at=_latest_history_at(proposal) or proposal.created_at,
        resolved_at=None,
        expires_at=None,
        feedback_signal=None,
        feedback_reason="",
        source_ref={"proposal_id": proposal.id, "bot_id": proposal.bot_id},
        # L1-L6 enrichment
        dimension=proposal.dimension,
        urgency=proposal.urgency,
        adjacency_type=proposal.adjacency_type,
        generator_id=proposal.generator_id,
        action_kind=action_kind,
        approval_audience=audience,
        guardian_annotations=guardian_annotations,
        conflicts_with=conflicts_with,
        claim=claim_dict,
        revert_plan_summary=revert_summary,
        verify_status=verify_status,
        verify_detail=None,
        score_breakdown=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _scope_for_audience(audience: str, bot_id: str) -> tuple[str, str]:
    if audience == "pod_operator":
        return "admin", "admin"
    # bot_primary_user or both — anchor to the bot
    return "bot", bot_id


def _priority_score(proposal) -> int:
    base = URGENCY_SCORE.get(proposal.urgency, 0)
    annotation_boost = _annotation_boost(proposal)
    tiebreak = _age_tiebreak(proposal)
    # Clamp to 0-100 for UI slider compatibility if needed, but leave
    # as-is so the bridge spec's 10-1000 ladder works when the engine
    # sorts. The portfolio layer normalizes.
    return base + annotation_boost + tiebreak


def _annotation_boost(proposal) -> int:
    boost = 0
    for ann in proposal.guardian_annotations:
        if ann.severity == "critical":
            boost += 20
        elif ann.severity == "high":
            boost += 10
        elif ann.severity == "medium":
            boost += 3
    return boost


def _age_tiebreak(proposal) -> int:
    """Newer wins within an urgency tier — a tiny bonus for freshness."""
    try:
        from datetime import datetime, timezone

        created = datetime.fromisoformat(
            proposal.created_at.replace("Z", "+00:00")
        )
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_hours < 24:
            return 3
        if age_hours < 72:
            return 1
        return 0
    except Exception:  # noqa: BLE001
        return 0


def _tags_for(proposal) -> list[str]:
    tags: list[str] = []
    if proposal.dimension:
        tags.append(f"dimension:{proposal.dimension}")
    if proposal.urgency:
        tags.append(f"urgency:{proposal.urgency}")
    if proposal.adjacency_type:
        tags.append(f"adjacency:{proposal.adjacency_type}")
    for t in proposal.risk_tag.touches:
        tags.append(f"touches:{t}")
    for ann in proposal.guardian_annotations:
        tags.append(f"warn:{ann.guardian_id}:{ann.severity}")
    if proposal.conflicts_with:
        tags.append(f"conflict:{len(proposal.conflicts_with)}")
    return tags


def _render_context(proposal) -> str:
    """One-paragraph summary combining trigger observations + annotations."""
    parts: list[str] = []
    for ann in proposal.guardian_annotations:
        parts.append(f"{ann.guardian_id} ({ann.severity}): {ann.reason}")
    if proposal.trigger_observations:
        n = len(proposal.trigger_observations)
        parts.append(f"Based on {n} observation{'s' if n != 1 else ''}.")
    signals = proposal.provenance.signals if proposal.provenance else {}
    if signals:
        # Keep the summary short; only show first couple of keys.
        items = list(signals.items())[:3]
        rendered = ", ".join(f"{k}={v}" for k, v in items)
        parts.append(f"Signals: {rendered}")
    return " ".join(parts)


def _normalize_status(proposal_status: str) -> str:
    """Map Proposal.status → Recommendation.status vocabulary.

    Recommendation uses: pending / snoozed / resolved / dismissed.
    Proposal uses a much larger vocabulary — collapse for the UI.
    """
    mapping = {
        "draft": "pending",
        "pending": "pending",
        "approved_auto": "pending",  # still visible until applier finishes
        "approved_human": "pending",
        "applied": "pending",  # still visible; verify daemon hasn't decided
        "succeeded": "resolved",
        "failed_reverted": "resolved",
        "failed_flagged": "pending",  # needs attention
        "failed_revert_failed": "pending",
        "rejected": "dismissed",
        "dismissed": "dismissed",
        "snoozed": "snoozed",
        "superseded": "dismissed",
    }
    return mapping.get(proposal_status, "pending")


def _member_bot_surface(proposal, audience: str):
    """Render (title, detail) for the evo keyword surface.

    Phase 4 polish: the generator-written ``conversational_pitch`` remains
    the spine of the message, but we prepend a short dimension word to the
    title when it's natural, weave guardian concerns into the detail, and
    add an adjacency framing line for Adjacency Explorer proposals. All
    phrasing stays in end-user tone — no admin jargon, no reference to
    dashboards, severities, or the RSI system.
    """
    if audience not in ("bot_primary_user", "both"):
        return None, None
    pitch = proposal.conversational_pitch
    if not pitch:
        return None, None

    title = _member_bot_title(proposal, pitch)
    detail = _member_bot_detail(proposal, pitch)
    return title, detail


# Dimensions whose names make for readable plain-English prefixes on a
# member-bot title. Others are too jargon-y ("substrate_health",
# "capability_growth") — leave them out of the title entirely.
_TITLE_DIMENSION_WORDS: dict[str, str] = {
    "cost": "cost",
    "safety": "safety",
    "utility": "day-to-day",
    "voice_fit": "tone",
    "efficiency": "efficiency",
}


def _member_bot_title(proposal, pitch: str) -> str:
    # First sentence of the pitch, capped at 60 chars.
    first_sentence = pitch.split(". ")[0].strip()
    if len(first_sentence) > 60:
        first_sentence = first_sentence[:60].rstrip() + "…"
    dim_word = _TITLE_DIMENSION_WORDS.get(proposal.dimension or "")
    # Don't double-tag if the generator already used the dimension word.
    if dim_word and dim_word.lower() not in first_sentence.lower():
        return f"{dim_word}: {first_sentence}"
    return first_sentence


# Natural-language translations of adjacency_type, rendered as a short
# framing line before the pitch's body for Adjacency Explorer proposals.
_ADJACENCY_FRAMING: dict[str, str] = {
    "extend_same_cell": "This is a small extension of what I already do.",
    "adjacent_noun": "This is close to what I do today — just a new area.",
    "adjacent_verb": "This is close to what I do today — a new way of helping.",
    "chain_completion": "This is a step further than what I do today — it finishes a flow.",
}


def _member_bot_detail(proposal, pitch: str) -> str:
    parts: list[str] = []

    # Adjacency framing leads so the user knows how "new" this feels
    # before reading the ask.
    if proposal.adjacency_type:
        framing = _ADJACENCY_FRAMING.get(proposal.adjacency_type)
        if framing and framing.lower() not in pitch.lower():
            parts.append(framing)

    parts.append(pitch)

    # Guardian concerns go after the pitch so the ask is clear first,
    # then the caveat. We pick the strongest annotation and phrase it
    # conversationally — no "Security Warden (severity: high)" jargon.
    concern = _guardian_concern_line(proposal)
    if concern:
        parts.append(concern)

    return " ".join(parts)


def _guardian_concern_line(proposal) -> Optional[str]:
    """Pick the strongest guardian annotation and render it in plain English.

    Returns None if there are no annotations worth surfacing.
    """
    if not proposal.guardian_annotations:
        return None
    # Severity ordering: critical > high > medium > low. Low annotations
    # don't warrant a line in the member-bot surface.
    severity_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    strongest = max(
        proposal.guardian_annotations,
        key=lambda a: severity_rank.get(a.severity, -1),
        default=None,
    )
    if strongest is None or severity_rank.get(strongest.severity, -1) < 1:
        return None
    prefix = "Heads up:" if strongest.severity in ("medium",) else "One concern:"
    # The guardian's `reason` field is meant to be human-readable; use it.
    return f"{prefix} {strongest.reason.rstrip('.')}."


def _revert_summary(proposal) -> Optional[str]:
    rp = proposal.revert_on_failure
    if rp is None:
        return None
    # Human-readable one-liner derived from the revert action kind.
    try:
        kind = getattr(rp.revert_action, "kind", type(rp.revert_action).__name__)
    except Exception:  # noqa: BLE001
        kind = "unknown"
    return f"If this fails, undo via {kind}."


def _verify_status_from_proposal(proposal) -> str:
    status = proposal.status
    if status in ("applied",):
        return "pending"
    if status == "succeeded":
        return "confirmed"
    if status in ("failed_reverted", "failed_flagged", "failed_revert_failed"):
        return "refuted"
    return "unknown"


def _latest_history_at(proposal) -> Optional[str]:
    if not proposal.history:
        return None
    return proposal.history[-1].at


# ─────────────────────────────────────────────────────────────────────────────
# Module-level convenience wrapper
# ─────────────────────────────────────────────────────────────────────────────


def generate(shared_dir: Path, network: dict) -> list[Recommendation]:
    """Module-level entry point; delegates to ProposalReaderAdapter.generate."""
    return ProposalReaderAdapter().generate(shared_dir, network)

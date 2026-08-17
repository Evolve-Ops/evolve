"""
better_engine/model.py — Recommendation dataclass (Tier 0 data layer).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from evolve_util import now_iso_micro as now_iso  # re-exported (engine, hints, …)


@dataclass
class Recommendation:
    # ── Identity ──────────────────────────────────────────────────────────────
    id: str                          # "rec_{created_epoch}_{dedup_key_hash}"
    dedup_key: str                   # stable, deterministic — "{source}::{scope_id}::{discriminator}"

    # ── Classification ────────────────────────────────────────────────────────
    type: str                        # operational | security | cost | app_quality |
                                     # onboarding | whimsy
    source: str                      # health_check | security_review | spend_alert |
                                     # onboarding | whimsy | proposal_reader
                                     # (Pipeline-unification arc retired the
                                     # compliance_scan / scoreboard / llm_suggestion
                                     # sources; ComplianceAdapter and
                                     # ScoreboardAdapter are deleted, and the
                                     # suggestions.py LLM-stub adapter migrated
                                     # to the generators/app_suggester generator
                                     # whose proposals surface via proposal_reader.)
    scope: str                       # bot | admin
    scope_id: str                    # bot_id, or "admin" for pod-level issues

    # ── Content ───────────────────────────────────────────────────────────────
    title: str
    detail: str
    context: str
    action_label: Optional[str]
    action: Optional[str]
    action_args: dict = field(default_factory=dict)

    # ── Executability ─────────────────────────────────────────────────────────
    bot_executable: bool = True
    bridge_strategy: Optional[str] = None
    accept_label: str = "Accept"

    # ── Member Bot Tone Variants ──────────────────────────────────────────────
    member_bot_title: Optional[str] = None
    member_bot_detail: Optional[str] = None

    # ── Scoring ───────────────────────────────────────────────────────────────
    priority_score: int = 0
    priority_components: dict = field(default_factory=dict)
    learning_weight: float = 1.0

    # ── Tags ──────────────────────────────────────────────────────────────────
    tags: list = field(default_factory=list)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: str = "pending"
    snooze_count: int = 0
    snooze_until: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    resolved_at: Optional[str] = None
    expires_at: Optional[str] = None

    # ── Feedback ──────────────────────────────────────────────────────────────
    feedback_signal: Optional[str] = None
    feedback_reason: str = ""

    # ── Traceability ──────────────────────────────────────────────────────────
    source_ref: dict = field(default_factory=dict)

    # ── L1-L6 enrichment (Phase 1 of UI rationalization) ──────────────────────
    # All optional so the bridge can populate incrementally and legacy
    # adapters can continue emitting without these fields.
    dimension: Optional[str] = None
    urgency: Optional[str] = None
    adjacency_type: Optional[str] = None
    generator_id: Optional[str] = None
    action_kind: Optional[str] = None
    approval_audience: Optional[str] = None
    guardian_annotations: list = field(default_factory=list)
    conflicts_with: list = field(default_factory=list)
    claim: Optional[dict] = None
    revert_plan_summary: Optional[str] = None
    verify_status: str = "unknown"
    verify_detail: Optional[dict] = None
    score_breakdown: Optional[dict] = None

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "id": self.id,
            "dedup_key": self.dedup_key,
            "type": self.type,
            "source": self.source,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "title": self.title,
            "detail": self.detail,
            "context": self.context,
            "action_label": self.action_label,
            "action": self.action,
            "action_args": self.action_args,
            "bot_executable": self.bot_executable,
            "bridge_strategy": self.bridge_strategy,
            "accept_label": self.accept_label,
            "member_bot_title": self.member_bot_title,
            "member_bot_detail": self.member_bot_detail,
            "priority_score": self.priority_score,
            "priority_components": self.priority_components,
            "learning_weight": self.learning_weight,
            "tags": self.tags,
            "status": self.status,
            "snooze_count": self.snooze_count,
            "snooze_until": self.snooze_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "expires_at": self.expires_at,
            "feedback_signal": self.feedback_signal,
            "feedback_reason": self.feedback_reason,
            "source_ref": self.source_ref,
            # L1-L6 enrichment
            "dimension": self.dimension,
            "urgency": self.urgency,
            "adjacency_type": self.adjacency_type,
            "generator_id": self.generator_id,
            "action_kind": self.action_kind,
            "approval_audience": self.approval_audience,
            "guardian_annotations": list(self.guardian_annotations),
            "conflicts_with": list(self.conflicts_with),
            "claim": self.claim,
            "revert_plan_summary": self.revert_plan_summary,
            "verify_status": self.verify_status,
            "verify_detail": self.verify_detail,
            "score_breakdown": self.score_breakdown,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Recommendation":
        """Reconstruct from dict; handles missing optional fields gracefully."""
        return cls(
            id=d["id"],
            dedup_key=d["dedup_key"],
            type=d["type"],
            source=d["source"],
            scope=d["scope"],
            scope_id=d["scope_id"],
            title=d["title"],
            detail=d["detail"],
            context=d.get("context", ""),
            action_label=d.get("action_label"),
            action=d.get("action"),
            action_args=d.get("action_args", {}),
            bot_executable=d.get("bot_executable", True),
            bridge_strategy=d.get("bridge_strategy"),
            accept_label=d.get("accept_label", "Accept"),
            member_bot_title=d.get("member_bot_title"),
            member_bot_detail=d.get("member_bot_detail"),
            priority_score=d.get("priority_score", 0),
            priority_components=d.get("priority_components", {}),
            learning_weight=d.get("learning_weight", 1.0),
            tags=d.get("tags", []),
            status=d.get("status", "pending"),
            snooze_count=d.get("snooze_count", 0),
            snooze_until=d.get("snooze_until"),
            created_at=d.get("created_at", now_iso()),
            updated_at=d.get("updated_at", now_iso()),
            resolved_at=d.get("resolved_at"),
            expires_at=d.get("expires_at"),
            feedback_signal=d.get("feedback_signal"),
            feedback_reason=d.get("feedback_reason", ""),
            source_ref=d.get("source_ref", {}),
            dimension=d.get("dimension"),
            urgency=d.get("urgency"),
            adjacency_type=d.get("adjacency_type"),
            generator_id=d.get("generator_id"),
            action_kind=d.get("action_kind"),
            approval_audience=d.get("approval_audience"),
            guardian_annotations=list(d.get("guardian_annotations") or []),
            conflicts_with=list(d.get("conflicts_with") or []),
            claim=d.get("claim"),
            revert_plan_summary=d.get("revert_plan_summary"),
            verify_status=d.get("verify_status", "unknown"),
            verify_detail=d.get("verify_detail"),
            score_breakdown=d.get("score_breakdown"),
        )

    def update_from_candidate(self, candidate: "Recommendation") -> None:
        """Merge mutable fields from a freshly-generated candidate.

        Preserves immutable fields: id, created_at, status, snooze_*, feedback_*.
        Updates content and scoring fields that may change each refresh cycle.
        """
        self.title = candidate.title
        self.detail = candidate.detail
        self.context = candidate.context
        self.priority_score = candidate.priority_score
        self.priority_components = candidate.priority_components
        self.updated_at = candidate.updated_at
        self.source_ref = candidate.source_ref
        self.member_bot_title = candidate.member_bot_title
        self.member_bot_detail = candidate.member_bot_detail
        # Also update tags and scoring weight from new candidate
        self.tags = candidate.tags
        self.learning_weight = candidate.learning_weight
        # Update action fields in case they change
        self.action_label = candidate.action_label
        self.action = candidate.action
        self.action_args = candidate.action_args
        self.bot_executable = candidate.bot_executable
        self.bridge_strategy = candidate.bridge_strategy
        self.accept_label = candidate.accept_label

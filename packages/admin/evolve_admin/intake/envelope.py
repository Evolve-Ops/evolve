"""Intake envelope dataclasses.

Shape locked in docs/spec-primary-bot-interface-2026-05-14.md §6.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from evolve_util import now_iso_offset as _utc_now_iso


IntakeKind = Literal["bug", "feature", "question"]
IntakeState = Literal["open", "triaged", "filed", "closed"]

INTAKE_SCHEMA_VERSION = 1


@dataclass
class IntakeContext:
    """Captured situational context. Default empty for safety — missing
    fields are not an error; the admin may not have an active bot in scope."""

    primary_bot: str | None = None
    active_bot: str | None = None
    git_commit: str | None = None
    evolve_version: str | None = None
    recent_turns_excerpt: list[dict[str, str]] = field(default_factory=list)
    active_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_bot": self.primary_bot,
            "active_bot": self.active_bot,
            "git_commit": self.git_commit,
            "evolve_version": self.evolve_version,
            "recent_turns_excerpt": list(self.recent_turns_excerpt),
            "active_signals": list(self.active_signals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IntakeContext":
        if not isinstance(data, dict):
            return cls()
        return cls(
            primary_bot=data.get("primary_bot"),
            active_bot=data.get("active_bot"),
            git_commit=data.get("git_commit"),
            evolve_version=data.get("evolve_version"),
            recent_turns_excerpt=list(data.get("recent_turns_excerpt") or []),
            active_signals=list(data.get("active_signals") or []),
        )


@dataclass
class IntakePromotion:
    """GitHub-promotion record. ``github_issue_url`` is the source of
    truth for "has been filed." Everything else is for audit + UI."""

    github_issue_url: str | None = None
    github_issue_number: int | None = None
    promoted_at: str | None = None
    promoted_by: str | None = None  # user_key of admin who promoted
    body_sent: str | None = None  # exact post-redaction body sent to GitHub
    redactions_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "github_issue_url": self.github_issue_url,
            "github_issue_number": self.github_issue_number,
            "promoted_at": self.promoted_at,
            "promoted_by": self.promoted_by,
            "body_sent": self.body_sent,
            "redactions_applied": list(self.redactions_applied),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IntakePromotion":
        if not isinstance(data, dict):
            return cls()
        return cls(
            github_issue_url=data.get("github_issue_url"),
            github_issue_number=data.get("github_issue_number"),
            promoted_at=data.get("promoted_at"),
            promoted_by=data.get("promoted_by"),
            body_sent=data.get("body_sent"),
            redactions_applied=list(data.get("redactions_applied") or []),
        )


ActivityKind = Literal["new_comment", "state_change", "closed", "reopened"]


# ── Triage record (Phase 4 of Issue Inbox) ─────────────────────────────────
#
# Attached to ``Intake.triage`` for inbound intakes — issues filed by
# OTHERS on tracked repos where the operator has maintainer perms.
# Phase 4a writes these on capture (via the inbound watcher); Phase 4b
# surfaces them in the Triage queue UI and gates action endpoints.


TriageCategory = Literal[
    "bug", "feature_request", "question", "duplicate", "spam", "docs", "unknown",
]
TriageMerit = Literal["real", "unclear", "low", "unknown"]
TriageUrgency = Literal["p0", "p1", "p2", "p3", "unknown"]
TriageRecommendation = Literal[
    "auto_close_duplicate",
    "auto_reply_clarifying",
    "route_to_admin",
    "needs_investigation",
    "unknown",
]
TriageEffort = Literal["trivial", "small", "medium", "large", "unknown"]


@dataclass
class TriageRecord:
    """LLM-produced triage verdict for an inbound issue.

    Fields default to safe "unknown" values so a missing-key payload
    (older snapshot, future schema field we don't know about) parses
    cleanly. Callers that want to gate on a real verdict should check
    ``confidence >= 0.5`` and the specific field they care about.

    ``recommendation`` is the load-bearing field for Phase 5 (auto-
    response policy): the operator can configure rules like
    "auto-close items where recommendation=auto_close_duplicate AND
    confidence >= 0.9". For Phase 4 it's advisory only.
    """

    category: TriageCategory = "unknown"
    merit: TriageMerit = "unknown"
    urgency: TriageUrgency = "unknown"
    duplicate_of: list[str] = field(default_factory=list)  # "owner/repo#42" refs
    recommendation: TriageRecommendation = "unknown"
    draft_reply: str = ""
    draft_labels: list[str] = field(default_factory=list)
    estimated_effort: TriageEffort = "unknown"
    confidence: float = 0.0
    reasoning: str = ""
    # Inbound-issue identifiers — denormalized from promotion so we can
    # render the triage queue without joining back to GitHub on every
    # page load. inbound_author is the GH login of whoever filed it.
    inbound_author: str = ""
    inbound_title: str = ""
    inbound_body_short: str = ""  # first ~500 chars of the issue body
    triaged_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "merit": self.merit,
            "urgency": self.urgency,
            "duplicate_of": list(self.duplicate_of),
            "recommendation": self.recommendation,
            "draft_reply": self.draft_reply,
            "draft_labels": list(self.draft_labels),
            "estimated_effort": self.estimated_effort,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "inbound_author": self.inbound_author,
            "inbound_title": self.inbound_title,
            "inbound_body_short": self.inbound_body_short,
            "triaged_at": self.triaged_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TriageRecord | None":
        if not isinstance(data, dict):
            return None
        # Defensive coercion: unknown literal values become "unknown".
        def _coerce(value: str | None, allowed: tuple[str, ...]) -> str:
            v = str(value or "").strip().lower()
            return v if v in allowed else "unknown"
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return cls(
            category=_coerce(data.get("category"),  # type: ignore[arg-type]
                             ("bug", "feature_request", "question",
                              "duplicate", "spam", "docs", "unknown")),
            merit=_coerce(data.get("merit"),  # type: ignore[arg-type]
                          ("real", "unclear", "low", "unknown")),
            urgency=_coerce(data.get("urgency"),  # type: ignore[arg-type]
                            ("p0", "p1", "p2", "p3", "unknown")),
            duplicate_of=[str(x) for x in (data.get("duplicate_of") or [])
                          if isinstance(x, str)],
            recommendation=_coerce(  # type: ignore[arg-type]
                data.get("recommendation"),
                ("auto_close_duplicate", "auto_reply_clarifying",
                 "route_to_admin", "needs_investigation", "unknown"),
            ),
            draft_reply=str(data.get("draft_reply") or ""),
            draft_labels=[str(x) for x in (data.get("draft_labels") or [])
                          if isinstance(x, str)],
            estimated_effort=_coerce(  # type: ignore[arg-type]
                data.get("estimated_effort"),
                ("trivial", "small", "medium", "large", "unknown"),
            ),
            confidence=confidence,
            reasoning=str(data.get("reasoning") or ""),
            inbound_author=str(data.get("inbound_author") or ""),
            inbound_title=str(data.get("inbound_title") or ""),
            inbound_body_short=str(data.get("inbound_body_short") or ""),
            triaged_at=str(data.get("triaged_at") or _utc_now_iso()),
        )


# ── Auto-action record (Phase 5 of Issue Inbox) ─────────────────────────────
#
# Recorded on ``Intake.auto_action`` when the auto-responder fires
# against an inbound intake (e.g. close-as-duplicate, post a clarifying
# reply). The ``undo_deadline_at`` is a hard 24-hour window — the
# operator can roll back the action via the Triage UI until then, after
# which the row hardens and the undo button disappears.

AutoActionKind = Literal[
    "close_duplicate",
    "reply_clarifying",
    "label_only",
]


@dataclass
class AutoActionRecord:
    """A single auto-response action taken against an inbound issue.

    The auto-responder writes this on success; the undo endpoint reads
    it to reverse the action (reopen the issue + delete the bot comment)
    so long as ``undo_deadline_at`` hasn't passed and ``undone`` is False.

    ``undo_handle`` carries whatever info the undo path needs — for
    ``close_duplicate`` and ``reply_clarifying`` that's the ``comment_id``
    the bot posted; for ``label_only`` that's the labels added (so undo
    knows which ones to remove vs. which the human had already applied).
    """

    kind: AutoActionKind
    acted_at: str = field(default_factory=_utc_now_iso)
    actor: str = ""  # GH login that the action was posted as
    undo_deadline_at: str = ""  # ISO; auto-set to acted_at + 24h on construction
    undone: bool = False
    undone_at: str = ""
    undo_handle: dict[str, Any] = field(default_factory=dict)
    reason: str = ""  # short note for the audit trail ("policy", "manual", etc.)

    def __post_init__(self) -> None:
        # Default the undo deadline to acted_at + 24h. The auto-responder
        # can override this when constructing the record (e.g. for tests
        # that want a shorter window). Once written, never recomputed.
        if not self.undo_deadline_at and self.acted_at:
            try:
                t = datetime.fromisoformat(self.acted_at.replace("Z", "+00:00"))
                self.undo_deadline_at = (
                    t + timedelta(hours=24)
                ).isoformat(timespec="seconds")
            except ValueError:
                # Malformed acted_at — leave deadline empty; is_undoable
                # returns False, which is the safe default.
                pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "acted_at": self.acted_at,
            "actor": self.actor,
            "undo_deadline_at": self.undo_deadline_at,
            "undone": self.undone,
            "undone_at": self.undone_at,
            "undo_handle": dict(self.undo_handle),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AutoActionRecord | None":
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind not in ("close_duplicate", "reply_clarifying", "label_only"):
            return None
        return cls(
            kind=kind,  # type: ignore[arg-type]
            acted_at=str(data.get("acted_at") or _utc_now_iso()),
            actor=str(data.get("actor") or ""),
            undo_deadline_at=str(data.get("undo_deadline_at") or ""),
            undone=bool(data.get("undone", False)),
            undone_at=str(data.get("undone_at") or ""),
            undo_handle=dict(data.get("undo_handle") or {}),
            reason=str(data.get("reason") or ""),
        )

    def is_undoable(self, *, now_iso: str | None = None) -> bool:
        """True iff the operator can still roll this back.

        Hardens (returns False) once ``undo_deadline_at`` has passed or
        ``undone`` has been set. Cheap string compare on ISO-8601 — the
        timestamps are written in the same UTC offset, so lexicographic
        order is chronological order.
        """
        if self.undone:
            return False
        if not self.undo_deadline_at:
            return False
        cursor = now_iso or _utc_now_iso()
        return cursor < self.undo_deadline_at


@dataclass
class ActivityEvent:
    """An external event that happened to a filed intake.

    Written by ``upstream_issues_watcher`` (and any future producer)
    when activity is detected on the GitHub issue corresponding to a
    filed intake. The Inbox UI surfaces these as "new activity" rows.

    ``kind`` is the kind of event. ``actor`` is the GitHub login that
    caused it (e.g. the commenting maintainer). ``observed_at`` is the
    moment the watcher detected the event (NOT the moment the maintainer
    posted — those can differ by up to one poll interval). ``snippet``
    is the first ~200 chars of the comment body for at-a-glance preview.
    ``ref`` is whatever the producer wants to record for back-reference
    (a comment_id, a state name, etc.); the schema doesn't constrain it.
    """

    kind: ActivityKind
    actor: str = ""
    observed_at: str = field(default_factory=_utc_now_iso)
    snippet: str = ""
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "actor": self.actor,
            "observed_at": self.observed_at,
            "snippet": self.snippet,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ActivityEvent | None":
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind not in ("new_comment", "state_change", "closed", "reopened"):
            return None
        return cls(
            kind=kind,  # type: ignore[arg-type]
            actor=str(data.get("actor") or ""),
            observed_at=str(data.get("observed_at") or _utc_now_iso()),
            snippet=str(data.get("snippet") or ""),
            ref=str(data.get("ref") or ""),
        )


@dataclass
class Intake:
    """A captured bug report / feature request / question.

    ``state`` is authoritative; the subdir on disk is a physical index.
    """

    id: str
    kind: IntakeKind
    body: str
    submitter_user_key: str | None = None
    submitter_role: str | None = None  # "admin" | "primary" | "secondary"
    submitter_channel: str | None = None  # "telegram:12345", "slack:U…", …
    state: IntakeState = "open"
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    context: IntakeContext = field(default_factory=IntakeContext)
    promotion: IntakePromotion = field(default_factory=IntakePromotion)
    triage_notes: str = ""
    # Phase 1: activity log for filed intakes. Watcher (and any future
    # producer) appends events when something happens on the GitHub
    # issue side. last_seen_activity_at is the operator's read cursor
    # — events with observed_at > last_seen are "unread" in the Inbox UI.
    activity_log: list[ActivityEvent] = field(default_factory=list)
    last_seen_activity_at: str = ""
    # Phase 2: revision_history for `evo revise <id> <instruction>`. Each
    # entry captures one round of operator-driven LLM redraft so the body
    # change is auditable + reversible. The "title" key is optional —
    # older entries may have only "prior_body" when the reviser produced
    # a same-title revision.
    revision_history: list[dict[str, Any]] = field(default_factory=list)
    # Phase 4: inbound intakes. True when this Intake was captured by
    # the inbound_issues_watcher (an issue filed by SOMEONE ELSE on a
    # repo we maintain) — as opposed to the operator's own submissions
    # which default to False. Inbound intakes are pre-promoted (created
    # in state=filed with promotion.github_issue_url pointing at the
    # source issue) and carry an LLM-produced ``triage`` verdict.
    inbound: bool = False
    triage: "TriageRecord | None" = None
    # Phase 5: auto-response record. Written by the auto-responder when
    # the inbound intake's triage verdict matches the operator's policy
    # (or when the operator manually clicks "apply recommendation").
    # Carries the undo handle so the action can be rolled back within
    # the 24-hour window.
    auto_action: "AutoActionRecord | None" = None
    schema_version: int = INTAKE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Intake.id must be non-empty")
        if self.kind not in ("bug", "feature", "question"):
            raise ValueError(f"Intake.kind must be bug|feature|question, got {self.kind!r}")
        if not self.body or not self.body.strip():
            raise ValueError("Intake.body must be non-empty")
        if self.state not in ("open", "triaged", "filed", "closed"):
            raise ValueError(f"Intake.state must be open|triaged|filed|closed, got {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "body": self.body,
            "submitter": {
                "user_key": self.submitter_user_key,
                "role": self.submitter_role,
                "channel": self.submitter_channel,
            },
            "context": self.context.to_dict(),
            "promotion": self.promotion.to_dict(),
            "triage_notes": self.triage_notes,
            "activity_log": [e.to_dict() for e in self.activity_log],
            "last_seen_activity_at": self.last_seen_activity_at,
            "revision_history": list(self.revision_history),
            "inbound": self.inbound,
            "triage": self.triage.to_dict() if self.triage else None,
            "auto_action": self.auto_action.to_dict() if self.auto_action else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Intake":
        submitter = data.get("submitter") or {}
        raw_log = data.get("activity_log") or []
        parsed_log = [
            ev for ev in (ActivityEvent.from_dict(e) for e in raw_log)
            if ev is not None
        ]
        return cls(
            id=data["id"],
            kind=data["kind"],
            body=data["body"],
            submitter_user_key=submitter.get("user_key"),
            submitter_role=submitter.get("role"),
            submitter_channel=submitter.get("channel"),
            state=data.get("state", "open"),
            created_at=data.get("created_at", _utc_now_iso()),
            updated_at=data.get("updated_at", _utc_now_iso()),
            context=IntakeContext.from_dict(data.get("context")),
            promotion=IntakePromotion.from_dict(data.get("promotion")),
            triage_notes=data.get("triage_notes", ""),
            activity_log=parsed_log,
            last_seen_activity_at=str(data.get("last_seen_activity_at") or ""),
            revision_history=list(data.get("revision_history") or []),
            inbound=bool(data.get("inbound", False)),
            triage=TriageRecord.from_dict(data.get("triage")),
            auto_action=AutoActionRecord.from_dict(data.get("auto_action")),
            schema_version=int(data.get("schema_version", INTAKE_SCHEMA_VERSION)),
        )

    def unread_activity_count(self) -> int:
        """Number of activity events with observed_at > last_seen_activity_at.

        Used by the Inbox UI to render "needs your reply" badges.
        """
        cursor = self.last_seen_activity_at
        if not cursor:
            return len(self.activity_log)
        return sum(1 for e in self.activity_log if e.observed_at > cursor)

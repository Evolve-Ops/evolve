"""arbiter.conflict — Conflict detection between open proposals.

Spec §4.4. Three conflict types:

  - touches_overlap: same touched surface + incompatible action values
  - metric_direction_opposite: same claim metric, opposite directions
  - exclusive_choice: similar fingerprint targeting the same "gap"
    (both propose installing sleep tracker, for example)

This module is PURE detection — it annotates proposals with
``ConflictAnnotation`` entries but does not resolve conflicts. The
referee surfaces the conflict to the user as a tradeoff, per the
"referee is a mediator, not a judge" principle (§2.12).
"""

from __future__ import annotations

from arbiter.dedup import compute_fingerprint
from schema.proposal import ConflictAnnotation, Proposal


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────


def _touches_overlap(a: Proposal, b: Proposal) -> ConflictAnnotation | None:
    """Two proposals modify the same surface with incompatible actions."""
    a_touches = set(a.risk_tag.touches)
    b_touches = set(b.risk_tag.touches)
    overlap = a_touches & b_touches
    if not overlap:
        return None

    # Same action kind + same apparent target → likely conflicting
    a_kind = getattr(a.action, "kind", type(a.action).__name__)
    b_kind = getattr(b.action, "kind", type(b.action).__name__)
    if a_kind != b_kind:
        return None

    # For ConfigPatch and ManifestUpdate, compare target paths
    a_target = _action_target(a)
    b_target = _action_target(b)
    if a_target and b_target and a_target == b_target and not _values_equal(a, b):
        return ConflictAnnotation(
            with_proposal_id=b.id,
            conflict_type="touches_overlap",
            description=f"both modify {a_target!r} with different values",
        )
    return None


def _metric_direction_opposite(
    a: Proposal, b: Proposal
) -> ConflictAnnotation | None:
    if a.claim is None or b.claim is None:
        return None
    if a.claim.metric != b.claim.metric:
        return None
    if a.claim.direction == b.claim.direction:
        return None
    # One says "up", the other says "down" (or "equal" vs "up", etc.)
    if {a.claim.direction, b.claim.direction} == {"up", "down"}:
        return ConflictAnnotation(
            with_proposal_id=b.id,
            conflict_type="metric_direction_opposite",
            description=(
                f"both target metric {a.claim.metric!r}; "
                f"one direction up, other down"
            ),
        )
    return None


def _exclusive_choice(
    a: Proposal, b: Proposal
) -> ConflictAnnotation | None:
    """Proposals with identical fingerprints from different generators.

    The dedup/merge judge handles these at ingest; at rank time the
    referee also annotates as a conflict so the UI can surface "one or
    the other" framing if both slipped through merge as ``keep_both``.
    """
    if a.generator_id == b.generator_id:
        return None
    if compute_fingerprint(a) != compute_fingerprint(b):
        return None
    return ConflictAnnotation(
        with_proposal_id=b.id,
        conflict_type="exclusive_choice",
        description="both address the same underlying condition",
    )


def _action_target(proposal: Proposal) -> str:
    action = proposal.action
    for attr in ("target_path", "app_id", "path", "dimension", "generator_id"):
        if hasattr(action, attr):
            value = getattr(action, attr)
            if value:
                return f"{attr}:{value}"
    return ""


def _values_equal(a: Proposal, b: Proposal) -> bool:
    """Approximate equality check for action payloads."""
    for attr in ("value", "content", "new_tier", "new_value"):
        if hasattr(a.action, attr) and hasattr(b.action, attr):
            return getattr(a.action, attr) == getattr(b.action, attr)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Detection entry point
# ─────────────────────────────────────────────────────────────────────────────


def detect_conflicts(proposals: list[Proposal]) -> None:
    """Annotate ``proposals`` with conflicts_with entries in place.

    Walks all pairs; annotations are attached symmetrically.
    Idempotent: running twice produces the same set of annotations (we
    check for existing entries before adding).
    """
    # Precompute a set-of-(id, conflict_type) already present to skip dupes
    existing: set[tuple[str, str, str]] = set()
    for p in proposals:
        for c in p.conflicts_with:
            existing.add((p.id, c.with_proposal_id, c.conflict_type))

    for i in range(len(proposals)):
        a = proposals[i]
        for j in range(i + 1, len(proposals)):
            b = proposals[j]
            for detector in (
                _touches_overlap,
                _metric_direction_opposite,
                _exclusive_choice,
            ):
                ann = detector(a, b)
                if ann is None:
                    continue
                # Attach to a
                if (a.id, b.id, ann.conflict_type) not in existing:
                    a.conflicts_with.append(
                        ConflictAnnotation(
                            with_proposal_id=b.id,
                            conflict_type=ann.conflict_type,
                            description=ann.description,
                        )
                    )
                    existing.add((a.id, b.id, ann.conflict_type))
                # Mirror for b
                if (b.id, a.id, ann.conflict_type) not in existing:
                    b.conflicts_with.append(
                        ConflictAnnotation(
                            with_proposal_id=a.id,
                            conflict_type=ann.conflict_type,
                            description=ann.description,
                        )
                    )
                    existing.add((b.id, a.id, ann.conflict_type))
                break  # one conflict per pair is enough

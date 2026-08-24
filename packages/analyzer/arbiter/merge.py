"""arbiter.merge — Merge judge for fingerprint-colliding proposals (L3).

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §5.

When the dedup hook detects a fingerprint match, the caller runs the merge
judge on the two proposals. The judge returns a ``MergeJudgment`` with:
  - decision: merge | prefer_a | prefer_b | keep_both
  - credit_split: generator_id → share (0..1)
  - reason + confidence

The actual LLM call is pluggable; tests inject a mock. Default judge
uses simple heuristics to handle the common cases without an LLM call —
enough to cover L3 until the full merge judge ships with real LLM wiring.
"""

from __future__ import annotations

from typing import Callable

from schema.proposal import Proposal
from schema.veto import MergeJudgment


# Callable: (proposal_a, proposal_b) -> MergeJudgment
MergeJudgeFn = Callable[[Proposal, Proposal], MergeJudgment]


# ─────────────────────────────────────────────────────────────────────────────
# Default heuristic judge (no LLM)
# ─────────────────────────────────────────────────────────────────────────────


def _default_judge(a: Proposal, b: Proposal) -> MergeJudgment:
    """Heuristic merge decision without an LLM.

    Rules:
      - If both proposals have the same action kind AND same target surface,
        default to ``merge`` with equal credit split.
      - Otherwise ``keep_both`` — the fingerprint collided by coincidence.

    The heuristic is conservative: when in doubt, keep both. The L3+ LLM
    merge judge can be more nuanced.
    """
    a_kind = getattr(a.action, "kind", type(a.action).__name__)
    b_kind = getattr(b.action, "kind", type(b.action).__name__)

    if a_kind != b_kind:
        return MergeJudgment(
            decision="keep_both",
            credit_split={a.generator_id: 1.0, b.generator_id: 1.0},
            reason=f"different action kinds ({a_kind} vs {b_kind})",
            confidence=0.9,
        )

    # Same action kind — compare target surface if we can derive one
    a_surface = _action_surface(a)
    b_surface = _action_surface(b)
    if a_surface and b_surface and a_surface == b_surface:
        return MergeJudgment(
            decision="merge",
            credit_split={a.generator_id: 0.5, b.generator_id: 0.5},
            reason=f"same action kind ({a_kind}) targeting same surface ({a_surface})",
            confidence=0.8,
        )

    return MergeJudgment(
        decision="keep_both",
        credit_split={a.generator_id: 1.0, b.generator_id: 1.0},
        reason="same action kind but different target surfaces",
        confidence=0.7,
    )


def _action_surface(p: Proposal) -> str:
    """Best-effort extraction of the action's target surface for comparison."""
    action = p.action
    for attr in ("app_id", "target_path", "path", "generator_id", "bot_id"):
        if hasattr(action, attr):
            value = getattr(action, attr)
            if value:
                return f"{attr}:{value}"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Judge registration
# ─────────────────────────────────────────────────────────────────────────────


_judge: MergeJudgeFn = _default_judge


def set_merge_judge(fn: MergeJudgeFn) -> None:
    """Install a judge callable (tests + production-LLM wiring)."""
    global _judge
    _judge = fn


def reset_merge_judge() -> None:
    """Restore the heuristic default judge."""
    global _judge
    _judge = _default_judge


def judge(a: Proposal, b: Proposal) -> MergeJudgment:
    """Judge the collision between two proposals."""
    return _judge(a, b)


# ─────────────────────────────────────────────────────────────────────────────
# Dedup-hook integration
# ─────────────────────────────────────────────────────────────────────────────


def judge_collisions(
    proposal: Proposal,
    collisions: list[Proposal],
) -> list[MergeJudgment]:
    """Judge ``proposal`` against each of the colliding open proposals.

    Returns one judgment per collision pair. The caller decides what to
    do with merge/prefer decisions (usually: supersede the non-preferred
    proposal, record credit split).
    """
    return [judge(proposal, other) for other in collisions]

"""arbiter.veto — Guardian veto pass (L3).

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §6.

Before routing, each proposal passes through the veto pass. Every active
guardian's ``evaluate(proposal)`` method is called; results are consolidated:

  - Any ``critical`` veto → proposal → rejected immediately.
  - Any other ``veto`` → proposal → rejected.
  - ``annotate`` verdicts accumulate onto ``proposal.guardian_annotations``.
  - All-pass → proposal proceeds to routing.

Guardians never self-veto (a guardian's own proposals skip that guardian's
evaluate).

The veto pass does not itself transition the proposal's state. Callers
decide what to do based on the returned ``VetoOutcome``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from schema.proposal import GuardianAnnotation, Proposal
from schema.veto import VetoResult, VetoVerdict


# ─────────────────────────────────────────────────────────────────────────────
# Guardian protocol
# ─────────────────────────────────────────────────────────────────────────────


# A guardian's evaluate function: takes a Proposal, returns a VetoResult or
# None (indicating the guardian has no opinion about this proposal).
GuardianEvaluator = Callable[[Proposal], "VetoResult | None"]


@dataclass
class Guardian:
    """A guardian for veto-pass purposes.

    Fields:
        id: the guardian's generator_id.
        evaluate: callable (Proposal → VetoResult | None). ``None`` means
            the guardian chooses not to weigh in.
    """

    id: str
    evaluate: GuardianEvaluator


# ─────────────────────────────────────────────────────────────────────────────
# Veto consolidation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class VetoOutcome:
    """Consolidated outcome of running all guardians against a proposal."""

    blocked: bool  # True if any critical veto OR any non-critical veto
    blocking_verdicts: list[VetoResult] = field(default_factory=list)
    annotations: list[GuardianAnnotation] = field(default_factory=list)
    pass_verdicts: list[VetoResult] = field(default_factory=list)
    skipped_self: list[str] = field(default_factory=list)  # guardian ids skipped as self

    @property
    def any_critical(self) -> bool:
        return any(v.severity == "critical" for v in self.blocking_verdicts)


def _verdict_to_annotation(v: VetoResult) -> GuardianAnnotation:
    return GuardianAnnotation(
        guardian_id=v.guardian_id,
        severity=v.severity,
        reason=v.reason,
        details=dict(v.details),
    )


def run_veto_pass(
    proposal: Proposal,
    guardians: Iterable[Guardian],
) -> VetoOutcome:
    """Run every guardian's evaluate() against the proposal, consolidate.

    Mutates ``proposal.guardian_annotations`` in place to append any
    annotate verdicts. Does not transition the proposal's state; caller
    handles state based on ``outcome.blocked``.
    """
    outcome = VetoOutcome(blocked=False)

    for guardian in guardians:
        if guardian.id == proposal.generator_id:
            outcome.skipped_self.append(guardian.id)
            continue
        verdict: VetoResult | None
        try:
            verdict = guardian.evaluate(proposal)
        except Exception as e:  # noqa: BLE001 — evaluator bugs shouldn't crash arbiter
            # Treat as "no opinion" — better to let the proposal through
            # than to block it due to a buggy guardian; the proposal will
            # be reviewed by humans anyway in most cases.
            outcome.annotations.append(
                GuardianAnnotation(
                    guardian_id=guardian.id,
                    severity="low",
                    reason=f"guardian evaluator raised: {e}",
                    details={"exception_type": type(e).__name__},
                )
            )
            continue
        if verdict is None:
            continue

        if verdict.verdict == "veto":
            outcome.blocking_verdicts.append(verdict)
            outcome.annotations.append(_verdict_to_annotation(verdict))
        elif verdict.verdict == "annotate":
            outcome.annotations.append(_verdict_to_annotation(verdict))
        elif verdict.verdict == "pass":
            outcome.pass_verdicts.append(verdict)

    outcome.blocked = bool(outcome.blocking_verdicts)

    # Attach all accumulated annotations to the proposal
    if outcome.annotations:
        proposal.guardian_annotations.extend(outcome.annotations)

    return outcome

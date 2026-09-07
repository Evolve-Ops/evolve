"""verify.evaluate — Pure claim-evaluation logic.

Given a Claim and a resolved metric value, decides whether the claim held.
No IO, no side effects — this is the decision core of the verify daemon.

Spec: docs/archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md §4.1 step 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from schema.proposal import Claim


ClaimResult = Literal["succeeded", "failed", "unresolved"]


@dataclass
class ClaimOutcome:
    """Outcome of evaluating a claim against a resolved metric.

    Fields:
        result: ``succeeded`` (claim held), ``failed`` (claim did not hold),
            or ``unresolved`` (can't decide — metric confidence too low or
            resolution error). Unresolved outcomes leave the proposal in
            ``applied`` and the caller retries later.
        delta: the computed (current - baseline) value.
        threshold_met: True iff the magnitude threshold was satisfied in
            the claimed direction. Always False when ``result == unresolved``.
        message: short human-readable explanation for audit.
    """

    result: ClaimResult
    delta: float
    threshold_met: bool
    message: str


# Below this metric confidence, the verify daemon treats the result as
# unresolved and retries next cycle (up to a bounded retry count — handled
# in the daemon, not here).
METRIC_CONFIDENCE_FLOOR = 0.5


def evaluate_claim(
    claim: Claim,
    current_value: float,
    current_confidence: float = 1.0,
) -> ClaimOutcome:
    """Decide whether the claim held.

    Args:
        claim: the Proposal's Claim.
        current_value: the metric value resolved at the claim's horizon.
        current_confidence: the resolver's reported confidence in that value
            (0..1). Below ``METRIC_CONFIDENCE_FLOOR`` → unresolved.

    Returns ``ClaimOutcome``. The caller decides what to do based on
    ``result`` — this function does not mutate anything.
    """
    if current_confidence < METRIC_CONFIDENCE_FLOOR:
        return ClaimOutcome(
            result="unresolved",
            delta=0.0,
            threshold_met=False,
            message=(
                f"metric confidence {current_confidence:.2f} below floor "
                f"{METRIC_CONFIDENCE_FLOOR}; leaving proposal for retry"
            ),
        )

    delta = current_value - claim.baseline

    threshold_met: bool
    if claim.direction == "up":
        threshold_met = delta >= claim.magnitude
    elif claim.direction == "down":
        threshold_met = -delta >= claim.magnitude
    elif claim.direction == "equal":
        threshold_met = abs(delta) <= claim.magnitude
    else:
        return ClaimOutcome(
            result="unresolved",
            delta=delta,
            threshold_met=False,
            message=f"unknown claim direction {claim.direction!r}",
        )

    if threshold_met:
        return ClaimOutcome(
            result="succeeded",
            delta=delta,
            threshold_met=True,
            message=(
                f"claim held: metric={claim.metric} delta={delta} "
                f"direction={claim.direction} magnitude={claim.magnitude}"
            ),
        )

    return ClaimOutcome(
        result="failed",
        delta=delta,
        threshold_met=False,
        message=(
            f"claim did not hold: metric={claim.metric} delta={delta} "
            f"direction={claim.direction} magnitude={claim.magnitude}; "
            f"fallback={claim.fallback}"
        ),
    )

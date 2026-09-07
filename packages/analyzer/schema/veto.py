"""schema.veto — VetoResult + MergeJudgment types (L3).

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §3.2, §3.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


VetoVerdict = Literal["pass", "veto", "annotate"]
VetoSeverity = Literal["low", "medium", "high", "critical"]


@dataclass
class VetoResult:
    """One guardian's verdict on one proposal."""

    guardian_id: str
    verdict: VetoVerdict
    reason: str = ""
    severity: VetoSeverity = "medium"
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "guardian_id": self.guardian_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "severity": self.severity,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VetoResult":
        return cls(
            guardian_id=data["guardian_id"],
            verdict=data["verdict"],
            reason=data.get("reason", ""),
            severity=data.get("severity", "medium"),
            details=dict(data.get("details") or {}),
        )


MergeDecision = Literal["merge", "prefer_a", "prefer_b", "keep_both"]


@dataclass
class MergeJudgment:
    """Judgment about two fingerprint-colliding proposals."""

    decision: MergeDecision
    credit_split: dict[str, float]  # generator_id → share (0..1), sums ~= 1.0
    reason: str = ""
    confidence: float = 0.5
    # When decision == "merge", the caller constructs the merged Proposal;
    # this object just records the judgment.

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"MergeJudgment.confidence out of range: {self.confidence}"
            )

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "credit_split": dict(self.credit_split),
            "reason": self.reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MergeJudgment":
        return cls(
            decision=data["decision"],
            credit_split=dict(data.get("credit_split") or {}),
            reason=data.get("reason", ""),
            confidence=float(data.get("confidence", 0.5)),
        )

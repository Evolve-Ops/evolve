"""schema.provenance — Provenance metadata for proposals.

Every proposal carries provenance describing the technique that produced it,
the signals that matched, and the emitter's confidence. Provenance drives
calibration in L5+; in L1 it is recorded but not yet aggregated.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Provenance:
    """Describes how a proposal came to be.

    Fields:
        technique: identifier of the generator's detection technique
            (e.g. ``"adjacency_detection"``, ``"threshold_crossed"``).
        signals: technique-specific evidence. Keys and shapes vary by
            technique; kept free-form for now because the consumers in L1
            are auditors and debuggers, not calibration (that's L5).
        confidence: emitter's self-reported confidence in 0..1. Informational
            only in L1.
    """

    technique: str
    signals: dict = field(default_factory=dict)
    confidence: float = 0.5

    def __post_init__(self) -> None:
        if not self.technique:
            raise ValueError("Provenance.technique must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Provenance.confidence must be in [0, 1]; got {self.confidence}"
            )

    def to_dict(self) -> dict:
        return {
            "technique": self.technique,
            "signals": dict(self.signals),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Provenance":
        return cls(
            technique=data["technique"],
            signals=dict(data.get("signals") or {}),
            confidence=float(data.get("confidence", 0.5)),
        )

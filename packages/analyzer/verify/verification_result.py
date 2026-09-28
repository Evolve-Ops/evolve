"""verify.verification_result — Audit artifact per verified proposal.

For each proposal the verify daemon processes, a result record is written
to ``{shared_dir}/proposals/verification-results/{id}.json`` capturing the
claim, the resolved metric, the outcome, and what happened next
(revert/flag/escalate).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VerificationResult:
    """A single verification audit record."""

    proposal_id: str
    bot_id: str
    verified_at: str  # ISO8601 UTC
    metric: str
    baseline: float
    current_value: float
    current_confidence: float
    delta: float
    direction: str
    magnitude: float
    window_days: int
    fallback: str
    outcome: str  # "succeeded" | "failed_reverted" | "failed_flagged" | "failed_revert_failed" | "unresolved"
    revert_ok: bool | None = None
    revert_message: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "bot_id": self.bot_id,
            "verified_at": self.verified_at,
            "metric": self.metric,
            "baseline": self.baseline,
            "current_value": self.current_value,
            "current_confidence": self.current_confidence,
            "delta": self.delta,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "window_days": self.window_days,
            "fallback": self.fallback,
            "outcome": self.outcome,
            "revert_ok": self.revert_ok,
            "revert_message": self.revert_message,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VerificationResult":
        return cls(
            proposal_id=data["proposal_id"],
            bot_id=data["bot_id"],
            verified_at=data["verified_at"],
            metric=data["metric"],
            baseline=float(data["baseline"]),
            current_value=float(data["current_value"]),
            current_confidence=float(data.get("current_confidence", 1.0)),
            delta=float(data["delta"]),
            direction=data["direction"],
            magnitude=float(data["magnitude"]),
            window_days=int(data["window_days"]),
            fallback=data["fallback"],
            outcome=data["outcome"],
            revert_ok=data.get("revert_ok"),
            revert_message=data.get("revert_message", ""),
            notes=list(data.get("notes") or []),
        )


def write_result(
    result: VerificationResult,
    shared_dir: Path,
) -> Path:
    """Atomically write the verification result to disk.

    Returns the written path.
    """
    out_dir = shared_dir / "proposals" / "verification-results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result.proposal_id}.json"

    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_dir),
        prefix=f".{result.proposal_id}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return out_path

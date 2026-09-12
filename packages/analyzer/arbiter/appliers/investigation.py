"""arbiter.appliers.investigation — Apply an Investigation proposal.

Investigation proposals don't mutate state — they exist to surface a
situation to a human. The applier is essentially a no-op that records
the investigation was "acknowledged" by the apply pipeline.

Reverting an Investigation is also a no-op (nothing was changed to undo).
"""

from __future__ import annotations

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import Investigation


class InvestigationApplier:
    """No-op applier for Investigation proposals."""

    def capture_snapshot(self, action: Investigation, bot_id: str) -> dict:
        return {"action_kind": "Investigation", "bot_id": bot_id}

    def apply(self, action: Investigation, bot_id: str) -> ApplyResult:
        return ApplyResult(
            ok=True,
            details={"bot_id": bot_id, "context": action.context},
            message="Investigation acknowledged (no mutation).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        return RevertResult(
            ok=True,
            details={"bot_id": bot_id},
            message="Investigation revert is a no-op.",
        )


register_applier("Investigation", InvestigationApplier())

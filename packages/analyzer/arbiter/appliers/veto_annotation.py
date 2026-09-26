"""arbiter.appliers.veto_annotation — Apply a VetoAnnotation proposal.

``VetoAnnotation(reason, severity)`` is guardian-only: it carries risk
information about a proposal for a human to read, and the schema is explicit
that it "does not mutate state when applied". So the applier is a no-op that
records the acknowledgement, exactly like ``investigation``.

Why it exists at all, given nothing mutates: ``apply.INFORMATIONAL_KINDS``
tags the FYI kinds so the UI routes them to the calmer Observations stream —
it is a *display* predicate and blocks nothing. A VetoAnnotation proposal is
still a proposal with an Act button behind it, and apply.py still calls
``get_applier(kind)``. Without this module that call raises, so acting on a
VetoAnnotation failed while its three siblings (Investigation,
WorkflowInstruction, AddSignalCollection) succeeded — the same defect class
as the missing AgentsAppend and InstallApp appliers, just latent because no
generator emits the kind yet.

Reverting is a no-op for the same reason applying is: nothing was changed.
"""

from __future__ import annotations

from typing import cast

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import Action, VetoAnnotation


class VetoAnnotationApplier:
    """No-op applier for VetoAnnotation proposals."""

    def capture_snapshot(self, action: Action, bot_id: str) -> dict:
        veto = cast(VetoAnnotation, action)
        return {
            "action_kind": "VetoAnnotation",
            "bot_id": bot_id,
            "severity": veto.severity,
        }

    def apply(self, action: Action, bot_id: str) -> ApplyResult:
        veto = cast(VetoAnnotation, action)
        return ApplyResult(
            ok=True,
            details={
                "bot_id": bot_id,
                "reason": veto.reason,
                "severity": veto.severity,
            },
            message=f"Veto annotation acknowledged ({veto.severity}; no mutation).",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        return RevertResult(
            ok=True,
            details={"bot_id": bot_id},
            message="VetoAnnotation revert is a no-op.",
        )


register_applier("VetoAnnotation", VetoAnnotationApplier())

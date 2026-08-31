"""arbiter.appliers.add_signal_collection — Apply a SignalGapProposal.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §6.3.

SignalGapProposals propose extending evolve's own observation layer —
the synthesizer hit a gap and wants a new monitor written. The fix is
**code**: an engineer reads the proposal, writes the monitor, ships it.
There is no automated applier that can write monitor code; the apply
step is a no-op that acknowledges the proposal entered the operator's
work queue. The operator clicks "Mark complete" once the monitor is
landed in a future PR.

This is the same pattern as Investigation: no state mutation on apply,
no revert work. The distinction is purely UX (a separate action kind
so the UI can badge these distinctly and the operator knows it's a
substrate-level work item, not a per-bot task).
"""

from __future__ import annotations

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import AddSignalCollection


class AddSignalCollectionApplier:
    """No-op applier; manual completion by the operator-developer."""

    def capture_snapshot(self, action: AddSignalCollection, bot_id: str) -> dict:
        return {
            "action_kind": "AddSignalCollection",
            "bot_id": bot_id,
            "producer": action.producer,
            "signal_type": action.signal_type,
        }

    def apply(self, action: AddSignalCollection, bot_id: str) -> ApplyResult:
        return ApplyResult(
            ok=True,
            details={
                "producer": action.producer,
                "signal_type": action.signal_type,
                "motivating_candidates": list(action.motivating_candidate_ids),
            },
            message=(
                f"Signal gap acknowledged: {action.producer}.{action.signal_type}. "
                "An engineer writes the monitor; mark complete when shipped."
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        return RevertResult(
            ok=True,
            details={"action_kind": "AddSignalCollection"},
            message="AddSignalCollection revert is a no-op.",
        )


register_applier("AddSignalCollection", AddSignalCollectionApplier())

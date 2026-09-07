"""generators.workspace_security.observe — Detector entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generators.workspace_security.signal_proposals import (
    make_misplaced_secret_proposal,
)
from schema.proposal import Proposal


GENERATOR_ID = "workspace_security"
DIMENSION = "safety"

COMPLIANCE_SCAN_PRODUCER = "compliance_scan"
CONSUMED_SIGNAL_TYPE = "misplaced_secret"


@dataclass
class WorkspaceSecurityContext:
    """Per-bot run context."""

    bot_id: str
    shared_dir: Path


def observe(ctx: WorkspaceSecurityContext) -> list[Proposal]:
    """One Proposal per firing misplaced_secret Signal."""
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    # Phase A.5 — preload suppressions for this bot.
    from arbiter.dismissals import preload_suppressed_signatures
    suppressed = preload_suppressed_signatures(ctx.shared_dir, ctx.bot_id)

    proposals: list[Proposal] = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=COMPLIANCE_SCAN_PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        if getattr(sig, "type", None) != CONSUMED_SIGNAL_TYPE:
            continue
        try:
            for proposal in make_misplaced_secret_proposal(sig):
                if proposal.dismiss_signature in suppressed:
                    continue
                proposals.append(proposal)
        except Exception:
            continue
    return proposals



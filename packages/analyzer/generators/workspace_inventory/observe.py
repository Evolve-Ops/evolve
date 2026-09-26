"""generators.workspace_inventory.observe — Detector entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generators.workspace_inventory.signal_proposals import (
    SIGNAL_TYPE_TO_FACTORY,
)
from schema.proposal import Proposal


GENERATOR_ID = "workspace_inventory"
DIMENSION = "app_quality"

COMPLIANCE_SCAN_PRODUCER = "compliance_scan"


@dataclass
class WorkspaceInventoryContext:
    """Per-bot run context."""

    bot_id: str
    shared_dir: Path


def observe(ctx: WorkspaceInventoryContext) -> list[Proposal]:
    """One Proposal per firing unregistered_script / unregistered_cron Signal."""
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
        factory = SIGNAL_TYPE_TO_FACTORY.get(getattr(sig, "type", None))
        if factory is None:
            continue
        try:
            for proposal in factory(sig):
                if proposal.dismiss_signature in suppressed:
                    continue
                proposals.append(proposal)
        except Exception:
            continue
    return proposals



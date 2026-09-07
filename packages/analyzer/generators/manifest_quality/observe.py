"""generators.manifest_quality.observe — Detector entry point.

Reads firing manifest-lifecycle Signals from the ``compliance_scan``
producer and dispatches each through ``SIGNAL_TYPE_TO_FACTORY``.
Mirrors the cache_ttl_tuner observe shape — the upstream monitor has
already computed the conditions; this generator just templates
Proposals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generators.manifest_quality.signal_proposals import (
    SIGNAL_TYPE_TO_FACTORY,
)
from schema.proposal import Proposal


GENERATOR_ID = "manifest_quality"
DIMENSION = "app_quality"

COMPLIANCE_SCAN_PRODUCER = "compliance_scan"


@dataclass
class ManifestQualityContext:
    """Per-bot run context."""

    bot_id: str
    shared_dir: Path
    # Phase A.5 — universal dismiss suppression. Per-(kind, app) signature
    # means dismissing stale finding for app X doesn't suppress
    # validation_error for app X, or the same kind for app Y.
    consult_dismissals: bool = True


def observe(ctx: ManifestQualityContext) -> list[Proposal]:
    """Emit one Proposal per firing manifest-lifecycle Signal for this bot.

    Filters by ``bot_id`` so a per-bot generator run only emits
    proposals for its own bot. Signal types not in
    ``SIGNAL_TYPE_TO_FACTORY`` are silently skipped — this generator
    intentionally consumes only the manifest-lifecycle subset of
    compliance_scan signals; workspace_inventory and workspace_security
    own the other types.
    """
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    # Phase A.5 — preload active dismiss signatures for this bot.
    suppressed = _load_suppressed_signatures(
        ctx.shared_dir, ctx.bot_id, consult=ctx.consult_dismissals,
    )

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
            # factory returns one Proposal per item in the rollup signal's
            # ``details.items[]``. Filter per-(kind, app) at the per-proposal
            # level so dismissing one app's stale finding doesn't suppress
            # other apps' findings of the same kind.
            for proposal in factory(sig):
                if proposal.dismiss_signature in suppressed:
                    continue
                proposals.append(proposal)
        except Exception:
            continue
    return proposals


def _load_suppressed_signatures(
    shared_dir: Path, bot_id: str, *, consult: bool,
) -> set[str]:
    """Thin wrapper around :func:`arbiter.dismissals.preload_suppressed_signatures`
    that honors the local ``consult`` test hook."""
    if not consult:
        return set()
    from arbiter.dismissals import preload_suppressed_signatures
    return preload_suppressed_signatures(shared_dir, bot_id)

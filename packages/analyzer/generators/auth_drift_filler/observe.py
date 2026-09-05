"""generators.auth_drift_filler.observe — Detector entry point.

Reads firing ``perm_config_drift`` Signals from the
``permission_monitor`` producer and fans each one out into per-field
ConfigPatch Proposals via the factory in ``signal_proposals.py``.
The factory does all the work; this module is just the signal-store
iterator + per-signal dispatcher with error swallowing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generators._signal_consumer import iter_firing_signals
from generators.auth_drift_filler.signal_proposals import (
    dismiss_signature_for_field,
    make_drift_proposals,
)
from schema.proposal import Proposal


GENERATOR_ID = "auth_drift_filler"
DIMENSION = "safety"

PERMISSION_MONITOR_PRODUCER = "permission_monitor"
CONSUMED_SIGNAL_TYPE = "perm_config_drift"


@dataclass
class AuthDriftFillerContext:
    """Per-bot run context.

    ``bot_id`` filters signals to this bot; ``shared_dir`` locates the
    Signal store. Like cron_caps_filler, this generator is per-bot —
    one drift signal per bot, fan-out into per-field proposals.
    """

    bot_id: str
    shared_dir: Path
    # Phase A.5 — universal dismiss suppression. Per-field signature
    # means dismissing one drifted field doesn't suppress unrelated
    # drift on other fields for the same bot.
    consult_dismissals: bool = True


def observe(ctx: AuthDriftFillerContext) -> list[Proposal]:
    """Emit one Proposal per drifted field, across all firing drift signals.

    Per-signal failures are swallowed so one bad payload doesn't
    torpedo the rest of the run.
    """
    # Phase A.5 — preload active dismiss signatures for this bot.
    # Filter per-field after the factory produces the per-field
    # proposals, since the signature uses the field name.
    suppressed = _load_suppressed_signatures(
        ctx.shared_dir, ctx.bot_id, consult=ctx.consult_dismissals,
    )

    proposals: list[Proposal] = []
    for sig in iter_firing_signals(
        ctx.shared_dir,
        ctx.bot_id,
        PERMISSION_MONITOR_PRODUCER,
        CONSUMED_SIGNAL_TYPE,
    ):
        try:
            # Pass shared_dir so the factory can check the config-intent
            # sidecar (spec internal/spec-config-intent-system-2026-05-21.md §4)
            # and suppress proposals for deliberate deviations. Direct
            # factory call sites that don't supply shared_dir keep the
            # legacy "always propose" behavior — see the make_drift_proposals
            # signature docstring.
            for proposal in make_drift_proposals(sig, shared_dir=ctx.shared_dir):
                # Phase A.5 per-field gate.
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

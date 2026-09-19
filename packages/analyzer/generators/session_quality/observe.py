"""generators.session_quality.observe — Detector entry point.

Reads firing ``session_quality`` Signals from the ``cost_watchdog``
producer and dispatches each through ``make_session_quality_proposal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generators.session_quality.signal_proposals import (
    DISMISS_SIG_SESSION_QUALITY,
    make_session_quality_proposal,
)
from schema.proposal import Proposal


GENERATOR_ID = "session_quality"
DIMENSION = "cost"

COST_WATCHDOG_PRODUCER = "cost_watchdog"
CONSUMED_SIGNAL_TYPE = "session_quality"


@dataclass
class SessionQualityContext:
    """Per-bot run context."""

    bot_id: str
    shared_dir: Path


def observe(ctx: SessionQualityContext) -> list[Proposal]:
    """Emit one Proposal per firing session_quality Signal for this bot.

    Per-signal failures are swallowed so one malformed payload doesn't
    torpedo the rest of the run.
    """
    if ctx.shared_dir is None:
        return []
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    if _is_dismissed(ctx.shared_dir, ctx.bot_id):
        return []

    proposals: list[Proposal] = []
    for sig in signals_store.iter_active(
        ctx.shared_dir,
        producer=COST_WATCHDOG_PRODUCER,
        bot_id=ctx.bot_id,
        state="firing",
    ):
        if getattr(sig, "type", None) != CONSUMED_SIGNAL_TYPE:
            continue
        try:
            proposals.append(make_session_quality_proposal(sig))
        except Exception:
            continue
    return proposals


def _is_dismissed(shared_dir: Path, bot_id: str) -> bool:
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(
            shared_dir,
            signature=DISMISS_SIG_SESSION_QUALITY,
            bot_id=bot_id,
        )
    except Exception:
        return False

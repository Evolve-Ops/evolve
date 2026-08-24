"""generators.app_promotion.observe — one promotion offer per bot per day.

See ``__init__.py`` for why this package exists. This module is the adapter
between ``generator_runner``'s contract (build a context, call ``observe(ctx)``,
ingest whatever Proposals come back) and the promotion path's own decision
function.

THE DECISION IS NOT HERE. ``app_promotion_sweep.plan_offer`` scores every draft
with ``app_readiness``, gates it with ``app_promotion.evaluate_offer`` (the
"never" shield, a live snooze, already-defined, the cadence cap and readiness —
in that order of severity), ranks the survivors deterministically and mints at
most one Proposal. This module calls it and returns the result. Keeping the
decision in one function is what makes the scheduled path and the operator's
``--dry-run`` answer the same question.

WRITING IS NOT HERE EITHER, and that is the one real difference from the CLI
path. ``plan_offer`` returns the Proposal in ``draft``; the runner puts it
through ``arbiter.ingest``, so the scheduled path additionally gets fingerprint
dedup, the charter invariants and the operator's 14-day rejection cooldown. The
cadence cap still comes from the proposal store, so the two paths cannot
double-offer each other's day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

GENERATOR_ID = "app_promotion"

logger = logging.getLogger("generators.app_promotion")


@dataclass
class PromotionOfferContext:
    """Per-bot context built by ``generator_runner._make_app_promotion_ctx``.

    **There is deliberately no ``read_manifests`` seam here, and there was
    one.** It was added by reflex — ``sweep_bot`` takes an injected reader, so
    the context grew a field to pass one through. The independent review of
    #3750 (N3) then measured what that field actually did: the factory never set
    it, no test ever set it (every one patches
    ``app_promotion_sweep._read_manifests_for_bot``, which is the seam that
    already exists), and deleting the injection left the whole suite green. **A
    seam with no writer and no user is not a seam** — it is a second way for the
    reader to be resolved that nothing exercises, which is this arc's own
    "consumer with no producer" wearing a parameter's clothes. Removed rather
    than given a test, because the test would have been the only caller.
    """

    bot_id: str
    shared_dir: Path
    network: Mapping[str, Any]
    now: datetime


def observe(ctx: PromotionOfferContext) -> list:
    """Return at most one promotion Proposal for ``ctx.bot_id`` (often none).

    Never raises: ``generator_runner`` logs and counts an exception as a
    generator error, but a bot whose manifests are momentarily unreadable must
    not cost the other bots their sweep. A failure here contributes nothing,
    which is the same thing "no draft is eligible" contributes — the difference
    is in the log line, deliberately, because the two look identical in the
    outcome and only one of them is a problem.
    """
    from evolve_admin.applications import (  # pyright: ignore[reportMissingImports]
        app_promotion_sweep as sweep,
    )

    try:
        result, proposal = sweep.plan_offer(
            ctx.shared_dir,
            ctx.bot_id,
            network=ctx.network,
            read_manifests=sweep._read_manifests_for_bot,
            now=ctx.now,
        )
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        logger.warning(
            "app_promotion: sweep failed for %s (%s) — no offer this cycle",
            ctx.bot_id,
            exc,
        )
        return []

    if proposal is None:
        logger.info(
            "app_promotion: %s — considered %d draft(s), offered none (%s)",
            ctx.bot_id,
            result.considered,
            "cadence cap spent" if result.cadence_blocked else "nothing eligible",
        )
        return []
    return [proposal]

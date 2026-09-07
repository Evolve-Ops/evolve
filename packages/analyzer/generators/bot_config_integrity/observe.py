"""generators.bot_config_integrity.observe — dispatcher for openclaw.json checks.

Reads the bot's full openclaw.json once via the runtime seam, then
fans the parsed config out to each check registered in
``checks/__init__.py``. Each check returns 0+ Proposals; the
dispatcher concatenates them.

The per-bot openclaw.json read is intentionally pulled up here rather
than repeated inside each check — it's a sudo-subprocess call (~50ms
on the mini) and we want adding a new check to be free, not "+1
config read per check per bot per day".

Defensive against import + read failures: any failure returns []
rather than raising, so one bad check or a transient oc_cli error
doesn't poison the generator loop for the rest of the bot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schema.proposal import Proposal


GENERATOR_ID = "bot_config_integrity"
DIMENSION = "safety"


@dataclass
class BotConfigIntegrityContext:
    """Per-bot run context.

    Each registered check receives this ctx + the parsed openclaw.json
    config dict. ``shared_dir`` and ``now`` are passed through for
    checks that need them (e.g. checks that read auth-profiles or stamp
    timestamps).
    """

    bot_id: str
    shared_dir: Path
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _full_config_get_safe():
    """Resolve the runtime seam's full_config_get; return None if unavailable.

    The generator's import surface stays narrow so a missing analyzer
    dep doesn't crash the entire run.
    """
    try:
        from runtime.agent_runtime import get_runtime  # type: ignore
        return get_runtime().full_config_get
    except Exception:
        return None


def observe(ctx: BotConfigIntegrityContext) -> list[Proposal]:
    """Run every registered check against this bot's openclaw.json.

    Returns the union of every check's proposals. Each check is
    independent — one's failure doesn't block the others.
    """
    full_config_get = _full_config_get_safe()
    if full_config_get is None:
        return []

    cfg = full_config_get(ctx.bot_id)
    if cfg is None:
        # Either the bot is down or its config is unreadable. Either
        # way, no actionable proposal from this generator on this run.
        return []

    # Import here so a test can monkeypatch the registry without having
    # to redo the dispatcher's plumbing. The registry is small (~handful
    # of checks); the import cost is negligible.
    from generators.bot_config_integrity.checks import CHECKS

    all_proposals: list[Proposal] = []
    for check in CHECKS:
        try:
            proposals = check.run(ctx, cfg)
        except Exception:
            # One bad check shouldn't poison the loop. Log via the
            # generator runner's logger when this lands in production;
            # for now swallow + continue so the rest of the checks run.
            continue
        if proposals:
            all_proposals.extend(proposals)
    return all_proposals

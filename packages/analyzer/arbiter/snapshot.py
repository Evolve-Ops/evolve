"""arbiter.snapshot — Before-state capture via the applier dispatch table.

This module is a thin wrapper around ``arbiter.appliers.get_applier`` for
the snapshot step. Separating it lets future layers (L2 verify daemon)
import just the snapshot logic without pulling in the apply path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arbiter.appliers import get_applier
from schema.proposal import Action, RevertPlan, action_from_dict


def capture(action: Action, bot_id: str, *, ttl_days: int = 60) -> RevertPlan:
    """Capture a before-snapshot and build a RevertPlan for ``action``.

    The RevertPlan bundles:
      - before_snapshot: applier-specific blob
      - revert_action:   the same Action (appliers revert from their own
                         snapshot, so the same Action is replayed through
                         ``revert()`` — this is a bookkeeping marker)
      - expires_at:      when the snapshot can be GC'd on success path
    """
    kind = getattr(action, "kind", type(action).__name__)
    applier = get_applier(kind)
    snapshot = applier.capture_snapshot(action, bot_id)
    # Revert action is the same Action — the revert method uses the snapshot
    # as its source of truth, not a re-derived action.
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(
        timespec="seconds"
    )
    return RevertPlan(
        before_snapshot=snapshot,
        revert_action=action,
        expires_at=expires_at,
    )

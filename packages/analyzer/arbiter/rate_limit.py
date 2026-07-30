"""arbiter.rate_limit — Weekly surface-count limiter.

Spec §4.5. Default cap: 7 proposals surfaced per pod per week.
Critical urgencies bypass; everything else over the cap is held to next week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from schema.proposal import Proposal


# Urgencies that always surface, regardless of the weekly cap
BYPASS_URGENCIES: frozenset[str] = frozenset(
    {"security_critical", "operational_urgent"}
)


@dataclass
class RateLimitResult:
    """Output of rate-limit filtering.

    Fields:
        surfaceable: proposals eligible to be surfaced this cycle
        held: proposals held until the next week (reason: over the cap)
        cap: the effective cap applied
        cycle_key: a stable identifier for the current week (for the
            surface counter's bookkeeping — tests use this to reset)
    """

    surfaceable: list[Proposal] = field(default_factory=list)
    held: list[Proposal] = field(default_factory=list)
    cap: int = 7
    cycle_key: str = ""


def _iso_week_key(now: datetime) -> str:
    """Stable week key. Monday-based ISO weeks (%G-W%V)."""
    return now.strftime("%G-W%V")


def apply_rate_limit(
    sorted_proposals: list[Proposal],
    *,
    already_surfaced_this_week: int = 0,
    cap: int = 7,
    now: datetime | None = None,
) -> RateLimitResult:
    """Filter a ranked proposal list by the weekly cap.

    Args:
        sorted_proposals: proposals ordered highest-priority first
        already_surfaced_this_week: count of surfaced proposals so far
            in the current week (from persistent counter)
        cap: the weekly cap
        now: clock override for tests

    Critical-urgency proposals always surface. Others surface only if
    the running count hasn't exceeded ``cap``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    result = RateLimitResult(cap=cap, cycle_key=_iso_week_key(now))
    count = already_surfaced_this_week

    for p in sorted_proposals:
        if p.urgency in BYPASS_URGENCIES:
            result.surfaceable.append(p)
            continue
        if count < cap:
            result.surfaceable.append(p)
            count += 1
        else:
            result.held.append(p)

    return result

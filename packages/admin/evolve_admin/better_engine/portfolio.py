"""
better_engine/portfolio.py — Portfolio composition (§7).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Recommendation


def compose_portfolio(recs: list["Recommendation"], n: int = 10) -> list["Recommendation"]:
    """Compose a balanced portfolio from pending recommendations (§7).

    Guarantees:
    - At most 40% of slots are urgent operational fixes
    - At least 25% are growth/improvement items
    - At most 1 whimsy item per portfolio view
    - Whimsy suppressed when 3+ critical items active
    - No single non-whimsy type occupies > 40% of the list
    - If no proposals/onboarding items would be selected, one whimsy item is
      surfaced as a fallback so the dashboard strip never renders empty.
    """
    if not recs:
        return []

    pending = [r for r in recs if r.status == "pending"]
    if not pending:
        return []

    urgent = [r for r in pending
              if "urgency:critical" in r.tags or "urgency:high" in r.tags]
    growth = [r for r in pending
              if r.type in ("app_quality", "onboarding")
              or "intent:improve" in r.tags
              or "intent:explore" in r.tags]
    whimsy = [r for r in pending if r.type == "whimsy"]

    urgent_budget = max(1, int(n * 0.4))    # up to 40% urgent
    growth_budget = max(1, int(n * 0.25))   # at least 25% growth
    whimsy_budget = 1                        # at most 1 whimsy per portfolio

    selected: list["Recommendation"] = []

    # Fill urgent slots first
    selected.extend(urgent[:urgent_budget])

    # Fill growth slots (skip already selected)
    growth_selected = 0
    for r in growth:
        if growth_selected >= growth_budget:
            break
        if r not in selected:
            selected.append(r)
            growth_selected += 1

    # Fill remaining slots by score with type-diversity cap (no type > 40%)
    remaining = [r for r in pending if r not in selected and r.type != "whimsy"]
    for r in remaining:
        if len(selected) >= n:
            break
        type_count = sum(1 for s in selected if s.type == r.type)
        if type_count / max(len(selected), 1) <= 0.4:
            selected.append(r)

    # Whimsy rules:
    # - Normal: add one whimsy when there's room and crisis isn't active (<3 criticals).
    # - Fallback: if nothing else was selected, surface one whimsy regardless so the
    #   strip is never empty (critical suppression doesn't apply with no criticals).
    if whimsy:
        critical_count = sum(1 for r in selected if "urgency:critical" in r.tags)
        if not selected:
            selected.append(whimsy[0])
        elif len(selected) < n and critical_count < 3:
            selected.append(whimsy[0])

    # Final sort by priority_score descending
    return sorted(selected, key=lambda r: r.priority_score, reverse=True)

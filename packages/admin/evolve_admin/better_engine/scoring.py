"""
better_engine/scoring.py — Priority score computation (§4).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Recommendation

# The named URGENCY_* / IMPACT_* / ACTIONABILITY_* constants tabled here
# under §4.2–§4.4 of the original spec are retired alongside the
# pipeline-unification arc (ScoreboardAdapter / ComplianceAdapter /
# suggestions.py adapter retirements). All actionable recs now flow
# through generator → arbiter Proposals, whose ``urgency`` field uses
# string labels (security_critical / operational_urgent / cost_alert /
# substrate_warn / improvement / hygiene / whimsy). The ladder lives in
# ``better_engine/proposal_reader.py::URGENCY_SCORE`` — the canonical
# numeric mapping for the bridge.
#
# The compute_*_score / freshness_score / urgency_label functions below
# still drive priority scoring (taking already-numeric component
# values from ``rec.priority_components``); they no longer need the
# named numeric anchors.


# ── §4.5 Freshness ────────────────────────────────────────────────────────────

def freshness_score(created_at: str, snooze_count: int) -> int:
    """Compute freshness component (0–10).

    Clamped to 1 if snooze_count >= 3 (user is actively deferring).
    'First seen this refresh' is approximated by created_at being within
    the last 15 minutes (one scheduler cycle).
    """
    if snooze_count >= 3:
        return 1

    now = datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(created_at)
        # Ensure timezone-aware for comparison
        if created.tzinfo is None:
            from datetime import timezone as _tz
            created = created.replace(tzinfo=_tz.utc)
    except (ValueError, TypeError):
        return 1

    age_seconds = (now - created).total_seconds()
    age_days = age_seconds / 86400

    if age_seconds < 900:       # < 15 minutes — first seen this refresh
        return 10
    elif age_days < 1:          # < 1 day
        return 7
    elif age_days <= 3:         # 1–3 days
        return 5
    elif age_days <= 7:         # 3–7 days
        return 3
    else:                       # > 7 days
        return 1


def compute_base_score(urgency: int, impact: int, actionability: int, freshness: int) -> int:
    """Sum of the four components; does not clamp."""
    return urgency + impact + actionability + freshness


def compute_priority_score(rec: "Recommendation") -> int:
    """Apply learning_weight to base_score and clamp to 0–100."""
    components = rec.priority_components
    urgency = components.get("urgency", 0)
    impact = components.get("impact", 0)
    actionability = components.get("actionability", 0)
    freshness = components.get("freshness", freshness_score(rec.created_at, rec.snooze_count))

    base = compute_base_score(urgency, impact, actionability, freshness)
    raw = round(base * rec.learning_weight)
    return max(0, min(100, raw))


def urgency_label(score: int) -> str:
    """Map a priority_score to a human-readable urgency tier label."""
    if score >= 70:
        return "critical"
    elif score >= 40:
        return "high"
    elif score >= 20:
        return "medium"
    else:
        return "low"

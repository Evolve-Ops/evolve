"""
better_engine/learning.py — Bayesian learning engine (§5).
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Recommendation


def _age_days(ts: str) -> float:
    """Return age in days of an ISO timestamp relative to now."""
    now = datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return 9999.0


# ── §5.3 Bayesian Rate ────────────────────────────────────────────────────────

def bayesian_rate(accepted: int, rejected: int, snoozed: int) -> float:
    """Compute acceptance rate with Bayesian prior (§5.3).

    Prior: 2 pseudocounts at 0.5 — regresses to neutral with small samples.
    Snooze counts as 0.4 positive (useful but bad timing).
    """
    effective_positive = accepted + (snoozed * 0.4) + 1.0
    effective_total = accepted + rejected + (snoozed * 0.4) + 2.0
    return effective_positive / effective_total


# ── §5.4 Weight Computation ───────────────────────────────────────────────────

def compute_learning_weight(rec: "Recommendation", signals: dict) -> float:
    """Compute the learning weight for a recommendation (§5.4).

    Single-tag signals require >= 5 observations.
    Pair signals require >= 3 observations.
    Pairs weighted 2x (more specific signal).
    Maps blended_rate (0.0–1.0) → weight (0.4–1.6).
    """
    # Single-tag signals
    single_weights: list[tuple[float, int]] = []
    for tag in rec.tags:
        if tag in signals:
            s = signals[tag]
            total = s["accepted"] + s["rejected"] + s["snoozed"]
            if total >= 5:
                rate = bayesian_rate(s["accepted"], s["rejected"], s["snoozed"])
                single_weights.append((rate, total))

    # Two-tag combination signals
    pair_weights: list[tuple[float, int]] = []
    for t1, t2 in combinations(sorted(rec.tags), 2):
        key = f"{t1}+{t2}"
        if key in signals:
            s = signals[key]
            total = s["accepted"] + s["rejected"] + s["snoozed"]
            if total >= 3:
                rate = bayesian_rate(s["accepted"], s["rejected"], s["snoozed"])
                pair_weights.append((rate, total))

    if not single_weights and not pair_weights:
        return 1.0  # no data — neutral

    # Pairs weighted 2x; observation counts capped to limit runaway influence
    weighted_sum = (
        sum(rate * min(count, 20) * 1.0 for rate, count in single_weights) +
        sum(rate * min(count, 10) * 2.0 for rate, count in pair_weights)
    )
    weight_total = (
        sum(min(count, 20) * 1.0 for _, count in single_weights) +
        sum(min(count, 10) * 2.0 for _, count in pair_weights)
    )

    blended_rate = weighted_sum / weight_total

    # Map 0.0–1.0 → 0.4–1.6
    return 0.4 + (blended_rate * 1.2)


# ── §5.5 Bot-Level Override ───────────────────────────────────────────────────

def get_effective_weight(rec: "Recommendation", learning: dict) -> float:
    """Combine tag weight × bot override multiplier (§5.5)."""
    tag_weight = compute_learning_weight(rec, learning.get("signals", {}))

    bot_overrides = learning.get("bot_overrides", {})
    bot_override = bot_overrides.get(rec.scope_id, {}).get(rec.type, {})
    multiplier = bot_override.get("multiplier", 1.0) if bot_override else 1.0

    return tag_weight * multiplier


# ── §5.6 Time Decay ───────────────────────────────────────────────────────────

def apply_time_decay(events: list[dict]) -> list[dict]:
    """Attach a _weight field to each event based on age (§5.6)."""
    result = []
    for event in events:
        age = _age_days(event.get("ts", ""))
        if age < 30:
            weight = 1.0
        elif age <= 60:
            weight = 0.5
        else:
            weight = 0.2
        result.append({**event, "_weight": weight})
    return result


# ── §5.7 Feedback Recording ───────────────────────────────────────────────────

def record_feedback(
    rec: "Recommendation",
    signal: str,
    reason: str,
    learning: dict,
) -> dict:
    """Record feedback and update learning signals (§5.7).

    Does NOT save to disk — caller is responsible for saving.
    Returns the updated learning dict.
    """
    from .model import now_iso

    # 1. Update all single-tag signals
    for tag in rec.tags:
        s = learning["signals"].setdefault(tag, {"accepted": 0, "rejected": 0, "snoozed": 0})
        s[signal] = s.get(signal, 0) + 1

    # 2. Update all 2-tag combination signals
    for t1, t2 in combinations(sorted(rec.tags), 2):
        key = f"{t1}+{t2}"
        s = learning["signals"].setdefault(key, {"accepted": 0, "rejected": 0, "snoozed": 0})
        s[signal] = s.get(signal, 0) + 1

    # 3. Check for bot-level override trigger
    if signal == "rejected":
        recent_rejects = [
            e for e in learning.get("recent_events", [])
            if e.get("scope_id") == rec.scope_id
            and e.get("type") == rec.type
            and e.get("signal") == "rejected"
            and _age_days(e.get("ts", "")) <= 14
        ]
        if len(recent_rejects) >= 2:
            learning.setdefault("bot_overrides", {}) \
                    .setdefault(rec.scope_id, {})[rec.type] = {
                "multiplier": 0.5,
                "reason": f"rejected {len(recent_rejects) + 1}x in 14 days",
                "set_at": now_iso(),
            }

    # 4. Append to rolling event log (cap at 200)
    learning.setdefault("recent_events", []).append({
        "rec_id": rec.id,
        "dedup_key": rec.dedup_key,
        "type": rec.type,
        "source": rec.source,
        "scope_id": rec.scope_id,
        "tags": rec.tags,
        "signal": signal,
        "reason": reason,
        "ts": now_iso(),
    })
    learning["recent_events"] = learning["recent_events"][-200:]

    return learning

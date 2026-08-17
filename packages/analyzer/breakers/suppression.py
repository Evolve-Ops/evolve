"""breakers.suppression — the "don't fight the breaker" contract.

Spec: docs/spec-circuit-breakers-2026-05-21.md §5.5.

When a breaker is tripped, the operator has already taken (or had taken
on their behalf) a deliberate action to stop a class of activity on
that bot. The rest of the observability + automation stack MUST NOT
keep generating panic alerts about the very thing the trip addressed,
or try to "fix" the state by piling on with more proposals or applying
config changes against a halted bot.

This module is the shared helper that lets every monitor/generator/
applier do the lookup uniformly:

    from breakers.suppression import find_suppressing_breaker
    rec = find_suppressing_breaker(shared_dir, bot_id, category="cost")
    if rec is not None:
        # skip / tag for forensics — do not generate a fresh alert
        ...

Failure-mode discipline: this helper is fail-open. If the breaker
store is unreachable or the read fails, ``find_suppressing_breaker``
returns ``None``. We never block legitimate alerting on a broken
breaker file — that direction is the whole reason the breaker exists.

Pod-scope (``bot_id == "pod"``) breakers suppress for every bot — a
pod-wide L2 means everything is halted, and a pod-wide L1 means we
deliberately stopped background activity across the pod. The lookup
returns the matching pod-scope record so the caller can tag the
suppression with the pod-scope trip_id.

Suppression categories:

  ``cost``           — cost-anomaly signals + cost-related proposals.
                       Suppressed when the bot has an active L1 ``cost``
                       breaker (or pod-wide), OR an L2 ``full`` (which
                       implies all activity is stopped, including cost).

  ``config_change``  — proposal application against a bot's config.
                       Suppressed when the bot has any active L2
                       breaker (or pod-wide) — applying a config tweak
                       to a halted bot is meaningless and the new
                       value won't take effect until the operator
                       reactivates the bot, by which point the
                       proposal may no longer be appropriate. L1
                       breakers do NOT suppress this (the gateway is
                       still up and config writes still work).

  ``gateway_alert``  — "gateway is down" / "bot offline" signals.
                       Suppressed when the bot has an active L2 ``full``
                       breaker — the gateway-down state is by design.
                       L1 breakers do NOT suppress this (gateway should
                       be up).

  ``automation``     — automation/dominance/cron-overactive signals.
                       Suppressed by L1 ``cost`` (we already stopped
                       background activity) or L2 ``full``.

Categories are intentionally narrow — adding a new one requires a
deliberate code change here, and the default is "no suppression"
rather than "guess based on string matching". This is the "no
negative feedback loop" invariant: each suppression edge is a
named promise.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable

from breakers import store as _store
from breakers.store import BreakerRecord


log = logging.getLogger(__name__)


# Mapping from (suppression_category) → (set of breaker types that
# suppress that category). Edit this dict to add a new category;
# don't sprinkle the logic across callers.
_CATEGORY_TO_SUPPRESSING_TYPES: dict[str, frozenset[str]] = {
    "cost": frozenset({"cost", "full"}),
    "config_change": frozenset({"full"}),
    "gateway_alert": frozenset({"full"}),
    "automation": frozenset({"cost", "full"}),
}


# Valid category strings, exported for the convenience of test code +
# documentation. Callers can pass any string; an unknown category
# returns None (no suppression) — fail-open by default.
SUPPRESSION_CATEGORIES: frozenset[str] = frozenset(_CATEGORY_TO_SUPPRESSING_TYPES)


def get_active_breakers(
    shared_dir: Path,
    bot_id: str,
    *,
    now: datetime | None = None,
) -> list[BreakerRecord]:
    """Return active breakers affecting ``bot_id`` — its own trips plus
    any pod-scope trips.

    Fail-open: returns ``[]`` if the store can't be read. Callers must
    not treat an empty list as proof that nothing is tripped — but
    they MAY treat it as "no known suppression," which is the same
    behavior they'd have without this module wired in.
    """
    try:
        active = _store.list_active(shared_dir, now=now)
    except Exception as exc:  # noqa: BLE001
        log.warning("suppression: list_active failed: %s", exc)
        return []
    return [
        r for r in active
        if r.bot_id == bot_id or r.bot_id == _store.POD_SCOPE
    ]


def find_suppressing_breaker(
    shared_dir: Path,
    bot_id: str,
    *,
    category: str,
    now: datetime | None = None,
) -> BreakerRecord | None:
    """Return the breaker record currently suppressing ``category`` for
    ``bot_id``, or None if no relevant breaker is active.

    When more than one breaker could suppress (e.g. both a per-bot
    cost trip and a pod-wide full trip are active), the FIRST match
    in iteration order is returned — the choice is arbitrary as long
    as the caller's behavior is "skip / tag / defer," but the per-bot
    breaker is generally more specific so callers see the precise
    trip_id rather than the broader pod-wide one.
    """
    suppressing_types = _CATEGORY_TO_SUPPRESSING_TYPES.get(category)
    if not suppressing_types:
        return None

    records = get_active_breakers(shared_dir, bot_id, now=now)
    # Per-bot trips sort before pod-wide trips so the caller sees the
    # most-specific match. ``list_active`` already returns scopes in
    # sorted order (alphabetic), and "pod" sorts after most bot_ids
    # in practice — but pin the invariant explicitly.
    records.sort(key=lambda r: 1 if r.bot_id == _store.POD_SCOPE else 0)
    for rec in records:
        if rec.type in suppressing_types:
            return rec
    return None


def suppression_tag(record: BreakerRecord) -> dict[str, str]:
    """Build the tag dict callers attach to suppressed signals / log lines.

    Matches the shape the Signal store's ``details`` field expects.
    Stored alongside whatever fields the underlying detector wrote so
    forensics queries can find every signal that WOULD have fired
    during a trip's lifetime, even though they weren't surfaced as
    fresh alerts.
    """
    return {
        "suppressed_by_breaker": record.trip_id,
        "suppressed_breaker_type": record.type,
        "suppressed_breaker_scope": record.bot_id,
    }


# ── Convenience helpers — one-call yes/no for common callsites ──────────────


def is_cost_suppressed(
    shared_dir: Path, bot_id: str, *, now: datetime | None = None,
) -> BreakerRecord | None:
    """Shortcut for ``find_suppressing_breaker(category='cost')``."""
    return find_suppressing_breaker(
        shared_dir, bot_id, category="cost", now=now,
    )


def is_config_change_suppressed(
    shared_dir: Path, bot_id: str, *, now: datetime | None = None,
) -> BreakerRecord | None:
    """Shortcut for ``find_suppressing_breaker(category='config_change')``."""
    return find_suppressing_breaker(
        shared_dir, bot_id, category="config_change", now=now,
    )


__all__ = [
    "SUPPRESSION_CATEGORIES",
    "find_suppressing_breaker",
    "get_active_breakers",
    "is_config_change_suppressed",
    "is_cost_suppressed",
    "suppression_tag",
]

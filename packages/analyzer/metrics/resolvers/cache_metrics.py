"""metrics.resolvers.cache_metrics — Prompt-cache economics metrics (L6).

Resolves:
  - ``cache_invalidation_ratio``: of the cache-participating cost events
    over the trailing window, the share whose ``cache_state ==
    "invalidated"`` — i.e. turns where we paid the Anthropic prompt-cache
    *write* cost but the next turn missed the cache, so the write never
    paid off. This is the metric ``cache_ttl_tuner``'s UpdateAgentDefaults
    claim (``cacheRetention`` short → long) is graded against: the verify
    daemon reads it at ``apply_time + window_days`` and checks the ratio
    dropped (``direction=down``).

Data source. Every cost_event written by the plugin's ``CostLedger.ts``
carries a ``cache_state`` field (one of ``fresh``/``warm``/``invalidated``/
``unknown``, from ``inferCacheState()``). The ratio is computed over the
*cache-participating* set — events whose ``cache_state ∈ {warm,
invalidated}`` — exactly as the ``session_economics`` monitor does when it
emits the ``cache_invalidation_elevated`` Signal that motivates the
proposal in the first place. We import the monitor's ``_aggregate_cache``
as the single source of truth for the participating-set definition so the
apply-time baseline (computed by the monitor / generator) and the
verify-time reading (this resolver) can never drift apart.

Window. ``CACHE_INVALIDATION_LOOKBACK_DAYS`` MUST equal the ``window_days``
on the Claim that ``cache_ttl_tuner`` emits (7), so the baseline (pre-flip,
trailing-Nd) and the verify reading (post-flip, trailing-Nd resolved at
apply_time + window_days) cover symmetric windows. Same invariant the cost
resolvers document.

Confidence. Mirrors the cost resolvers: when there are too few
cache-participating events to compute a stable ratio (below the same
``cache_min_events`` floor the monitor requires before it will fire), we
return the ratio with *low* confidence so the verify daemon treats it as
unresolved/retry rather than mistaking a noisy reading for a real
improvement. A window with zero participating events likewise returns low
confidence — never a real ``0.0`` ratio that would spuriously satisfy a
``down`` claim.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR
from metrics.registry import MetricSpec, MetricValue, register


# Platform-keyed shared-dir root (``/Users/Shared/evolve`` on macOS,
# ``/var/lib/evolve`` on Linux — design doc 2026-06-10 §6). Resolved
# through evolve_config so the literal lives only in platform_profile.
# The verify daemon's tests override this via ``set_shared_dir``.
_SHARED_DIR = CANONICAL_SHARED_DIR


def set_shared_dir(path: Path) -> None:
    """Test hook: override the shared-dir root used for ledger lookups."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


# Window for the invalidation-ratio computation. MUST equal the
# ``window_days`` on cache_ttl_tuner's Claim (signal_proposals.py builds
# it with ``window_days=7``) and the monitor's default
# ``cache_window_days`` (session_economics.DEFAULTS), so the baseline and
# the verify reading cover symmetric trailing windows.
CACHE_INVALIDATION_LOOKBACK_DAYS: int = 7

# Minimum cache-participating events required before the ratio is treated
# as high-confidence. Below this the ratio is too noisy to verify a claim
# against — the verify daemon should retry, not grade. Kept in lockstep
# with session_economics.DEFAULTS["cache_min_events"]: the monitor refuses
# to *fire* the Signal below this count, so the resolver refusing to grade
# below it keeps the two ends symmetric.
CACHE_INVALIDATION_MIN_EVENTS: int = 50


def resolve_cache_invalidation_ratio(
    bot_id: str,
    as_of: datetime,
    *,
    lookback_days: int = CACHE_INVALIDATION_LOOKBACK_DAYS,
) -> MetricValue:
    """Invalidated share of cache-participating cost events over the window.

    Returns ``invalidated_count / participating_count`` where
    ``participating = warm + invalidated`` cost events (fresh/unknown
    excluded — a bot with mostly fresh sessions isn't misconfigured,
    there's just nothing to cache).

    Returns the ratio with low confidence (→ unresolved/retry in the
    verify daemon, which floors at 0.5) when there are fewer than
    ``CACHE_INVALIDATION_MIN_EVENTS`` cache-participating events in the
    window, so a thin/empty window is never mistaken for a real low-
    invalidation reading that would spuriously satisfy a ``down`` claim.
    """
    from cost_ledger import read_events
    from session_economics import _aggregate_cache

    events = read_events(
        bot_id, days=lookback_days, shared_dir=_SHARED_DIR, now=as_of
    )
    agg = _aggregate_cache(events)
    participating = int(agg["participating_count"])
    invalidated = int(agg["invalidated_count"])

    if participating < CACHE_INVALIDATION_MIN_EVENTS:
        # Compute the ratio anyway (harmless, useful in the note), but flag
        # it low-confidence so the verify daemon retries instead of grading.
        ratio = invalidated / participating if participating > 0 else 0.0
        return MetricValue(
            value=round(ratio, 6),
            confidence=0.3,
            source_note=(
                f"only {participating} cache-participating events in "
                f"trailing {lookback_days}d (need "
                f"{CACHE_INVALIDATION_MIN_EVENTS}); insufficient data "
                "(treated as unresolved, not a real low-invalidation reading)"
            ),
        )

    ratio = invalidated / participating
    return MetricValue(
        value=round(ratio, 6),
        confidence=1.0,
        source_note=(
            f"{invalidated} invalidated / {participating} participating "
            f"= {ratio:.3f} over {lookback_days}d"
        ),
    )


register(
    MetricSpec(
        name="cache_invalidation_ratio",
        description=(
            f"Share of trailing-{CACHE_INVALIDATION_LOOKBACK_DAYS}d "
            "cache-participating cost events (warm + invalidated) whose "
            "cache_state is invalidated. Direction down — a successful "
            "cache_ttl_tuner cacheRetention short→long flip should drop "
            "the share toward 0."
        ),
        unit="ratio",
        source=(
            "cost_event.cache_state via cost_ledger.read_events + "
            "session_economics._aggregate_cache"
        ),
    ),
    resolve_cache_invalidation_ratio,
)

"""Cost metric resolvers (L6).

Functional:
  - ``cost.background_share_trend``: share of cost over the trailing
    ``BACKGROUND_SHARE_LOOKBACK_DAYS`` that came from background
    trigger_kinds (heartbeat, classifier, summarizer, task_extractor)
    versus foreground (user_turn, fallback). Used by Efficiency Hawk's
    cron/heartbeat-dominance detector to verify that the operator's
    intervention actually moved the share down.

  - ``cost.maintenance_high_tier_share``: of the cost spent on sessions
    classified as ``maintenance``, what share ran on a high-tier model
    (Sonnet/Opus or equivalent) instead of a low-tier (Haiku) model.
    Used by Efficiency Hawk's tier-misrouting detector — a maintenance
    session running on Sonnet is wasted spend in most pods.

Both metrics are ratios in [0.0, 1.0]. Direction "down" means the share
shifted away from the wasteful bucket.

Trigger-kind classification rationale (background_share_trend):
  - ``user_turn`` — direct conversation; the foreground baseline.
  - ``fallback`` — gateway-retry of a user_turn on a fallback model. The
    cost replaces the user_turn's; treating it as background would
    inflate the share artificially when the gateway is flaky.
    Foreground.
  - ``subagent`` — present in the schema but not actually emitted by the
    plugin's inferTriggerKind today (falls through to "unknown"). Kept
    out of both buckets so reclassification later doesn't surprise us.
  - ``unknown`` / missing — excluded from both buckets so the
    classified-share gate can detect "we can't trust the split".

Tier classification (maintenance_high_tier_share):
  Heuristic-by-substring against the cost_event ``model`` string. We
  treat anything containing ``haiku``, ``mini``, or ``flash`` as
  low-tier (the cheap/fast tier3 models across major providers per
  ``model_registry.py``). Everything else with a recognisable provider
  family — Sonnet/Opus/GPT-4*/Gemini Pro/Grok-4 — is high-tier.
  Unrecognised model strings are excluded from both buckets so the
  classified-share gate can detect missing data.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from metrics.registry import MetricSpec, MetricValue, register


_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


# Single source of truth for the window used by both this resolver and
# the detector in generators/efficiency_hawk/cost_efficiency.py. If they
# diverge, the baseline at proposal-creation time will not match what
# the verify daemon resolves at apply_time + window_days.
BACKGROUND_SHARE_LOOKBACK_DAYS: int = 14

# Trigger kinds we treat as background (non-user-initiated) work. Kept
# in sync with the detector. Excludes ``fallback`` (user-induced retry)
# and ``subagent`` (currently unemitted; would be user-induced when it is).
BACKGROUND_TRIGGER_KINDS: frozenset[str] = frozenset(
    {
        "heartbeat",
        "classifier",
        "summarizer",
        "task_extractor",
    }
)
FOREGROUND_TRIGGER_KINDS: frozenset[str] = frozenset({"user_turn", "fallback"})

# Confidence falls off when most events lack a trigger_kind tag — we
# can't reliably split background vs. foreground if classification is
# below this.
_HIGH_CONFIDENCE_CLASSIFIED_SHARE = 0.80


def resolve_cost_background_share_trend(
    bot_id: str,
    as_of: datetime,
    *,
    lookback_days: int = BACKGROUND_SHARE_LOOKBACK_DAYS,
) -> MetricValue:
    """Background-cost share over the trailing window.

    Returns ``background_cost_usd / (background + foreground)``. Events
    with a trigger_kind we don't classify (or no trigger_kind at all)
    are excluded from both numerator and denominator.

    Returns 0.0 with low confidence when there is no classifiable spend
    in the window — callers verifying a claim should treat that as
    "insufficient data" rather than success.
    """
    from cost_ledger import read_events

    end = as_of

    background_usd = 0.0
    foreground_usd = 0.0
    total_usd = 0.0
    for event in read_events(bot_id, days=lookback_days, shared_dir=_SHARED_DIR, now=end):
        # ``read_events`` already filters to the trailing window via
        # observations.access.window; no extra timestamp gating needed.
        cost = float(event.get("cost_usd", 0.0) or 0.0)
        total_usd += cost
        kind = event.get("trigger_kind") or ""
        if kind in BACKGROUND_TRIGGER_KINDS:
            background_usd += cost
        elif kind in FOREGROUND_TRIGGER_KINDS:
            foreground_usd += cost

    classified_usd = background_usd + foreground_usd
    if classified_usd <= 0:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note=(
                f"no classified cost in trailing {lookback_days}d "
                f"(total ${total_usd:.4f}); insufficient data"
            ),
        )

    share = background_usd / classified_usd
    classified_share = classified_usd / total_usd if total_usd > 0 else 0.0
    confidence = (
        1.0 if classified_share >= _HIGH_CONFIDENCE_CLASSIFIED_SHARE else 0.5
    )
    return MetricValue(
        value=round(share, 6),
        confidence=confidence,
        source_note=(
            f"background ${background_usd:.4f} / classified ${classified_usd:.4f} "
            f"= {share:.3f} over {lookback_days}d "
            f"(classified_share={classified_share:.2f})"
        ),
    )


register(
    MetricSpec(
        name="cost.background_share_trend",
        description=(
            f"Share of trailing-{BACKGROUND_SHARE_LOOKBACK_DAYS}d cost from "
            "background trigger_kinds (heartbeat, classifier, summarizer, "
            "task_extractor) over (background + foreground) cost. "
            "Foreground = user_turn + fallback."
        ),
        unit="ratio",
        source="cost_event records via cost_ledger.read_events",
    ),
    resolve_cost_background_share_trend,
)


# ─────────────────────────────────────────────────────────────────────────────
# cost.maintenance_high_tier_share
# ─────────────────────────────────────────────────────────────────────────────

# Substrings that identify low-tier (cheap/fast) models across providers.
# Keep in sync with model_registry.RECOMMENDED's tier3 entries:
#   anthropic/claude-haiku-4-5, openai/gpt-4o-mini, google/gemini-2.0-flash,
#   xai/grok-4-mini.
_LOW_TIER_MODEL_TOKENS: frozenset[str] = frozenset({"haiku", "mini", "flash"})

# Substrings that identify high-tier (workhorse/power) models. Anything
# containing one of these AND not a low-tier token is treated as high
# tier. Plain "gpt-4" matches because OpenAI tier1+tier2 both use 4o.
_HIGH_TIER_MODEL_TOKENS: frozenset[str] = frozenset(
    {"sonnet", "opus", "gpt-4", "pro", "grok-4"}
)


def classify_model_tier(model: str | None) -> str:
    """Return ``"high"``, ``"low"``, or ``"unknown"`` for a model string.

    Handles both bare names (``claude-sonnet-4-6``) and provider-prefixed
    forms (``anthropic/claude-sonnet-4-6``). Tokens are matched
    case-insensitively. Low-tier wins over high-tier when both match
    (``gpt-4o-mini`` contains both ``gpt-4`` and ``mini`` — it's low).
    """
    if not model:
        return "unknown"
    name = model.lower()
    if any(tok in name for tok in _LOW_TIER_MODEL_TOKENS):
        return "low"
    if any(tok in name for tok in _HIGH_TIER_MODEL_TOKENS):
        return "high"
    return "unknown"


# Same window as background_share_trend — keep them aligned so detectors
# that consume both metrics see consistent baselines.
MAINTENANCE_TIER_LOOKBACK_DAYS: int = 14

# Confidence falls off when we can't classify most of the maintenance
# spend by tier (e.g. cost_events with empty / unknown model strings).
_HIGH_CONFIDENCE_TIER_CLASSIFIED_SHARE = 0.80


def resolve_cost_maintenance_high_tier_share(
    bot_id: str,
    as_of: datetime,
    *,
    lookback_days: int = MAINTENANCE_TIER_LOOKBACK_DAYS,
) -> MetricValue:
    """High-tier share of maintenance-session cost over the trailing window.

    Joins ``cost_event`` records to ``session_summary.session_class``.
    For sessions where ``session_class == "maintenance"``, sums cost by
    tier of the event's model. Returns
    ``high_tier_cost / (high_tier_cost + low_tier_cost)``.

    Returns 0.0 with low confidence when there's no classifiable
    maintenance spend in the window — callers verifying a claim should
    treat that as "insufficient data" rather than success.
    """
    from datetime import timedelta

    from cost_ledger import read_events
    from observations.access import window as obs_window

    end = as_of

    # Build session_id → session_class from session_summary records.
    # Pin start/end so the summary window aligns with the cost reader's
    # trailing window (cost_ledger.read_events derives the same range
    # internally from `now=end`).
    w = obs_window(
        bot_id,
        start=end - timedelta(days=lookback_days),
        end=end,
        shared_dir=_SHARED_DIR,
    )
    session_class: dict[str, str] = {}
    for summary in w.session_summaries():
        sid = summary.get("session_id")
        cls = summary.get("session_class")
        if isinstance(sid, str) and isinstance(cls, str):
            session_class[sid] = cls

    high_usd = 0.0
    low_usd = 0.0
    maintenance_total_usd = 0.0
    for event in read_events(bot_id, days=lookback_days, shared_dir=_SHARED_DIR, now=end):
        sid = event.get("session_id")
        if not isinstance(sid, str):
            continue
        if session_class.get(sid) != "maintenance":
            continue
        cost = float(event.get("cost_usd", 0.0) or 0.0)
        maintenance_total_usd += cost
        tier = classify_model_tier(event.get("model"))
        if tier == "high":
            high_usd += cost
        elif tier == "low":
            low_usd += cost

    classified_usd = high_usd + low_usd
    if classified_usd <= 0:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note=(
                f"no classified maintenance cost in trailing {lookback_days}d "
                f"(maintenance total ${maintenance_total_usd:.4f}); "
                "insufficient data"
            ),
        )

    share = high_usd / classified_usd
    classified_share = (
        classified_usd / maintenance_total_usd if maintenance_total_usd > 0 else 0.0
    )
    confidence = (
        1.0
        if classified_share >= _HIGH_CONFIDENCE_TIER_CLASSIFIED_SHARE
        else 0.5
    )
    return MetricValue(
        value=round(share, 6),
        confidence=confidence,
        source_note=(
            f"high_tier ${high_usd:.4f} / classified ${classified_usd:.4f} "
            f"= {share:.3f} over {lookback_days}d "
            f"(classified_share={classified_share:.2f})"
        ),
    )


register(
    MetricSpec(
        name="cost.maintenance_high_tier_share",
        description=(
            f"Share of trailing-{MAINTENANCE_TIER_LOOKBACK_DAYS}d "
            "maintenance-session cost on high-tier models (Sonnet/Opus or "
            "equivalent) over (high + low tier) maintenance cost. Direction "
            "down — a successful TierAdjustment from this generator should "
            "drop the share toward 0."
        ),
        unit="ratio",
        source=(
            "cost_event records joined to session_summary.session_class "
            "via cost_ledger.read_events + observations.access.window"
        ),
    ),
    resolve_cost_maintenance_high_tier_share,
)


# ─────────────────────────────────────────────────────────────────────────────
# cost.daily_usd
# ─────────────────────────────────────────────────────────────────────────────

# Lookback for the daily-spend metric. MUST equal the ``window_days`` on Claims
# that target ``cost.daily_usd`` (Budget Hawk's TierAdjustment claims use 7), so
# the apply-time baseline (trailing-Nd average BEFORE the change) and the
# verify-time reading (trailing-Nd average AFTER, resolved at apply_time +
# window_days) cover symmetric windows. Budget Hawk imports this constant for its
# baseline fill — keep them in lockstep. Same invariant the background-share
# resolver documents above.
DAILY_USD_LOOKBACK_DAYS: int = 7


def resolve_cost_daily_usd(
    bot_id: str,
    as_of: datetime,
    *,
    lookback_days: int = DAILY_USD_LOOKBACK_DAYS,
) -> MetricValue:
    """Average daily API spend (USD) over the trailing window ending at ``as_of``.

    Sums ``cost_usd`` across cost_events in the trailing ``lookback_days`` and
    divides by the window length, giving a noise-robust $/day figure — a single
    calendar day is too volatile to verify a spend claim against.

    Returns low confidence (→ ``unresolved``/retry in the verify daemon, which
    floors at 0.5) when there is no spend data in the window, so a missing-data
    window is never mistaken for a real "$0/day" reading that would spuriously
    satisfy a ``down`` claim.
    """
    from cost_ledger import read_events

    total_usd = 0.0
    n_events = 0
    for event in read_events(bot_id, days=lookback_days, shared_dir=_SHARED_DIR, now=as_of):
        total_usd += float(event.get("cost_usd", 0.0) or 0.0)
        n_events += 1

    if n_events == 0:
        return MetricValue(
            value=0.0,
            confidence=0.3,
            source_note=(
                f"no cost events in trailing {lookback_days}d; insufficient "
                "data (treated as unresolved, not a real $0/day reading)"
            ),
        )

    avg_daily = total_usd / lookback_days
    return MetricValue(
        value=round(avg_daily, 6),
        confidence=1.0,
        source_note=(
            f"${total_usd:.4f} over {lookback_days}d = ${avg_daily:.4f}/day avg "
            f"({n_events} events)"
        ),
    )


register(
    MetricSpec(
        name="cost.daily_usd",
        description=(
            f"Average daily API spend (USD) over the trailing "
            f"{DAILY_USD_LOOKBACK_DAYS}d, from cost_event records. Direction "
            "down — a successful Budget Hawk TierAdjustment should drop $/day."
        ),
        unit="usd_per_day",
        source="cost_event records via cost_ledger.read_events",
    ),
    resolve_cost_daily_usd,
)

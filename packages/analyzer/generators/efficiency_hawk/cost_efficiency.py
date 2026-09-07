"""generators.efficiency_hawk.cost_efficiency — Cost-ledger-backed detectors.

Two detectors live here:

  * ``detect_background_dominance`` — fires when a bot's spend over the
    trailing window is dominated by background trigger_kinds (heartbeats,
    crons, classifiers, summarizers, etc.) rather than user_turn. Proposes
    an Investigation with a verifiable claim against
    ``cost.background_share_trend``.

  * ``detect_tier_misrouting`` — fires when sessions classified as
    ``maintenance`` (via session_summary.session_class) are running on
    high-tier (Sonnet/Opus or equivalent) models. Proposes a
    TierAdjustment with a verifiable claim against
    ``cost.maintenance_high_tier_share``. Requires the optional
    ``session_reader`` on the context; absent it, the detector skips.

Each detector is a pure function over its injected readers. Tests
inject fakes; production wires them to ``cost_ledger.read_events`` and
``observations.access.window().session_summaries``.

Intentionally not in this module:
  * Repetitive clarifying questions (charter responsibility, deferred —
    needs turn-text scanning we don't have access to here)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from generators.efficiency_hawk.proposals import (
    make_background_dominance,
    make_tier_misrouting,
)
from metrics.resolvers.cost_metrics import (
    BACKGROUND_SHARE_LOOKBACK_DAYS,
    BACKGROUND_TRIGGER_KINDS,
    FOREGROUND_TRIGGER_KINDS,
    classify_model_tier,
)
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.proposal import Proposal


# ledger_reader: (bot_id, days_back) -> Iterable of cost_event dicts
LedgerReader = Callable[[str, int], Iterable[dict]]
# session_reader: (bot_id, days_back) -> Iterable of session_summary dicts
SessionReader = Callable[[str, int], Iterable[dict]]
# routing_reader: (bot_id) -> current routing dict from evolve-tiers.json
RoutingReader = Callable[[str], dict]

# Friendly model aliases → canonical tier IDs. Mirrors the applier's map so
# the generator can compare proposed tier against the live routing config
# without depending on arbiter code.
_TIER_ALIAS: dict[str, str] = {
    "haiku": "tier3",
    "sonnet": "tier2",
    "opus": "tier1",
    "judge": "tier0",
    "tier0": "tier0",
    "tier1": "tier1",
    "tier2": "tier2",
    "tier3": "tier3",
}


def _read_routing_default(bot_id: str) -> dict:
    """Production fallback: read routing via the runtime seam.

    Returns an empty dict on ImportError or any runtime failure so the
    caller's no-op guard degrades gracefully (proposal fires rather than
    crashes the generator run).
    """
    try:
        from runtime.agent_runtime import get_runtime  # type: ignore[import]

        config = get_runtime().full_config_get(bot_id) or {}
        return dict(config.get("routing") or {})
    except Exception:
        return {}


@dataclass
class CostEfficiencyContext:
    """Bundles the dependencies the cost-ledger detectors need.

    Tunables live on the dataclass so tests can vary them independently
    of the GeneratorRecord's evolvable config. Production callers pass
    overrides through ``EfficiencyHawkContext`` (see ``observe.py``).
    """

    bot_id: str
    ledger_reader: LedgerReader
    # Optional: session_summary reader. When unset, tier-misrouting is
    # skipped (background-dominance still runs from ledger alone).
    session_reader: SessionReader | None = None
    lookback_days: int = BACKGROUND_SHARE_LOOKBACK_DAYS
    # Background-dominance thresholds — see scope recommendation history.
    min_total_cost_usd: float = 1.00
    min_classified_share: float = 0.80
    min_distinct_days: int = 7
    background_share_threshold: float = 0.40
    # Tier-misrouting thresholds.
    tier_min_maintenance_sessions: int = 5
    tier_min_classified_share: float = 0.80
    tier_min_high_tier_cost_usd: float = 0.50
    tier_high_tier_share_threshold: float = 0.50
    tier_proposed_target_tier: str = "haiku"
    # Optional: routing config reader. When None, ``_read_routing_default``
    # is used in production (lazy oc_cli import). Tests inject a fake to
    # avoid subprocess calls.
    routing_reader: RoutingReader | None = None
    # Optional: where Phase 1 candidate emission writes. When unset
    # (e.g. unit tests), candidate emission is skipped — the
    # detectors still return Proposals.
    shared_dir: Path | None = None


def detect_background_dominance(ctx: CostEfficiencyContext) -> list[Proposal]:
    """Fire when background trigger_kinds dominate the bot's spend.

    Signal:
        * Sum cost over the trailing ``lookback_days``,
        * Split events into background/foreground/unknown by trigger_kind,
        * Fire only when:
            - total_cost ≥ ``min_total_cost_usd``               (quiet-bot gate),
            - distinct event-days ≥ ``min_distinct_days``       (first-week gate),
            - classified_cost / total_cost ≥ ``min_classified_share``
              (so we trust the split),
            - background_cost / classified_cost ≥ ``background_share_threshold``.

    The proposal claims that ``cost.background_share_trend`` will drop by
    at least 0.10 within ``lookback_days`` after the operator acts (or
    decides not to). If they don't act, the claim fails and the verify
    daemon flags it.
    """
    background_usd = 0.0
    foreground_usd = 0.0
    total_usd = 0.0
    per_kind_cost: dict[str, float] = defaultdict(float)
    distinct_days: set[str] = set()

    for event in ctx.ledger_reader(ctx.bot_id, ctx.lookback_days):
        cost = float(event.get("cost_usd", 0.0) or 0.0)
        total_usd += cost
        kind = event.get("trigger_kind") or ""
        if kind in BACKGROUND_TRIGGER_KINDS:
            background_usd += cost
            per_kind_cost[kind] += cost
        elif kind in FOREGROUND_TRIGGER_KINDS:
            foreground_usd += cost
        ts = event.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            distinct_days.add(ts[:10])

    # Edge gates — return [] (silent) on each.
    if total_usd < ctx.min_total_cost_usd:
        return []
    if len(distinct_days) < ctx.min_distinct_days:
        return []

    classified_usd = background_usd + foreground_usd
    if classified_usd <= 0:
        return []
    classified_share = classified_usd / total_usd if total_usd > 0 else 0.0
    if classified_share < ctx.min_classified_share:
        return []

    background_share = background_usd / classified_usd
    if background_share < ctx.background_share_threshold:
        return []

    top_kinds = sorted(per_kind_cost.items(), key=lambda kv: -kv[1])

    proposal = make_background_dominance(
        bot_id=ctx.bot_id,
        background_share=background_share,
        background_usd=background_usd,
        classified_usd=classified_usd,
        total_usd=total_usd,
        top_kinds=top_kinds,
        lookback_days=ctx.lookback_days,
    )
    # Phase 1 shadow emission. background_share is the fraction of
    # classified spend going to non-user-initiated work.
    emit_from_proposal(
        proposal,
        variant="background_dominance",
        magnitude=Magnitude(unit="pct/share", value=background_share),
        shared_dir=ctx.shared_dir,
    )
    return [proposal]


def detect_tier_misrouting(ctx: CostEfficiencyContext) -> list[Proposal]:
    """Fire when maintenance-class sessions are running on high-tier models.

    Joins cost_events to session_summaries on ``session_id``. For
    sessions whose ``session_class == "maintenance"``, splits cost by
    tier of the event's model (``classify_model_tier``).

    Fire only when ALL hold:
      - ``maintenance_session_count`` ≥ ``tier_min_maintenance_sessions``
        (statistical floor),
      - distinct event-days ≥ ``min_distinct_days`` (first-week gate, shared
        with background-dominance),
      - classified maintenance cost / total maintenance cost
        ≥ ``tier_min_classified_share`` (so we trust the split),
      - high-tier maintenance cost ≥ ``tier_min_high_tier_cost_usd``
        (don't pester for pennies),
      - high-tier share ≥ ``tier_high_tier_share_threshold``.

    Skipped silently when ``session_reader`` is None — the dispatcher
    in observe.py wires it from observations.access; tests omit it
    when they only want to exercise background-dominance.
    """
    if ctx.session_reader is None:
        return []

    # Build session_id → session_class map. We only need maintenance.
    maintenance_sessions: set[str] = set()
    for summary in ctx.session_reader(ctx.bot_id, ctx.lookback_days):
        sid = summary.get("session_id")
        cls = summary.get("session_class")
        if isinstance(sid, str) and cls == "maintenance":
            maintenance_sessions.add(sid)

    if len(maintenance_sessions) < ctx.tier_min_maintenance_sessions:
        return []

    high_tier_usd = 0.0
    low_tier_usd = 0.0
    total_maintenance_usd = 0.0
    per_high_tier_model: dict[str, float] = defaultdict(float)
    distinct_days: set[str] = set()

    for event in ctx.ledger_reader(ctx.bot_id, ctx.lookback_days):
        sid = event.get("session_id")
        if not isinstance(sid, str) or sid not in maintenance_sessions:
            continue
        cost = float(event.get("cost_usd", 0.0) or 0.0)
        total_maintenance_usd += cost
        ts = event.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            distinct_days.add(ts[:10])

        model = event.get("model") or ""
        tier = classify_model_tier(model)
        if tier == "high":
            high_tier_usd += cost
            per_high_tier_model[model] += cost
        elif tier == "low":
            low_tier_usd += cost

    # Edge gates
    if len(distinct_days) < ctx.min_distinct_days:
        return []

    classified_usd = high_tier_usd + low_tier_usd
    if classified_usd <= 0:
        return []
    if total_maintenance_usd <= 0:
        return []
    classified_share = classified_usd / total_maintenance_usd
    if classified_share < ctx.tier_min_classified_share:
        return []

    if high_tier_usd < ctx.tier_min_high_tier_cost_usd:
        return []

    high_tier_share = high_tier_usd / classified_usd
    if high_tier_share < ctx.tier_high_tier_share_threshold:
        return []

    # Skip if the routing config already targets the proposed tier. The cost
    # signal is real but the TierAdjustment would be a no-op; the actual
    # problem is classifier recall, not routing config.
    target_tier = _TIER_ALIAS.get(ctx.tier_proposed_target_tier.strip().lower())
    if target_tier is not None:
        reader = ctx.routing_reader or _read_routing_default
        try:
            current_routing = reader(ctx.bot_id)
        except Exception:
            current_routing = {}
        if current_routing.get("maintenanceTier") == target_tier:
            return []

    high_tier_models = sorted(
        per_high_tier_model.items(), key=lambda kv: -kv[1]
    )

    proposal = make_tier_misrouting(
        bot_id=ctx.bot_id,
        high_tier_share=high_tier_share,
        high_tier_cost_usd=high_tier_usd,
        classified_maintenance_cost_usd=classified_usd,
        maintenance_session_count=len(maintenance_sessions),
        high_tier_models=high_tier_models,
        lookback_days=ctx.lookback_days,
        new_tier=ctx.tier_proposed_target_tier,
    )
    # Phase 1 shadow emission. high_tier_share is the fraction of
    # classified maintenance spend on Sonnet/Opus-tier models.
    emit_from_proposal(
        proposal,
        variant="tier_misrouting",
        magnitude=Magnitude(unit="pct/share", value=high_tier_share),
        shared_dir=ctx.shared_dir,
    )
    return [proposal]


def run_all_detectors(ctx: CostEfficiencyContext) -> list[Proposal]:
    """Run every cost-ledger detector and flatten the results."""
    out: list[Proposal] = []
    out.extend(detect_background_dominance(ctx))
    out.extend(detect_tier_misrouting(ctx))
    return out

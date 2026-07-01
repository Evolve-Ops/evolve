"""generators.efficiency_hawk.proposals — Proposal factories.

Houses the factories for the cost-ledger-backed detectors. The original
``observe.py`` keeps its inline ``_build_streamline_proposal`` (still the
only AgentsAppend it emits); future cost detectors share this module.
"""

from __future__ import annotations

from schema.proposal import (
    Claim,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    TierAdjustment,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "efficiency_hawk"
DIMENSION = "efficiency"


# ── Dismiss signatures (Phase A.5 + Phase C-5) ──────────────────────────────
#
# Cost-detector findings. Per-bot signatures; the store layer scopes by
# bot_id. Distinct from the signal-driven dismiss signatures in
# signal_proposals.py.
DISMISS_SIG_BACKGROUND_DOMINANCE = "efficiency_hawk:background_dominance"
DISMISS_SIG_TIER_MISROUTING = "efficiency_hawk:tier_misrouting"


def make_background_dominance(
    bot_id: str,
    *,
    background_share: float,
    background_usd: float,
    classified_usd: float,
    total_usd: float,
    top_kinds: list[tuple[str, float]],
    lookback_days: int,
    audience: str = "pod_operator",
) -> Proposal:
    """Investigation: background trigger_kinds dominate this bot's spend.

    ``top_kinds`` is the list of ``(trigger_kind, cost_usd)`` pairs sorted
    by cost descending. Only the top three end up in the proposal context —
    we don't need a per-kind dump on every proposal, just enough for the
    operator to see where to look.
    """
    bot_name = bot_label(bot_id)
    pct = int(round(background_share * 100))
    problem = (
        f"{bot_name}: {pct}% of spend (${background_usd:.2f} of "
        f"${classified_usd:.2f}) is background work over {lookback_days}d"
    )
    headline = f"Most of {bot_name}'s spending is background work"

    # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────────────
    summary = (
        f"{pct}% of {bot_name}'s LLM spend over the last {lookback_days} "
        f"days went to background work — heartbeats, crons, summarizers — "
        f"rather than user conversations (${background_usd:.2f} of "
        f"${classified_usd:.2f}). The Cost tab's trigger-kind "
        f"breakdown shows which background source is the biggest "
        f"share."
    )
    explanation = (
        f"Every bot's API spend splits across two buckets: user-turn "
        f"work (you said something) and background work (heartbeat, "
        f"cron, classifier, summarizer, task extractor). User-turn "
        f"work is usually the point of the bot; background work is "
        f"infrastructure. When background dominates, the "
        f"infrastructure is costing more than the conversations.\n\n"
        f"Diagnosis. Background share is {pct}% over the last "
        f"{lookback_days} days. The top contributors by cost are "
        f"the dominant trigger_kinds — usually a heartbeat firing "
        f"too often, a cron doing work that could be deterministic, "
        f"or a classifier called more aggressively than it needs to "
        f"be.\n\n"
        f"What to do. (1) Reduce the cadence of the dominant cron "
        f"or heartbeat. (2) Replace routine LLM calls with "
        f"deterministic Python (a summarizer that always extracts "
        f"the same structure rarely needs an LLM). (3) Accept the "
        f"cost if the background work is genuinely the bot's "
        f"primary purpose (a classifier or sandbox bot).\n\n"
        f"What could go wrong. If the background work IS the bot's "
        f"job — a continuously-running monitor or sandbox — the "
        f"right call is to dismiss this finding; the engine then "
        f"stops nagging. Don't cut cadence on work the bot's purpose "
        f"depends on."
    )

    top_lines = "\n".join(
        f"  - {kind}: ${cost:.2f}"
        for kind, cost in top_kinds[:3]
    )
    context = (
        f"Bot {bot_name}'s API spend over the last {lookback_days} days is "
        f"dominated by background work. {pct}% of classified cost "
        f"(${background_usd:.4f} of ${classified_usd:.4f}) came from "
        "non-user-initiated trigger_kinds — heartbeats, crons, classifier "
        "calls, summarizers, or task extractors — rather than user_turn "
        f"conversations. Total spend in window: ${total_usd:.4f}.\n\n"
        "Top background trigger_kinds by cost:\n"
        f"{top_lines}\n\n"
        "Consider:\n"
        "  - Reducing the cadence of the dominant cron / heartbeat\n"
        "  - Replacing routine LLM calls with deterministic Python (a "
        "summarizer or task_extractor that always extracts the same "
        "structure rarely needs an LLM)\n"
        "  - Accepting the cost if the background work is genuinely the "
        "bot's primary purpose (e.g. a classifier or sandbox bot)\n\n"
        "Verification: cost.background_share_trend should drop by at "
        f"least 0.10 (10 percentage points) within {lookback_days} days "
        "after the change."
    )

    claim = Claim(
        metric="cost.background_share_trend",
        direction="down",
        magnitude=0.10,
        window_days=lookback_days,
        baseline=round(background_share, 6),
        # Investigation has nothing to revert; flag terminal state on
        # failure so the operator sees that the suggested change either
        # wasn't made or didn't move the metric.
        fallback="flag",
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, detector); detail lives in provenance.signals.
        trigger_observations=[f"background_dominance:{bot_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.background_dominance",
            signals={
                "background_share": round(background_share, 4),
                "background_usd": round(background_usd, 6),
                "classified_usd": round(classified_usd, 6),
                "total_usd": round(total_usd, 6),
                "top_kinds": [
                    {"kind": k, "cost_usd": round(c, 6)}
                    for k, c in top_kinds[:3]
                ],
                "lookback_days": lookback_days,
            },
            confidence=0.85,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=[],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        conversational_pitch=(
            f"Most of this bot's API cost is going to background work, "
            f"not your conversations ({pct}% over {lookback_days} days). "
            "Want me to flag it for review?"
        ),
        # ── Phase C-5 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_name} → trigger-kind breakdown",
        dismiss_signature=DISMISS_SIG_BACKGROUND_DOMINANCE,
        dismiss_scope="kind",
    )


def make_tier_misrouting(
    bot_id: str,
    *,
    high_tier_share: float,
    high_tier_cost_usd: float,
    classified_maintenance_cost_usd: float,
    maintenance_session_count: int,
    high_tier_models: list[tuple[str, float]],
    lookback_days: int,
    new_tier: str = "haiku",
    audience: str = "pod_operator",
) -> Proposal:
    """TierAdjustment: maintenance sessions are running on a high tier.

    ``high_tier_models`` is the list of ``(model_string, cost_usd)``
    pairs for the high-tier models that fired on maintenance sessions,
    sorted by cost descending. Only the top three end up in the
    proposal context — enough for the operator to identify which model
    is the culprit without dumping every event.
    """
    bot_name = bot_label(bot_id)
    pct = int(round(high_tier_share * 100))
    problem = (
        f"{bot_name}: {pct}% of maintenance spend "
        f"(${high_tier_cost_usd:.2f} of "
        f"${classified_maintenance_cost_usd:.2f}) ran on high-tier models "
        f"over {lookback_days}d"
    )
    headline = f"Move {bot_name}'s maintenance work to a cheaper model"

    # ── Phase C-5 operator-first content (Tier 1 — auto-apply) ──────────────
    summary = (
        f"{bot_name}'s maintenance sessions ({maintenance_session_count} "
        f"over {lookback_days} days) ran {pct}% of their spend on "
        f"high-tier models — paying premium rates for work the cheaper "
        f"{new_tier} tier handles fine. Routing maintenance to "
        f"{new_tier} drops the bill without changing what the bot does."
    )
    explanation = (
        f"Bots route different kinds of work to different model tiers. "
        f"Maintenance sessions — short, structurally simple, no fresh "
        f"reasoning — should run on the cheapest tier that gets the "
        f"answer right. Anthropic's Haiku-tier handles maintenance for "
        f"a fraction of the cost of Sonnet-tier or above.\n\n"
        f"Diagnosis. {pct}% of {bot_name}'s maintenance spend over the "
        f"last {lookback_days} days landed on high-tier models — "
        f"${high_tier_cost_usd:.2f} of ${classified_maintenance_cost_usd:.2f}. "
        f"The router for the maintenance class is sending work to a "
        f"tier richer than maintenance needs.\n\n"
        f"What this changes. The recommendation flips the maintenance "
        f"class to {new_tier!r}. Productive sessions stay on their "
        f"current tier; only maintenance moves. Fully reversible — "
        f"the applier captures the prior routing in a snapshot, and "
        f"auto-reverts if the metric doesn't improve.\n\n"
        f"What could go wrong. If your maintenance work is unusually "
        f"complex on this bot — say, the bot routes 'maintenance' to "
        f"sessions that actually need reasoning — the cheaper tier "
        f"will produce worse results. Watch the next week's quality "
        f"after the change; the auto-revert handles the obvious case."
    )

    top_lines = "\n".join(
        f"  - {model}: ${cost:.2f}"
        for model, cost in high_tier_models[:3]
    )
    context = (
        f"Bot {bot_name}'s maintenance-classified sessions over the last "
        f"{lookback_days} days spent ${high_tier_cost_usd:.4f} on "
        f"high-tier models ({pct}% of "
        f"${classified_maintenance_cost_usd:.4f} classified maintenance "
        f"cost) across {maintenance_session_count} maintenance sessions. "
        "Maintenance work is short and structurally simple — Haiku-tier "
        "models handle it for a fraction of the cost.\n\n"
        "Top high-tier models on maintenance sessions:\n"
        f"{top_lines}\n\n"
        f"Proposed action: route maintenance-class sessions to {new_tier!r} "
        "via the bot's tier configuration.\n\n"
        f"Verification: cost.maintenance_high_tier_share should drop by "
        f"at least 0.40 (40 percentage points) within {lookback_days} "
        "days after the routing change. Auto-revert on failure."
    )

    claim = Claim(
        metric="cost.maintenance_high_tier_share",
        direction="down",
        magnitude=0.40,
        window_days=lookback_days,
        baseline=round(high_tier_share, 6),
        # TierAdjustment IS revertable (the applier captures the prior
        # routing in the snapshot), so revert on claim failure rather
        # than flag.
        fallback="revert",
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, detector) — detail lives in provenance.signals.
        # Note: budget_hawk uses ``hard_cap_tradeoff:{bot_id}`` for its
        # tier_downgrade trigger; ours is distinct so the two don't
        # dedup against each other when both happen to fire.
        trigger_observations=[f"tier_misrouting:{bot_id}"],
        provenance=Provenance(
            technique="efficiency_hawk.tier_misrouting",
            signals={
                "high_tier_share": round(high_tier_share, 4),
                "high_tier_cost_usd": round(high_tier_cost_usd, 6),
                "classified_maintenance_cost_usd": round(
                    classified_maintenance_cost_usd, 6
                ),
                "maintenance_session_count": maintenance_session_count,
                "top_models": [
                    {"model": m, "cost_usd": round(c, 6)}
                    for m, c in high_tier_models[:3]
                ],
                "lookback_days": lookback_days,
                "proposed_tier": new_tier,
            },
            confidence=0.85,
        ),
        problem=problem,
        action=TierAdjustment(
            bot_id=bot_id,
            target_class="maintenance",
            new_tier=new_tier,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["tier_routing"],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="hygiene",
        admin_surface_summary=headline[:120],
        conversational_pitch=(
            f"This bot's maintenance work is running on a high-tier model "
            f"({pct}% of ${classified_maintenance_cost_usd:.2f} over "
            f"{lookback_days} days). Want me to propose routing it to "
            f"{new_tier} instead?"
        ),
        # ── Phase C-5 operator-first content (Tier 1 — auto-apply) ──────
        summary=summary,
        explanation=explanation,
        action_label=f"Route maintenance to {new_tier}",
        manual_path=f"Settings → Models → {bot_name}",
        dismiss_signature=DISMISS_SIG_TIER_MISROUTING,
        dismiss_scope="kind",
    )

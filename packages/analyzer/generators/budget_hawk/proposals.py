"""generators.budget_hawk.proposals — Proposal factories."""

from __future__ import annotations

from schema.proposal import (
    Claim,
    ConfigPatch,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    TierAdjustment,
    new_proposal_id,
)

from evolve_config import bot_home as _bot_home


GENERATOR_ID = "budget_hawk"
DIMENSION = "cost"


# ── Dismiss signatures (Phase A.5 + Phase C-7) ──────────────────────────────
#
# Per-finding-kind signatures. The store layer scopes by bot_id; per-bot
# scoping handles the "different bots dismiss independently" case. The
# only per-resource signature is app_cost_imbalance.dominance, which
# embeds the dominant_app so dismissing concentration on app X doesn't
# suppress a future finding on app Y.
DISMISS_SIG_WARN_CAP_CROSSED = "budget_hawk:warn_cap_crossed"
DISMISS_SIG_TIER_DOWNGRADE = "budget_hawk:tier_downgrade"
DISMISS_SIG_COST_ANOMALY = "budget_hawk:cost_anomaly"
DISMISS_SIG_WARN_CAP_PATTERN = "budget_hawk:warn_cap_pattern"
DISMISS_SIG_SUMMARIZER_TRIVIAL = "budget_hawk:summarizer_on_trivial"
DISMISS_SIG_APP_COST_COVERAGE = "budget_hawk:app_cost_coverage_gap"
DISMISS_SIG_CLASSIFIER_NOISE = "budget_hawk:classifier_noise"


def dismiss_signature_for_app_dominance(dominant_app: str) -> str:
    """Per-app dominance signature so dismissing concentration on app X
    doesn't suppress a future finding on app Y."""
    return f"budget_hawk:app_cost_dominance:{dominant_app}"


def _openclaw_config_target(bot_id: str, field: str) -> str:
    """Build a ConfigPatch target_path for a field on the Evolve plugin config.

    Convention: ``{file}::{dotted.key.path}`` — see
    ``arbiter/appliers/config_patch.py::_parse_target_path``. Evolve plugin
    config lives at ``plugins.entries.evolve.config.*`` within the bot's
    ``openclaw.json`` (see ``deploy.py:640``).
    """
    oc_path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    return f"{oc_path}::plugins.entries.evolve.config.{field}"


def _provenance(technique: str, **signals) -> Provenance:
    return Provenance(
        technique=f"budget_hawk.{technique}",
        signals=dict(signals),
        confidence=0.9,
    )


def make_warn_cap_crossed(
    bot_id: str,
    *,
    current_usd: float,
    cap_usd: float,
    audience: str = "pod_operator",
) -> Proposal:
    admin = f"{bot_id} crossed its daily spend warning"
    summary = (
        f"{bot_id} spent ${current_usd:.2f} today, crossing the "
        f"${cap_usd:.2f} warning cap you set for it. The Cost tab's "
        f"trigger-kind breakdown shows whether the jump is "
        f"user-driven or automation."
    )
    explanation = (
        f"Daily spend caps act as a check on cost — when a bot's "
        f"workload shifts, spend can grow before anyone notices. The "
        f"cap fires so you see the change.\n\n"
        f"Diagnosis. ${current_usd:.2f} today against a "
        f"${cap_usd:.2f} warn cap. The cap doesn't say whether the "
        f"spend is bad — it says the spend changed.\n\n"
        f"Where to look. Open the Cost tab → trigger-kind breakdown "
        f"for {bot_id}. If user-turn share is up, traffic grew; if "
        f"heartbeat or cron share jumped, look at the Sessions page "
        f"for repeats; if neither moved, the primary model may have "
        f"changed.\n\n"
        f"What could go wrong. If the spend is intentional, raising "
        f"the warn cap via the Cost tab keeps the engine from "
        f"nagging on an expected cost. Reverting an intentional "
        f"workload regresses the thing you wanted."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, signal) — current_usd lives in provenance.signals
        # so it can vary across re-detections without breaking dedup.
        trigger_observations=[f"warn_cap_crossed:{bot_id}"],
        provenance=_provenance(
            "cap_crossing", current_usd=current_usd, cap_usd=cap_usd
        ),
        problem=admin,
        action=Investigation(
            context=(
                f"Bot {bot_id}'s daily spend (${current_usd:.2f}) exceeded the "
                f"warn cap (${cap_usd:.2f}). Review recent usage and adjust "
                "tier routing or tool invocations as needed."
            )
        ),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,  # qualitative
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="cost_alert",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_id} → trigger-kind breakdown",
        dismiss_signature=DISMISS_SIG_WARN_CAP_CROSSED,
        dismiss_scope="kind",
    )


def make_tier_downgrade(
    bot_id: str,
    *,
    target_class: str,
    new_tier: str,
    audience: str = "pod_operator",
) -> Proposal:
    admin = f"Move {bot_id}'s {target_class} sessions to a cheaper tier"
    summary = (
        f"{bot_id} crossed its hard spend cap. To stay under, the "
        f"engine recommends routing {target_class} sessions to the "
        f"cheaper {new_tier} tier. Productive sessions stay on their "
        f"current tier; only the {target_class} class moves."
    )
    explanation = (
        f"When a bot crosses its hard cap, the engine has a few "
        f"levers — block new spend, ask for a raise, or move some "
        f"work to a cheaper model. The cheapest first move is "
        f"routing maintenance-class sessions to a Haiku-tier model: "
        f"the work is short and structurally simple, Haiku handles "
        f"it fine, and productive sessions stay where they are.\n\n"
        f"Diagnosis. {bot_id} hit its hard daily cap. The auto-applier "
        f"can flip {target_class} routing to {new_tier} in one step. "
        f"Fully reversible — the applier snapshots the prior routing "
        f"and auto-reverts if daily spend doesn't actually drop.\n\n"
        f"What could go wrong. If {target_class} work on this bot is "
        f"unusually complex, the cheaper tier may produce worse "
        f"results. Watch the next week's quality after applying; the "
        f"auto-revert handles the obvious case where spend doesn't "
        f"drop. For a bot where the cap was set too low for the "
        f"workload, raising the cap is the right answer instead."
    )
    claim = Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=0.50,  # expect at least $0.50/day savings
        window_days=7,
        baseline=0.0,  # placeholder — overwritten with the live reading at apply
        baseline_at_apply=True,
        fallback="revert",
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"hard_cap_tradeoff:{bot_id}"],
        provenance=_provenance(
            "hard_cap_tradeoff", target_class=target_class, new_tier=new_tier
        ),
        problem=admin,
        action=TierAdjustment(
            bot_id=bot_id,
            target_class=target_class,
            new_tier=new_tier,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["tier_routing"],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="cost_alert",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 1 — auto-apply) ──────
        summary=summary,
        explanation=explanation,
        action_label=f"Move {target_class} to {new_tier}",
        manual_path=f"Settings → Models → {bot_id}",
        dismiss_signature=DISMISS_SIG_TIER_DOWNGRADE,
        dismiss_scope="kind",
    )


def make_cost_anomaly(
    bot_id: str,
    *,
    current_usd: float,
    mean_usd: float,
    stdevs: float,
    audience: str = "pod_operator",
) -> Proposal:
    admin = f"{bot_id}'s spend today is unusually high"
    summary = (
        f"{bot_id} spent ${current_usd:.2f} today, {stdevs:.1f} "
        f"standard deviations above its recent ${mean_usd:.2f} mean. "
        f"That's either a legitimate spike (new traffic, deliberate "
        f"new workload) or a runaway session worth catching now."
    )
    explanation = (
        f"Statistical anomaly detection complements the fixed daily "
        f"cap: a bot well under its cap can still have a session "
        f"that costs an order of magnitude more than usual. The "
        f"engine flags those for review.\n\n"
        f"Diagnosis. Recent 7-day mean: ${mean_usd:.2f}. Today: "
        f"${current_usd:.2f}, {stdevs:.1f}σ above the mean. The "
        f"jump is statistically real; what matters is whether it's "
        f"intentional.\n\n"
        f"Where to look. Open Sessions → {bot_id} sorted by cost — "
        f"the outlier session is usually at the top. Check the "
        f"trigger-kind + tool-use breakdown for that session to see "
        f"if it's a stuck loop, a runaway sub-agent, or genuinely "
        f"large work.\n\n"
        f"What could go wrong. If the anomaly is a deliberate "
        f"long-running task, no action needed; the signal "
        f"auto-resolves once the day rolls out of the window. Don't "
        f"interrupt a session you wanted."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cost_anomaly:{bot_id}"],
        provenance=_provenance(
            "cost_anomaly",
            current_usd=current_usd,
            mean_usd=mean_usd,
            stdevs=stdevs,
        ),
        problem=admin,
        action=Investigation(
            context=(
                f"Bot {bot_id}'s daily spend (${current_usd:.2f}) is "
                f"{stdevs:.1f} stdev above its 7-day mean (${mean_usd:.2f}). "
                "Investigate the cause — either a legitimate spike or a "
                "runaway session."
            )
        ),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="cost_alert",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Sessions page",
        manual_path=f"Cost → Sessions → {bot_id}",
        dismiss_signature=DISMISS_SIG_COST_ANOMALY,
        dismiss_scope="kind",
    )


def make_warn_pattern_investigation(
    bot_id: str,
    *,
    current_usd: float,
    cap_usd: float,
    observation_count: int,
    audience: str = "pod_operator",
) -> Proposal:
    """Investigation proposal emitted after repeated warn-cap crossings.

    Fires when the warn_cap_crossed Signal's observation_count reaches the
    pattern threshold, signalling that this isn't a one-off spike — it's a
    recurring pattern worth operator attention. Unlike the raw threshold
    alert (which routes to Signals), this is an actionable Proposal: the
    operator should investigate cost drivers or adjust the cap.
    """
    admin = f"{bot_id}'s spend keeps crossing the warning cap"
    summary = (
        f"{bot_id} has crossed its ${cap_usd:.2f} warning cap "
        f"{observation_count} times now — it's no longer a one-off "
        f"spike. Either the cap is too low for the bot's real "
        f"workload, or there's a cost driver worth fixing."
    )
    explanation = (
        f"The warn cap is a soft signal: one crossing is interesting, "
        f"three is a pattern. After enough crossings, the engine "
        f"escalates from a Signal (visible on Alerts) to a Proposal "
        f"(in this queue) — the assumption being that the operator "
        f"should make an explicit decision instead of acknowledging "
        f"the same alert repeatedly.\n\n"
        f"Diagnosis. {observation_count} warn-cap crossings over the "
        f"observation window. The cap fires at ${cap_usd:.2f}/day; "
        f"current spend ${current_usd:.2f}.\n\n"
        f"What to do. Four things worth checking in order: "
        f"(1) heartbeat or cron frequency — is something firing more "
        f"than expected? (2) Session volume trend — has traffic "
        f"genuinely grown? (3) Tier routing — are background "
        f"sessions running on a more expensive tier than needed? "
        f"(4) Whether the cap should be raised to match normal "
        f"operating costs for this bot.\n\n"
        f"What could go wrong. Raising the cap silences the engine "
        f"but doesn't address the underlying cost shift. If you "
        f"raise it, log why — future operators (or you, six months "
        f"from now) will want to know whether the cap reflects "
        f"intent or surrender."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable fingerprint — observation_count lives in provenance.signals.
        trigger_observations=[f"warn_cap_pattern:{bot_id}"],
        provenance=_provenance(
            "warn_cap_pattern",
            current_usd=current_usd,
            cap_usd=cap_usd,
            observation_count=observation_count,
        ),
        problem=admin,
        action=Investigation(
            context=(
                f"Bot {bot_id}'s daily spend has exceeded the warn cap "
                f"(${cap_usd:.2f}) {observation_count} times. This recurring "
                "pattern suggests the cap no longer reflects realistic "
                "expectations, or there is an identifiable cost driver to "
                "address. Suggested review: (1) heartbeat / cron frequency, "
                "(2) session volume trend, (3) tier routing for background "
                "sessions, (4) whether the warn cap should be raised to match "
                "normal operating costs for this bot."
            )
        ),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="cost_alert",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_id}",
        dismiss_signature=DISMISS_SIG_WARN_CAP_PATTERN,
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Budget Hawk v2 — forensics-driven ConfigPatch proposals
# ─────────────────────────────────────────────────────────────────────────────


def make_summarizer_min_turns_patch(
    bot_id: str,
    *,
    new_value: int,
    offending_events: int,
    cost_waste_usd: float,
    lookback_days: int,
    example_session_ids: list[str],
    audience: str = "pod_operator",
) -> Proposal:
    """ConfigPatch proposing ``summarizerMinTurns = new_value``.

    Fires when ``detect_summarizer_on_trivial`` finds ≥2 cost_events where
    the session summarizer ran an LLM outcome extraction on a session with
    fewer than N turns — that call is almost always waste; the keyword
    inferOutcome fallback is strictly cheaper and good enough for short
    sessions.
    """
    admin = f"Stop {bot_id}'s summarizer from burning money on tiny sessions"
    summary = (
        f"{bot_id}'s session summarizer ran {offending_events} LLM "
        f"call(s) on sessions with fewer than {new_value} turns "
        f"over the last {lookback_days} days — wasting about "
        f"${cost_waste_usd:.4f}. The cheaper keyword fallback handles "
        f"short sessions fine. Raising `summarizerMinTurns` to "
        f"{new_value} keeps the LLM out of the path."
    )
    explanation = (
        f"The session summarizer extracts a structured summary at "
        f"session end. For long sessions, an LLM call is worth it. "
        f"For short sessions (one or two turns), the keyword fallback "
        f"is strictly cheaper and good enough — there isn't enough "
        f"there for the LLM to add value.\n\n"
        f"Diagnosis. The summarizer fired on "
        f"{offending_events} short sessions over the last "
        f"{lookback_days} days. Sample session ids in Details. The "
        f"cost waste is small per call but compounds across "
        f"hundreds of short sessions per day.\n\n"
        f"What this changes. Setting `summarizerMinTurns` to "
        f"{new_value} on this bot. Sessions below that threshold "
        f"use the keyword summarizer instead of the LLM. Fully "
        f"reversible from the bot's openclaw.json.\n\n"
        f"What could go wrong. If this bot's short sessions "
        f"legitimately need LLM summarization (rare; usually "
        f"happens when the bot is a chat handoff that ends abruptly "
        f"mid-conversation), the keyword fallback will produce "
        f"thinner summaries. The auto-revert handles the case where "
        f"the savings don't materialize."
    )
    claim = Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=round(cost_waste_usd / max(lookback_days, 1), 6),
        window_days=lookback_days,
        baseline=0.0,  # placeholder — overwritten with the live reading at apply
        baseline_at_apply=True,
        fallback="revert",
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, detector); event count lives in provenance.signals.
        trigger_observations=[f"summarizer_on_trivial:{bot_id}"],
        provenance=_provenance(
            "summarizer_on_trivial",
            offending_events=offending_events,
            cost_waste_usd=cost_waste_usd,
            example_session_ids=example_session_ids,
            lookback_days=lookback_days,
        ),
        problem=admin,
        action=ConfigPatch(
            target_path=_openclaw_config_target(bot_id, "summarizerMinTurns"),
            operation="set",
            value=new_value,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["plugin_config"],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="hygiene",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 1 — auto-apply) ──────
        summary=summary,
        explanation=explanation,
        action_label=f"Raise summarizerMinTurns to {new_value}",
        manual_path=f"Settings → Cost levers → {bot_id}",
        dismiss_signature=DISMISS_SIG_SUMMARIZER_TRIVIAL,
        dismiss_scope="kind",
    )


def make_app_cost_imbalance(
    bot_id: str,
    *,
    kind: str,  # "dominance" | "coverage"
    dominant_app: str | None,
    dominant_cost_usd: float,
    dominant_share: float,
    total_cost_usd: float,
    lookback_days: int,
    audience: str = "pod_operator",
) -> Proposal:
    """Investigation proposal for per-application cost attribution findings.

    Two shapes:

    * ``kind="dominance"`` — one app dominates attributed spend. The
      proposal asks the operator whether that concentration matches intent.
      ``dominant_app`` must be set.
    * ``kind="coverage"`` — majority of spend is unattributed. Proposal
      suggests extending ``applicationPatterns`` in network.json. Here
      ``dominant_app`` is ``None``.

    Investigations are the right shape because attribution decisions are
    user-priority calls, not auto-tunable knobs. The operator reads the
    evidence and either accepts the concentration, narrows the pattern,
    or digs into why the app is costly.
    """
    if kind == "dominance":
        admin = f"One app dominates {bot_id}'s spend"
        summary = (
            f"On {bot_id}, app `{dominant_app}` ate "
            f"{int(dominant_share*100)}% of attributed LLM spend "
            f"(${dominant_cost_usd:.2f} of ${total_cost_usd:.2f}) "
            f"over the last {lookback_days} days. Worth confirming "
            f"that concentration matches what you intended."
        )
        explanation = (
            f"Cost attribution by application is how the engine "
            f"answers \"what is this bot actually doing with its "
            f"budget?\" When one app dominates, the bot's purpose "
            f"is effectively that app — which is fine if it's "
            f"intentional, worth investigating if not.\n\n"
            f"Diagnosis. `{dominant_app}` accounted for "
            f"{int(dominant_share*100)}% of attributed spend on "
            f"{bot_id} over the last {lookback_days} days "
            f"(${dominant_cost_usd:.2f} of ${total_cost_usd:.2f}). "
            f"That's the concentration the engine sees; what we "
            f"don't know is whether it matches your operating "
            f"intent.\n\n"
            f"What to do. If the app is supposed to be load-bearing "
            f"on this bot — accept the concentration and dismiss; "
            f"this surface confirms operating reality. If the app "
            f"shouldn't be drawing this much traffic, narrow its "
            f"keyword/heartbeat budget or tighten its scope.\n\n"
            f"What could go wrong. Tightening an app's scope changes "
            f"how the bot routes work — sessions that previously hit "
            f"this app may now hit something else (or nothing). "
            f"Watch the session-class breakdown after changing to "
            f"confirm work isn't getting dropped."
        )
        action_label = "Open Cost tab"
        manual_path = f"Cost tab → {bot_id} → app breakdown"
        dismiss_sig = dismiss_signature_for_app_dominance(
            dominant_app or "unknown",
        )
        context = (
            f"Bot {bot_id}'s application-attributed cost over the last "
            f"{lookback_days} days is concentrated on {dominant_app!r} "
            f"(${dominant_cost_usd:.4f}, {int(dominant_share*100)}% of "
            f"${total_cost_usd:.4f} total). Review whether this matches "
            "expectations — if this app was installed on purpose and the "
            "activity is valuable, accept. Otherwise consider narrowing "
            "its scope or tightening the keyword / heartbeat budget that "
            "draws traffic to it."
        )
        trigger_obs = f"app_cost_dominance:{bot_id}:{dominant_app}"
    elif kind == "coverage":
        admin = f"Most of {bot_id}'s spend can't be tagged to any app"
        summary = (
            f"{int(dominant_share*100)}% of {bot_id}'s spend over "
            f"the last {lookback_days} days "
            f"(${dominant_cost_usd:.2f} of ${total_cost_usd:.2f}) "
            f"can't be tagged to any installed application. "
            f"Extending `applicationPatterns` in network.json lets "
            f"the engine report cost by app accurately."
        )
        explanation = (
            f"Application attribution works by matching session "
            f"content against a per-pod pattern dictionary "
            f"(`applicationPatterns` in network.json). When most "
            f"sessions don't match any pattern, the cost shows up "
            f"as \"unattributed\" — useful as a signal but not "
            f"actionable for tuning.\n\n"
            f"Diagnosis. The pattern dictionary doesn't cover this "
            f"bot's vocabulary. Common gap: domain-specific terms "
            f"(project names, people, recurring topics) that "
            f"weren't in the default seed.\n\n"
            f"What to do. Open `network.json`'s "
            f"`applicationPatterns` block and add keywords for this "
            f"bot's domain. The engine re-classifies on the next "
            f"sweep, and unattributed share drops.\n\n"
            f"What could go wrong. Over-broad patterns can "
            f"misattribute work (a generic keyword like \"report\" "
            f"matches too many sessions). Start with specific terms; "
            f"if you can't think of any, the bot's work may "
            f"genuinely be general-purpose — dismiss this finding."
        )
        action_label = "Open network.json"
        manual_path = "Settings → Application patterns"
        dismiss_sig = DISMISS_SIG_APP_COST_COVERAGE
        context = (
            f"Bot {bot_id}'s cost attribution has a coverage gap: "
            f"{int(dominant_share*100)}% of spend over the last "
            f"{lookback_days} days (${dominant_cost_usd:.4f} of "
            f"${total_cost_usd:.4f}) can't be tagged to any application. "
            "Extend ``applicationPatterns`` in network.json with keywords "
            "for this deployment's domain vocabulary (project names, "
            "people, recurring topics) so Budget Hawk can report cost-by-"
            "application accurately."
        )
        trigger_obs = f"app_cost_coverage_gap:{bot_id}"
    else:
        raise ValueError(f"make_app_cost_imbalance: unknown kind {kind!r}")

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[trigger_obs],
        provenance=_provenance(
            f"app_cost_imbalance.{kind}",
            dominant_app=dominant_app,
            dominant_cost_usd=dominant_cost_usd,
            dominant_share=dominant_share,
            total_cost_usd=total_cost_usd,
            lookback_days=lookback_days,
        ),
        problem=admin,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,  # qualitative — no mechanical claim to verify
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="hygiene",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label=action_label,
        manual_path=manual_path,
        dismiss_signature=dismiss_sig,
        dismiss_scope="kind",
    )


def make_classifier_threshold_patch(
    bot_id: str,
    *,
    new_value: float,
    classifier_events: int,
    per_day: float,
    confident_share: float,
    total_cost_usd: float,
    lookback_days: int,
    audience: str = "pod_operator",
) -> Proposal:
    """ConfigPatch proposing ``classifierKeywordConfidenceFloor = new_value``.

    Fires when the tier classifier is making too many LLM calls while
    sessions mostly classify with high confidence anyway — raising the
    keyword floor keeps the classifier out of the LLM path for cases the
    keywords already nailed.
    """
    admin = f"Quiet down {bot_id}'s tier classifier"
    summary = (
        f"{bot_id}'s tier classifier called the LLM "
        f"{classifier_events} times over the last {lookback_days} "
        f"days ({per_day:.1f}/day) — but {int(confident_share*100)}% "
        f"of sessions already classified at high keyword confidence. "
        f"The LLM calls are mostly redundant. Raising the keyword "
        f"floor to {new_value} keeps the classifier out of the LLM "
        f"path when keywords already nailed it."
    )
    explanation = (
        f"The tier classifier picks which model tier a session "
        f"runs on. For most sessions, simple keyword matching is "
        f"already confident enough — only ambiguous sessions need "
        f"the LLM to make the call. Lowering the LLM-fallback "
        f"threshold sends more sessions through the cheap path.\n\n"
        f"Diagnosis. Across {lookback_days} days: "
        f"{classifier_events} classifier LLM calls "
        f"(${total_cost_usd:.4f}). Of all sessions, "
        f"{int(confident_share*100)}% classified at ≥0.80 keyword "
        f"confidence — well above the new floor. The LLM was "
        f"firing on sessions whose tier was already obvious.\n\n"
        f"What this changes. Raising "
        f"`classifierKeywordConfidenceFloor` to {new_value}. "
        f"Sessions above that floor skip the LLM call entirely. "
        f"Fully reversible; auto-reverts if savings don't "
        f"materialize.\n\n"
        f"What could go wrong. If a chunk of borderline sessions "
        f"were quietly routed correctly thanks to the LLM "
        f"fallback, raising the floor sends them to the keyword "
        f"router instead — which could route them to the wrong "
        f"tier. The auto-revert handles obvious cases (spend "
        f"didn't drop), but quality regression on borderline "
        f"sessions is harder to spot — watch session-class "
        f"distribution after applying."
    )
    claim = Claim(
        metric="cost.daily_usd",
        direction="down",
        magnitude=round(total_cost_usd / max(lookback_days, 1), 6),
        window_days=lookback_days,
        baseline=0.0,  # placeholder — overwritten with the live reading at apply
        baseline_at_apply=True,
        fallback="revert",
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        # Stable per (bot, detector); per-day rate lives in provenance.signals.
        trigger_observations=[f"classifier_noise:{bot_id}"],
        provenance=_provenance(
            "classifier_noise",
            classifier_events=classifier_events,
            per_day=per_day,
            confident_share=confident_share,
            total_cost_usd=total_cost_usd,
            lookback_days=lookback_days,
        ),
        problem=admin,
        action=ConfigPatch(
            target_path=_openclaw_config_target(
                bot_id, "classifierKeywordConfidenceFloor"
            ),
            operation="set",
            value=new_value,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["plugin_config"],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="hygiene",
        admin_surface_summary=admin[:120],
        # ── Phase C-7 operator-first content (Tier 1 — auto-apply) ──────
        summary=summary,
        explanation=explanation,
        action_label=f"Raise keyword floor to {new_value}",
        manual_path=f"Settings → Cost levers → {bot_id}",
        dismiss_signature=DISMISS_SIG_CLASSIFIER_NOISE,
        dismiss_scope="kind",
    )

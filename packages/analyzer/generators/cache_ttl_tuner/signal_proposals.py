"""generators.cache_ttl_tuner.signal_proposals — Signal → Proposal factories.

Builds Proposals from session_economics Signals. One factory per consumed
signal type; each is pure string templating, no LLM.

Two factories, two failure modes:

  * ``make_cache_invalidation_elevated_proposal`` — emits an
    :class:`UpdateAgentDefaults` Proposal flipping
    ``agents.defaults.models.*.params.cacheRetention`` from ``"short"`` to
    ``"long"``. Closes the loop on the team-bot-a session 3d5cde22 incident
    (2026-05-29 Slack DM, $7.67, 92% from prompt-cache writes that TTL'd
    at the 5-minute Anthropic default before each human-paced reuse).
    PR A (#1870) exposed the knob to operators; PR B (#1874) added the L2
    applier; this factory makes the generator actually emit it.

  * ``make_cache_hit_rate_low_proposal`` — stays Investigation. Low hit
    rate with stable inter-turn gaps is a prompt-structure failure mode,
    not a TTL one — there's no single knob to flip. The operator does
    the legwork; the generator points at the likely causes.

Every Proposal sets ``motivating_signals=[signal.id]`` so the inverse
link on the Signal points back here. The UpdateAgentDefaults factory
also writes ``provenance.signals.root_cause_attribution.cause_key`` so
:func:`investigation.proposal_history.proposal_history` can dedup by
cause across reruns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from schema.proposal import (
    Claim,
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    UpdateAgentDefaults,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "cache_ttl_tuner"
DIMENSION = "efficiency"

# Operator-facing path that lands on the in-place cost-lever editor for
# this knob. Kept as a constant so the message text stays consistent and
# one rename moves it everywhere. The Usage page renders per-bot tiles
# with an "Edit cost levers" button that reuses the same picker the
# Customizations card uses — the fastest path to the override surface
# when triaging a fresh proposal.
COST_TAB_LOCATION = "admin UI → Usage page → bot tile → Edit cost levers"

# Dotpath the UpdateAgentDefaults action writes. The ``*`` is a wildcard
# the applier's materializer fans out across every Anthropic model in
# the catalog. Sourced from the same whitelist the applier reads in
# schema.proposal._UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS.
CACHE_RETENTION_DOTPATH = "agents.defaults.models.*.params.cacheRetention"

# cause_key the UpdateAgentDefaults proposal records in
# provenance.signals.root_cause_attribution. Stable string so the
# proposal_history dedup window can match across reruns and so
# operator_already_declined() can suppress after repeated dismissals.
CAUSE_KEY_TTL_TOO_SHORT = "cache_retention_too_short_for_cadence"

# Hit-rate-low investigation cause_key — kept distinct so a recurring
# prompt-structure investigation doesn't suppress a fresh TTL fix or
# vice versa.
CAUSE_KEY_HIT_RATE_LOW = "cache_hit_rate_low_likely_prompt_churn"

# How far back to look for an existing similar proposal before
# re-emitting. Picked at 14 days to match the v1 default elsewhere
# (primary_model_floor_advisor uses operator_already_declined semantics;
# this generator uses a positive-recency window because UpdateAgentDefaults
# is decidable and we want to avoid spamming once it's been applied).
PROPOSAL_HISTORY_WINDOW_DAYS = 14

# Field path used when recording config-intent on a successful apply
# (UpdateAgentDefaultsApplier._record_applier_intents). Generators
# consult get_intent(bot_id, INTENT_FIELD_PATH) to decide whether the
# operator has already chosen the OPPOSITE value deliberately.
INTENT_FIELD_PATH = CACHE_RETENTION_DOTPATH


# ── Savings estimate (PR H) ──────────────────────────────────────────────────
#
# Heuristic for the cacheRetention "short → long" flip. The operator
# motivating incident (team_bot_a session 3d5cde22 on 2026-05-29) put
# 92% of a $7.67 turn into cacheWrite cost on cache invalidations.
# Generalizing across a window:
#
#   invalidated_ratio  — share of cache-participating turns whose cache
#                        was invalidated (the signal's primary detail)
#   bot_7d_cost_usd    — bot's total LLM spend over the same 7-day window
#   CACHE_WRITE_FRACTION_ON_INVALIDATED ≈ 0.92  (from the incident postmortem)
#   FRACTION_REMEDIABLE_BY_TTL_BUMP    ≈ 0.50  (rough — TTL bump cuts
#       about half the invalidations, not all of them; many invalidations
#       are model rotation or prompt-structure churn the TTL knob can't help)
#
#   estimated_weekly_savings_usd ≈
#       bot_7d_cost_usd × invalidated_ratio
#       × CACHE_WRITE_FRACTION_ON_INVALIDATED
#       × FRACTION_REMEDIABLE_BY_TTL_BUMP
#
# This is an ESTIMATE. The downstream UI labels it "est. ~$X/wk" with a
# tilde and tooltip explaining the heuristic. Savings is capped at the
# bot's 7d cost (can't save more than you spent) and returned as None
# when cost data is unavailable rather than failing the proposal.
CACHE_WRITE_FRACTION_ON_INVALIDATED: float = 0.92
FRACTION_REMEDIABLE_BY_TTL_BUMP: float = 0.50
SAVINGS_WINDOW_DAYS: int = 7


# ── Dismiss signatures (Phase A.5) ───────────────────────────────────────────
#
# Stable per-finding-type strings the dismissals store keys on. Per-bot
# scoping happens at the store layer (record_dismissal carries bot_id).
# Bumping these invalidates prior dismissals for everyone on the pod —
# only do that on a deliberate finding-shape change. Generator name +
# short suffix is the convention so the operator-visible Suppressions
# list reads as "<generator>: <what was dismissed>".
DISMISS_SIG_INVALIDATION_TYPED = "cache_ttl_tuner:invalidation_elevated_fix"
DISMISS_SIG_INVALIDATION_INVESTIGATE = (
    "cache_ttl_tuner:invalidation_elevated_investigate"
)
DISMISS_SIG_HIT_RATE_LOW = "cache_ttl_tuner:hit_rate_low_investigate"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _bot_cost_over_window(
    bot_id: str,
    *,
    days: int,
    shared_dir: Path | None = None,
) -> float | None:
    """Return the bot's total LLM spend over the trailing window in USD.

    Fail-open: any import error or read error returns ``None``. The
    caller treats that as "no estimate available" — the proposal still
    ships, just without the savings chip.
    """
    try:
        from cost_ledger import read_events
    except ImportError:
        return None
    try:
        if shared_dir is None:
            events = read_events(bot_id, days=days)
        else:
            events = read_events(bot_id, days=days, shared_dir=shared_dir)
        total = 0.0
        for e in events:
            try:
                total += float(e.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
        return total
    except Exception:
        return None


def _estimate_savings_for_invalidation(
    *,
    invalidated_ratio: float,
    bot_7d_cost_usd: float | None,
) -> float | None:
    """Compute the cacheRetention-flip savings estimate.

    Returns None when cost data is missing or non-positive (no useful
    base to scale). Capped at the bot's 7d cost (savings can't exceed
    what was actually spent). Returns a positive USD/week estimate
    otherwise — the caller decides whether to round / floor for display.
    """
    if bot_7d_cost_usd is None or bot_7d_cost_usd <= 0.0:
        return None
    if invalidated_ratio <= 0.0:
        return None
    estimate = (
        bot_7d_cost_usd
        * invalidated_ratio
        * CACHE_WRITE_FRACTION_ON_INVALIDATED
        * FRACTION_REMEDIABLE_BY_TTL_BUMP
    )
    return min(estimate, bot_7d_cost_usd)


def make_cache_invalidation_elevated_proposal(
    signal: Any,
    *,
    shared_dir: Path | None = None,
) -> Proposal:
    """`cache_invalidation_elevated` → UpdateAgentDefaults flipping cacheRetention.

    The signal already encodes the diagnostic: cacheWrite cost was paid
    without reaping cacheRead savings on a meaningful share of
    cache-participating turns. The dominant cause is the Anthropic
    prompt-cache TTL being shorter than the inter-turn gap.

    Anthropic's prompt-cache TTL is controlled per-model via
    ``params.cacheRetention``: ``"short"`` = 5 minutes (default,
    1x cache-write price); ``"long"`` = 1 hour (~2x cache-write price
    but eliminates TTL invalidations on conversational cadences).

    The fix is a single enum flip from "short" to "long", per-bot,
    fully reversible. Exactly the auto-small candidate from the
    cache-retention design discussion — PR B's eligibility table
    resolves ``UpdateAgentDefaults`` + ``hygiene``/``improvement``
    urgency + low risk_tag to ``tier_floor=auto-small``.

    ``shared_dir`` is forwarded to the cost-ledger lookup used to
    estimate weekly savings for the TTL flip. None falls back to the
    cost_ledger default. Cost-lookup failure leaves
    ``estimated_savings_usd`` as None (no chip rendered) rather than
    failing the Proposal.
    """
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    invalidated = int(details.get("invalidated_count") or 0)
    participating = int(details.get("participating_count") or 0)
    ratio = float(details.get("invalidated_ratio") or 0.0)
    threshold = float(details.get("threshold_ratio") or 0.0)
    window_days = int(details.get("window_days") or 7)

    # Savings estimate — feed the proposal's ranking + UI chip. Heuristic
    # documented at the top of this module; bot-cost lookup is fail-open.
    bot_7d_cost = _bot_cost_over_window(
        bot_id, days=SAVINGS_WINDOW_DAYS, shared_dir=shared_dir,
    )
    estimated_savings_usd = _estimate_savings_for_invalidation(
        invalidated_ratio=ratio,
        bot_7d_cost_usd=bot_7d_cost,
    )

    problem = (
        f"{bot_name} prompt cache: {ratio:.0%} of cached turns invalidated "
        f"({invalidated}/{participating} over {window_days}d). "
        f"Flipping Anthropic ``cacheRetention`` from ``short`` (5min) to "
        f"``long`` (1h) keeps the cache warm across human-paced gaps."
    )
    headline = f"Make {bot_name}'s prompt cache last longer"

    # ── Phase B (2026-06-04 protocol) — operator-first content ──────────────
    summary = (
        f"{bot_name} is paying to re-read the same system prompt {ratio:.0%} "
        f"of the time because its prompt-cache window (5 minutes) is shorter "
        f"than the gap between your messages. Switching to the longer "
        f"(1 hour) window keeps the cache warm so the bot doesn't redo work "
        f"between turns."
    )
    explanation = (
        f"Anthropic's prompt-cache feature lets the model skip re-reading "
        f"the system prompt on follow-up turns. Anthropic charges less for "
        f"those cached reads, so a warm cache means lower cost per turn.\n\n"
        f"Diagnosis. The cache has a TTL (time to live). {bot_name} is set to "
        f"short (5 minutes), which is fine for back-to-back chatbot turns "
        f"but expires during human-paced conversations where you reply "
        f"minutes later. We watched {bot_name} over the last {window_days} "
        f"days: {invalidated} of {participating} cached turns expired "
        f"before your next reply, then got fully re-read and re-charged. "
        f"That {ratio:.0%} invalidation rate is the signal that the TTL "
        f"is mis-tuned for your usage rhythm.\n\n"
        f"Why long helps. The bot holds the cache for an hour after each "
        f"turn. Most human-paced gaps (5 to 20 minutes) stay inside that "
        f"window, so the model skips re-reading the prompt and uses the "
        f"cached copy instead.\n\n"
        f"What could go wrong. The longer window means slightly higher "
        f"cache-storage cost per cached turn. In practice that's dominated "
        f"by the savings from cache hits — but if your usage shifts to "
        f"lots of short bursts later, the math might flip and you'd want "
        f"to revert. We auto-revert if the invalidation rate doesn't "
        f"improve after 7 days. This only applies to Anthropic models "
        f"(we checked: yes); the setting is a no-op on OpenAI or Google."
    )
    context = (
        f"{bot_name} re-wrote prompt cache without reaping read savings on "
        f"{invalidated} of {participating} cache-participating turns over "
        f"the last {window_days} days ({ratio:.0%}, threshold "
        f"{threshold:.0%}). The dominant cause of this pattern is the "
        f"Anthropic prompt-cache TTL expiring between turns — users "
        f"pause longer than the cache lives, so each follow-up turn "
        f"pays cacheWrite cost again with no cacheRead benefit.\n\n"
        f"**What this proposal does:** flips "
        f"``params.cacheRetention`` from ``short`` (5-minute TTL, "
        f"OpenClaw's default) to ``long`` (1-hour TTL) on every "
        f"Anthropic model in the bot's catalog. The applier writes the "
        f"override once and the materializer fans it out per-model.\n\n"
        f"**Trade-off:** ``long`` costs ~2x per cache-write but "
        f"eliminates TTL invalidations on conversational cadences. For "
        f"the pattern this signal detects (high invalidation ratio over "
        f"a meaningful window) the breakeven is well under one reused "
        f"turn, so the expected $ swing is strongly negative — i.e. "
        f"the bot pays less, not more.\n\n"
        f"**Reversal:** fully reversible. The matching applier can flip "
        f"the value back to ``short`` at any time; the operator can also "
        f"do so manually on the {COST_TAB_LOCATION}. The override is "
        f"recorded in the per-bot sandbox file so the choice survives "
        f"deploys.\n\n"
        f"**Skip this fix if:** the bot is purely 1-turn (no follow-up "
        f"turns to benefit from a longer TTL — e.g. webhook-driven "
        f"automations with no conversation), or you've intentionally "
        f"set ``cacheRetention=short`` for a reason the generator can't "
        f"see. Recording a config-intent with ``set_intent(bot_id, "
        f"{INTENT_FIELD_PATH!r}, 'short', reason='…')`` will suppress "
        f"future re-proposals."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cache_invalidation_elevated:{bot_id}"],
        provenance=Provenance(
            technique="cache_ttl_tuner.cache_invalidation_elevated",
            signals={
                "invalidated_count": invalidated,
                "participating_count": participating,
                "invalidated_ratio": round(ratio, 4),
                "threshold_ratio": threshold,
                "window_days": window_days,
                # Standardized attribution block so proposal_history can
                # dedup by cause across re-runs. The dedup window is
                # PROPOSAL_HISTORY_WINDOW_DAYS days enforced by the
                # generator before emitting; the cause_key is stable.
                "root_cause_attribution": {
                    "cause_key": CAUSE_KEY_TTL_TOO_SHORT,
                    "headline": (
                        f"{bot_name}: Anthropic prompt-cache TTL "
                        f"(``cacheRetention``) shorter than the bot's "
                        f"inter-turn gap"
                    ),
                    "confidence": 0.85,
                    "primary_target": INTENT_FIELD_PATH,
                    "evidence": {
                        "invalidated_ratio": round(ratio, 4),
                        "invalidated_count": invalidated,
                        "participating_count": participating,
                        "window_days": window_days,
                    },
                },
                # Recorded for traceability of the savings estimate. Null
                # when the cost-ledger lookup fails (estimate is also None).
                "bot_7d_cost_usd": bot_7d_cost,
            },
            confidence=0.85,
        ),
        problem=problem,
        action=UpdateAgentDefaults(
            bot_id=bot_id,
            fields={CACHE_RETENTION_DOTPATH: "long"},
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["model_config"],
        ),
        # Falsifiable claim the verify daemon resolves at apply_time +
        # window_days: the bot's invalidated_ratio should drop after the
        # flip. We pin direction (down) but leave magnitude soft (0.0
        # baseline means "any improvement is a win" — strict-zero
        # invalidation is unrealistic on real traffic). Fallback=revert
        # so a non-improvement walks itself back without operator
        # intervention.
        claim=Claim(
            metric="cache_invalidation_ratio",
            direction="down",
            magnitude=0.0,
            window_days=7,
            baseline=round(ratio, 4),
            fallback="revert",
        ),
        approval_audience="pod_operator",
        urgency="hygiene",  # decidable cost cleanup; magnitude≤1 → auto-small
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        estimated_savings_usd=estimated_savings_usd,
        # ── Phase B operator-first content (Tier 1 — auto-apply) ───────
        summary=summary,
        explanation=explanation,
        action_label="Switch to long-window caching",
        manual_path=f"Cost Optimization → {bot_name}",
        dismiss_signature=DISMISS_SIG_INVALIDATION_TYPED,
        dismiss_scope="kind",
    )


def make_cache_invalidation_investigation_fallback(signal: Any) -> Proposal:
    """`cache_invalidation_elevated` → Investigation, used as a fallback.

    When the autonomous-fix path is suppressed (operator already pinned
    cacheRetention=short with intent, or a similar Proposal was emitted
    in the dedup window) but the signal is still firing, surface an
    Investigation so the operator at least sees the finding.

    This is the same shape as primary_model_floor_advisor's Investigation
    fallback path — never silently swallow a firing signal when the
    autonomous fix can't proceed; tell the operator why.
    """
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    invalidated = int(details.get("invalidated_count") or 0)
    participating = int(details.get("participating_count") or 0)
    ratio = float(details.get("invalidated_ratio") or 0.0)
    threshold = float(details.get("threshold_ratio") or 0.0)
    window_days = int(details.get("window_days") or 7)

    problem = (
        f"{bot_name}: {ratio:.0%} of cached turns were invalidated "
        f"({invalidated}/{participating} over {window_days}d)"
    )
    headline = f"Take another look at {bot_name}'s prompt-cache setting"

    # ── Phase B operator-first content (Tier 2 — UI manual only) ────────────
    summary = (
        f"{bot_name} is still re-paying for the prompt cache on "
        f"{ratio:.0%} of cached turns, but a previous decision is "
        f"blocking the autonomous fix from re-emitting. Worth a quick "
        f"look to decide whether that earlier call still holds."
    )
    explanation = (
        f"Anthropic's prompt-cache feature lets the model skip re-reading "
        f"the system prompt on follow-up turns. When the cache window "
        f"is too short for your usage rhythm, each turn re-pays the "
        f"cacheWrite cost without reaping the cacheRead savings.\n\n"
        f"What happened. {bot_name}'s invalidation rate has been "
        f"{ratio:.0%} over the last {window_days} days "
        f"({invalidated} of {participating} turns), which would "
        f"normally trigger the autonomous fix (flip the window from "
        f"short to long). That fix was suppressed this run because "
        f"either an operator deliberately pinned the current setting, "
        f"or the same proposal was raised recently.\n\n"
        f"What to check. Open the Cost Optimization page for {bot_name} "
        f"and confirm the current cacheRetention setting. If the "
        f"earlier decision was based on a usage pattern that has "
        f"since changed, clear the operator override and the next "
        f"generator run will re-emit the typed fix automatically.\n\n"
        f"What could go wrong. If the earlier decision still applies, "
        f"acting now might re-introduce a problem the operator already "
        f"diagnosed. Reading the prior intent or proposal history "
        f"before acting is the safer move."
    )
    context = (
        f"{bot_name} re-wrote prompt cache without reaping read savings on "
        f"{invalidated} of {participating} cache-participating turns over "
        f"the last {window_days} days ({ratio:.0%}, threshold "
        f"{threshold:.0%}).\n\n"
        f"**The autonomous fix (flip ``cacheRetention`` from ``short`` "
        f"to ``long``) was not emitted this run** — either an operator "
        f"explicitly recorded a config-intent pinning ``cacheRetention`` "
        f"to its current value, or a similar Proposal was already raised "
        f"recently.\n\n"
        f"**Operator triage:** check the {COST_TAB_LOCATION} for the "
        f"current setting. If the existing intent / prior proposal is "
        f"stale (the bot's traffic pattern has changed, or the operator "
        f"who set the intent has moved on), revoke the intent via "
        f"``revoke_intent(bot_id, intent_id, actor=…)`` and the next "
        f"generator run will re-emit the typed fix."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cache_invalidation_elevated:{bot_id}"],
        provenance=Provenance(
            technique="cache_ttl_tuner.cache_invalidation_elevated.fallback",
            signals={
                "invalidated_count": invalidated,
                "participating_count": participating,
                "invalidated_ratio": round(ratio, 4),
                "threshold_ratio": threshold,
                "window_days": window_days,
                "root_cause_attribution": {
                    "cause_key": CAUSE_KEY_TTL_TOO_SHORT,
                    "headline": (
                        f"{bot_name}: prompt-cache invalidation elevated "
                        f"but autonomous fix suppressed"
                    ),
                    "confidence": 0.4,
                    "primary_target": INTENT_FIELD_PATH,
                    "evidence": {
                        "invalidated_ratio": round(ratio, 4),
                        "fallback_reason": "intent_or_history_suppressed",
                    },
                },
            },
            confidence=0.4,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # Investigation fallback intentionally omits the savings chip — the
        # autonomous fix was suppressed, so we don't want to advertise
        # savings on a Proposal that won't apply itself. Operator can see
        # the upstream estimate on the typed-action Proposal in history.
        # ── Phase B operator-first content (Tier 2 — UI manual only) ───
        summary=summary,
        explanation=explanation,
        action_label="Open Cost Optimization",
        manual_path=f"Cost Optimization → {bot_name}",
        dismiss_signature=DISMISS_SIG_INVALIDATION_INVESTIGATE,
        dismiss_scope="kind",
    )


def make_cache_hit_rate_low_proposal(
    signal: Any,
    *,
    shared_dir: Path | None = None,
) -> Proposal:
    """`cache_hit_rate_low` → Investigation pointing at prompt structure.

    Low realized hit rate over cache-participating events is a different
    failure mode from elevated invalidation: it suggests the cache is
    engaging but not paying off — typically because the prompt prefix
    differs between turns (system prompt regenerating, dynamic block at
    the top, model rotation). The proposal points operators at prompt
    structure first; TTL is the secondary suspect.

    Stays Investigation deliberately — there's no single autonomous
    knob to flip for prompt-structure churn. The fix is a code change
    to whatever generates the prompt; the generator's job is naming
    that.
    """
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    details: dict = _signal_dict_get(signal, "details") or {}
    hit_rate = float(details.get("hit_rate") or 0.0)
    threshold = float(details.get("threshold_ratio") or 0.0)
    participating = int(details.get("participating_count") or 0)
    window_days = int(details.get("window_days") or 7)
    cache_read = int(details.get("cache_read_tokens") or 0)
    cache_write = int(details.get("cache_write_tokens") or 0)
    fresh_input = int(details.get("input_tokens") or 0)

    problem = (
        f"{bot_name}: cache hit rate {hit_rate:.0%} "
        f"(threshold {threshold:.0%}, {participating} turns over "
        f"{window_days}d)"
    )
    headline = f"Look at why {bot_name}'s prompt cache isn't paying off"

    # ── Phase B operator-first content (Tier 5 — paste-to-bot instruction) ──
    summary = (
        f"{bot_name} only realized {hit_rate:.0%} of its prompt tokens "
        f"from cache over the last {window_days} days — the cache is "
        f"engaging but not saving you much. The usual cause is the "
        f"system prompt changing slightly each turn, which resets the "
        f"cache. There's no autonomous fix for this one; it's a "
        f"prompt-structure change."
    )
    explanation = (
        f"Anthropic's prompt-cache feature lets the model reuse the "
        f"system prompt across turns when the prefix matches exactly. "
        f"Low hit rate with stable response times usually means the "
        f"prefix is being invalidated by small changes you didn't "
        f"notice — not by the cache window being too short.\n\n"
        f"Likely causes, most common first. (1) The system prompt has "
        f"a dynamic field at the top — current time, recent events, a "
        f"memory dump — and every turn re-templates it. Move the "
        f"dynamic content into the user message or out of the system "
        f"prompt. (2) A high-churn block sits before stable content. "
        f"Reorder so the stable content comes first. (3) The bot is "
        f"rotating between models for different turns (Haiku for "
        f"heartbeats, Sonnet for replies) so neither cache pool warms "
        f"up. Pin to one model for cache-sensitive workloads.\n\n"
        f"What could go wrong. Moving content out of the system prompt "
        f"can change the bot's behavior in subtle ways if the model "
        f"was leaning on that text. Test on a low-traffic bot first, "
        f"and watch the hit rate over the next few days to confirm "
        f"the change actually helped."
    )

    # Tier-5 paste-to-bot instruction. The operator can hand this
    # verbatim to the bot whose cache is misbehaving, and the bot will
    # do the structural analysis itself. Kept tight + concrete so the
    # bot has a chance of running it without follow-up clarification.
    manual_instruction = (
        f"Audit your prompt structure for cache efficiency. Read your "
        f"current system prompt and identify any content that changes "
        f"between turns: dynamic timestamps, recent events, memory "
        f"dumps, anything templated per-turn. List the dynamic fields "
        f"you found, propose a refactor that moves them out of the "
        f"system prompt (or to its tail), and explain the trade-offs "
        f"of each move. Cache hit rate over the last "
        f"{window_days} days was {hit_rate:.0%} "
        f"(threshold {threshold:.0%}); the goal is getting that above "
        f"{threshold:.0%}."
    )
    context = (
        f"{bot_name} realized only {hit_rate:.0%} of its prompt tokens "
        f"from cache across {participating} cache-participating turns over "
        f"the last {window_days} days (threshold {threshold:.0%}). "
        f"Token totals on participating turns: cacheRead={cache_read:,}, "
        f"cacheWrite={cache_write:,}, fresh input={fresh_input:,}.\n\n"
        f"Low hit rate with stable inter-turn gaps usually points to "
        f"**prompt-structure churn** rather than TTL expiry. Common "
        f"causes worth checking in order:\n\n"
        f"  - **System prompt regenerating each turn.** If the system "
        f"prompt is templated with a dynamic field (current time, recent "
        f"events, ephemeral context block), every turn invalidates the "
        f"prefix cache. Move the dynamic content into the user message "
        f"or out of the system prompt entirely.\n"
        f"  - **Dynamic context block at the top of the prompt.** Prompt "
        f"caching keys on the prefix. A high-churn block (memory dump, "
        f"recent events) before stable content breaks the cache. Reorder "
        f"so stable content sits at the prompt prefix.\n"
        f"  - **Model rotation.** Different models have different "
        f"caches. Heartbeats on Haiku + user turns on Sonnet means "
        f"neither pool warms up. Pin the bot to one model for cache-"
        f"sensitive workloads.\n"
        f"  - **TTL too short** (secondary suspect — check "
        f"`cache_invalidation_elevated` first if it's also firing). "
        f"The cache_ttl_tuner generator emits an UpdateAgentDefaults "
        f"proposal autonomously when that signal is the dominant "
        f"failure mode.\n\n"
        f"To silence this signal for a bot where low hit rate is "
        f"expected, raise "
        f"`session_economics.bots.{bot_id}.hit_rate_threshold` in "
        f"network.json."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cache_hit_rate_low:{bot_id}"],
        provenance=Provenance(
            technique="cache_ttl_tuner.cache_hit_rate_low",
            signals={
                "hit_rate": round(hit_rate, 4),
                "threshold_ratio": threshold,
                "participating_count": participating,
                "window_days": window_days,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "fresh_input_tokens": fresh_input,
                "root_cause_attribution": {
                    "cause_key": CAUSE_KEY_HIT_RATE_LOW,
                    "headline": (
                        f"{bot_name}: cache hit rate below threshold; "
                        f"likely prompt-structure churn"
                    ),
                    "confidence": 0.6,
                    "primary_target": "prompt_structure",
                    "evidence": {
                        "hit_rate": round(hit_rate, 4),
                        "threshold_ratio": threshold,
                    },
                },
            },
            confidence=0.75,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[_signal_dict_get(signal, "id") or ""],
        # ── Phase B operator-first content (Tier 5 — paste-to-bot) ─────
        summary=summary,
        explanation=explanation,
        manual_instruction=manual_instruction,
        # No action_label — Investigation proposals without a typed
        # action default to "Take this on" / generic Investigation
        # button. The manual_instruction is the operator-actionable
        # tier here.
        dismiss_signature=DISMISS_SIG_HIT_RATE_LOW,
        dismiss_scope="kind",
    )


# ── Dispatch ──────────────────────────────────────────────────────────────────

# Map session_economics signal type → proposal factory. Signal types not in
# this map are silently ignored — keeps the generator forward-compatible
# with new session_economics types it doesn't yet know about. Notably
# ``bot_unused`` is intentionally absent: it's an informational severity
# with no config knob the system can suggest.
#
# ``cache_invalidation_elevated`` factory returns the autonomous-fix
# Proposal. The observer applies suppression checks (intent + history)
# before emitting; when suppressed it falls through to
# :func:`make_cache_invalidation_investigation_fallback`.
#
# Factories accept ``(signal, *, shared_dir=None)``. observe.py forwards
# the context's shared_dir so the savings estimator can scope its
# cost-ledger lookup; callers that don't have a shared_dir (legacy
# tests, exploratory scripts) omit the kwarg and the savings estimate
# falls back to None (no chip rendered).
SIGNAL_TYPE_TO_FACTORY: dict[str, Callable[..., Proposal]] = {
    "cache_invalidation_elevated": make_cache_invalidation_elevated_proposal,
    "cache_hit_rate_low": make_cache_hit_rate_low_proposal,
}

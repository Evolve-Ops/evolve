"""generators.cost_spike.signal_proposals — Signal → Proposal factory.

``make_cost_spike_proposal`` takes one ``cost_spike`` Signal and emits
one Investigation Proposal. Investigation (claim=None) is the right
shape: the spike is real but the *response* depends on context the
operator has (was a new app rolled out? did a model upgrade ship?) and
we don't. The Proposal surfaces the comparison and points at the
typical causes; the operator applies the fix manually.
"""

from __future__ import annotations

from typing import Any

from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "cost_spike"
DIMENSION = "cost"


# Stable Phase A.5 suppression signature. Kind-scoped — dismissing the
# spike finding for a bot suppresses repeat emissions until the
# operator lifts it. Per-bot scoping flows through the store layer.
DISMISS_SIG_COST_SPIKE = "cost_spike:elevated_spend"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def make_cost_spike_proposal(signal: Any) -> Proposal:
    """`cost_spike` Signal → Investigation Proposal.

    The signal already encodes the diagnostic (cur/prior totals, ratio,
    thresholds). The Proposal restates it in operator-facing prose and
    enumerates the usual suspects so the next click leads somewhere
    useful.
    """
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    bot_name = bot_label(bot_id)
    sig_id = _signal_dict_get(signal, "id") or ""
    details: dict = _signal_dict_get(signal, "details") or {}

    cur = float(details.get("cost_cur_usd") or 0.0)
    prior = float(details.get("cost_prior_usd") or 0.0)
    ratio = float(details.get("ratio") or 0.0)
    multiplier = float(details.get("multiplier_threshold") or 0.0)
    floor = float(details.get("floor_usd") or 0.0)
    window_days = int(details.get("window_days") or 7)

    problem = (
        f"{bot_name}: {window_days}d cost ${cur:.2f} is {ratio:.1f}× prior "
        f"{window_days}d (${prior:.2f})"
    )
    headline = f"Look at why {bot_name}'s spend just jumped"

    # ── Phase C-2 (2026-06-04 protocol) — operator-first content ────────────
    summary = (
        f"{bot_name} spent ${cur:.2f} over the last {window_days} days, "
        f"about {ratio:.1f}× the prior {window_days} days (${prior:.2f}). "
        f"The jump is real — usually a model upgrade, a traffic spike, "
        f"or a runaway heartbeat or cron. Worth a quick look before it "
        f"compounds."
    )
    explanation = (
        f"Bot spending comes from two things: how often the bot runs "
        f"and how much each run costs. A sudden jump in the rolling "
        f"total means one of those changed in the last few days.\n\n"
        f"Diagnosis. We compared {bot_name}'s spend in the last "
        f"{window_days} days against the {window_days} days before "
        f"that. Current: ${cur:.2f}. Prior: ${prior:.2f}. Ratio: "
        f"{ratio:.1f}× (above the {multiplier:.1f}× multiplier and the "
        f"${floor:.0f} absolute floor — both have to clear before this "
        f"signal fires, so the spike isn't noise).\n\n"
        f"Where to look. Open the Cost tab and switch to the "
        f"trigger-kind breakdown for {bot_name} — that splits spend by "
        f"heartbeat vs user turn vs cron. If heartbeat share grew, the "
        f"primary-model override may have stopped applying (sessions "
        f"billing the wrong tier). If user-turn share grew, it's "
        f"usually a new app or a traffic shift.\n\n"
        f"What could go wrong. If the increase is intentional — you "
        f"shipped a new app or rolled out a smarter model — the right "
        f"call is to dismiss this and let the engine stop nagging you. "
        f"Reverting an intentional change would regress the thing the "
        f"change was meant to improve."
    )
    context = (
        f"{bot_name}'s spend over the past {window_days} days (${cur:.2f}) is "
        f"{ratio:.1f}× the prior {window_days} days (${prior:.2f}) and above "
        f"the ${floor:.0f} absolute floor (multiplier threshold "
        f"{multiplier:.1f}×). The spike is real; what matters is whether the "
        f"increase is intentional or a regression.\n\n"
        f"**Common causes:**\n"
        f"- Model upgrade — primary or heartbeat model changed to a higher tier.\n"
        f"- Traffic spike — new app, new user cohort, or a season-driven jump.\n"
        f"- Heartbeat leak — heartbeat sessions running long, billing primary "
        f"model instead of the override.\n"
        f"- Runaway cron — a job firing more often than declared, or each fire "
        f"waking the main agent.\n\n"
        f"**Where to look:**\n"
        f"- Cost tab → per-bot trend chart for the 14-day comparison.\n"
        f"- Cost tab → trigger-kind breakdown for the current 7d — heartbeat "
        f"share vs user_turn share.\n"
        f"- Sessions page filtered to this bot — long sessions or repeat "
        f"session_ids often surface here first.\n\n"
        f"If the increase is intentional, dismiss this proposal; if not, the "
        f"trigger-kind breakdown usually points at the right fix."
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"cost_spike:{bot_id}"],
        provenance=Provenance(
            technique="cost_spike.week_over_week",
            signals={
                "cost_cur_usd": round(cur, 2),
                "cost_prior_usd": round(prior, 2),
                "ratio": round(ratio, 3),
                "multiplier_threshold": multiplier,
                "floor_usd": floor,
                "window_days": window_days,
            },
            confidence=0.9,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=[sig_id],
        # ── Phase C-2 operator-first content (Tier 2 — UI manual) ───────
        summary=summary,
        explanation=explanation,
        action_label="Open Cost tab",
        manual_path=f"Cost tab → {bot_name} → trigger-kind breakdown",
        dismiss_signature=DISMISS_SIG_COST_SPIKE,
        dismiss_scope="kind",
    )

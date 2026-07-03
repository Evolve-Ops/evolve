"""generators.budget_hawk.evaluate — Veto logic for arbiter's veto pass.

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §7.5.

Rules (in order):

  1. If the proposal's urgency is ``security_critical`` or ``operational_urgent``,
     Budget Hawk never vetoes. Those paths must go through.

  2. If the proposal's bot is at or over its per-bot daily hard cap and the
     proposal would incur additional LLM cost, veto.

  3. If the proposal's bot is over the warn cap AND the proposal came from
     an optimizer, annotate (soft signal — show in UI, don't block).

  4. Otherwise, pass.

Budget Hawk does not evaluate its own proposals (arbiter.veto.run_veto_pass
skips self-guardian).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from better_engine_config import BetterEngineConfig
from schema.proposal import Proposal
from schema.veto import VetoResult


# Callable: (bot_id) -> today's spend in USD
SpendNowReader = Callable[[str], float]


@dataclass
class BudgetHawkEvalContext:
    config: BetterEngineConfig
    spend_now: SpendNowReader
    # A minimal set of action kinds considered "LLM-spending" — used by
    # rule 2. Conservative: if we don't recognize the kind, we assume it
    # doesn't incur cost.
    cost_incurring_action_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "InstallApp",  # new app often adds background crons
                "ManifestUpdate",  # broadening scope adds queries
                # NB: Investigation and config patches generally don't
                # incur meaningful incremental cost.
            }
        )
    )


def make_evaluator(ctx: BudgetHawkEvalContext):
    """Return a guardian evaluator bound to a config + spend reader."""

    def evaluate(proposal: Proposal) -> VetoResult | None:
        return _evaluate(proposal, ctx)

    return evaluate


def _evaluate(
    proposal: Proposal,
    ctx: BudgetHawkEvalContext,
) -> VetoResult | None:
    # Rule 1: urgent paths always pass
    if proposal.urgency in ("security_critical", "operational_urgent"):
        return VetoResult(
            guardian_id="budget_hawk",
            verdict="pass",
            reason="urgent bypass",
        )

    try:
        today_usd = ctx.spend_now(proposal.bot_id)
    except Exception as e:  # noqa: BLE001 — if we can't read spend, don't block
        return VetoResult(
            guardian_id="budget_hawk",
            verdict="pass",
            reason=f"spend unknown ({e}); defaulting to pass",
        )

    hard_cap = ctx.config.budget_hard_cap_usd(proposal.bot_id)
    warn_cap = ctx.config.budget_warn_cap_usd(proposal.bot_id)

    action_kind = getattr(
        proposal.action, "kind", type(proposal.action).__name__
    )

    cost_incurring = action_kind in ctx.cost_incurring_action_kinds

    # Rule 2: hard-cap breach + cost-incurring → veto
    if hard_cap > 0 and today_usd >= hard_cap and cost_incurring:
        return VetoResult(
            guardian_id="budget_hawk",
            verdict="veto",
            severity="high",
            reason=(
                f"bot {proposal.bot_id} is at or over daily hard cap "
                f"(${today_usd:.2f} >= ${hard_cap:.2f}); proposal would add cost"
            ),
            details={
                "today_usd": today_usd,
                "hard_cap_usd": hard_cap,
                "action_kind": action_kind,
            },
        )

    # Rule 3: warn zone + optimizer → annotate
    # We don't have the generator type here, but urgency=improvement or hygiene
    # is a strong signal of optimizer-origin.
    if (
        warn_cap > 0
        and today_usd >= warn_cap
        and proposal.urgency in ("improvement", "hygiene")
    ):
        return VetoResult(
            guardian_id="budget_hawk",
            verdict="annotate",
            severity="medium",
            reason=(
                f"bot {proposal.bot_id} spend ${today_usd:.2f} in warn zone; "
                "optimizer proposals may be deferred"
            ),
            details={
                "today_usd": today_usd,
                "warn_cap_usd": warn_cap,
            },
        )

    return VetoResult(
        guardian_id="budget_hawk",
        verdict="pass",
        reason="no budget concern",
    )


def evaluate(
    proposal: Proposal,
    *,
    config: BetterEngineConfig,
    spend_now: SpendNowReader,
) -> VetoResult | None:
    """Convenience one-shot evaluator without creating a context object."""
    ctx = BudgetHawkEvalContext(config=config, spend_now=spend_now)
    return _evaluate(proposal, ctx)

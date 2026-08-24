"""arbiter.routing — Autonomous-vs-human + approval-audience routing.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §3.5 and §3.10.

Two decisions:

  1. **Autonomy gate** — is this proposal eligible for autonomous apply?
     The rule is permissive-by-default (Principle 5: optimize for learning),
     with a narrow blocklist of Irreversibility Surfaces.

  2. **Approval audience resolution** — if not autonomous, which user type
     should review this? Depends on bot configuration (``sysadmin_audience``
     and ``role``/``multi_user``) and the proposal's dimension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from schema.proposal import (
    IRREVERSIBILITY_SURFACES,
    ApprovalAudience,
    Proposal,
    action_to_dict,
)

from .security_screen import ScreenResult, screen_proposal


# ─────────────────────────────────────────────────────────────────────────────
# Bot configuration for routing (subset of what loads from network.json etc.)
# ─────────────────────────────────────────────────────────────────────────────


SysadminAudience = Literal["pod_operator", "primary_user", "both"]

BotRole = Literal["primary", "member"]


@dataclass
class BotRoutingConfig:
    """Just enough bot config to make routing decisions.

    The full bot config comes from ``network.json`` + ``better-engine-config.json``
    plus the bot-level overrides. The registry assembles this.
    """

    bot_id: str
    role: BotRole  # "primary" for the Evolve bot; "member" otherwise
    multi_user: bool = False
    sysadmin_audience: SysadminAudience | None = None  # None → use default for shape

    def resolved_sysadmin_audience(self) -> SysadminAudience:
        """Return the effective sysadmin_audience, applying defaults."""
        if self.sysadmin_audience is not None:
            return self.sysadmin_audience
        if self.role == "primary":
            return "pod_operator"
        if self.multi_user:
            # Team bot — admin noise in a shared channel is wrong
            return "pod_operator"
        # Single-user personal bot — owner is their own sysadmin by default
        return "both"


# ─────────────────────────────────────────────────────────────────────────────
# Autonomy gate
# ─────────────────────────────────────────────────────────────────────────────


def _is_upward_autonomy_action(proposal: Proposal) -> bool:
    """True when the action widens an autonomy-ladder posture.

    The permanent carve-out (spec-autonomy-ladder-2026-06-10.md §3.2):
    ``UpdateAutonomyPosture`` promotions never run autonomously, no
    matter what risk_tag a generator attached — applying a promotion IS
    the deliberate operator act the whole ladder is built around.
    Fail closed: if the direction can't be computed (missing fields,
    autonomy package unimportable), treat it as upward.
    """
    action = proposal.action
    if isinstance(action, dict):  # tolerate not-yet-typed actions
        kind = action.get("kind")
        expected = action.get("expected_current_rung")
        rung = action.get("rung")
    else:
        kind = getattr(action, "kind", "")
        expected = getattr(action, "expected_current_rung", None)
        rung = getattr(action, "rung", None)
    if kind != "UpdateAutonomyPosture":
        return False
    try:
        from autonomy.catalog import action_is_promotion
    except ImportError:  # pragma: no cover — partial installs fail closed
        return True
    return action_is_promotion(expected, rung)


def _security_screen(proposal: Proposal) -> ScreenResult:
    """Run the folded security mandate over the proposal's action.

    The screen (``arbiter.security_screen``) is the retirement home of
    review.py's auto-reject rules + AST layer (decision 2026-07-28). Any
    screen crash fails closed — a broken screen must not widen autonomy.
    """
    try:
        action = proposal.action
        action_dict = action if isinstance(action, dict) else action_to_dict(action)
        return screen_proposal({"action": action_dict, "bot_id": proposal.bot_id})
    except Exception as exc:  # noqa: BLE001 — fail closed, never fail open
        from .security_screen import SecurityDenial

        return ScreenResult(
            denials=[
                SecurityDenial(
                    "security_screen_error",
                    "security screen crashed — failing closed",
                    str(exc),
                )
            ],
            ast_available=False,
        )


def is_autonomous_eligible(proposal: Proposal) -> bool:
    """Return True if the proposal can run autonomously.

    Rule (spec §3.5): all three must hold.
      - ``reversibility == "auto"`` — applier can undo via paired RevertPlan
      - ``blast_radius ∈ {"local", "bot"}`` — no platform-wide effect
      - ``touches`` disjoint from IRREVERSIBILITY_SURFACES

    A proposal without a claim cannot be autonomous (nothing to verify against
    and nothing to revert from). ``InvestigationAction`` proposals fall here.

    Additionally: autonomy-ladder PROMOTIONS are permanently excluded
    (see :func:`_is_upward_autonomy_action`); demotions fall through to
    the normal rules — narrowing is always safe to apply.
    """
    if _is_upward_autonomy_action(proposal):
        return False

    rt = proposal.risk_tag

    if rt.reversibility != "auto":
        return False
    if rt.blast_radius not in ("local", "bot"):
        return False
    if rt.touches_irreversibility_surface():
        return False
    if proposal.claim is None:
        # Cannot auto-revert without a claim; cannot verify without a claim.
        return False
    if proposal.revert_on_failure is None:
        # Revert plan must be attached for autonomous path.
        return False

    # Folded security mandate (review.py retirement, 2026-07-28): a proposal
    # that trips a deny rule never applies autonomously, and a missing AST
    # layer fails closed to human review (parity with the retired reviewer's
    # force-flag on review_ast import failure).
    screen = _security_screen(proposal)
    if screen.denials or not screen.ast_available:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Approval audience resolution
# ─────────────────────────────────────────────────────────────────────────────


# Dimensions that are operational concerns (honor ``sysadmin_audience``).
_OPERATIONAL_DIMENSIONS: frozenset[str] = frozenset(
    {
        "substrate_health",  # Sysadmin Watchdog
        "cost",  # Budget Hawk
        "safety",  # Security Warden
    }
)

# Dimensions that are improvement concerns (route to bot owner).
_IMPROVEMENT_DIMENSIONS: frozenset[str] = frozenset(
    {
        "utility",
        "capability_growth",
        "efficiency",
        "voice_fit",
        "hygiene",
    }
)

# Dimensions that are always pod-operator (meta concerns).
_META_DIMENSIONS: frozenset[str] = frozenset({"meta_health"})


def resolve_audience(proposal: Proposal, bot: BotRoutingConfig) -> ApprovalAudience:
    """Determine the approval audience for a proposal given its bot's config.

    Three cases, per the dimension:
      - Operational (substrate/cost/safety): honor the bot's sysadmin_audience.
        Security proposals with urgency ``security_critical`` additionally
        reach pod_operator as a hard override.
      - Improvement (utility, capability_growth, etc.): route to the bot's owner.
      - Meta (meta_health): always pod_operator.

    Returns the ApprovalAudience value; ``"none"`` means autonomous (caller
    should verify via ``is_autonomous_eligible``).
    """
    dim = proposal.dimension

    # Security-critical hard override: always reaches pod_operator.
    # For non-critical severities, fall through to normal rules.
    security_critical_override = (
        dim == "safety" and proposal.urgency == "security_critical"
    )

    if dim in _META_DIMENSIONS:
        return "pod_operator"

    if dim in _IMPROVEMENT_DIMENSIONS:
        # Route to bot's owner: primary user for single-user member bots;
        # pod_operator for primary bots and multi-user member bots.
        if bot.role == "primary" or bot.multi_user:
            return "pod_operator"
        return "bot_primary_user"

    if dim in _OPERATIONAL_DIMENSIONS:
        audience = bot.resolved_sysadmin_audience()
        mapped: ApprovalAudience
        if audience == "pod_operator":
            mapped = "pod_operator"
        elif audience == "primary_user":
            mapped = "bot_primary_user"
        else:  # "both"
            mapped = "both"

        if security_critical_override and mapped == "bot_primary_user":
            # Force dual surface so pod_operator also sees it
            return "both"
        return mapped

    # Unknown dimension — default to pod_operator to keep proposals visible
    # somewhere rather than routed to ``none``.
    return "pod_operator"


# ─────────────────────────────────────────────────────────────────────────────
# Full routing decision
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RoutingDecision:
    """The arbiter's combined routing output for a single proposal."""

    autonomous: bool
    audience: ApprovalAudience  # meaningful when autonomous == False
    reasons: list[str]  # short strings describing the decision


def route(proposal: Proposal, bot: BotRoutingConfig) -> RoutingDecision:
    """Run both gates — autonomy and audience — and return a decision.

    Caller is responsible for applying the decision (transitioning the
    proposal to ``approved_auto`` or writing it for the appropriate human
    audience).
    """
    reasons: list[str] = []

    auto = is_autonomous_eligible(proposal)
    if auto:
        reasons.append("autonomous: reversible + bounded blast + safe surfaces")
        return RoutingDecision(
            autonomous=True, audience="none", reasons=reasons
        )

    # Not autonomous — figure out who reviews.
    if _is_upward_autonomy_action(proposal):
        reasons.append(
            "autonomy promotion: permanently excluded from autonomous apply"
        )
    if proposal.risk_tag.reversibility != "auto":
        reasons.append(f"not reversible (reversibility={proposal.risk_tag.reversibility})")
    if proposal.risk_tag.blast_radius not in ("local", "bot"):
        reasons.append(
            f"platform-level blast radius ({proposal.risk_tag.blast_radius})"
        )
    if proposal.risk_tag.touches_irreversibility_surface():
        offending = sorted(
            set(proposal.risk_tag.touches) & IRREVERSIBILITY_SURFACES
        )
        reasons.append(
            f"touches irreversibility surface(s): {offending}"
        )
    if proposal.claim is None:
        reasons.append("no claim (cannot verify or auto-revert)")
    if proposal.revert_on_failure is None and proposal.claim is not None:
        reasons.append("claim present but revert_plan missing")
    screen = _security_screen(proposal)
    for denial in screen.denials:
        reasons.append(f"security screen deny: {denial.rule_id} ({denial.detail})")
    if not screen.ast_available:
        reasons.append(
            "security AST layer unavailable — autonomy fails closed to human review"
        )

    audience = resolve_audience(proposal, bot)
    reasons.append(f"audience resolved: {audience}")

    return RoutingDecision(autonomous=False, audience=audience, reasons=reasons)

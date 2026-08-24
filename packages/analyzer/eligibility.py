"""eligibility — fix-risk + decidability classifier for the tier-c gate.

The Home page's authority dropdown ("Ask first" / "Auto small, ask big" /
"Decide for me when you can") drives whether evo auto-fires actions
without an operator click. This module classifies each Signal or
Proposal into a ``(fix_risk, decidable, tier_floor)`` triple — the
single attribute the JS auto-act loop reads.

Three axes:

  * ``fix_risk`` (low | medium | high) — how bad if the fix itself
    goes sideways. Spec: internal/spec-severity-framework-2026-05-18.md §3.
  * ``decidable`` (bool) — does the action have a clear computable
    right answer that doesn't need human judgment.
  * ``tier_floor`` (auto-small | auto | ask) — the minimum tier at
    which the system auto-fires. Sole input to the JS auto-act loop.

Tier floors:

  * ``auto-small`` — auto-fires in tier (b) "Auto small, ask big"
    AND tier (c) "Decide for me". Reserved for low-risk + decidable
    actions on low-severity findings.
  * ``auto`` — auto-fires in tier (c) only. Low/medium risk +
    decidable actions on real-but-bounded findings.
  * ``ask`` — never auto-fires regardless of tier. Includes:
    - high fix_risk (always confirm)
    - security-tagged findings (always confirm)
    - undecidable actions (no clear answer)
    - novel producers (no track record — future work)

Spec: internal/spec-severity-framework-2026-05-18.md §1, §3, §8.
"""

from __future__ import annotations

from typing import Literal, NamedTuple


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────


FixRisk = Literal["low", "medium", "high"]
TierFloor = Literal["auto-small", "auto", "ask"]


class Eligibility(NamedTuple):
    """Tier-c gate inputs for a Signal or Proposal action.

    ``reason`` is a short human-readable justification surfaced in the
    auto-act report toast so the operator can audit what fired and why.

    ``auto_snooze`` is an alternate auto-action path for *low-priority,
    non-security, non-actively-firing* signals: instead of running a fix
    (which they don't have anyway), the auto-act loop quietly snoozes
    them so they stop crowding the narrative. Independent of
    ``tier_floor`` — a signal can be ``tier_floor="ask"`` (no fix
    available) but ``auto_snooze=True`` (it's noise, hush it). Snooze
    duration is in :data:`AUTO_SNOOZE_DURATION` below.
    """

    fix_risk: FixRisk
    decidable: bool
    tier_floor: TierFloor
    reason: str = ""
    auto_snooze: bool = False

    def to_dict(self) -> dict:
        return {
            "fix_risk": self.fix_risk,
            "decidable": self.decidable,
            "tier_floor": self.tier_floor,
            "reason": self.reason,
            "auto_snooze": self.auto_snooze,
        }


# Auto-snooze duration string for the snooze endpoint. 7d is the
# default: a signal that fires for 7 consecutive days gets re-examined.
# Long enough to clear the narrative; short enough that genuinely
# important conditions resurface.
AUTO_SNOOZE_DURATION: str = "7d"


# ─────────────────────────────────────────────────────────────────────────────
# Remediation kind → fix_risk lookup
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors packages/admin/evolve_admin/remediation/handlers.py HANDLERS.
# Conservative defaults — anything not in this table falls through to
# "medium" via :func:`fix_risk_for_remediation`. The admin handlers.py
# carries the canonical operator-readable annotations; this table is
# the analyzer-side mirror so signal-detail responses can attach risk
# without depending on evolve_admin.


REMEDIATION_FIX_RISK: dict[str, FixRisk] = {
    # Cosmetic / cache-clear — fully reversible, no blast radius
    "reset_baseline": "low",
    # Single-bot single-cron config flip — reversible by re-running
    # with the opposite param
    "flip_cron_session_target": "low",
    # Pod-wide infra reinstall — reversible (re-runs idempotently) but
    # touches every LaunchDaemon. Bounded but broad
    "install_infra_jobs": "medium",
    # Security-policy changes — never auto-fire even in tier (c). A
    # wrong allowlist could expose or break legitimate tools; a wrong
    # exec policy could open the bot to escape
    "set_exec_allowlist": "high",
    "set_exec_security": "high",
    # Restoring a demoted autonomy posture is a promotion — permanently
    # excluded from auto-fire (spec-autonomy-ladder §3.2/§3.3).
    "restore_autonomy_posture": "high",
    # cacheRetention flip via UpdateAgentDefaults — per-bot, single
    # enum knob, fully reversible. Mirrors _ACTION_KIND_BASE_RISK so
    # signals whose remediation block names this kind get the same
    # low-risk classification proposals do.
    "UpdateAgentDefaults": "low",
}


def fix_risk_for_remediation(kind: str | None) -> FixRisk:
    """Look up the fix-risk for a remediation kind. Unknown kinds fall
    through to "medium" — conservative; means new handlers never
    auto-fire until explicitly tagged here."""
    if not kind:
        return "medium"
    return REMEDIATION_FIX_RISK.get(kind, "medium")


# ─────────────────────────────────────────────────────────────────────────────
# Proposal action kind → decidability + base fix_risk
# ─────────────────────────────────────────────────────────────────────────────
# Mirrors the Action variants in schema/proposal.py. Decidability says
# "the action has a clear computable right answer." Fix-risk is the
# base before risk_tag composition.
#
# Action kinds NOT listed default to undecidable + medium-risk.


# Decidable kinds — the action has a concrete right answer that follows
# from the proposal's claim + structured params. Most pure config edits
# qualify.
_DECIDABLE_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "ConfigPatch",
        "ManifestUpdate",
        "TierAdjustment",
        "ThrottleGenerator",
        "PauseGenerator",
        "UpdatePluginConfig",
        "UpdatePluginLoadPaths",
        "EnablePluginEntry",
        "DisablePluginEntry",
        "AddSignalCollection",
        "UpdateMcpServerConfig",
        "MemoryCurate",
        # UpdateAgentDefaults (PR B, 2026-05-31) — narrow whitelist
        # (currently only cacheRetention enum); reversible by re-applying
        # the inverse value; bounded blast radius (per-bot, one knob).
        # Exactly the auto-small candidate from the cache-retention
        # design discussion.
        "UpdateAgentDefaults",
        # DEMOTIONS ONLY ever reach this lookup: classify_proposal's
        # autonomy carve-out returns "ask" for promotions before the
        # allowlist check runs (spec-autonomy-ladder §3.2 — narrowing
        # may use auto lanes; widening never does).
        "UpdateAutonomyPosture",
    }
)


# Kinds that ALWAYS need a person regardless of risk_tag — judgment-
# required by nature (free-text edits, investigations).
_HUMAN_ONLY_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "WorkflowInstruction",   # free-text agenda; needs human read
        "AgentsAppend",          # free-text instruction; needs read
        "SoulEdit",              # persona editing
        "Investigation",         # ask-a-human-to-look
        "DeprecateApp",          # destructive
        # AL-1.7: promotion is THE operator vouch (design §4/§7.2). An
        # auto-applied promotion launders a manifest past the only gate
        # the lifecycle has, so it is human-only by nature regardless of
        # how benign its risk_tag looks. Second, independent enforcement
        # of the rule app_promotion.promotion_policy states.
        "PromoteApp",            # the operator vouch itself
        "RetireOrphan",          # destructive
        "BuildApp",              # new app generation
        "InstallApp",            # new code on bot
        "InstallMcpServer",      # new external service
        "RemoveMcpServer",       # removal of capability
        "UpdatePluginAllowDeny", # security policy
        "VetoAnnotation",        # guardian-driven, human review path
    }
)


# Per-kind base fix_risk floor. Composed with risk_tag below so a low-
# risk-tagged ConfigPatch can be "low" while a platform-blast ConfigPatch
# escalates. Unknown kinds default to "medium" via :func:`_base_fix_risk`.
_ACTION_KIND_BASE_RISK: dict[str, FixRisk] = {
    "ConfigPatch": "low",
    "ManifestUpdate": "low",
    "TierAdjustment": "low",
    "ThrottleGenerator": "low",
    "PauseGenerator": "low",
    "UpdatePluginConfig": "low",
    "UpdatePluginLoadPaths": "low",
    "EnablePluginEntry": "medium",   # turning on a tool — slightly riskier
    "DisablePluginEntry": "low",      # turning off — usually safe
    "AddSignalCollection": "low",
    "UpdateMcpServerConfig": "medium",
    "MemoryCurate": "low",
    # cacheRetention flip: per-bot, single enum knob, bounded
    # blast radius. Inverse flip restores prior state — fully
    # reversible without operator action. tier_floor=auto-small
    # follows from {low risk × decidable × magnitude≤1 urgency}.
    "UpdateAgentDefaults": "low",
    # Demotions only (promotions return "ask" before base-risk
    # composition — see classify_proposal's autonomy carve-out).
    # Narrowing a posture is one-click reversible by re-promoting.
    "UpdateAutonomyPosture": "low",
}


def _base_fix_risk(action_kind: str) -> FixRisk:
    return _ACTION_KIND_BASE_RISK.get(action_kind, "medium")


# Surfaces from schema/proposal.IRREVERSIBILITY_SURFACES that force "high"
# fix_risk regardless of action kind. Kept here as a copy to avoid the
# import dependency; bring them into sync if the schema list changes.
_IRREVERSIBILITY_SURFACES: frozenset[str] = frozenset(
    {"auth", "tools", "channel_config", "gateway_core",
     "app_install", "app_removal", "bot_specialization"}
)


def _compose_proposal_fix_risk(
    action_kind: str,
    risk_tag: dict | None,
) -> FixRisk:
    """Combine action-kind base risk with risk_tag (blast_radius,
    reversibility, touches) into a final fix_risk.

    Rules (most-restrictive-wins):
      - touches any irreversibility surface (auth/tools/etc.) → high
      - reversibility="none" → high
      - blast_radius="platform" → bump base up one level
      - reversibility="manual" → bump base up one level
      - otherwise → base from action kind
    """
    base = _base_fix_risk(action_kind)
    if not isinstance(risk_tag, dict):
        return base

    touches = risk_tag.get("touches") or []
    if any(t in _IRREVERSIBILITY_SURFACES for t in touches):
        return "high"

    reversibility = risk_tag.get("reversibility")
    if reversibility == "none":
        return "high"

    blast = risk_tag.get("blast_radius")
    if blast == "platform" or reversibility == "manual":
        return _bump(base)
    return base


def _bump(risk: FixRisk) -> FixRisk:
    if risk == "low":
        return "medium"
    if risk == "medium":
        return "high"
    return "high"


def _autonomy_action_is_promotion(action: dict) -> bool:
    """Direction of an UpdateAutonomyPosture action dict; fails closed
    to promotion (missing fields / unimportable catalog ⇒ True)."""
    try:
        from autonomy.catalog import action_is_promotion
    except ImportError:  # pragma: no cover — partial installs fail closed
        return True
    if not isinstance(action, dict):
        return True
    return action_is_promotion(
        action.get("expected_current_rung"), action.get("rung"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Severity-framework safety rails
# ─────────────────────────────────────────────────────────────────────────────


def _is_security_critical(severity_framework: dict | None) -> bool:
    """True when the finding is security-vector + magnitude ≥ 3.
    Security-critical findings never auto-fire even when decidable."""
    if not isinstance(severity_framework, dict):
        return False
    return (
        severity_framework.get("vector") == "security"
        and int(severity_framework.get("magnitude") or 0) >= 3
    )


def _is_auto_snooze_eligible(signal_dict: dict) -> bool:
    """True when a signal is safe to silently snooze under tier-b/c.

    Criteria (ALL must hold):
      * magnitude ≤ 1 (cosmetic / advisory tier; nothing user-affecting)
      * vector is NOT "security" (security stays visible at any magnitude)
      * details.severity_active is NOT true (the condition isn't
        currently firing — if it were, the operator should see it)

    Targets the canonical "low-priority noise" class:
    - audit_missing/audit_stale, pod_silent, session_drop, tasks_blocked
      (operations:1, not active)
    - bot_recovered (operations:0, info-tier historical)
    - bot_unused (quality:0, judgment call)
    - version_behind (operations:1, advisory)
    """
    sf = signal_dict.get("severity_framework") or {}
    if not isinstance(sf, dict):
        return False
    vector = sf.get("vector")
    if vector == "security":
        return False
    magnitude = int(sf.get("magnitude") or 0)
    if magnitude > 1:
        return False
    details = signal_dict.get("details") or {}
    if isinstance(details, dict) and details.get("severity_active") is True:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Tier-floor composition
# ─────────────────────────────────────────────────────────────────────────────


def _tier_floor(
    *,
    fix_risk: FixRisk,
    decidable: bool,
    security_critical: bool,
    severity_magnitude: int = 2,
) -> TierFloor:
    """Compose the minimum-tier-for-auto-fire from the three axes.

    Rules:
      * Not decidable                → "ask"
      * Security-critical            → "ask"
      * fix_risk = high              → "ask"
      * fix_risk = low AND
        severity magnitude ≤ 1        → "auto-small"
      * fix_risk in {low, medium}    → "auto"
    """
    if not decidable:
        return "ask"
    if security_critical:
        return "ask"
    if fix_risk == "high":
        return "ask"
    if fix_risk == "low" and severity_magnitude <= 1:
        return "auto-small"
    return "auto"


# ─────────────────────────────────────────────────────────────────────────────
# Public API — classify a Signal or Proposal
# ─────────────────────────────────────────────────────────────────────────────


def classify_signal(signal_dict: dict) -> Eligibility:
    """Classify a Signal (already-serialized dict) for the tier-c gate.

    A signal is decidable when it carries a structured ``remediation``
    block with a known kind. Without remediation, the system has no
    handle to apply — but it may still be eligible for the auto-snooze
    quieting path (see :func:`_is_auto_snooze_eligible`).
    """
    auto_snooze = _is_auto_snooze_eligible(signal_dict)

    remediation = signal_dict.get("remediation")
    if not isinstance(remediation, dict):
        return Eligibility(
            fix_risk="medium",
            decidable=False,
            tier_floor="ask",
            reason="no structured remediation; needs human read"
                   + (" (auto-snoozable noise)" if auto_snooze else ""),
            auto_snooze=auto_snooze,
        )

    kind = remediation.get("kind")
    if not kind:
        return Eligibility(
            fix_risk="medium",
            decidable=False,
            tier_floor="ask",
            reason="remediation has no kind",
            auto_snooze=auto_snooze,
        )

    fix_risk = fix_risk_for_remediation(kind)
    decidable = fix_risk != "high"  # high-risk handlers never auto-fire

    severity_framework = signal_dict.get("severity_framework") or {}
    if _is_security_critical(severity_framework):
        return Eligibility(
            fix_risk=fix_risk,
            decidable=decidable,
            tier_floor="ask",
            reason=f"security-critical (magnitude {severity_framework.get('magnitude')}); always confirm",
            auto_snooze=False,   # security never auto-snoozes
        )

    magnitude = int(severity_framework.get("magnitude") or 2)
    tier_floor = _tier_floor(
        fix_risk=fix_risk,
        decidable=decidable,
        security_critical=False,
        severity_magnitude=magnitude,
    )
    return Eligibility(
        fix_risk=fix_risk,
        decidable=decidable,
        tier_floor=tier_floor,
        reason=f"remediation kind={kind!r} (risk={fix_risk})",
        # When the fix path is "ask" (high-risk) BUT the signal also
        # qualifies as low-priority noise, auto_snooze still applies —
        # they're independent decisions on the same finding.
        auto_snooze=auto_snooze,
    )


def classify_proposal(proposal_dict: dict) -> Eligibility:
    """Classify a Proposal (already-serialized dict) for the tier-c gate.

    Reads ``action.kind``, ``risk_tag``, ``urgency``. Hygiene-urgency +
    low-risk + decidable actions become "auto-small"; security-critical
    urgency always becomes "ask"; everything else falls through the
    composed rules.
    """
    action = proposal_dict.get("action") or {}
    action_kind = action.get("kind", "") if isinstance(action, dict) else ""
    urgency = proposal_dict.get("urgency") or "improvement"
    risk_tag = proposal_dict.get("risk_tag")

    # Safety rails — always ask, regardless of how decidable the action is.
    #
    # Folded security mandate (review.py retirement, 2026-08-14): this is the
    # OTHER auto-fire lane besides arbiter.routing.is_autonomous_eligible, so
    # it consults the same deny mandate — a proposal that trips a deny rule
    # (or that cannot get AST coverage, or whose screen crashes) never
    # auto-fires. Ask-only, not blocked: the operator can still act manually.
    try:
        from arbiter.security_screen import screen_proposal

        _screen = screen_proposal(proposal_dict)
        _screen_deny = bool(_screen.denials) or not _screen.ast_available
        _screen_reason = (
            f"security screen deny: {_screen.denials[0].rule_id}"
            if _screen.denials
            else "security AST layer unavailable — failing closed"
        )
    except Exception as exc:  # noqa: BLE001 — a broken screen must not auto-fire
        _screen_deny = True
        _screen_reason = f"security screen crashed — failing closed ({exc})"
    if _screen_deny:
        return Eligibility(
            fix_risk="high",
            decidable=False,
            tier_floor="ask",
            reason=f"{_screen_reason}; always confirm",
        )

    if urgency == "security_critical":
        return Eligibility(
            fix_risk="high",
            decidable=False,
            tier_floor="ask",
            reason="security_critical urgency; always confirm",
        )
    if action_kind == "UpdateAutonomyPosture":
        # Autonomy-ladder carve-out (spec-autonomy-ladder §3.2):
        # promotions are PERMANENTLY excluded from every auto-approve
        # lane — applying one is the deliberate operator act the ladder
        # is built around. Direction comes from the action's CAS witness
        # (expected_current_rung → rung) and fails closed to promotion.
        # Demotions fall through to the normal composed rules below —
        # narrowing is always safe to apply.
        if _autonomy_action_is_promotion(action):
            return Eligibility(
                fix_risk="high",
                decidable=False,
                tier_floor="ask",
                reason=(
                    "autonomy promotion: permanently excluded from "
                    "auto-approve lanes; always confirm"
                ),
            )
    if action_kind in _HUMAN_ONLY_ACTION_KINDS:
        return Eligibility(
            fix_risk="medium",
            decidable=False,
            tier_floor="ask",
            reason=f"action kind {action_kind!r} requires human judgment",
        )
    if action_kind not in _DECIDABLE_ACTION_KINDS:
        # Conservative default — anything not on the allowlist asks.
        return Eligibility(
            fix_risk="medium",
            decidable=False,
            tier_floor="ask",
            reason=f"action kind {action_kind!r} not on auto-eligible allowlist",
        )

    # Verify the apply-then-revert plumbing exists.
    has_claim = bool(proposal_dict.get("claim"))
    has_revert = bool(proposal_dict.get("revert_on_failure"))
    if not (has_claim and has_revert):
        return Eligibility(
            fix_risk="medium",
            decidable=False,
            tier_floor="ask",
            reason="missing claim or revert plan; can't be auto-verified",
        )

    fix_risk = _compose_proposal_fix_risk(action_kind, risk_tag)
    decidable = fix_risk != "high"

    # Magnitude proxy from urgency — hygiene/whimsy lean low; the
    # severity framework's vector × magnitude lives on Signals, not
    # Proposals, so we infer from urgency for the auto-small split.
    if urgency in ("hygiene", "whimsy"):
        magnitude_proxy = 1
    elif urgency in ("improvement", "substrate_warn"):
        magnitude_proxy = 2
    else:
        magnitude_proxy = 3

    tier_floor = _tier_floor(
        fix_risk=fix_risk,
        decidable=decidable,
        security_critical=False,
        severity_magnitude=magnitude_proxy,
    )
    return Eligibility(
        fix_risk=fix_risk,
        decidable=decidable,
        tier_floor=tier_floor,
        reason=f"kind={action_kind} urgency={urgency} risk={fix_risk}",
    )

"""generators.sysadmin_watchdog.proposals — Helpers for building typed Proposals.

Sysadmin Watchdog now emits **Signals** for every detected platform-failure
condition (see ``signals.py`` and the per-detector modules). The one
remaining Proposal-shaped output is ACL drift — an autonomous-eligible
ConfigPatch the applier can run unattended. Other historical factories
(gateway-down, plugin-missing, launchd-not-loaded, openclaw-config-invalid,
user-missing, version-behind) were removed when their detectors moved to
the Signal store; the alerts route through ``signals.store.observe`` and
surface on the Alerts page.
"""

from __future__ import annotations

from typing import Any

from schema.proposal import (
    Claim,
    ConfigPatch,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "sysadmin_watchdog"
DIMENSION = "substrate_health"


# Phase A.5 dismiss signature — per-bot via store.
DISMISS_SIG_ACL_DRIFT = "sysadmin_watchdog:acl_drift"


def _provenance(technique: str, **signals: Any) -> Provenance:
    return Provenance(
        technique=f"sysadmin_watchdog.{technique}",
        signals=dict(signals),
        confidence=0.95,
    )


def make_acl_drift_fix(
    bot_id: str,
    *,
    acl_config_path: str,
    admin_summary: str,
    conversational_pitch: str | None = None,
    audience: str = "pod_operator",
) -> Proposal:
    """Autonomous-eligible ConfigPatch to restore evolve ACL on bot's .openclaw/.

    ``acl_config_path`` is a site-specific config pointer the applier reads.
    For L2 we emit the proposal; the applier dispatch needs to be set up to
    map this back to the real ACL-setting logic.
    """
    claim = Claim(
        metric="acl.evolve_read",
        direction="up",
        magnitude=1.0,
        window_days=1,
        baseline=0.0,
        fallback="revert",
    )
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"acl_drift:{bot_id}"],
        provenance=_provenance("acl_drift_detector"),
        problem=admin_summary,
        action=ConfigPatch(
            target_path=acl_config_path,
            operation="set",
            value={"acl_restored": True},
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["acl"],
        ),
        claim=claim,
        approval_audience=audience,  # type: ignore[arg-type]
        urgency="substrate_warn",
        admin_surface_summary=admin_summary[:120],
        conversational_pitch=conversational_pitch,
        # ── Phase C-8 operator-first content (Tier 1 — auto-apply) ──────
        summary=(
            f"The evolve user lost macOS ACL read access on {bot_id}'s "
            f".openclaw directory. Without it, monitors and the admin "
            f"server can't read the bot's config — features that read "
            f"from .openclaw will silently degrade. Restoring the ACL "
            f"is a one-step fix."
        ),
        explanation=(
            f"Member-bot .openclaw directories are owned by the bot's "
            f"macOS user. The evolve service user reads them via an "
            f"inherited ACL granted at deploy time. When that ACL "
            f"drifts (operator chmod'd, a deploy hiccup, a backup-"
            f"restore that didn't preserve ACLs), the evolve user "
            f"starts getting EACCES on the config — and the rest of "
            f"the engine degrades quietly.\n\n"
            f"Diagnosis. The ACL-drift detector saw evolve lose read "
            f"access on `/Users/{bot_id}/.openclaw/`. The applier "
            f"restores the inherited ACL at the directory level (the "
            f"same call deploy.py uses).\n\n"
            f"What this changes. Re-applies the read ACL to "
            f"{bot_id}'s .openclaw directory. Idempotent — applying "
            f"on a healthy directory is a no-op. Fully reversible by "
            f"running `chmod -R -N` on the directory (don't, "
            f"unless you mean to).\n\n"
            f"What could go wrong. If the ACL is missing because the "
            f"bot user was deliberately tightening permissions, this "
            f"reverses that. Confirm with the operator before "
            f"applying if you suspect intent."
        ),
        action_label="Restore evolve ACL",
        manual_path=f"Permissions → {bot_id}",
        dismiss_signature=DISMISS_SIG_ACL_DRIFT,
        dismiss_scope="kind",
    )

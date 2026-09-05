"""Security Warden scope evaluator.

Applies during arbiter veto pass for any proposal touching sensitive surfaces.

Rules:
  - Proposal touches ``auth`` → veto (configuration of credentials is
    human-approval only regardless of source)
  - Proposal touches ``tools`` and the action kind EXPANDS scope
    (InstallApp, ManifestUpdate with broaden_scope/add_capability) → veto
  - Proposal touches ``channel_config`` → annotate (user should know)
  - Proposal touches ``memory`` → annotate (not blocked, but flagged)
  - Otherwise pass

User-profile inference (``user_profile_inferrer``) doesn't go through
the proposal queue at all — it writes bot-side per-session — so it's
uninvolved here. ACL fix proposals from Sysadmin Watchdog DO touch
``acl`` but not ``auth``, so they're not blocked.
"""

from __future__ import annotations

from schema.proposal import Proposal
from schema.veto import VetoResult


_EXPAND_MANIFEST_OPS = frozenset({"broaden_scope", "add_capability"})


def evaluate_scope(proposal: Proposal) -> VetoResult | None:
    """Return a VetoResult, or None if Warden has no opinion about this proposal."""
    touches = set(proposal.risk_tag.touches)

    # Warden only weighs in on proposals that touch sensitive surfaces
    sensitive = {"auth", "tools", "channel_config", "memory"}
    if not (touches & sensitive):
        return None

    # Rule 1: auth touch → veto, regardless of action kind
    if "auth" in touches:
        return VetoResult(
            guardian_id="security_warden",
            verdict="veto",
            severity="critical",
            reason="proposal touches auth surface; requires human review via explicit admin flow",
            details={"touched_surfaces": sorted(touches)},
        )

    # Rule 2: tool-scope expansion → veto
    action = proposal.action
    action_kind = getattr(action, "kind", type(action).__name__)
    if "tools" in touches:
        expands = False
        if action_kind == "InstallApp":
            expands = True
        elif action_kind == "ManifestUpdate":
            op = getattr(action, "operation", "")
            if op in _EXPAND_MANIFEST_OPS:
                expands = True
        if expands:
            return VetoResult(
                guardian_id="security_warden",
                verdict="veto",
                severity="high",
                reason=(
                    f"proposal {action_kind} expands tool scope; "
                    "requires explicit user/admin confirmation"
                ),
                details={
                    "touched_surfaces": sorted(touches),
                    "action_kind": action_kind,
                },
            )
        # Tool-scope contraction or same-scope is fine with an annotation
        return VetoResult(
            guardian_id="security_warden",
            verdict="annotate",
            severity="medium",
            reason="proposal touches tools; verify scope change is not expansion",
            details={"action_kind": action_kind},
        )

    # Rule 3: channel_config touch → annotate
    if "channel_config" in touches:
        return VetoResult(
            guardian_id="security_warden",
            verdict="annotate",
            severity="medium",
            reason="proposal touches channel_config; user should be aware",
        )

    # Rule 4: memory touch → annotate
    if "memory" in touches:
        return VetoResult(
            guardian_id="security_warden",
            verdict="annotate",
            severity="low",
            reason="proposal touches memory content",
        )

    return None

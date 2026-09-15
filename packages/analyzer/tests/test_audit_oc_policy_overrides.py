"""Pin the suppression of OC native audit findings that contradict
documented Evolve policy.

Background: OpenClaw's native ``security audit --deep --json`` includes
the advisory ``tools.exec.security_full_configured`` which warns
whenever ``tools.exec.security = "full"``. The advisory's recommendation
("Prefer allowlist with ask prompts; reserve full for tightly scoped
break-glass agents only") is the OPPOSITE of Evolve's documented
member-bot default — see internal/spec-app-derived-permissions-2026-05-24.md
§"Why full as default": a member bot runs as its own macOS user
account with no privileged reach, so "full" inside that account is
the right default.

Pre-fix, the advisory was firing nightly across every bot since
Phase A shipped (2026-05-25). The operator saw an alert that said
"Exec security=full is configured" with a fix recommendation of
"switch to allowlist" — exactly the regression Phase A was meant to
fix. Filter it at the audit forwarding step so the OC binary's
opinion doesn't override Evolve's policy.

Adding to ``_OC_POLICY_OVERRIDES_SUPPRESS`` is a policy statement.
When the OC upstream advisory accepts a policy hint (e.g. a config
key that toggles the recommendation off), this filter should become
that toggle and these tests retire. Until then, the explicit policy
list is the right shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def test_full_exec_advisory_is_suppressed():
    """The exact check id firing on personal_bot/etc post-Phase-A is in the
    suppression list."""
    assert audit._is_oc_policy_override(
        "tools.exec.security_full_configured"
    ) is True


def test_unrelated_oc_findings_are_NOT_suppressed():
    """Suppression list is narrow — only known policy contradictions.
    Real findings like 'plugins.code_safety' or 'gateway.auth.missing'
    still flow through.
    """
    assert audit._is_oc_policy_override("plugins.code_safety") is False
    assert audit._is_oc_policy_override("gateway.auth.missing") is False
    assert audit._is_oc_policy_override("") is False
    assert audit._is_oc_policy_override("tools.exec.security_allowlist") is False


def test_policy_override_table_carries_spec_links():
    """Every entry in _OC_POLICY_OVERRIDES_SUPPRESS must explain WHY
    via a spec path — guards against silent additions that aren't
    grounded in a documented policy decision.

    ``internal/`` joins ``docs/`` as an accepted prefix: PUB-2 (PD-3c) moved
    the design corpus there, and a reason citing a diagnosis or spec doc is
    grounded whichever tree the doc lives in.
    """
    overrides = audit._OC_POLICY_OVERRIDES_SUPPRESS
    assert overrides, "expected at least one entry"
    for check_id, reason in overrides.items():
        assert reason, f"missing reason for {check_id}"
        assert (
            "internal/" in reason
            or "docs/" in reason
            or "spec-" in reason
            or "openclaw#" in reason
        ), (
            f"entry for {check_id} must reference an internal/ or docs/ spec "
            f"path or upstream issue: got reason={reason!r}"
        )


# ── 2026-06-04 quality-control pass: 4 new suppressions ─────────────────────


def test_multi_user_heuristic_suppressed():
    """OC's generic multi-user heuristic is suppressed — Evolve has its
    own specific posture checks in security_warden.posture that point
    at concrete config the operator can fix."""
    assert audit._is_oc_policy_override(
        "security.trust_model.multi_user_heuristic"
    ) is True


def test_tools_reachable_permissive_policy_suppressed():
    """Same rationale as tools.exec.security_full_configured —
    permissive plugin-tool reach inside a bot's own macOS user account
    is the documented Evolve default."""
    assert audit._is_oc_policy_override(
        "plugins.tools_reachable_permissive_policy"
    ) is True


def test_perms_world_readable_NOT_suppressed():
    """Security fix 2026-06-12: OC's world-readable-config finding is NO LONGER
    suppressed. openclaw.json + auth-profiles.json hold tokens/keys and are
    enforced to 0600 (the old suppression's "parent dir 0700" premise was false
    — deploy.py forces the home to 0755). A world-readable bot config is now a
    real finding we want surfaced, not hidden."""
    assert audit._is_oc_policy_override(
        "fs.config.perms_world_readable"
    ) is False


def test_weak_tier_suppressed():
    """OC's weak_tier check fires on fallbacks (gpt-4o-mini, haiku-4-5)
    which are intentionally cheaper. Hard-suppress until upstream adds
    a primary-specific check that distinguishes weak primary from
    intentionally-cheap fallback."""
    assert audit._is_oc_policy_override("models.weak_tier") is True

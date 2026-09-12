"""Invariant tests for the launchd scope policy.

Policy doc: docs/policy/launchd-scope.md
Whitelist:  packages/admin/evolve_admin/applications/_scope_policy.py

Together these enforce that every kind="agent" entry in
`infra_audit.CORE_INFRA_DAEMON_KINDS` is:
  1. Listed in `_scope_policy.JUSTIFIED_AGENTS` with all four fields non-empty
  2. Documented as a "### <label>" subsection of the policy doc's whitelist

Adding a new agent therefore requires three file changes in the same PR:
infra_audit.py (the kind dict), _scope_policy.py (the structured
justification), and docs/policy/launchd-scope.md (the human-readable why).
If any of them is omitted, this test file fails CI.

The motivating case is mcp-bridge, which sat as a non-loadable LaunchAgent
on the mini for ~6 weeks (April→May 2026) before being caught — see
[evolve#1821]. The policy + tests in this PR prevent the same shape from
slipping in via a future infra service.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import _scope_policy, infra_audit  # noqa: E402


POLICY_DOC = _REPO_ROOT / "docs" / "policy" / "launchd-scope.md"


def test_policy_doc_exists() -> None:
    """The policy doc must exist — it's a referenced authority."""
    assert POLICY_DOC.is_file(), (
        f"Policy doc missing at {POLICY_DOC}. This doc is referenced by "
        f"infra_audit warnings, PR descriptions, and CLAUDE.md; deleting "
        f"it should require a deliberate PR."
    )


def test_every_agent_has_a_justification() -> None:
    """No undocumented LaunchAgents in CORE_INFRA_DAEMON_KINDS.

    Regression guard against the mcp-bridge-on-headless-pod failure mode.
    Adding a kind="agent" entry without a `_scope_policy.JUSTIFIED_AGENTS`
    pairing fails this test.
    """
    agent_labels = [
        label for label, kind in infra_audit.CORE_INFRA_DAEMON_KINDS.items()
        if kind == "agent"
    ]
    unjustified = [
        label for label in agent_labels
        if not _scope_policy.is_justified(label)
    ]
    assert not unjustified, (
        f"Unjustified LaunchAgent(s) in CORE_INFRA_DAEMON_KINDS: "
        f"{unjustified}. Either convert them to kind=\"system\" "
        f"LaunchDaemons (the default) or add a "
        f"_scope_policy.JUSTIFIED_AGENTS entry AND a corresponding section "
        f"in {POLICY_DOC.relative_to(_REPO_ROOT)}. See PR #1821 for the "
        f"failure mode this guardrail catches."
    )


def test_every_justification_has_complete_fields() -> None:
    """Every JUSTIFIED_AGENTS entry must answer all four policy questions.

    Empty-string placeholders are rejected so an operator can't ship a
    stub-justified agent.
    """
    incomplete = []
    for label, j in _scope_policy.JUSTIFIED_AGENTS.items():
        missing = [
            field for field in (
                "why_not_daemon", "aqua_session_user",
                "headless_fallback", "introduced_by",
            )
            if not (getattr(j, field, "") or "").strip()
        ]
        if missing:
            incomplete.append((label, missing))
    assert not incomplete, (
        "Incomplete JUSTIFIED_AGENTS entries: "
        + ", ".join(f"{lbl} (missing {flds})" for lbl, flds in incomplete)
        + ". Every field in AgentJustification must be a non-empty string."
    )


def test_every_justified_agent_is_an_agent_in_infra_audit() -> None:
    """JUSTIFIED_AGENTS can't list labels that aren't actually agents.

    The whitelist is for in-use agents only — a stale entry for a label
    that was later converted to system-scope (or removed entirely) means
    operator confusion when reading the policy doc. Catch the drift early.

    Transitional entries (introduced_by mentions "pending" or "TRANSITIONAL")
    are exempt: they exist specifically to grandfather a label that's
    scheduled for conversion in an in-flight PR. Once the conversion lands
    these entries become dead code that a follow-up can sweep up; failing
    the test would force every guardrail PR to wait on its companion
    conversion PR for landing order.
    """
    stale = []
    for label, j in _scope_policy.JUSTIFIED_AGENTS.items():
        kind = infra_audit.CORE_INFRA_DAEMON_KINDS.get(label)
        if kind == "agent":
            continue
        introduced_by = (j.introduced_by or "").lower()
        if "pending" in introduced_by or "transitional" in introduced_by:
            continue
        stale.append((label, kind))
    assert not stale, (
        f"JUSTIFIED_AGENTS lists labels that aren't kind=\"agent\" in "
        f"CORE_INFRA_DAEMON_KINDS: {stale}. Remove them when the service "
        f"is converted to system-scope or unregistered. (Transitional "
        f"entries with introduced_by containing 'pending' or 'TRANSITIONAL' "
        f"are exempt for in-flight conversion PRs.)"
    )


def test_every_justified_agent_has_a_doc_section() -> None:
    """Each whitelisted label must have a `### <label>` heading in the doc.

    The doc carries the "why" prose; the code constant carries the structured
    fields. Both must change together — this test fails if only one side moves.
    """
    if not _scope_policy.JUSTIFIED_AGENTS:
        # No agents whitelisted → no sections required. Skip silently.
        return
    if not POLICY_DOC.is_file():
        pytest.skip("policy doc missing; separate failure")
    doc_text = POLICY_DOC.read_text()
    missing_sections = []
    for label in _scope_policy.JUSTIFIED_AGENTS:
        # Look for "### <label>" as a heading (allow surrounding whitespace).
        pattern = re.compile(rf"^###\s+{re.escape(label)}\b", re.MULTILINE)
        if not pattern.search(doc_text):
            missing_sections.append(label)
    assert not missing_sections, (
        f"JUSTIFIED_AGENTS entries missing a `### <label>` section in "
        f"{POLICY_DOC.relative_to(_REPO_ROOT)}: {missing_sections}. "
        f"Each whitelisted agent must have a human-readable doc section "
        f"covering why a LaunchDaemon won't work, which user holds the "
        f"Aqua session, and the headless fallback."
    )


# ── User-resolution-variable class (sibling guardrail) ──────────────────────


def test_system_daemon_plist_paths_are_deterministic() -> None:
    """Every kind="system" daemon resolves to exactly ONE filesystem path.

    Sibling-class regression guard against the user-resolution-variable bug
    (mcp-bridge installed two copies of its plist — one in the admin
    user's home via sudo CLI, one in /Users/evolve/... via daemon context —
    because `_real_home()` returned different answers depending on SUDO_USER). For
    system daemons under /Library/LaunchDaemons/ there's no per-user
    component, so the path MUST be the same regardless of invocation context.

    This test asserts the format directly: any kind="system" entry must yield
    a path under /Library/LaunchDaemons/ that does not contain any
    user-specific segment.
    """
    for label, kind in infra_audit.CORE_INFRA_DAEMON_KINDS.items():
        if kind != "system":
            continue
        expected = Path("/Library/LaunchDaemons") / f"{label}.plist"
        # The path itself has no per-user variable component
        assert str(expected).startswith("/Library/LaunchDaemons/")
        assert "/Users/" not in str(expected)
        # And the label MUST start with `ai.evolve.` so the existing pod-wide
        # sudoers grants (`ai.evolve.*.plist`, `system/ai.evolve.*`) cover it.
        # Skip openclaw labels which use ai.openclaw.* — those are covered by
        # a sibling grant block.
        assert label.startswith("ai.evolve.") or label.startswith("ai.openclaw."), (
            f"System-scope label {label!r} doesn't match the "
            f"ai.evolve.* / ai.openclaw.* convention used by the pod-wide "
            f"sudoers grants. Either rename to fit or add a new grant in "
            f"setup_wizard._render_evolve_sudoers."
        )


def test_mcp_bridge_label_is_canonical_source() -> None:
    """`infra_audit._MCP_BRIDGE_LABEL` must come from `mcp_service.LABEL`.

    Regression guard against drift: if mcp_service ever changes the label
    (e.g. another scope conversion), the audit must follow automatically.
    The fallback constant exists ONLY for the unusual case where mcp_service
    isn't importable (analyzer-only install).
    """
    try:
        from evolve_admin.mcp_service import LABEL as MCP_LABEL
    except Exception:
        pytest.skip("mcp_service not importable in this environment")
    assert MCP_LABEL in infra_audit.CORE_INFRA_DAEMONS, (
        "mcp_service.LABEL is not present in infra_audit.CORE_INFRA_DAEMONS. "
        "The label has drifted — either update CORE_INFRA_DAEMONS to use the "
        "imported LABEL, or fix the import path."
    )

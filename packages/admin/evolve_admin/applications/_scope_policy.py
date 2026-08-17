"""launchd scope whitelist for `CORE_INFRA_DAEMON_KINDS`.

Policy doc: docs/policy/launchd-scope.md.

Adding a per-user LaunchAgent to `infra_audit.CORE_INFRA_DAEMON_KINDS`
(kind="agent") requires an entry here AND a matching section in the policy
doc. The invariant test in `test_launchd_scope_policy.py` enforces parity —
if either side drifts, CI fails.

Why this exists: Evolve pods run headless. Admin users only ever log in via
SSH, so they have no Aqua session and no `gui/<uid>` launchd domain.
LaunchAgents bootstrapped against gui/<uid> on such a user fail with error
125 ("Domain does not support specified action") and sit in a
proposal-dismiss-re-emit loop forever. See [evolve#1821] for the canonical
case (mcp-bridge).

Default for new infra services: system-scope LaunchDaemon under
/Library/LaunchDaemons/, running as the `evolve` user. Only override when
there is a specific technical reason a LaunchDaemon won't work.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentJustification:
    """Required fields for an entry in `JUSTIFIED_AGENTS`.

    Each field corresponds to one of the policy doc's mandatory questions.
    Empty strings are NOT allowed — the test asserts non-empty values so the
    operator can't ship a placeholder.
    """
    # 1. Why a LaunchDaemon won't work. Specific technical reason — not
    #    "feels per-user." Examples that qualify: per-user keychain access,
    #    per-user Aqua UI rendering, per-user file format only readable while
    #    that user is logged in.
    why_not_daemon: str

    # 2. Which user account holds the Aqua session this agent will run in.
    #    Concretely name it. If it's an admin account that only logs in via
    #    SSH, the answer is "no one" — convert to LaunchDaemon instead.
    aqua_session_user: str

    # 3. What the service should do on a headless pod where the named user
    #    doesn't have an Aqua session. Acceptable answers:
    #       "skip_silently" — service is genuinely optional
    #       "signal:<sig>"  — service raises a Signal of that type
    #       "convert_to_daemon" — service has a system-scope alternate mode
    headless_fallback: str

    # PR or commit SHA that introduced this justification — for audit trail.
    introduced_by: str


# The whitelist. New entries require:
#   1. An AgentJustification with all four fields non-empty
#   2. A "### <label>" section in docs/policy/launchd-scope.md
#   3. The corresponding kind="agent" entry in CORE_INFRA_DAEMON_KINDS
#   4. Plist install/uninstall code that handles headless_fallback
JUSTIFIED_AGENTS: dict[str, AgentJustification] = {
    # ── Transitional grandfathering (remove with [evolve#1821]) ─────────────
    # mcp-bridge is being converted from LaunchAgent → LaunchDaemon in PR
    # #1821. Until that PR merges, the kind="agent" entry in
    # CORE_INFRA_DAEMON_KINDS would trip `test_every_agent_has_a_justification`
    # because it has no justification entry. Rather than ship a guardrail
    # PR with a deliberately-failing test, we grandfather mcp-bridge here
    # with a clear "scheduled for removal" rationale. PR #1821's diff
    # changes the kind to "system" and removes this entry in the same
    # commit, so the post-#1821 main has neither the kind="agent" entry
    # nor this grandfathered justification. Test stays green throughout.
    "com.evolve.mcp-bridge": AgentJustification(
        why_not_daemon=(
            "TRANSITIONAL: no legitimate technical reason — this is "
            "scheduled for conversion to a system-scope LaunchDaemon. "
            "Grandfathered only so that the guardrail PR can land before "
            "[evolve#1821] without a deliberately-failing test."
        ),
        aqua_session_user="(no Aqua session exists; that's the bug)",
        headless_fallback="convert_to_daemon",
        introduced_by=(
            "[evolve#1821] — pending; this entry must be removed "
            "by that PR's diff before merge."
        ),
    ),
}


def is_justified(label: str) -> bool:
    """Has this label been explicitly justified as an agent (not daemon)?"""
    return label in JUSTIFIED_AGENTS


def justification_for(label: str) -> AgentJustification | None:
    return JUSTIFIED_AGENTS.get(label)

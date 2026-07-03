"""Bot-side session-surface "Apps" block builder — PR 12.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

The bot's session_start mechanism (which already handles POD_CONDUCT)
gains an "Apps" block — a short summary of current findings on this
bot's apps so the bot has context to mention issues conversationally
("can't send the briefing because the cron is missing — want me to
fix it?").

Companion: ``signal_notifier`` (already extended in #2204) carries
proactive notifications. This module builds the always-present
context block.

## Content rules (spec §10.9.6)

  1. Header: ``[Apps] N finding(s) to be aware of.``  Or:
     ``[Apps] N visible to <audience>.`` for team bots.
  2. Group by severity (Critical first, Other second).
  3. Each line: ``<app>: <one-line description>. <repair-hint>.``
  4. Cap at 5 visible findings; overflow line for the rest.
  5. Snoozed findings excluded.
  6. Block absent when no eligible findings.

## Personal vs team bot

For personal bots, the block has no audience qualifier — the user IS
the operator. For team bots, the qualifier names the primary user
(spec §10.9.5 routing).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# Maximum findings shown in the block before overflow line.
MAX_VISIBLE_FINDINGS = 5

# Severity ordering for grouping. Critical first.
_SEVERITY_ORDER = {
    "critical": 0,
    "major":    1,
    "minor":    2,
    "info":     3,
    "":         4,
}


@dataclass
class FindingForBlock:
    """Flattened finding shape the block uses. Producer agnostic —
    Tier 2 audit, Pass A coherence, reconciliation all produce findings
    that the caller normalizes into this shape.
    """
    app_id: str
    severity: str
    summary: str         # one-line description for the block
    repair_hint: str = ""
    signature: str = ""  # for snooze suppression
    snoozed: bool = False

    def severity_sort_key(self) -> int:
        return _SEVERITY_ORDER.get((self.severity or "").lower(), 99)


def _format_finding_line(finding: FindingForBlock) -> str:
    """Build one block line for a finding. Trims to keep block short."""
    line = f"- {finding.app_id}: {finding.summary.strip()}"
    if finding.repair_hint:
        line += f" {finding.repair_hint.strip()}"
    return line


def build_apps_block(
    findings: list[FindingForBlock],
    *,
    audience: str | None = None,
    max_visible: int = MAX_VISIBLE_FINDINGS,
) -> str:
    """Build the "Apps" session-start block.

    Args:
        findings: every finding the bot's apps have. Snoozed entries
            are filtered out here (spec §10.9.6 rule 5).
        audience: when set, the header reads "visible to <audience>"
            instead of the bare count. Use for team bots where the
            primary user is named.
        max_visible: cap on per-section lines before overflow.

    Returns:
        Multiline string ready to inject into session_surface. Empty
        string when no eligible findings — the caller can drop the
        section entirely (spec §10.9.6 rule 6).
    """
    eligible = [f for f in (findings or []) if isinstance(f, FindingForBlock)
                and not f.snoozed]
    if not eligible:
        return ""

    eligible.sort(key=lambda f: (f.severity_sort_key(), f.app_id))

    # Header.
    n = len(eligible)
    if audience:
        header = f"[Apps] {n} finding{'s' if n != 1 else ''} visible to {audience}."
    else:
        header = f"[Apps] {n} finding{'s' if n != 1 else ''} to be aware of."

    # Split into CRITICAL and OTHER groups.
    critical = [f for f in eligible if f.severity_sort_key() == 0]
    other = [f for f in eligible if f.severity_sort_key() > 0]

    lines: list[str] = [header, ""]

    # CRITICAL section.
    if critical:
        lines.append("CRITICAL:")
        shown = critical[:max_visible]
        for f in shown:
            lines.append(_format_finding_line(f))
        overflow = len(critical) - len(shown)
        if overflow > 0:
            lines.append(f"  ({overflow} more critical — `evo app-changes` "
                         f"for full list)")
        lines.append("")

    # OTHER section.
    if other:
        # Account for critical already shown — remaining budget.
        remaining_budget = max_visible - len(critical[:max_visible])
        if remaining_budget < 1:
            remaining_budget = 1
        shown_other = other[:max(1, remaining_budget)]
        if shown_other:
            label = "OTHER" if not critical else f"OTHER ({len(other)} more)"
            if critical:
                # When critical was shown, keep "OTHER (N more)" compact.
                lines.append(label)
            else:
                lines.append("OTHER:")
                shown_other = other[:max_visible]
            for f in shown_other:
                lines.append(_format_finding_line(f))
            overflow = len(other) - len(shown_other)
            if overflow > 0 and not critical:
                lines.append(f"  ({overflow} more — `evo app-changes` "
                             f"for full list)")

    # Trim trailing blank line.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ── Audience routing (spec §10.9.5) ────────────────────────────────────────

# Authorization tiers for team bots.
AUDIENCE_POD_ADMIN     = "pod_admin"
AUDIENCE_PRIMARY_USER  = "primary_user"
AUDIENCE_TEAM_MEMBER   = "team_member"
AUDIENCE_OTHER         = "other"


# Repair authority: who can dispatch repair sessions.
_REPAIR_AUTHORIZED = frozenset({
    AUDIENCE_POD_ADMIN, AUDIENCE_PRIMARY_USER,
})


def can_dispatch_repair(audience: str) -> bool:
    """True iff the audience can dispatch a repair session.

    Spec §10.9.7: team members trigger escalation; only the primary
    user and pod admin can dispatch directly.
    """
    return audience in _REPAIR_AUTHORIZED


def escalation_message_for_team_member(
    *, member_name: str, primary_user_name: str, finding_summary: str,
) -> str:
    """The acknowledgement the bot sends when a team member asks for
    a repair on a team bot. Spec §10.9.7."""
    return (
        f"Thanks for letting me know — I'll flag this for {primary_user_name}."
    )


def escalation_dispatch_message(
    *, member_name: str, finding_summary: str,
) -> str:
    """The message the bot sends to the primary user after a team
    member flagged something. Spec §10.9.7."""
    return (
        f"Family member {member_name} reported a finding worth your "
        f"attention: {finding_summary}. Want me to look into it? "
        f"Reply yes to repair, ignore to skip, or what's wrong for details."
    )

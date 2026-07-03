"""permissions.denylist — regex denylist for approved patterns + cron payloads.

Spec: docs/spec-permission-posture-2026-05-10.md §3.1 (denylist_patterns)
+ §3.3 (perm_approvals_denylist_match + perm_cron_denylist_match signals).

Pure-Python regex matching. The default lists ship with the most
egregious near-term risks; operators can extend via a future
baseline-management proposal flow (Phase B).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Default denylist applied to canonical approved-command patterns.
# Patterns match against the canonicalized form (paths → <path>, urls →
# <url>, values → <arg>) so the regex shape matches the inventory's
# stored shape. The spec's raw regexes (§3.1) are adapted accordingly.
DEFAULT_APPROVAL_DENYLIST: tuple[str, ...] = (
    r"^rm\s+(-rf?|-fr?)\s+(/|<path>)",
    r"^curl\s+\S+\s*\|\s*(bash|sh|zsh)",
    r"^sudo(\s|$)",
    r"^chmod\s+.*777",
    r"^launchctl\s+(load|bootstrap)",
    r"^chown(\s|$)",
    r":\(\)\{\s*:\|:\s*&\s*\};:",  # fork bomb
)

# Default denylist applied to cron payloads (shell or agentTurn message).
# These run against the *raw* payload string (after the "msg: "/"cmd: "
# /"event: " prefix is stripped), so they keep the spec's original
# raw-form patterns.
DEFAULT_CRON_DENYLIST: tuple[str, ...] = (
    r"curl\s+\S+\s*\|\s*(bash|sh|zsh)",
)


@dataclass
class Match:
    pattern: str  # the offending input (raw or canonical)
    rule: str  # the regex that matched
    surface: str  # "approval" | "cron"
    context: str  # "agent:main" | "cron:job-id" etc.


def _compile_all(patterns: tuple[str, ...] | list[str]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for p in patterns:
        try:
            compiled.append((p, re.compile(p)))
        except re.error:
            # Skip malformed rules silently — operator may have a typo;
            # the alert noise from raising would be worse than the miss.
            continue
    return compiled


def scan_approvals(
    inv_dict: dict,
    rules: tuple[str, ...] | list[str] = DEFAULT_APPROVAL_DENYLIST,
) -> list[Match]:
    """Scan `inv.exec_approvals.agents[*].patterns` against the denylist.

    Returns one Match per (pattern × rule) hit.
    """
    compiled = _compile_all(rules)
    matches: list[Match] = []
    ea = inv_dict.get("exec_approvals") or {}
    for agent in ea.get("agents") or []:
        agent_id = agent.get("agent_id", "?")
        for pat in agent.get("patterns") or []:
            for rule_src, rx in compiled:
                if rx.search(pat):
                    matches.append(Match(
                        pattern=pat,
                        rule=rule_src,
                        surface="approval",
                        context=f"agent:{agent_id}",
                    ))
    return matches


def scan_cron(
    inv_dict: dict,
    rules: tuple[str, ...] | list[str] = DEFAULT_CRON_DENYLIST,
) -> list[Match]:
    """Scan cron-job payload summaries against the denylist."""
    compiled = _compile_all(rules)
    matches: list[Match] = []
    si = inv_dict.get("scheduled_invocations") or {}
    for job in si.get("jobs") or []:
        summary = job.get("payload_summary") or ""
        # Strip the "msg: " / "cmd: " / "event: " prefix used for UI display
        # so the regexes match the actual payload string.
        for prefix in ("msg: ", "cmd: ", "event: "):
            if summary.startswith(prefix):
                summary = summary[len(prefix):]
                break
        for rule_src, rx in compiled:
            if rx.search(summary):
                matches.append(Match(
                    pattern=summary,
                    rule=rule_src,
                    surface="cron",
                    context=f"cron:{job.get('name') or job.get('id') or '?'}",
                ))
    return matches

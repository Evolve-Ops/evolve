"""evo app-changes / app-coherence reply renderer — PR 13.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §10.4.

Renders the bot-facing reply for each parsed command. The renderer is
pure: it takes parsed commands + manifest data + reconciliation state
and returns a string. No I/O, no LLM calls — the handler is
responsible for loading the manifests and dispatching backend side-
effects.

Example reply from spec §10.4:

> *"The journal app's manifest updated three things since you last
> looked. New file `scripts/analyze.py` was added (you asked me to
> add summary analysis last week — this is the script for that). Two
> new entries in `data/journal/` are normal data growth. The 6pm
> trigger is **missing from crontab** — looks like a quiet failure.
> Want me to reinstall it from the manifest?"*

We can't recreate that level of synthesis without LLM context — but
we CAN produce a structured summary the bot's session-start surface
can use to compose the conversational reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Max apps shown in the "list changes" overview before a "+N more" line.
_LIST_OVERVIEW_CAP = 8


@dataclass
class AppChangeSummary:
    """One app's recent-change summary used by the renderer."""
    app_id: str
    additions: int = 0
    removals: int = 0
    drifted: int = 0
    quiet_failures: int = 0
    authored_drift: int = 0       # subset of drifted — operator should know
    coherence_warnings: int = 0
    has_changes: bool = True

    @property
    def is_quiet(self) -> bool:
        return (self.additions + self.removals + self.drifted +
                self.quiet_failures + self.coherence_warnings +
                self.authored_drift) == 0


def render_list_changes(
    summaries: list[AppChangeSummary],
) -> str:
    """Render `evo app-changes` (no app id) — list every app's status."""
    if not summaries:
        return ("No applications installed on this bot yet. Run "
                "`evo install <app>` to add one, or `evo app-scan` "
                "to rediscover existing scripts.")

    has_any_changes = any(not s.is_quiet for s in summaries)
    if not has_any_changes:
        return ("All apps are quiet — no changes since the last review. "
                "Use `evo app-changes <app>` to inspect one anyway.")

    lines: list[str] = ["Apps with changes since the last review:"]
    visible = summaries[:_LIST_OVERVIEW_CAP]
    for s in visible:
        if s.is_quiet:
            continue
        flag = _changes_flag_for(s)
        lines.append(f"  • {s.app_id}: {flag}")
    overflow = len(summaries) - len(visible)
    if overflow > 0:
        lines.append(f"  + {overflow} more (lower priority)")
    lines.append("")
    lines.append("`evo app-changes <app>` for one-app detail; "
                  "`<app> approve` accepts all current observational changes; "
                  "`<app> promote` marks the current state as authored.")
    return "\n".join(lines)


def _changes_flag_for(s: AppChangeSummary) -> str:
    """Compose the short status flag per app on the list view."""
    parts: list[str] = []
    if s.quiet_failures:
        parts.append(f"{s.quiet_failures} quiet failure"
                       + ("s" if s.quiet_failures != 1 else ""))
    if s.authored_drift:
        parts.append(f"{s.authored_drift} authored field"
                       + ("s" if s.authored_drift != 1 else "")
                       + " drifted")
    if s.additions:
        parts.append(f"+{s.additions} file"
                       + ("s" if s.additions != 1 else ""))
    if s.removals:
        parts.append(f"-{s.removals} file"
                       + ("s" if s.removals != 1 else ""))
    if s.coherence_warnings:
        parts.append(f"{s.coherence_warnings} coherence warning"
                       + ("s" if s.coherence_warnings != 1 else ""))
    return ", ".join(parts) or "no change"


def render_show_changes(
    summary: AppChangeSummary,
    *,
    additions: list[dict] | None = None,
    removals: list[dict] | None = None,
    drifted: list[dict] | None = None,
    quiet_failures: list[dict] | None = None,
) -> str:
    """Render `evo app-changes <app>` — detail view for one app.

    Lists each change category with a few examples. The bot composes
    the conversational version using this as the data prompt.
    """
    if summary.is_quiet and not any([additions, removals, drifted,
                                      quiet_failures]):
        return (f"{summary.app_id}: no changes since last review. "
                f"Use `evo app-coherence {summary.app_id}` to run a "
                f"capability check anyway.")

    lines: list[str] = [
        f"{summary.app_id}: changes since last review",
        "",
    ]

    if quiet_failures:
        lines.append(f"⚠ {len(quiet_failures)} quiet failure"
                      + ("s" if len(quiet_failures) != 1 else "") + ":")
        for q in quiet_failures[:3]:
            label = q.get("summary") or q.get("message") or str(q)
            lines.append(f"  • {label}")
        if len(quiet_failures) > 3:
            lines.append(f"  • +{len(quiet_failures) - 3} more")
        lines.append("")

    if drifted:
        authored = [d for d in drifted if d.get("authored")]
        if authored:
            lines.append(f"Authored fields that drifted ({len(authored)}):")
            for d in authored[:3]:
                field = d.get("field") or d.get("path") or "?"
                before = d.get("before", "?")
                after = d.get("after", "?")
                lines.append(f"  • {field}: {before!r} → {after!r}")
            if len(authored) > 3:
                lines.append(f"  • +{len(authored) - 3} more")
            lines.append("")

    if additions:
        lines.append(f"New files ({len(additions)}):")
        for a in additions[:3]:
            path = a.get("path", "?")
            layer = a.get("layer", "?")
            lines.append(f"  + {path} ({layer})")
        if len(additions) > 3:
            lines.append(f"  + {len(additions) - 3} more")
        lines.append("")

    if removals:
        lines.append(f"Removed files ({len(removals)}):")
        for r in removals[:3]:
            path = r.get("path", "?")
            lines.append(f"  - {path}")
        if len(removals) > 3:
            lines.append(f"  - {len(removals) - 3} more")
        lines.append("")

    lines.append(
        f"Reply: `approve` to accept changes, `promote` to mark "
        f"current state as authored, `flag <description>` to escalate "
        f"to the operator."
    )
    # Trim trailing blank lines.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def render_approve_result(
    *, app_id: str, approved_count: int,
) -> str:
    if approved_count == 0:
        return (f"{app_id}: nothing to approve — no observational changes "
                f"pending review.")
    return (f"{app_id}: approved {approved_count} change"
            + ("s" if approved_count != 1 else "") + ". "
            f"The manifest now reflects current state.")


def render_promote_result(
    *, app_id: str, promoted_fields: list[str],
) -> str:
    if not promoted_fields:
        return (f"{app_id}: nothing to promote — manifest is empty or "
                f"already fully authored.")
    return (f"{app_id}: promoted {len(promoted_fields)} field"
            + ("s" if len(promoted_fields) != 1 else "")
            + " to authored. Future drift on these fields will surface "
            f"as a change.")


def render_flag_result(
    *, app_id: str, description: str, proposal_id: str = "",
) -> str:
    base = (f"{app_id}: thanks. I've flagged this for the operator: "
            f"\"{description.strip()}\"")
    if proposal_id:
        base += f" (Proposal {proposal_id})."
    else:
        base += "."
    return base


# ── Coherence reply ────────────────────────────────────────────────

def render_coherence_result(
    *,
    app_id: str,
    pass_a_status: str = "",         # "ok" | "warnings" | "incoherent"
    pass_a_findings: list[dict] | None = None,
    capability_severity: str = "",   # "feasible" | "unclear" | "incoherent"
    capability_rationale: str = "",
    rate_limited: bool = False,
) -> str:
    """Render the result of `evo app-coherence <app>` — Pass A + C3."""
    if rate_limited:
        return (f"{app_id}: a coherence check already ran today. "
                f"Try again tomorrow.")

    lines: list[str] = [f"{app_id}: coherence check"]

    # Pass A.
    if pass_a_status:
        label = {"ok": "✓ pass", "warnings": "⚠ warnings",
                  "incoherent": "✗ incoherent"}.get(
            pass_a_status, pass_a_status,
        )
        n = len(pass_a_findings or [])
        suffix = f" ({n} finding{'s' if n != 1 else ''})" if n else ""
        lines.append(f"  Pass A (manifest graph): {label}{suffix}")
        if pass_a_findings:
            for f in pass_a_findings[:3]:
                msg = f.get("message") or f.get("summary") or "?"
                lines.append(f"    • {msg}")
            if len(pass_a_findings) > 3:
                lines.append(f"    • +{len(pass_a_findings) - 3} more")

    # Pass C3.
    if capability_severity:
        label = {
            "feasible":   "✓ feasible",
            "unclear":    "? unclear",
            "incoherent": "✗ incoherent",
        }.get(capability_severity, capability_severity)
        lines.append(f"  Pass C3 (capability): {label}")
        if capability_rationale:
            lines.append(f"    → {capability_rationale.strip()}")

    if len(lines) == 1:
        lines.append("  (no result — neither pass ran)")

    return "\n".join(lines)


# ── Scan reply ────────────────────────────────────────────────────

def render_scan_started(*, bot_id: str) -> str:
    return (f"Scan started for {bot_id}. New / changed apps will show "
            f"up in `evo app-changes` once the scan completes.")


# ── Auth / error replies ──────────────────────────────────────────

def render_not_for_you() -> str:
    """Team-bot users hitting app-changes / app-coherence (spec §10.4
    auth gate). They can flag but not edit."""
    return ("This command is for the primary user of this bot and pod "
            "admins. You can still flag concerns via `evo app-changes "
            "<app> flag <description>` — the operator will see it.")


def render_app_not_found(app_id: str) -> str:
    return (f"No app named {app_id!r} on this bot. Run `evo apps` to "
            f"see the list.")

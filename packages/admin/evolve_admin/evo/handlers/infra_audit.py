"""``evo audit infra`` — pod-wide infrastructure audit on demand.

Grammar (spec §4.3 + §4.6 of internal/spec-audit-extensions-2026-05-17.md):

  evo audit infra                      Queue a pod-wide infra audit.
                                       Reply: "Started auditing pod
                                       infrastructure. I'll let you
                                       know what I find."
  evo audit infra <element>            Queue an audit limited to one
                                       element (daemons, sudoers, acls,
                                       network_json, repo_puller,
                                       signal_retention).
  evo audit infra status               Compact summary of the latest
                                       infra audit run.
  evo audit infra history              Last few entries from the
                                       infra-audit trail (all elements).

Distinct from `evo audit` (single-app audit on this bot) and `evo
app-audit` (per-app audit, also bot-side). Infrastructure isn't bot-
owned — it lives admin-side — so this handler talks to the admin's
infra_audit module directly rather than queueing a bot-inbox file.

Auth model: primary user + pod admins. Other roles get a "not for
you" reply. (The admin server's auth layer already gates the
dispatcher; this handler trusts the role argument.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import speak


_VALID_ELEMENTS = (
    "daemons", "sudoers", "acls",
    "network_json", "repo_puller", "signal_retention",
)


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    """Top-level dispatcher for `evo audit infra ...`.

    args is the rest of the command after `audit infra` — empty string
    when the user typed bare `evo audit infra`.
    """
    tokens = [t for t in (args or "").strip().split() if t]
    if tokens and tokens[0].lower() == "status":
        body = _render_status()
        return speak("audit-infra", body, role)

    if tokens and tokens[0].lower() == "history":
        body = _render_history()
        return speak("audit-infra", body, role)

    elements: list[str] | None = None
    if tokens:
        first = tokens[0].lower()
        if first in _VALID_ELEMENTS:
            elements = [first]
        else:
            return speak(
                "audit-infra",
                (
                    f"Unknown element `{first}`. Valid: "
                    + ", ".join(f"`{e}`" for e in _VALID_ELEMENTS)
                    + ". Bare `evo audit infra` audits everything."
                ),
                role,
            )

    body = _kick(elements=elements, bot_id=bot_id)
    return speak("audit-infra", body, role)


def _kick(*, elements: list[str] | None, bot_id: str) -> str:
    """Queue an infra audit run and return the immediate confirmation."""
    try:
        from ...applications.infra_audit import request_infra_audit
        res = request_infra_audit(
            requested_by=f"evo:{bot_id}",
            elements=elements,
        )
    except Exception as exc:
        return f"Infra audit request failed: {exc}"

    if not res.get("ok"):
        return f"Infra audit request failed: {res.get('error', 'unknown error')}"

    target = (
        "pod infrastructure"
        if not elements
        else f"infra element `{elements[0]}`"
    )
    return (
        f"Started auditing {target}. "
        f"I'll let you know what I find."
    )


# ── Status / history ────────────────────────────────────────────────────────


def _render_status() -> str:
    """Compact 'latest run' summary."""
    try:
        from ...applications.infra_audit import latest_run_summary
        summary = latest_run_summary()
    except Exception as exc:
        return f"Couldn't read infra audit status: {exc}"

    if not summary:
        return (
            "**Infra audit status**\n\n"
            "No infra audit has run yet. Type `evo audit infra` to run one now."
        )

    completed = summary.get("completed_at") or "?"
    findings = summary.get("findings_count", 0)
    outcomes = summary.get("outcomes") or {}
    proposed = outcomes.get("propose", 0)
    elements = summary.get("elements_checked") or []

    lines = [
        "**Infra audit status**",
        "",
        f"Last run: `{completed}`",
        f"Elements checked: {', '.join(elements) or '?'}",
        f"Findings: {findings} ({proposed} surfaced as Proposals)",
        "",
    ]
    if findings:
        lines.append("Type `evo proposals` to see the findings.")
    else:
        lines.append("No issues — pod infrastructure looks healthy.")
    return "\n".join(lines)


def _render_history() -> str:
    """Last few trail entries across all elements."""
    try:
        from ...applications.infra_audit import (
            infra_audits_root, _ALL_ELEMENTS,
        )
        import json
    except Exception as exc:
        return f"Couldn't read infra audit history: {exc}"

    try:
        from ...config import DEFAULT_SHARED_DIR
        shared_dir = DEFAULT_SHARED_DIR
    except Exception:
        shared_dir = Path("/Users/Shared/evolve")

    root = infra_audits_root(shared_dir)
    if not root.exists():
        return (
            "**Infra audit history**\n\n"
            "No infra audits recorded yet. Type `evo audit infra` to run one."
        )

    all_entries: list[tuple[str, str, dict]] = []
    for elem_dir in root.iterdir():
        if not elem_dir.is_dir():
            continue
        trail = elem_dir / "trail.jsonl"
        if not trail.exists():
            continue
        try:
            lines = trail.read_text().splitlines()
        except OSError:
            continue
        for line in lines[-5:]:
            try:
                rec = json.loads(line)
                all_entries.append((rec.get("ts", "?"), elem_dir.name, rec))
            except json.JSONDecodeError:
                continue

    if not all_entries:
        return (
            "**Infra audit history**\n\n"
            "Trails are empty — no infra audits recorded yet."
        )

    all_entries.sort(key=lambda x: x[0])
    recent = all_entries[-10:]

    lines = [f"**Infra audit history** (last {len(recent)})", ""]
    for ts, elem, rec in recent:
        kind = rec.get("kind", "?")
        severity = rec.get("severity", "")
        if kind == "audit_run":
            status = rec.get("status", "?")
            count = rec.get("findings_count", 0)
            lines.append(
                f"• `{ts}` `{elem}` audit_run {status} — {count} findings"
            )
        elif kind.startswith("infra_"):
            outcome = kind.split("_", 1)[1]
            cat = rec.get("category", "?")
            lines.append(
                f"  ↳ `{ts}` `{elem}` {outcome} ({severity}): `{cat}`"
            )
        else:
            lines.append(f"• `{ts}` `{elem}` {kind}")
    return "\n".join(lines)

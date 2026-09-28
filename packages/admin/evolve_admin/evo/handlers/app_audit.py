"""``evo audit ...`` — user-facing app-audit commands in messaging.

App-audit grammar (spec §7.4):
  evo audit                           list audit status for every app on this bot
  evo audit <app>                     queue a Tier-3 audit for one app
  evo audit <app> full                ... with --ignore-accepted
  evo audit all                       queue audits for every eligible app
  evo audit all full                  ... with --ignore-accepted
  evo audit accept <app> <sig>        mark a finding as accepted (from a Proposal)
  evo audit history <app>             show recent trail entries for one app

Substrate audit grammar (Workstream B-skills, spec-audit-extensions §4.6):
  evo audit skill                     list status for every skill on this bot
  evo audit skill <name>              queue a skill audit
  evo audit skill <name> full         ... with --ignore-accepted
  evo audit skill all                 queue audits for every skill
  evo audit skill accept <name> <sig> mark a skill finding accepted
  evo audit skill history <name>      trail tail

  (same grammar with "provider" in place of "skill")

All forms write through ``audit_dispatch.request_audit`` /
``audit_dispatch.request_substrate_audit`` (and matching mark-accepted
helpers). The handler auth-gates the call: only the primary user of this
bot and pod admins can run any form. Other users on team bots get a
"not for you" reply.

Notifications fire on audit completion via the existing forge_complete-
style notification queue. The handler returns the immediate "started"
reply; the user gets the result message later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import speak


_MAX_HISTORY_ENTRIES = 5
_MAX_STATUS_APPS = 12


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    """Top-level dispatcher for ``evo audit ...``.

    args is the rest of the command after "audit " — empty string when the
    user typed bare `evo audit`. We split on whitespace and route by the
    first token.
    """
    tokens = [t for t in (args or "").strip().split() if t]
    if not tokens:
        body = _render_status(bot_id, network)
        return speak("audit", body, role)

    first = tokens[0].lower()

    if first == "accept":
        body = _handle_accept(bot_id, tokens[1:], network)
        return speak("audit", body, role)

    if first == "history":
        if len(tokens) < 2:
            body = "Usage: `evo audit history <app>` — names the app whose audit trail to show."
            return speak("audit", body, role)
        body = _render_history(bot_id, tokens[1], network)
        return speak("audit", body, role)

    # Workstream B-skills (spec-audit-extensions §4.6 deliverable 9):
    # extend the audit grammar to cover the substrate layer too.
    if first in ("skill", "provider"):
        body = _handle_substrate(first, bot_id, tokens[1:], network)
        return speak("audit", body, role)

    if first == "all":
        full = (len(tokens) > 1 and tokens[1].lower() == "full")
        body = _kick_audit(bot_id, network, apps=None, full_audit=full)
        return speak("audit", body, role)

    # Single-app form: <app> [full]
    app_id = first
    full = (len(tokens) > 1 and tokens[1].lower() == "full")
    body = _kick_audit(bot_id, network, apps=[app_id], full_audit=full)
    return speak("audit", body, role)


# ── Substrate (skill / provider) audit entry ────────────────────────────────


def _handle_substrate(
    element_type: str,         # "skill" or "provider"
    bot_id: str,
    tokens: list[str],
    network: dict[str, Any],
) -> str:
    """Dispatch ``evo audit skill ...`` / ``evo audit provider ...`` forms.

    Grammar mirrors the app-audit one:
      evo audit skill                      list status for all skills
      evo audit skill <name>               queue one skill audit
      evo audit skill <name> full          ... with --ignore-accepted
      evo audit skill all                  queue audits for every skill
      evo audit skill accept <name> <sig>  mark a finding accepted
      evo audit skill history <name>       trail tail

      (same for provider)
    """
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id

    if not tokens:
        return _render_substrate_status(element_type, bot_user, bot_id)

    first = tokens[0].lower()

    if first == "accept":
        return _handle_substrate_accept(element_type, bot_id, bot_user, tokens[1:])

    if first == "history":
        if len(tokens) < 2:
            return (
                f"Usage: `evo audit {element_type} history <name>` — "
                f"names the {element_type} whose audit trail to show."
            )
        return _render_substrate_history(element_type, bot_user, bot_id, tokens[1])

    if first == "all":
        full = (len(tokens) > 1 and tokens[1].lower() == "full")
        return _kick_substrate_audit(
            element_type, bot_id, bot_user, elements=None, full=full,
        )

    # Single-element form: <name> [full]
    element_id = first
    full = (len(tokens) > 1 and tokens[1].lower() == "full")
    return _kick_substrate_audit(
        element_type, bot_id, bot_user, elements=[element_id], full=full,
    )


def _kick_substrate_audit(
    element_type: str, bot_id: str, bot_user: str,
    *, elements: list[str] | None, full: bool,
) -> str:
    try:
        from ...applications.audit_dispatch import request_substrate_audit
        result = request_substrate_audit(
            bot_id=bot_id, bot_user=bot_user,
            element_type=element_type,
            elements=elements,
            full_audit=full,
            requested_by=f"evo:{bot_id}",
            kick=True,
        )
    except Exception as exc:
        return f"Audit request failed: {exc}"

    if not result.ok:
        return f"Audit request failed: {result.error}"

    target_phrase = (
        f"all {element_type}s" if elements is None
        else f"{element_type} `{elements[0]}`"
    )
    full_note = " (full audit — ignoring accepted findings)" if full else ""
    return (
        f"Started auditing {target_phrase} on `{bot_id}`{full_note}.\n"
        f"I'll let you know when it's done."
    )


def _handle_substrate_accept(
    element_type: str, bot_id: str, bot_user: str, tokens: list[str],
) -> str:
    if len(tokens) < 2:
        return (
            f"Usage: `evo audit {element_type} accept <name> <signature>`\n"
            f"Find the signature in the proposal's body — it's a hex "
            f"prefix after the audit metadata block."
        )
    element_id, signature = tokens[0], tokens[1]
    try:
        from ...applications.audit_dispatch import mark_substrate_finding_accepted
        ok, err = mark_substrate_finding_accepted(
            bot_id=bot_id, bot_user=bot_user,
            element_type=element_type, element_id=element_id,
            signature=signature, accepted_by=f"evo:{bot_id}",
        )
    except Exception as exc:
        return f"Accept failed: {exc}"
    if not ok:
        return f"Accept failed: {err}"
    return (
        f"Marked the finding as accepted on `{bot_id}/{element_type}/{element_id}`. "
        f"Future audits will skip it. Type `evo audit {element_type} "
        f"{element_id} full` to re-evaluate accepted findings."
    )


def _render_substrate_status(
    element_type: str, bot_user: str, bot_id: str,
) -> str:
    """Compact status listing for skills or providers on this bot."""
    parent = "skill_audits" if element_type == "skill" else "provider_audits"
    root = Path(f"/Users/{bot_user}/.openclaw/workspace/evolve/{parent}")
    title = f"**{element_type.title()} audit status — {bot_id}**"
    if not root.is_dir():
        return f"{title}\n\nNo {element_type}-audit trails yet — never audited."

    lines = [title, ""]
    shown = 0
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return f"{title}\n\nTrail directory not readable."
    for d in entries:
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        trail = d / "trail.jsonl"
        if not trail.exists():
            continue
        # Read the last `audit_run` entry to compute the badge.
        badge = "– never audited"
        try:
            last_run = None
            for ln in trail.read_text().splitlines():
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if e.get("kind") == "audit_run":
                    last_run = e
            if last_run is None:
                badge = "– never audited"
            elif last_run.get("status") == "failed":
                badge = "❌ last audit failed"
            elif (last_run.get("findings_count") or 0) > 0:
                outcomes = last_run.get("outcomes") or {}
                raised = outcomes.get("propose") or 0
                badge = f"⚠ {raised} raised, {last_run.get('findings_count')} total"
            else:
                badge = "✓ clean"
        except OSError:
            badge = "? trail unreadable"
        lines.append(f"• `{d.name}` — {badge}")
        shown += 1
    if shown == 0:
        lines.append(f"_(no {element_type} trails yet)_")
    lines.append("")
    lines.append(
        f"Type `evo audit {element_type} <name>` to queue an audit, "
        f"`evo audit {element_type} all` for everything."
    )
    return "\n".join(lines)


def _render_substrate_history(
    element_type: str, bot_user: str, bot_id: str, element_id: str,
) -> str:
    """Trail tail for one skill or provider — last N entries."""
    parent = "skill_audits" if element_type == "skill" else "provider_audits"
    trail_path = Path(
        f"/Users/{bot_user}/.openclaw/workspace/evolve/{parent}/"
        f"{element_id}/trail.jsonl"
    )
    title = f"**{element_type.title()} audit history — {bot_id}/{element_id}**"
    text: str | None = None
    try:
        text = trail_path.read_text()
    except PermissionError:
        import subprocess
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(trail_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                text = r.stdout
        except subprocess.SubprocessError:
            pass
    except (OSError, FileNotFoundError):
        return f"{title}\n\nNo audit trail yet — never audited."

    if text is None:
        return f"{title}\n\nTrail file not readable."

    entries: list[dict] = []
    for line in text.splitlines()[-_MAX_HISTORY_ENTRIES:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not entries:
        return f"{title}\n\nTrail empty — no audits recorded yet."

    lines = [title, ""]
    for e in entries:
        ts = e.get("ts", "?")
        kind = e.get("kind", "?")
        if kind == "audit_run":
            status = e.get("status", "?")
            findings = e.get("findings_count", 0)
            lines.append(f"• `{ts}` {element_type}-audit {status} — {findings} findings")
        elif kind.startswith(f"{element_type}_"):
            outcome = kind.split("_", 1)[1]
            sev = e.get("severity", "?")
            summary = (e.get("summary") or e.get("rationale") or "")[:80]
            lines.append(f"  ↳ `{ts}` {outcome} ({sev}): {summary}")
        else:
            lines.append(f"• `{ts}` {kind}")
    return "\n".join(lines)


# ── Status listing ──────────────────────────────────────────────────────────


def _render_status(bot_id: str, network: dict[str, Any]) -> str:
    """Compact per-app audit summary for this bot.

    Lines: ✓ healthy / ⚠ N findings / ❌ audit failed / – never audited
    """
    manifests = _load_manifests(bot_id, network)
    if manifests is None:
        return f"**Audit — {bot_id}**\n\nCouldn't read this bot's manifests."
    if not manifests:
        return f"**Audit — {bot_id}**\n\nNo apps installed yet."

    lines = [f"**Audit status — {bot_id}**", ""]
    manifests.sort(key=lambda m: str(m.get("display_name") or m.get("id") or ""))
    shown = manifests[:_MAX_STATUS_APPS]
    for m in shown:
        app_id = m.get("id") or "?"
        name = m.get("display_name") or m.get("name") or app_id
        last = m.get("last_audit") or {}
        eligible = m.get("audit_eligible", True)
        if not last:
            badge = "– never audited" if eligible else "⊘ ineligible (manual only)"
        elif last.get("status") == "failed":
            badge = f"❌ last audit failed ({last.get('error', '')[:40]})"
        elif (last.get("findings_count") or 0) > 0:
            outcomes = last.get("outcomes") or {}
            raised = (outcomes.get("propose") or 0) + (outcomes.get("conflict_notice") or 0)
            badge = f"⚠ {raised} raised, {last.get('findings_count')} total"
        else:
            badge = "✓ clean"
        lines.append(f"• `{app_id}` ({name}) — {badge}")
    if len(manifests) > _MAX_STATUS_APPS:
        lines.append(f"…and {len(manifests) - _MAX_STATUS_APPS} more.")
    lines.append("")
    lines.append(
        "Type `evo audit <app>` to queue an audit, "
        "`evo audit all` for everything eligible."
    )
    return "\n".join(lines)


# ── Trigger forms ───────────────────────────────────────────────────────────


def _kick_audit(
    bot_id: str,
    network: dict[str, Any],
    *,
    apps: list[str] | None,
    full_audit: bool,
) -> str:
    """Common path for evo audit <app> / evo audit all (with optional full)."""
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id

    # Validate apps exist when targeted
    if apps:
        all_ids = {
            m.get("id") for m in (_load_manifests(bot_id, network) or [])
        }
        missing = [a for a in apps if a not in all_ids]
        if missing:
            return (
                f"App not found: `{missing[0]}`.\n"
                f"Type `evo apps` to see installed apps."
            )

    try:
        from ...applications.audit_dispatch import request_audit
        result = request_audit(
            bot_id=bot_id, bot_user=bot_user,
            apps=apps,
            full_audit=full_audit,
            requested_by=f"evo:{bot_id}",
            kick=True,
        )
    except Exception as exc:
        return f"Audit request failed: {exc}"

    if not result.ok:
        return f"Audit request failed: {result.error}"

    target_phrase = (
        f"all eligible apps" if apps is None else f"`{apps[0]}`"
    )
    full_note = " (full audit — ignoring accepted findings)" if full_audit else ""
    return (
        f"Started auditing {target_phrase} on `{bot_id}`{full_note}.\n"
        f"I'll let you know when it's done."
    )


# ── Accept form ────────────────────────────────────────────────────────────


def _handle_accept(
    bot_id: str, tokens: list[str], network: dict[str, Any],
) -> str:
    """`evo audit accept <signature>` — mark a finding as accepted.

    The signature comes from the Proposal that surfaced the finding. The
    proposal-detail UI exposes it directly; the chat surface gets it via
    the Proposal's text body. We don't ask the user to type the whole
    SHA — they can paste it. We accept any reasonable prefix length to
    forgive truncation.

    To resolve which app the signature belongs to, we walk every manifest
    on the bot and look for a matching prefix in audit_accepted history
    OR in the Proposal store. For v1 we keep it simple: require the
    operator to pass app and signature.
    """
    if len(tokens) < 2:
        return (
            "Usage: `evo audit accept <app> <signature>`\n"
            "Find the signature in the proposal's body — it's a hex prefix "
            "after the audit metadata block."
        )
    app_id, signature = tokens[0], tokens[1]
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id

    try:
        from ...applications.audit_dispatch import mark_finding_accepted
        ok, err = mark_finding_accepted(
            bot_id=bot_id, bot_user=bot_user, app_id=app_id,
            signature=signature,
            accepted_by=f"evo:{bot_id}",
        )
    except Exception as exc:
        return f"Accept failed: {exc}"

    if not ok:
        return f"Accept failed: {err}"
    return (
        f"Marked the finding as accepted on `{bot_id}/{app_id}`. "
        f"Future audits will skip it. Type `evo audit {app_id} full` "
        f"to re-evaluate accepted findings."
    )


# ── History form ────────────────────────────────────────────────────────────


def _render_history(bot_id: str, app_id: str, network: dict[str, Any]) -> str:
    """Tail the per-app trail.jsonl for the last few entries."""
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    trail_path = Path(
        f"/Users/{bot_user}/.openclaw/workspace/evolve/audits/{app_id}/trail.jsonl"
    )

    text: str | None = None
    try:
        text = trail_path.read_text()
    except PermissionError:
        import subprocess
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(trail_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                text = r.stdout
        except subprocess.SubprocessError:
            pass
    except (OSError, FileNotFoundError):
        return (
            f"**Audit history — {bot_id}/{app_id}**\n\n"
            f"No audit trail yet — this app hasn't been audited."
        )

    if text is None:
        return (
            f"**Audit history — {bot_id}/{app_id}**\n\n"
            f"Trail file not readable."
        )

    entries = []
    for line in text.splitlines()[-_MAX_HISTORY_ENTRIES:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        return (
            f"**Audit history — {bot_id}/{app_id}**\n\n"
            f"Trail file is empty — no audits recorded yet."
        )

    lines = [f"**Audit history — {bot_id}/{app_id}** (last {len(entries)})", ""]
    for e in entries:
        ts = e.get("ts", "?")
        kind = e.get("kind", "?")
        if kind == "audit_run":
            tier = e.get("tier", "?")
            status = e.get("status", "?")
            findings = e.get("findings_count", 0)
            lines.append(f"• `{ts}` tier{tier} {status} — {findings} findings")
        elif kind.startswith("tier") and "_" in kind:
            outcome = kind.split("_", 1)[1]
            sev = e.get("severity", "?")
            summary = (e.get("summary") or e.get("rationale") or "")[:80]
            lines.append(f"  ↳ `{ts}` {outcome} ({sev}): {summary}")
        else:
            lines.append(f"• `{ts}` {kind}")
    return "\n".join(lines)


# ── Manifest loader (mirrors apps.py) ──────────────────────────────────────


def _load_manifests(bot_id: str, network: dict[str, Any]) -> list[dict] | None:
    """Return manifest dicts from the bot's workspace, or None if unreadable."""
    import pwd
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        home = Path(pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"

    if not manifests_dir.is_dir():
        return None
    out: list[dict] = []
    try:
        entries = sorted(manifests_dir.iterdir())
    except (OSError, PermissionError):
        # Try sudo ls fallback
        import subprocess
        try:
            r = subprocess.run(
                ["sudo", "/bin/ls", str(manifests_dir)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
            names = [n for n in r.stdout.splitlines() if n.endswith(".json")]
            for name in names:
                p = manifests_dir / name
                try:
                    raw = subprocess.run(
                        ["sudo", "/bin/cat", str(p)],
                        capture_output=True, text=True, timeout=5,
                    )
                    if raw.returncode == 0:
                        out.append(json.loads(raw.stdout))
                except (subprocess.SubprocessError, json.JSONDecodeError):
                    continue
            return out
        except subprocess.SubprocessError:
            return None

    for f in entries:
        if f.suffix != ".json" or f.name.startswith("_") or f.name.startswith("."):
            continue
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out

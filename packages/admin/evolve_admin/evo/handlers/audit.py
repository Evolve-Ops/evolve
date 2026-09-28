"""``evo audit`` — latest security_warden findings.

Reads the same signal store as ``evo alerts`` but filtered to the
``security_warden`` producer (the audit) so operators can spot-check
identity / config / machine / proposal findings without paging through
unrelated alerts.

The audit itself runs on a 15-minute cron — there's no programmatic
"run now" entry point, and there doesn't need to be since the latest
findings are at most ~15 minutes old.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, load_firing_signal_dicts, speak


_AUDIT_PRODUCER = "security_warden"
_SEVERITY_ORDER = {"alert": 0, "warn": 1, "info": 2}
_SEVERITY_ICON = {"alert": "🔴", "warn": "🟡", "info": "🔵"}


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    # `evo audit infra ...` delegates to the infra-audit handler
    # (Workstream B-infra of the audit-extensions sprint). Keeps the
    # security-warden viewer reachable as bare `evo audit`.
    tokens = (args or "").strip().split()
    if tokens and tokens[0].lower() == "infra":
        from . import infra_audit as _infra_handler
        rest = " ".join(tokens[1:])
        return _infra_handler.render(
            role=role, bot_id=bot_id, args=rest, network=network,
        )

    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    signals = [
        s for s in load_firing_signal_dicts(shared_dir)
        if str(s.get("producer") or "") == _AUDIT_PRODUCER
    ]

    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(signals)
    else:
        body = _render_bot(signals, bot_id)
    return speak("audit", body, role)


def _relevant_to_bot(sig: dict, bot_id: str) -> bool:
    scope = sig.get("scope")
    if scope == "bot" and sig.get("bot_id") == bot_id:
        return True
    if scope == "pod":
        return True
    return False


def _render_bot(signals: list[dict], bot_id: str) -> str:
    relevant = [s for s in signals if _relevant_to_bot(s, bot_id)]
    if not relevant:
        return (
            f"**Audit — {bot_id}**\n\n"
            "No outstanding security findings. 🟢"
        )
    relevant.sort(key=_severity_key)
    lines = [f"**Audit — {bot_id}** ({len(relevant)} finding"
             f"{'s' if len(relevant) != 1 else ''})", ""]
    for sig in relevant[:10]:
        lines.append(_format_line(sig))
    if len(relevant) > 10:
        lines.append(f"…and {len(relevant) - 10} more.")
    return "\n".join(lines)


def _render_pod(signals: list[dict]) -> str:
    if not signals:
        return "**Audit — pod**\n\nNo outstanding security findings. 🟢"
    signals.sort(key=_severity_key)
    counts = {"alert": 0, "warn": 0, "info": 0}
    for s in signals:
        sev = str(s.get("severity") or "info")
        if sev in counts:
            counts[sev] += 1
    summary = []
    if counts["alert"]:
        summary.append(f"{counts['alert']} alert")
    if counts["warn"]:
        summary.append(f"{counts['warn']} warn")
    if counts["info"]:
        summary.append(f"{counts['info']} info")
    lines = [f"**Audit — pod** ({len(signals)} finding"
             f"{'s' if len(signals) != 1 else ''})", ""]
    if summary:
        lines.append(", ".join(summary))
        lines.append("")
    for sig in signals[:8]:
        lines.append(_format_line(sig))
    if len(signals) > 8:
        lines.append(f"…and {len(signals) - 8} more.")
    return "\n".join(lines)


def _severity_key(sig: dict) -> tuple[int, str]:
    sev = str(sig.get("severity") or "info")
    return _SEVERITY_ORDER.get(sev, 99), str(sig.get("created_at") or "")


def _format_line(sig: dict) -> str:
    sev = str(sig.get("severity") or "info")
    icon = _SEVERITY_ICON.get(sev, "•")
    title = str(sig.get("title") or sig.get("type") or sig.get("id") or "(untitled)")
    sid = str(sig.get("id") or "")
    bot = sig.get("bot_id")
    suffix = f" [{bot}]" if bot else ""
    # Trail with truncated id so users can `evo mute <id>` from this view.
    short_id = sid[:12] if sid else ""
    if short_id:
        return f"{icon} {title}{suffix} ({short_id})"
    return f"{icon} {title}{suffix}"

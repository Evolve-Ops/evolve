"""``evo health`` — operational health snapshot.

Narrower than ``evo alerts``: focuses on substrate health — gateway
state, daemon liveness, daily backup status, host load. Reads the same
signal store but filters to ``pod_health`` / ``host_health`` producers
(maintenance-flavor signals).

Scope: per-bot reports signals scoped to this bot plus pod-wide
maintenance; sysadmin bot (``evolve``) gets the full pod view.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, load_firing_signal_dicts, speak


_HEALTH_PRODUCERS = {"pod_health", "host_health"}
_SEVERITY_ORDER = {"alert": 0, "warn": 1, "info": 2}
_SEVERITY_ICON = {"alert": "🔴", "warn": "🟡", "info": "🔵"}


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    signals = [s for s in load_firing_signal_dicts(shared_dir)
               if str(s.get("producer") or "") in _HEALTH_PRODUCERS]

    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(signals)
    else:
        body = _render_bot(signals, bot_id)
    return speak("health", body, role)


def _relevant_to_bot(sig: dict, bot_id: str) -> bool:
    scope = sig.get("scope")
    if scope == "bot" and sig.get("bot_id") == bot_id:
        return True
    if scope in ("pod", "host"):
        return True
    return False


def _render_bot(signals: list[dict], bot_id: str) -> str:
    relevant = [s for s in signals if _relevant_to_bot(s, bot_id)]
    if not relevant:
        return (
            f"**Health — {bot_id}**\n\n"
            "Everything looks healthy. 🟢"
        )
    relevant.sort(key=lambda s: (
        _SEVERITY_ORDER.get(str(s.get("severity") or "info"), 99),
        str(s.get("created_at") or ""),
    ))
    lines = [f"**Health — {bot_id}**", ""]
    for sig in relevant[:10]:
        lines.append(_format_line(sig))
    if len(relevant) > 10:
        lines.append(f"…and {len(relevant) - 10} more.")
    return "\n".join(lines)


def _render_pod(signals: list[dict]) -> str:
    if not signals:
        return "**Health — pod**\n\nEverything looks healthy across the pod. 🟢"

    signals.sort(key=lambda s: (
        _SEVERITY_ORDER.get(str(s.get("severity") or "info"), 99),
        str(s.get("created_at") or ""),
    ))
    counts = {"alert": 0, "warn": 0, "info": 0}
    by_producer: dict[str, int] = {}
    for sig in signals:
        sev = str(sig.get("severity") or "info")
        if sev in counts:
            counts[sev] += 1
        prod = str(sig.get("producer") or "unknown")
        by_producer[prod] = by_producer.get(prod, 0) + 1

    lines = ["**Health — pod**", ""]
    summary_parts = []
    if counts["alert"]:
        summary_parts.append(f"{counts['alert']} alert")
    if counts["warn"]:
        summary_parts.append(f"{counts['warn']} warn")
    if counts["info"]:
        summary_parts.append(f"{counts['info']} info")
    lines.append(", ".join(summary_parts) + f" across {len(by_producer)} producer"
                 f"{'s' if len(by_producer) != 1 else ''}.")
    lines.append("")
    lines.append("Top items:")
    for sig in signals[:5]:
        lines.append(_format_line(sig))
    return "\n".join(lines)


def _format_line(sig: dict) -> str:
    sev = str(sig.get("severity") or "info")
    icon = _SEVERITY_ICON.get(sev, "•")
    title = str(sig.get("title") or sig.get("type") or sig.get("id") or "(untitled)")
    bot = sig.get("bot_id")
    suffix = f" [{bot}]" if bot else ""
    return f"{icon} {title}{suffix}"

"""``evo alerts`` — currently firing signals.

Scope: per-bot reports firing signals where ``signal.bot_id == this bot``
or ``signal.scope == "pod"`` (pod-wide alerts are relevant to every
bot). Invoked from the sysadmin ``evolve`` bot, reports every firing
signal grouped by producer.

Data source: firing Signals via ``signals.store.iter_active`` (the
sanctioned read path — spec-state-store-and-deploy-resilience §1.1
Phase B), surfaced through ``_shared.load_firing_signal_dicts`` which
falls back to a raw read only when the analyzer signals module isn't
importable (keeps the handler robust if analyzer is partially deployed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, load_firing_signal_dicts, speak


_SEVERITY_ORDER = {"alert": 0, "warn": 1, "info": 2}
_SEVERITY_ICON = {"alert": "🔴", "warn": "🟡", "info": "🔵"}


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
    signals = load_firing_signal_dicts(shared_dir)

    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(signals)
    else:
        body = _render_bot(signals, bot_id)
    return speak("alerts", body, role)


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
            f"**Alerts — {bot_id}**\n\n"
            "Nothing firing right now. 🟢"
        )

    relevant.sort(key=_severity_key)
    lines = [f"**Alerts — {bot_id}**", ""]
    for sig in relevant[:10]:
        lines.append(_format_signal_line(sig))
    if len(relevant) > 10:
        lines.append(f"…and {len(relevant) - 10} more.")
    return "\n".join(lines)


def _render_pod(signals: list[dict]) -> str:
    if not signals:
        return "**Alerts — pod**\n\nNothing firing across the pod. 🟢"

    signals.sort(key=_severity_key)
    by_producer: dict[str, int] = {}
    for sig in signals:
        prod = str(sig.get("producer") or "unknown")
        by_producer[prod] = by_producer.get(prod, 0) + 1

    lines = ["**Alerts — pod**", ""]
    total = len(signals)
    lines.append(f"{total} firing across {len(by_producer)} producer"
                 f"{'s' if len(by_producer) != 1 else ''}.")
    lines.append("")
    lines.append("By producer:")
    for prod, count in sorted(by_producer.items(), key=lambda x: -x[1]):
        lines.append(f"  {prod}: {count}")
    lines.append("")
    lines.append("Top by severity:")
    for sig in signals[:5]:
        lines.append(_format_signal_line(sig))
    return "\n".join(lines)


def _severity_key(sig: dict) -> tuple[int, str]:
    sev = str(sig.get("severity") or "info")
    return _SEVERITY_ORDER.get(sev, 99), str(sig.get("created_at") or "")


def _format_signal_line(sig: dict) -> str:
    sev = str(sig.get("severity") or "info")
    icon = _SEVERITY_ICON.get(sev, "•")
    title = str(sig.get("title") or sig.get("type") or sig.get("id") or "(untitled)")
    producer = str(sig.get("producer") or "")
    if producer:
        return f"{icon} {title} ({producer})"
    return f"{icon} {title}"

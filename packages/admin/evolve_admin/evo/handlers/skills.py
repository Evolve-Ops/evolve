"""``evo skills`` — skills (plugins + MCP servers) installed on this bot.

Delegates to the existing ``evolve_admin.skills.inventory`` module so the
in-chat surface stays aligned with what the Skills admin page shows.

Scope: per-bot lists its own skills; sysadmin ``evolve`` bot shows a
per-bot summary across the pod.
"""

from __future__ import annotations

from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, pod_member_bots, speak


_MAX_PER_BOT_LIST = 12


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(network)
    else:
        body = _render_bot(bot_id, network)
    return speak("skills", body, role)


def _render_bot(bot_id: str, network: dict[str, Any]) -> str:
    inv = _safe_inventory(bot_id, network)
    if inv is None:
        return (
            f"**Skills — {bot_id}**\n\n"
            "Couldn't read this bot's skills inventory. Ask your pod admin "
            "to check the OpenClaw config."
        )
    if inv.read_error:
        return (
            f"**Skills — {bot_id}**\n\n"
            f"Read error: {inv.read_error}"
        )
    if not inv.skills:
        return (
            f"**Skills — {bot_id}**\n\n"
            "No skills installed."
        )

    by_category: dict[str, list] = {}
    for s in inv.skills:
        by_category.setdefault(s.category, []).append(s)

    lines = [f"**Skills — {bot_id}** ({len(inv.skills)} installed)", ""]
    shown = 0
    for category in sorted(by_category.keys()):
        items = by_category[category]
        names = [s.display for s in sorted(items, key=lambda x: x.display)]
        lines.append(f"{category}: {', '.join(names)}")
        shown += len(items)
        if shown >= _MAX_PER_BOT_LIST:
            break
    if shown < len(inv.skills):
        lines.append(f"…and {len(inv.skills) - shown} more.")
    return "\n".join(lines)


def _render_pod(network: dict[str, Any]) -> str:
    members = pod_member_bots(network)
    rows: list[tuple[str, int, str | None]] = []
    for bot in members:
        inv = _safe_inventory(bot, network)
        if inv is None:
            rows.append((bot, 0, "unreadable"))
        elif inv.read_error:
            rows.append((bot, 0, inv.read_error))
        else:
            rows.append((bot, len(inv.skills), None))

    total = sum(c for _, c, err in rows if err is None)
    lines = [f"**Skills — pod** ({total} total across the pod)", ""]
    for bot, count, err in sorted(rows, key=lambda x: -x[1]):
        if err:
            lines.append(f"  {bot}: ({err})")
        else:
            lines.append(f"  {bot}: {count}")
    return "\n".join(lines)


def _safe_inventory(bot_id: str, network: dict[str, Any]):
    """Call get_bot_skills with safe fallback on import / runtime errors."""
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        from evolve_admin.skills.inventory import get_bot_skills
        return get_bot_skills(bot_id, bot_user, network)
    except Exception:
        return None

"""``evo apps`` — installed applications on this bot (or every bot).

Each application is represented by a manifest JSON file under
``{bot_home}/.openclaw/workspace/manifests/``. We list them by name +
short description, no live test status (that lives on the admin UI).

Replaces the previous ``apps`` stub in ``handlers/stub.py``.
"""

from __future__ import annotations

import json
import pwd
from pathlib import Path
from typing import Any

from ..identity import Role
from ..tools.fs_tristate import ReadState, stat_status
from ._shared import is_pod_wide_caller, pod_member_bots, speak


_MAX_PER_BOT_LIST = 12


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(network)
    else:
        body = _render_bot(bot_id, network)
    return speak("apps", body, role)


def _render_bot(bot_id: str, network: dict[str, Any]) -> str:
    manifests = _load_manifests(bot_id, network)
    if manifests is None:
        return (
            f"**Apps — {bot_id}**\n\n"
            "Couldn't read this bot's manifests directory."
        )
    if not manifests:
        return (
            f"**Apps — {bot_id}**\n\n"
            "No apps registered yet. Type `evo gallery` to see what you can install."
        )

    manifests.sort(key=lambda m: str(m.get("name") or m.get("id") or ""))
    lines = [f"**Apps — {bot_id}** ({len(manifests)} installed)", ""]
    shown = manifests[:_MAX_PER_BOT_LIST]
    for m in shown:
        name = str(m.get("name") or m.get("id") or "(untitled)")
        # Canonical identity source for the tray "list my apps" line. The Apps
        # page tile (routes_analytics.py) and detail/Edit modal both render
        # `identity.purpose`; prefer it here too so the tray reply shows the
        # SAME canonical string — a stale/contaminated top-level `description`
        # (e.g. atlas-article-capture's "Member Management" residue from the
        # #3095/#3108 conflation incident) no longer diverges from what the
        # admin UI shows. Falls back to the legacy top-level `description` when
        # no identity.purpose exists (v7-arc Instances, un-migrated legacy
        # manifests). `identity` is schema-typed as a dict, but guard non-dict
        # values defensively — matching the pattern landed in #3225.
        _ident = m.get("identity")
        _purpose = _ident.get("purpose", "") if isinstance(_ident, dict) else ""
        desc = str(_purpose or m.get("description") or "").strip()
        if desc:
            # Single-line description, truncated
            desc_line = desc.split("\n", 1)[0]
            if len(desc_line) > 80:
                desc_line = desc_line[:77] + "..."
            lines.append(f"• {name} — {desc_line}")
        else:
            lines.append(f"• {name}")
    if len(manifests) > _MAX_PER_BOT_LIST:
        lines.append(f"…and {len(manifests) - _MAX_PER_BOT_LIST} more.")
    return "\n".join(lines)


def _render_pod(network: dict[str, Any]) -> str:
    members = pod_member_bots(network)
    counts: list[tuple[str, int]] = []
    for bot in members:
        manifests = _load_manifests(bot, network)
        counts.append((bot, len(manifests) if manifests is not None else -1))

    total = sum(c for _, c in counts if c >= 0)
    lines = [f"**Apps — pod** ({total} total)", ""]
    for bot, count in sorted(counts, key=lambda x: -x[1]):
        if count < 0:
            lines.append(f"  {bot}: (unreadable)")
        else:
            lines.append(f"  {bot}: {count}")
    return "\n".join(lines)


def _load_manifests(bot_id: str, network: dict[str, Any]) -> list[dict] | None:
    """Return list of manifest dicts for ``bot_id`` or None if unreadable.

    Empty list = readable but no apps installed.
    """
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        home = Path(pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"

    # Tri-state the dir lookup so a genuinely-absent manifests dir (the bot
    # simply has no apps) is NOT conflated with a permission wall (EACCES
    # under the bot's 0700 home — we can't tell, so "unreadable"). Folding
    # the two together is the exists()-lies-under-0700 bug.
    dir_state = stat_status(manifests_dir)
    if dir_state is ReadState.ABSENT:
        return []  # no manifests dir → genuinely no apps installed
    if dir_state is ReadState.INDETERMINATE:
        return None  # permission wall — cannot determine; report unreadable

    out: list[dict] = []
    try:
        entries = sorted(manifests_dir.iterdir())
    except (OSError, PermissionError):
        return None
    for path in entries:
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out

"""``evo gallery`` — installable apps from the gallery catalog.

Reads ``packages/gallery/catalog.json`` and shows apps the caller can
install. Per-bot view filters out apps the bot already has (by name
match against its manifests). Pod-wide view shows the whole catalog.

No fancy ranking — that lives in ``gallery_recommender`` and depends on
a pre-built bot profile, which won't be present on every install.
Simple alphabetical listing is fine for v1 in chat.
"""

from __future__ import annotations

import json
import pwd
from pathlib import Path
from typing import Any

from platform_profile import get_profile
from ..identity import Role
from ._shared import is_pod_wide_caller, speak


_MAX_LIST = 12


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    catalog = _load_catalog()
    if not catalog:
        body = (
            "**Gallery**\n\n"
            "Couldn't read the gallery catalog. Ask your pod admin to check "
            "that the gallery package is installed."
        )
        return speak("gallery", body, role)

    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(catalog)
    else:
        body = _render_bot(bot_id, network, catalog)
    return speak("gallery", body, role)


def available_for_bot(
    bot_id: str, network: dict[str, Any], catalog: list[dict] | None = None,
) -> list[dict]:
    """Return the deterministic ordered list of installable apps for a bot.

    Same filter (not-yet-installed) + sort (alphabetical by name) the
    handler uses to render. Exposed so ``evo install <N>`` can pick the
    Nth entry without a state file — both surfaces stay aligned as long
    as they call this helper.
    """
    if catalog is None:
        catalog = _load_catalog()
    installed_names = {n.lower() for n in _installed_app_names(bot_id, network)}
    available = [
        app for app in catalog
        if str(app.get("name") or "").lower() not in installed_names
    ]
    available.sort(key=lambda a: str(a.get("name") or ""))
    return available


def _render_bot(bot_id: str, network: dict[str, Any], catalog: list[dict]) -> str:
    available = available_for_bot(bot_id, network, catalog)
    lines = [f"**Gallery — {bot_id}** ({len(available)} installable, "
             f"{len(catalog) - len(available)} already have)", ""]
    if not available:
        lines.append("You already have everything in the gallery installed.")
        return "\n".join(lines)
    for idx, app in enumerate(available[:_MAX_LIST], start=1):
        name = str(app.get("name") or "(untitled)")
        desc = str(app.get("description") or "").strip().split("\n", 1)[0]
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"{idx}. {name} — {desc}" if desc else f"{idx}. {name}")
    if len(available) > _MAX_LIST:
        lines.append(f"…and {len(available) - _MAX_LIST} more.")
    lines.append("")
    lines.append("Type `evo install <n>` (e.g. `evo install 1`) or "
                 "`evo install <name>` to add one.")
    return "\n".join(lines)


def _render_pod(catalog: list[dict]) -> str:
    by_cat: dict[str, int] = {}
    for app in catalog:
        for cat in (app.get("categories") or []):
            if isinstance(cat, str):
                by_cat[cat] = by_cat.get(cat, 0) + 1
    lines = [f"**Gallery — pod** ({len(catalog)} apps in catalog)", ""]
    if by_cat:
        lines.append("By category:")
        for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {count}")
    return "\n".join(lines)


def _load_catalog() -> list[dict]:
    """Find and load the gallery catalog. Returns [] on any failure."""
    candidates = [
        # Repo-local (worktree, dev)
        Path(__file__).resolve().parents[5] / "packages" / "gallery" / "catalog.json",
        # Deploy checkout (platform-keyed)
        Path(get_profile().deploy_checkout_default) / "packages" / "gallery" / "catalog.json",
    ]
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if isinstance(data, list):
                    return data
        except (OSError, json.JSONDecodeError):
            continue
    return []


def _installed_app_names(bot_id: str, network: dict[str, Any]) -> list[str]:
    """Return names of apps installed on ``bot_id`` (best-effort)."""
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        home = Path(pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")
    manifests_dir = home / ".openclaw" / "workspace" / "manifests"
    if not manifests_dir.exists():
        return []
    names: list[str] = []
    try:
        for path in manifests_dir.iterdir():
            if not path.is_file() or not path.name.endswith(".json"):
                continue
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            n = data.get("name") if isinstance(data, dict) else None
            if isinstance(n, str) and n:
                names.append(n)
    except (OSError, PermissionError):
        return []
    return names

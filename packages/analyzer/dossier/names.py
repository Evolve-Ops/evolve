"""dossier.names — the one app-name table a week's synthesis reads.

The usage rollups key every app by its manifest id (``p-9bfa1c84``, a v7-arc
``i-…`` instance id), because an id is what the plugin stamps. A report a
person reads weekly must lead with the NAME, so synthesis resolves ids
through this table — built ONCE per report, never per row.

Sources, later wins:

  1. the builtin catalogue, ``gallery/index.json`` (``pkg_id`` → name);
  2. every roster bot's installed manifests, keyed by the SAME resolver the
     rollup's writer used (``app_identity.resolve_app_id`` on the RAW
     manifest), named from the display-shaped copy (a v7-arc Instance has no
     name of its own — its bound Spec does).

Read-only over both. A manifest that cannot be read costs a name, never a
row: the id then renders as ``unknown app (<id>)`` and the card's detail
counts it, so a missing manifest is a finding rather than a mystery.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

#: The builtin catalogue, shipped in the repo beside this package.
GALLERY_INDEX = Path(__file__).resolve().parents[3] / "gallery" / "index.json"

#: The fields a manifest names itself by, most specific first.
NAME_FIELDS = ("title", "display_name", "name")


def name_of(manifest: Any) -> str | None:
    """The manifest's own title, falling back through ``NAME_FIELDS``."""
    if not isinstance(manifest, dict):
        return None
    for key in NAME_FIELDS:
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def app_names(
    shared_dir: Path,
    bots: Iterable[str],
    *,
    bot_home: Callable[[str], Path],
    gallery_index: Path = GALLERY_INDEX,
) -> dict[str, str]:
    """``{app_id: name}`` for every app the pod can name."""
    table: dict[str, str] = {}
    for row in _read_json_list(gallery_index):
        # identity: see resolve_app_id — the gallery catalogue's own row key; a
        # catalogue row is not a manifest, so there is nothing to resolve.
        pkg_id, name = row.get("pkg_id"), name_of(row)
        if isinstance(pkg_id, str) and pkg_id and name:
            table[pkg_id] = name

    try:
        from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
            resolve_app_id,
        )
        from evolve_admin.applications.manifest import (  # pyright: ignore[reportMissingImports]
            hydrate_v7_arc_instance,
        )
    except Exception:  # noqa: BLE001 — no admin package: the catalogue alone
        return table

    for bot in bots:
        try:
            manifests_dir = bot_home(bot) / ".openclaw" / "workspace" / "manifests"
            paths = sorted(manifests_dir.glob("*.json"))
        except Exception:  # noqa: BLE001 — an unreadable bot costs names only
            continue
        for path in paths:
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            app_id = resolve_app_id(raw)
            if not app_id:
                continue
            display = raw
            if raw.get("manifest_shape") == "v7-arc":
                try:
                    display = hydrate_v7_arc_instance(raw, Path(shared_dir))
                except Exception:  # noqa: BLE001 — hydration is a nicety
                    display = raw
            # identity: see resolve_app_id — deliberately the bound SPEC id, not
            # the Instance's resolved id: it looks up the Spec's catalogue name
            # when the Instance names nothing itself (the hydration split).
            spec_id = (raw.get("provenance") or {}).get("spec_id")
            name = name_of(display) or table.get(str(spec_id or ""))
            if name:
                table[app_id] = name
    return table


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

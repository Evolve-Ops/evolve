"""
template_installs.py — Per-bot manifest of template-installed plists.

Single source of truth for "which launchd plists did the embedded-apps
provisioner install on this bot." Read by:

  * ``deploy.expected_plist_labels`` so the orphan-sweeper does NOT
    delete template-installed system LaunchDaemons on the next deploy.
  * ``retire._per_bot_plist_labels`` so retire-bot bootouts those plists
    cleanly when a bot is retired.

Written by:

  * :func:`bot_templates.cli_integration.apply_embedded_app` after a
    successful ``launchctl bootstrap`` of each plist.

Cleared by:

  * ``retire.retire_bot`` on the success path.

Layout (under ``{shared_dir}``):

    {shared_dir}/template-installs/
    ├── <bot_id>.json     ← {"plists": [{"label": "...",
    │                                     "destination": "launchagent"|
    │                                                    "launchdaemon",
    │                                     "app_id": "...",
    │                                     "installed_at": "..."}]}

The file is owned by the ``evolve`` user (shared_dir has the right ACL);
writes are atomic via temp-file + rename — no sudo or /tmp staging
needed.

Without this layer, V1.1-1's substrate has the same drift DNA that C1.d
called out: a label installed by one path but not enumerated by the
paired uninstall path. The MVP retrospective made this concrete with
the single-source-of-truth pattern for per-bot Evolve daemons; this
module extends that discipline to template-installed plists.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json as _atomic_write_json


_log = logging.getLogger(__name__)


_DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")
_SUBDIR = "template-installs"


def _manifest_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / _SUBDIR / f"{bot_id}.json"


def read_template_installs(
    shared_dir: Path | str | None,
    bot_id: str,
) -> list[dict[str, Any]]:
    """Return the list of recorded plist entries for ``bot_id``.

    Each entry is ``{"label", "destination", "app_id"?, "installed_at"?}``.
    Returns an empty list if the manifest does not exist, is unreadable,
    or is malformed (logged at warning; never raises).
    """
    sd = Path(shared_dir) if shared_dir else _DEFAULT_SHARED_DIR
    path = _manifest_path(sd, bot_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning(
            "template-install manifest %s is malformed (%s); treating "
            "as empty",
            path, exc,
        )
        return []
    plists = doc.get("plists") if isinstance(doc, dict) else None
    if not isinstance(plists, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for entry in plists:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        destination = entry.get("destination")
        if not isinstance(label, str) or not label:
            continue
        if destination not in ("launchagent", "launchdaemon"):
            continue
        cleaned.append({
            "label": label,
            "destination": destination,
            "app_id": entry.get("app_id"),
            "installed_at": entry.get("installed_at"),
        })
    return cleaned


def template_installed_labels(
    shared_dir: Path | str | None,
    bot_id: str,
    *,
    destination: str | None = None,
) -> list[str]:
    """Return just the launchd labels recorded for ``bot_id``.

    If ``destination`` is provided, filter to that destination only
    (``"launchagent"`` or ``"launchdaemon"``).
    """
    entries = read_template_installs(shared_dir, bot_id)
    if destination is not None:
        entries = [e for e in entries if e["destination"] == destination]
    return [e["label"] for e in entries]


def record_template_install(
    shared_dir: Path | str | None,
    bot_id: str,
    *,
    label: str,
    destination: str,
    app_id: str | None = None,
) -> None:
    """Record one (label, destination) entry for ``bot_id``.

    Idempotent: re-recording the same label updates ``installed_at`` and
    ``app_id`` but does not duplicate the entry. Atomic: written via
    temp-file in the same dir + ``os.replace`` so a concurrent reader
    never sees a partial JSON doc.
    """
    if destination not in ("launchagent", "launchdaemon"):
        raise ValueError(
            f"destination must be launchagent or launchdaemon, "
            f"got {destination!r}"
        )
    sd = Path(shared_dir) if shared_dir else _DEFAULT_SHARED_DIR
    path = _manifest_path(sd, bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = read_template_installs(sd, bot_id)
    # Dedup on (label, destination).
    keep: list[dict[str, Any]] = []
    for entry in existing:
        if entry["label"] == label and entry["destination"] == destination:
            continue
        keep.append(entry)
    keep.append({
        "label": label,
        "destination": destination,
        "app_id": app_id,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    })
    doc = {"plists": keep}
    # mode=0o644 so other Evolve processes running as the same user can read it.
    _atomic_write_json(path, doc, sort_keys=True, mode=0o644)


def remove_template_install(
    shared_dir: Path | str | None,
    bot_id: str,
    *,
    label: str,
) -> None:
    """Remove the entry for ``label`` from the bot's manifest.

    No-op if the entry (or manifest) doesn't exist.
    """
    sd = Path(shared_dir) if shared_dir else _DEFAULT_SHARED_DIR
    path = _manifest_path(sd, bot_id)
    existing = read_template_installs(sd, bot_id)
    keep = [e for e in existing if e["label"] != label]
    if len(keep) == len(existing):
        return  # nothing to do
    if not keep:
        # Tidy: remove the manifest entirely when empty.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.warning(
                "could not unlink empty manifest %s: %s", path, exc,
            )
        return
    _atomic_write_json(path, {"plists": keep}, sort_keys=True, mode=0o644)


def clear_template_installs(
    shared_dir: Path | str | None,
    bot_id: str,
) -> None:
    """Remove the entire manifest for ``bot_id`` (e.g. on retire-bot).

    No-op if the manifest doesn't exist.
    """
    sd = Path(shared_dir) if shared_dir else _DEFAULT_SHARED_DIR
    path = _manifest_path(sd, bot_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("could not clear manifest %s: %s", path, exc)

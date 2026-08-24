"""app_cron_map — the deploy-side half of AL-1.2's OC-cron attribution join.

``{shared_dir}/{bot_id}/app-cron-map.json`` maps a cron job's **name** to the
``app_id`` whose manifest installed it. Written at the ``_merge_cron_entries``
call sites in ``deploy.py`` (the choke point every manifest ``crons[]`` entry
passes through on its way into the bot's ``~/.openclaw/cron/jobs.json``).
Keyed by name because that is the only stable identifier Evolve owns at
materialization — the OC store assigns the job ``id`` at load, after our
write. The plugin's ``apps/scheduledAttribution.ts`` closes the loop at
``before_agent_run``: gateway session key `` cron:<job.id> `` → job name via
the bot's own jobs.json → ``app_id`` via this map → the ``scheduled``
attribution grade (design-app-attribution-2026-08-15 §4.1).

Lives outside ``deploy.py`` because that file is size-ratcheted
(tools/file-size-baseline.txt) — the call site there is one line.

The map is observation-layer state: a failed write must WARN into the deploy
log, never fail the app install. The write itself is atomic
(same-dir temp + ``os.replace``) with the mode pinned to 0644 — ``mkstemp``
creates 0600 and a rename carries that onto the dest, which would lock the
bot-user reader out (the plugin reads this file cross-user).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

APP_CRON_MAP_FILENAME = "app-cron-map.json"


def _write_map_atomic(dest_dir: Path, merged: dict[str, str]) -> None:
    """Same-dir temp + rename with the mode pinned 0644 (see module doc)."""
    dest = dest_dir / APP_CRON_MAP_FILENAME
    fd, tmp = tempfile.mkstemp(dir=str(dest_dir), prefix=".app-cron-map-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)  # pin: mkstemp is 0600 and rename would carry it
        os.replace(tmp, dest)
    finally:
        try:
            os.unlink(tmp)
        except OSError as e:
            logger.debug("app-cron-map tmp cleanup: %s", e)  # gone after replace — normal


def read_app_cron_map(shared_dir: Path | str, bot_id: str) -> dict[str, str]:
    """Return the bot's cron-name → app_id map ({} when absent/unreadable)."""
    path = Path(shared_dir) / bot_id / APP_CRON_MAP_FILENAME
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and k and isinstance(v, str) and v
    }


def merge_app_cron_map(
    shared_dir: Path | str,
    bot_id: str,
    entries: dict[str, str],
    result=None,
) -> bool:
    """Merge ``{cron name: app_id}`` entries into the bot's app-cron-map.json.

    Additive merge (an app re-deploy refreshes its own names; other apps'
    entries are untouched). No-ops without touching the file when nothing
    changes. Best-effort by design: on failure, logs a warning (and a
    ``result.log`` line when a DeployResult is passed) and returns False —
    never raises, never fails the install.
    """
    try:
        clean = {
            str(k).strip(): str(v).strip()
            for k, v in (entries or {}).items()
            if isinstance(k, str) and str(k).strip()
            and isinstance(v, str) and str(v).strip()
        }
        if not clean:
            return True
        existing = read_app_cron_map(shared_dir, bot_id)
        merged = {**existing, **clean}
        if merged == existing:
            return True
        dest_dir = Path(shared_dir) / bot_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write_map_atomic(dest_dir, merged)
        if result is not None:
            result.log(f"Updated app-cron-map for {bot_id}: {sorted(clean)}")
        return True
    except Exception as e:  # noqa: BLE001 — observation write must not fail a deploy
        logger.warning("app-cron-map write failed for %s: %s", bot_id, e)
        if result is not None:
            try:
                result.log(f"WARNING: app-cron-map write failed for {bot_id}: {e}")
            except Exception as log_err:
                logger.debug("app-cron-map result.log failed: %s", log_err)
        return False


def remove_app_cron_map_entries(
    shared_dir: Path | str, bot_id: str, names: list[str]
) -> bool:
    """Drop the given cron names from the map (app uninstall / test cleanup).

    Missing names are ignored; a missing map is success. Same best-effort
    contract as :func:`merge_app_cron_map`.
    """
    try:
        existing = read_app_cron_map(shared_dir, bot_id)
        merged = {k: v for k, v in existing.items() if k not in set(names)}
        if merged == existing:
            return True
        _write_map_atomic(Path(shared_dir) / bot_id, merged)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("app-cron-map entry removal failed for %s: %s", bot_id, e)
        return False

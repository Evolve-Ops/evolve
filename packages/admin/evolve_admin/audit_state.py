"""evolve_admin.audit_state — shared OC security audit cache.

Pulled out of ``evolve_admin/web/server.py`` so ``pod_state.audits``
can read the same cache the existing ``/api/security/audit`` route
exposes, without forcing a circular import.

## Storage

Two backing stores, in this priority order:

  1. **Disk file** at ``{shared_dir}/security/audit-cache.json``
     (canonical source of truth for cross-process readers — the MCP
     server's evo-tools child process can read it without depending
     on admin-server memory). Atomic temp-file-then-rename writes;
     readers see consistent snapshots.

  2. **In-memory ``_state`` dict** (mirror — kept up to date so
     ``server.py``'s existing writes keep working without changes
     and ``snapshot()`` calls without a ``shared_dir`` still return
     the live data). Tests that populate ``_state`` directly continue
     to work because the in-memory snapshot is checked when no disk
     path is supplied.

The original module-global-only design was a cross-process trap: the
MCP server's child process imported the module fresh and got an empty
``_state`` dict (no writer over there), so ``pod_state.audit`` always
reported stale (#1337). #1337 patched ``pod_state.audit`` to HTTP-fetch
from the admin server. This PR adds the disk mirror so the trap is
gone at the source — every reader, in any process, can hit the same
disk file. The HTTP fallback is retained as defense-in-depth.

## Shape

``{"data": {bot_id: result_dict}, "cached_at": float | None}``.
``result_dict`` is whatever ``server._audit_run_one`` returns —
either a populated audit summary or ``{"unavailable": True, ...}``
when the OC audit subprocess failed.

## Writer / reader topology

Writer in production: the web server's background audit thread,
which updates ``_state["data"][bot_id]`` per completed audit and
calls ``persist(shared_dir)`` to mirror to disk. ``server.py`` keeps
the existing ``_audit_cache = _state`` alias so its writes still
land in memory; the explicit ``persist`` call after each batch is
what propagates to disk.

Readers:
  * Admin server (in-process): ``snapshot()`` returns the in-memory
    dict directly. Cheap, no I/O.
  * MCP server child process: ``snapshot(shared_dir=X)`` reads the
    disk file. The in-memory dict in that process is empty (no writer
    there), so falling back to it would be useless.
  * Tests: poke ``_state`` directly; ``snapshot()`` returns it.

Cache survives admin-server restarts now (it didn't before — was
purely process-local). The disk file is the recovery point.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# In-memory mirror. ``web/server.py`` aliases ``_audit_cache`` to this
# dict so existing writes (``_audit_cache["data"] = ...``) land here.
# Do not rebind this name — code elsewhere depends on the identity,
# not just the contents.
_state: dict[str, Any] = {"data": {}, "cached_at": None}


# Filename under shared_dir for the disk mirror. Stays under
# ``security/`` so it's grouped with related audit artifacts the
# operator might inspect.
_CACHE_REL = Path("security") / "audit-cache.json"


def _cache_path(shared_dir: Path) -> Path:
    """Compute the disk file location for a given shared_dir.

    Centralized so callers don't have to know the path layout.
    Mirrors the helper-function pattern used by ``signals.store`` etc.
    """
    return Path(shared_dir) / _CACHE_REL


def snapshot(shared_dir: Path | None = None) -> dict[str, Any]:
    """Return a deep copy of the cache.

    Resolution order:
      1. If ``shared_dir`` is supplied AND the disk file exists +
         parses → return that. This is the cross-process path.
      2. Otherwise return the in-memory ``_state`` (admin-server
         in-process path; also the test path where ``_state`` was
         poked directly).

    Per-bot result dicts contain nested summary/findings/details
    structures. A shallow copy would share those references, so a
    caller mutating ``snapshot()["data"]["team_bot_a"]["summary"]`` would
    silently corrupt the live cache. Deep copy is cheap at our
    volumes (≤20 bots × ~5KB).
    """
    if shared_dir is not None:
        try:
            path = _cache_path(shared_dir)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return {
                        "data": copy.deepcopy(payload.get("data") or {}),
                        "cached_at": payload.get("cached_at"),
                    }
        except (OSError, json.JSONDecodeError) as exc:
            # Disk read failure isn't fatal — fall through to the
            # in-memory mirror. Log so a stale on-disk file shows up
            # in admin-server logs.
            log.debug(
                "audit_state.snapshot: disk read failed (%s); using memory",
                exc,
            )
    return {
        "data": copy.deepcopy(_state.get("data") or {}),
        "cached_at": _state.get("cached_at"),
    }


def get_data(shared_dir: Path | None = None) -> dict[str, Any]:
    """Return just the per-bot result map (deep-copied; see :func:`snapshot`)."""
    return snapshot(shared_dir).get("data") or {}


def persist(shared_dir: Path) -> None:
    """Mirror the current in-memory ``_state`` to disk atomically.

    Called by ``server.py``'s audit background thread after each
    incremental update. Uses temp-file + rename so a reader between
    writes never observes a partial file.

    Errors are logged + swallowed: the in-memory cache is still
    authoritative for the admin-server process, and a disk write
    failure shouldn't crash the audit thread. The next successful
    write reconverges.
    """
    path = _cache_path(shared_dir)
    payload = {
        "data": _state.get("data") or {},
        "cached_at": _state.get("cached_at"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp-file + rename gives atomic visibility. dir=parent so
        # the rename is on the same filesystem (no cross-device).
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".audit-cache-",
            suffix=".json.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning(
            "audit_state.persist: could not write %s (%s) — "
            "in-memory cache still authoritative", path, exc,
        )


def hydrate(shared_dir: Path) -> None:
    """Load the disk mirror into ``_state``. Called once at server
    startup so the admin process picks up where it left off after a
    restart instead of starting empty.

    Idempotent + silent on missing file. Safe to call before any
    writers have run.
    """
    path = _cache_path(shared_dir)
    try:
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("audit_state.hydrate: skip (%s)", exc)
        return
    if not isinstance(payload, dict):
        return
    data = payload.get("data")
    if isinstance(data, dict):
        _state["data"] = data
    cached_at = payload.get("cached_at")
    if isinstance(cached_at, (int, float)):
        _state["cached_at"] = float(cached_at)


def reset() -> None:
    """Clear the in-memory cache. For tests; do not call from
    production code. Does NOT touch the disk mirror — tests that need
    the disk mirror cleared should ``Path(...).unlink()`` it
    explicitly so the intent is obvious."""
    _state["data"] = {}
    _state["cached_at"] = None

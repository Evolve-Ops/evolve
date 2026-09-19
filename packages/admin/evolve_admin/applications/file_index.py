"""
file_index.py — Global cross-manifest file index for the Evolve network.

Maintains a network-wide index at ``{shared_dir}/file_index.json`` that maps
every known file artifact (by file_id) to its path, bot, ownership, and version
metadata.  The index is rebuilt whenever any manifest changes (triggered from
manifest.save_manifest) and supports O(1) lookups without scanning all manifests.

Index format
────────────
The index is a flat JSON object keyed by file_id:

    {
        "f-d4e8f901": {
            "path":         "scripts/tasks.py",
            "bot_id":       "admin_bot",
            "owned_by":     "p-a3f91c8b",
            "shared_with":  ["p-b2e04d1a"],
            "layer":        "script",
            "lifecycle":    "owned",
            "file_version": "2026.04.15.1",
            "modified_at":  "2026-04-15T14:23:00Z"
        }
    }

lifecycle values (see provenance.FileLifecycle):
    owned    — all pkg_ids in marker resolve to active manifests
    shared   — owned_by active + shared_with entries active
    orphaned — one or more pkg_ids no longer have a manifest
    unowned  — no marker at all (data file that outlived its app, or never claimed)

v4/v5 compatibility
────────────────────
v4 manifests store ``files`` as list[str] (plain paths — no file_id to index).
v5 manifests store ``files`` as list[dict] with full provenance records.

This module uses :meth:`ApplicationManifest.file_paths` for backward-compatible
path access and inspects entries directly to extract v5 provenance fields.
v4 string entries are silently skipped for indexing (no file_id available).

identity: see resolve_app_id — AL-1.4b swept this module and kept every
``pkg_id`` in it. ``owned_by`` / ``shared_with`` are the FILE-INDEX
ATTRIBUTION NAMESPACE: their values are ``pkg_id`` strings written by the
forge/extend path and cross-checked against the ``_evolve`` marker's
``pkg_ids`` (see ``provenance.py``), so they must keep matching the literal
text already on disk in every marker and every ``files[*].owned_by``. Both
sides of ``compute_lifecycle`` are drawn from the same namespace — the
``all_pkg_ids`` set from ``manifest.pkg_id``, the ``owned_by`` default from
``manifest.pkg_id`` — so resolving one side to a canonical app id and not the
other would make every file look ``orphaned``. Re-keying this index is 1.4c
work with a rewrite migration, not a reader sweep.

"""

from __future__ import annotations

import json
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from .manifest import list_manifests
from .provenance import scan_workspace_marked_only, FileLifecycle


# ── Path helper ────────────────────────────────────────────────────────────────

def _index_path(shared_dir: Path) -> Path:
    return shared_dir / "file_index.json"


# ── Core index operations ──────────────────────────────────────────────────────

def rebuild_file_index(shared_dir: Path, bot_ids: list[str]) -> dict[str, dict]:
    """
    Scan all manifests for all bots and rebuild the global file index.

    Iterates over every manifest for every bot in *bot_ids*.  For each manifest,
    examines the ``files`` list:

    - **v5 dict entries** — extracts file_id, path, owned_by, shared_with,
      file_version, and modified_at.  Only entries with a non-empty file_id
      are added to the index.
    - **v4 string entries** — path is known but there is no file_id to key on,
      so these entries are silently skipped.

    Writes the resulting index atomically to ``{shared_dir}/file_index.json``
    and returns the index dict.

    Args:
        shared_dir: Path to the shared evolve data directory.
        bot_ids:    All bot identifiers whose manifests should be scanned.

    Returns:
        The newly built index dict (file_id → record).
    """
    # identity: see resolve_app_id — the attribution namespace — BOTH sides of
    # the compute_lifecycle join are pkg_ids (module note).
    index: dict[str, dict] = {}

    # Collect all active pkg_ids so we can compute lifecycle states
    all_pkg_ids: set[str] = set()
    for bot_id in bot_ids:
        try:
            for m in list_manifests(shared_dir, bot_id):
                if m.pkg_id:
                    all_pkg_ids.add(m.pkg_id)
        except Exception:
            continue

    for bot_id in bot_ids:
        try:
            manifests = list_manifests(shared_dir, bot_id)
        except Exception:
            continue

        for manifest in manifests:
            for entry in (manifest.files or []):
                if isinstance(entry, str):
                    # v4 manifest — string path only, no file_id to index
                    continue

                if not isinstance(entry, dict):
                    continue

                file_id = entry.get("file_id", "")
                if not file_id:
                    # No file_id means we cannot key this entry in the index
                    continue

                # identity: see resolve_app_id — the attribution namespace
                # (module note above); paired with ``all_pkg_ids`` below.
                owned_by   = entry.get("owned_by", manifest.pkg_id)
                shared_with = entry.get("shared_with") or []
                lifecycle  = compute_lifecycle(
                    owned_by=owned_by,
                    shared_with=shared_with,
                    all_pkg_ids=all_pkg_ids,
                )

                index[file_id] = {
                    "path":         entry.get("path", ""),
                    "bot_id":       bot_id,
                    "owned_by":     owned_by,
                    "shared_with":  shared_with,
                    "layer":        entry.get("layer", ""),
                    "lifecycle":    lifecycle,
                    "file_version": entry.get("file_version", ""),
                    "modified_at":  entry.get("modified_at", ""),
                }

    try:
        index_path = _index_path(shared_dir)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(index_path, index)
    except Exception:
        pass

    return index


def compute_lifecycle(
    owned_by: str,
    shared_with: list[str],
    all_pkg_ids: set[str],
) -> str:
    """
    Derive the lifecycle state for a file given its ownership and the current
    set of known (active) pkg_ids.

    Args:
        owned_by:    The pkg_id that owns this file (may be empty).
        shared_with: Other pkg_ids that use but don't own this file.
        all_pkg_ids: Set of all pkg_ids that have a live manifest.

    Returns:
        One of the FileLifecycle constant strings.
    """
    if not owned_by:
        return FileLifecycle.UNOWNED

    owner_alive = owned_by in all_pkg_ids

    if not owner_alive:
        return FileLifecycle.ORPHANED

    if shared_with:
        return FileLifecycle.SHARED

    return FileLifecycle.OWNED


def load_file_index(shared_dir: Path) -> dict[str, dict]:
    """
    Load the global file index from disk.

    Returns an empty dict if the index file does not exist or is unreadable.

    Args:
        shared_dir: Path to the shared evolve data directory.

    Returns:
        Index dict (file_id → record), or ``{}`` on any error.
    """
    path = _index_path(shared_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Lookup helpers ─────────────────────────────────────────────────────────────

def lookup_file(file_id: str, shared_dir: Path) -> dict | None:
    """
    O(1) lookup of a file record by file_id.

    Args:
        file_id:    The file artifact identifier (e.g. ``"f-d4e8f901"``).
        shared_dir: Path to the shared evolve data directory.

    Returns:
        The file record dict, or None if not found in the index.
    """
    return load_file_index(shared_dir).get(file_id)


def files_owned_by(pkg_id: str, shared_dir: Path) -> list[dict]:
    """
    Return all index records where ``owned_by`` equals *pkg_id*.

    Each returned dict includes the ``file_id`` key for convenience.

    Args:
        pkg_id:     Package identifier to filter by.
        shared_dir: Path to the shared evolve data directory.

    Returns:
        List of file record dicts (with ``file_id`` injected).
    """
    index = load_file_index(shared_dir)
    return [
        {**record, "file_id": fid}
        for fid, record in index.items()
        if record.get("owned_by") == pkg_id
    ]


def files_shared_with(pkg_id: str, shared_dir: Path) -> list[dict]:
    """
    Return all index records where *pkg_id* appears in ``shared_with``.

    These are files owned by another package that this package depends on.

    Each returned dict includes the ``file_id`` key for convenience.

    Args:
        pkg_id:     Package identifier to filter by.
        shared_dir: Path to the shared evolve data directory.

    Returns:
        List of file record dicts (with ``file_id`` injected).
    """
    index = load_file_index(shared_dir)
    return [
        {**record, "file_id": fid}
        for fid, record in index.items()
        if pkg_id in (record.get("shared_with") or [])
    ]


def files_on_bot(bot_id: str, shared_dir: Path) -> list[dict]:
    """
    Return all index records registered to a specific bot.

    Each returned dict includes the ``file_id`` key for convenience.

    Args:
        bot_id:     Bot identifier to filter by.
        shared_dir: Path to the shared evolve data directory.

    Returns:
        List of file record dicts (with ``file_id`` injected).
    """
    index = load_file_index(shared_dir)
    return [
        {**record, "file_id": fid}
        for fid, record in index.items()
        if record.get("bot_id") == bot_id
    ]


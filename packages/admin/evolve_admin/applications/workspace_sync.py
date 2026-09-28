"""
workspace_sync.py — re-sync a bot's workspace files from their canonical
source after a PR merge.

Spec: internal/spec-workspace-file-sync-2026-06-07.md.

The problem this module solves
------------------------------

`apply_actions(bot, app)` materialises a manifest's `scheduled_actions[]`
into launchd plists, but does NOT re-copy the bundled workspace files
that those plists execute. That gap caused the 2026-06-07 Atlas
classifier incident: PR #2294 fixed the leaked-API-key bug in the repo,
but bots kept running the old `classifier.py` because nothing copied the
new bytes into `/Users/atlas/.openclaw/workspace/scripts/`.

This module is the missing piece. `sync_workspace_files` is called from
`apply_actions` BEFORE `_materialize_scheduled_actions`; a launchd plist
that points at `scripts/atlas_digest.py` runs against the freshly-synced
version, not the stale one.

Source resolution
-----------------

The sync resolver looks for the canonical source in three places, in
priority order:

  1. `gallery/<slug>/files/` via `find_files_pack_dir(pkg_id)` — the
     existing files-pack convention. Used for ea-pack and any other
     app that ships canonical files in the builtin gallery layout.

  2. `manifest.workspace_files_source` — a repo-root-relative path on
     the manifest declaring where the bundled files live. Used for
     side-loaded apps like Atlas, whose source lives at
     `docs/atlas-app-manifests/` rather than `gallery/atlas-*/`.

  3. None — sync is a no-op. The manifest has no canonical source the
     resolver can find. The launchd plists still get materialised; only
     the file copy is skipped.

Drift detection
---------------

Default mode is `DRIFT_AWARE`: each file in the manifest's `files[]`
has its sha256 compared between source and workspace. Only mismatches
get re-copied. Clean runs are sub-millisecond per file. `FORCE` mode
re-copies regardless; `SKIP` disables sync entirely.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────


class WorkspaceSyncError(Exception):
    """Base class for sync errors that should abort apply-actions.

    Per-file write failures are recorded in the result instead of raised —
    sync stays best-effort within an otherwise-successful apply-actions
    run. Only structural problems (a traversal escape, a malformed
    declared source) propagate.
    """


class WorkspaceFilesSourceError(WorkspaceSyncError):
    """`workspace_files_source` doesn't resolve to a safe in-repo dir."""


# ── Modes ────────────────────────────────────────────────────────────────────


class SyncMode(str, Enum):
    DRIFT_AWARE = "drift_aware"
    FORCE       = "force"
    SKIP        = "skip"


# ── Result + source records ──────────────────────────────────────────────────


@dataclass
class WorkspaceFilesSource:
    """The resolved source the synchroniser will read from.

    `synthesise` differentiates the two source styles:
      * False — the source dir has its own `manifest.json` listing
        per-file sha256s (the existing gallery files-pack contract;
        load it with `load_files_pack_metadata`).
      * True  — the source dir has raw files but no manifest.json; the
        synchroniser walks the dir, hashes each file in
        `manifest.files[]`, and constructs an in-memory
        `FilesPackMetadata`.
    """

    files_pack_dir: Path
    synthesise:     bool
    origin:         str   # "gallery" | "manifest" — for stamping/log


@dataclass
class WorkspaceSyncResult:
    """Per-(bot, app) sync outcome.

    The stamp written to `manifest.workspace_sync` is a subset of these
    fields rendered via `to_stamp()`. The full dataclass is returned to
    `apply_actions` so the CLI can render a human-readable summary.
    """

    skipped:           bool       = False
    skipped_reason:    str        = ""
    origin:            str        = ""   # mirrors WorkspaceFilesSource.origin
    source_path:       str        = ""
    synced_count:      int        = 0
    drifted_paths:     list[str]  = field(default_factory=list)
    orphan_paths:      list[str]  = field(default_factory=list)
    missing_in_source: list[str]  = field(default_factory=list)
    pycache_cleared:   list[str]  = field(default_factory=list)
    errors:            list[str]  = field(default_factory=list)

    def to_stamp(self) -> dict:
        """The shape persisted on `manifest.workspace_sync`."""
        return {
            "last_synced_at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source":           self.source_path,
            "origin":           self.origin,
            "synced_count":     self.synced_count,
            "drifted_paths":    sorted(self.drifted_paths),
            "orphan_paths":     sorted(self.orphan_paths),
            "missing_in_source": sorted(self.missing_in_source),
            "errors":           list(self.errors),
        }


# ── Resolver ─────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """The repo root, mirroring gallery.py's resolution.

    On the mini this is `/Users/Shared/evolve-repo/` (the deploy
    checkout); on a dev laptop it's the worktree root. Deliberately
    derived from this file's path rather than configured separately so
    the gallery resolver and the workspace_files_source resolver always
    agree on where the source tree lives.
    """
    # packages/admin/evolve_admin/applications/workspace_sync.py
    # 4 levels up → repo root
    return Path(__file__).resolve().parents[4]


def resolve_workspace_files_source(
    manifest,
    *,
    repo_root: Path | None = None,
) -> WorkspaceFilesSource | None:
    """Resolve the canonical source for `manifest`'s bundled files.

    Priority:
      1. gallery files-pack via `find_files_pack_dir(pkg_id)`
      2. `manifest.workspace_files_source` (a repo-relative path)
      3. None — caller should skip sync

    Raises:
      WorkspaceFilesSourceError on a path-traversal escape from
      `workspace_files_source`. A missing-but-declared source returns
      None (caller logs); a traversal escape raises (caller fails
      loudly — this is operator-authored, not external).
    """
    root = repo_root or _repo_root()

    # identity: see resolve_app_id — NOT swept (AL-1.4b). ``find_files_pack_dir``
    # keys the gallery files-pack directory by pkg_id specifically; an ``id`` or
    # ``instance_id`` fallback names no directory, and a wrong hit here would
    # copy ANOTHER app's files into this bot's workspace.
    pkg_id = (getattr(manifest, "pkg_id", "") or "").strip()
    if pkg_id:
        from .gallery import find_files_pack_dir
        gallery_dir = find_files_pack_dir(pkg_id)
        if gallery_dir is not None:
            return WorkspaceFilesSource(
                files_pack_dir = gallery_dir,
                synthesise     = False,
                origin         = "gallery",
            )

    declared = (getattr(manifest, "workspace_files_source", "") or "").strip()
    if declared:
        candidate = (root / declared).resolve()
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            raise WorkspaceFilesSourceError(
                f"workspace_files_source {declared!r} resolves outside "
                f"the repo root ({root_resolved}) — refusing to sync"
            )
        if candidate.is_dir():
            return WorkspaceFilesSource(
                files_pack_dir = candidate,
                synthesise     = True,
                origin         = "manifest",
            )

    return None


# ── Synthesised files-pack metadata for side-loaded sources ──────────────────


def _infer_mode(path: str) -> str:
    """0755 for shell scripts, 0644 otherwise.

    Matches what the Atlas DEPLOY.md walkthrough does by hand:
    `chmod +x` on the cron scripts, default 644 everywhere else.
    Side-loaded sources don't carry a declared mode; this heuristic
    is the safest default. A manifest author who needs a non-default
    mode should move to a real files-pack manifest.json.
    """
    return "0755" if path.endswith(".sh") else "0644"


def synthesise_files_pack_metadata(source_dir: Path, manifest):
    """Build a `FilesPackMetadata` in memory from a side-loaded source dir.

    Walks `manifest.files[]` (NOT the source dir's filesystem; only
    declared files are eligible). For each declared file present on
    disk, hashes the source to fill in the sha256.

    Files declared but missing on the source side are excluded from
    the synthesised metadata; the caller surfaces them as
    `missing_in_source` in the sync result.

    Placeholders are always empty for synthesised packs. v1 contract:
    side-loaded sources are copied byte-for-byte. A side-loaded app
    that needs `{bot_id}`-style substitution has to be promoted to a
    real files-pack manifest.json.
    """
    from .files_pack import FilesPackFile, FilesPackMetadata, _sha256_file

    declared_paths: list[str] = []
    for entry in (getattr(manifest, "files", None) or []):
        if isinstance(entry, str):
            declared_paths.append(entry)
        elif isinstance(entry, dict):
            p = (entry.get("path") or "").strip()
            if p:
                declared_paths.append(p)

    files: list[FilesPackFile] = []
    for rel in declared_paths:
        src = source_dir / rel
        if not src.is_file():
            continue
        files.append(FilesPackFile(
            path                = rel,
            mode                = _infer_mode(rel),
            sha256              = _sha256_file(src),
            placeholders        = [],
            placeholders_in_path = [],
            size_bytes          = src.stat().st_size,
        ))

    return FilesPackMetadata(
        format_version  = "synthesised-1.0",
        snapshot_source = {
            "kind":   "side_loaded",
            "origin": str(source_dir),
        },
        files = files,
    )


# ── Drift detection + sync ──────────────────────────────────────────────────


def _file_sha256_or_none(path: Path) -> str | None:
    """sha256 of a file, or None if it doesn't exist / can't be read."""
    try:
        if not path.is_file():
            return None
        from .files_pack import _sha256_file
        return _sha256_file(path)
    except (PermissionError, OSError):
        return None


def _invalidate_pycache_under(
    workspace_dir: Path,
    paths: set[str],
) -> list[str]:
    """Remove `__pycache__/` next to each just-installed `.py` file.

    Python's import machinery normally re-compiles on mtime change, but
    a long-running daemon that already imported the old module will keep
    the stale bytecode resident. Removing the cache dirs makes the next
    cold-start clean. Failures are non-fatal — return the list of dirs
    that COULD be cleared.
    """
    py_parents = {
        (workspace_dir / p).parent
        for p in paths
        if p.endswith(".py")
    }
    cleared: list[str] = []
    for parent in py_parents:
        cache_dir = parent / "__pycache__"
        if cache_dir.is_dir():
            try:
                shutil.rmtree(cache_dir)
                cleared.append(str(cache_dir))
            except (PermissionError, OSError) as exc:
                logger.debug(
                    "pycache invalidate failed at %s: %s", cache_dir, exc,
                )
    return cleared


def _detect_orphans(
    workspace_dir: Path,
    pack_metadata,
    manifest,
) -> list[str]:
    """Files declared in `manifest.files[]` but no longer in the source.

    A file is an orphan when ALL three are true:
      1. It's still declared in `manifest.files[]`
      2. It's absent from `pack_metadata.files` (source side removed it)
      3. It's present in the workspace (a leftover from a prior sync)

    v1 reports orphans on `workspace_sync.orphan_paths` but does NOT
    delete them. Operator gesture required (a future `--prune-orphans`
    flag).
    """
    declared_paths: set[str] = set()
    for entry in (getattr(manifest, "files", None) or []):
        if isinstance(entry, str):
            declared_paths.add(entry)
        elif isinstance(entry, dict):
            p = (entry.get("path") or "").strip()
            if p:
                declared_paths.add(p)

    source_paths = {f.path for f in pack_metadata.files}
    candidates = declared_paths - source_paths

    orphans: list[str] = []
    for p in candidates:
        if (workspace_dir / p).is_file():
            orphans.append(p)
    return sorted(orphans)


def sync_workspace_files(
    manifest,
    shared_dir: Path,
    *,
    bot_user: str,
    workspace_dir: Path,
    mode: SyncMode = SyncMode.DRIFT_AWARE,
) -> WorkspaceSyncResult:
    """Re-copy any drifted workspace file from source.

    Side-effect-free when `mode == SyncMode.SKIP`. Side-effect-free on
    a clean run in DRIFT_AWARE mode (only sha256 reads). Re-copies
    drifted files via the existing `install_files_pack_to_workspace`
    path so the install context (placeholder substitution, atomic
    rename) stays consistent with what forge would have done.
    """
    if mode == SyncMode.SKIP:
        return WorkspaceSyncResult(skipped=True, skipped_reason="sync disabled")

    try:
        source = resolve_workspace_files_source(manifest)
    except WorkspaceFilesSourceError:
        raise

    if source is None:
        return WorkspaceSyncResult(
            skipped=True,
            skipped_reason="no source resolved (no files-pack, no workspace_files_source)",
        )

    from .files_pack import (
        load_files_pack_metadata,
        install_files_pack_to_workspace,
        resolve_install_context,
    )

    pack_metadata = (
        synthesise_files_pack_metadata(source.files_pack_dir, manifest)
        if source.synthesise
        else load_files_pack_metadata(source.files_pack_dir)
    )

    if pack_metadata is None or not pack_metadata.files:
        # Real files-pack with empty/missing manifest.json, or
        # synthesised pack where none of the declared files were
        # present on the source side. Either way, sync has nothing
        # to do. Surface the missing files for the operator.
        missing = _missing_declared_files(source.files_pack_dir, manifest)
        return WorkspaceSyncResult(
            skipped           = True,
            skipped_reason    = "source has no files to sync",
            origin            = source.origin,
            source_path       = str(source.files_pack_dir),
            missing_in_source = missing,
        )

    # Drift detection
    drifted: list[str] = []
    for entry in pack_metadata.files:
        if mode == SyncMode.FORCE:
            drifted.append(entry.path)
            continue
        target = workspace_dir / entry.path
        target_sha = _file_sha256_or_none(target)
        if target_sha != entry.sha256:
            drifted.append(entry.path)

    missing_in_source = _missing_declared_files(source.files_pack_dir, manifest)

    if not drifted:
        return WorkspaceSyncResult(
            skipped           = False,
            origin            = source.origin,
            source_path       = str(source.files_pack_dir),
            synced_count      = 0,
            drifted_paths     = [],
            orphan_paths      = _detect_orphans(workspace_dir, pack_metadata, manifest),
            missing_in_source = missing_in_source,
        )

    # Re-copy drifted files via the existing install path so we
    # inherit atomic rename, placeholder substitution, and the
    # existing per-file error surfacing.
    bot_id = (getattr(manifest, "bot_id", "") or "").strip()
    # identity: see resolve_app_id — NOT swept (AL-1.4b), and the local is named
    # for what it holds. ``pkg_id`` / ``app_id`` here are the ``{{pkg_id}}`` /
    # ``{{app_id}}`` PLACEHOLDER values substituted into files-pack file CONTENT
    # written into the bot's workspace, so their values are already recorded on
    # disk in that bot's tree. ``{{app_id}}`` has been bound to the manifest's
    # legacy ``id`` since files-pack shipped (``forge_engine`` binds it the same
    # way: ``job.app_id or manifest.id``); the canonical ``app_id`` of a stamped
    # manifest is its ``pkg_id``, so swapping would rewrite the substituted text
    # on the next drift re-copy and split each app's workspace files across two
    # identities. Reconciling the placeholder namespace is 1.4c work.
    pkg_id = (getattr(manifest, "pkg_id", "") or "").strip()
    placeholder_app_id = (getattr(manifest, "id", "") or "").strip()
    install_context = resolve_install_context(
        bot_id       = bot_id,
        bot_user     = bot_user,
        workspace    = str(workspace_dir),
        pkg_id       = pkg_id or "side-loaded",
        app_id       = placeholder_app_id,
        installed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        shared_dir   = str(shared_dir),
    )

    install_result = install_files_pack_to_workspace(
        pack_metadata,
        source.files_pack_dir,
        workspace_dir,
        install_context,
        allowed_paths = set(drifted),
    )

    pycache_cleared = _invalidate_pycache_under(workspace_dir, set(drifted))

    orphans = _detect_orphans(workspace_dir, pack_metadata, manifest)

    return WorkspaceSyncResult(
        skipped           = False,
        origin            = source.origin,
        source_path       = str(source.files_pack_dir),
        synced_count      = len(install_result.files_written),
        drifted_paths     = drifted,
        orphan_paths      = orphans,
        missing_in_source = missing_in_source,
        pycache_cleared   = pycache_cleared,
        errors            = list(install_result.errors),
    )


def _missing_declared_files(source_dir: Path, manifest) -> list[str]:
    """Files in `manifest.files[]` that don't exist on the source side.

    Reported regardless of drift so an operator can spot a manifest
    that references a stale or moved source path. Different from
    orphans (workspace-side leftover); these are source-side gaps.
    """
    missing: list[str] = []
    for entry in (getattr(manifest, "files", None) or []):
        if isinstance(entry, str):
            rel = entry
        elif isinstance(entry, dict):
            rel = (entry.get("path") or "").strip()
        else:
            continue
        if not rel:
            continue
        if not (source_dir / rel).is_file():
            missing.append(rel)
    return sorted(missing)

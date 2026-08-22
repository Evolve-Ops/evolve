"""
provenance.py — Embedded provenance marker system for the App Gallery & Forge engine.

Every artifact created by a forge run carries an embedded marker that links it back
to its owning app (pkg_id) and its stable file identity (file_id).  The marker format
is standardized per file type so a single parser can extract it from any artifact.

Marker format by file type
──────────────────────────
Python / Shell:   # evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1
Markdown:         <!-- evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1 -->
JSON:             {"_evolve": {"pkg": "p-a3f91c8b@...", "file": "f-d4e8f901@..."}, ...}
Plist (XML):      <!-- evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1 -->
Shell (cron):     embedded in job label: ai.openclaw.evolve.p-a3f91c8b.jobname

Multiple pkg_ids (shared files):
    # evolve: pkg=p-a3f91c8b@2026.04.15-1.3,p-b2e04d1a@2026.04.10-1.1 file=f-d4e8f901@2026.04.15.1

identity: see resolve_app_id — AL-1.4b swept this module and kept every
``pkg_id`` in it. This module DEFINES the marker namespace: ``pkg_id`` here is
whatever string a caller stamps into an artifact's ``_evolve`` marker
(``pkg=``/``spec=``), and every read (``pkg_ids``, ``pkg_refs``,
``pkg_version``, ``remove_pkg_id_from_marker``) is a lookup INSIDE that
already-written namespace, never a "which app is this manifest?" question —
no manifest is in scope here, only file bytes. What callers stamp is their
decision (the forge/extend path stamps a spec_id; see ``recon_ledger``); this
module must keep matching the literal text on disk, so it takes the id as a
parameter and resolves nothing.

"""

from __future__ import annotations

import json
import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from evolve_util import atomic_write_text as _atomic_write


# ── Marker pattern ────────────────────────────────────────────────────────────
#
# Matches: evolve: <keyword>=<ref>[,<ref>...] file=<ref>
# Where <keyword> ∈ {pkg, spec} and <ref> is: <id>@<version>  or  <id>.
#
# v6 markers use 'pkg=' (legacy "gallery package"); v7 markers use 'spec='
# (App Spec, see docs/spec-manifest-v7-2026-05-20.md §7). Both forms parse
# into the same ProvenanceMarker; the captured `keyword` field records which
# form was on disk so writers can preserve it on merge.

_MARKER_RE = re.compile(
    r'evolve:\s+(pkg|spec)=([^\s]+)\s+file=([^\s]+)'
)

# File extensions we know how to parse and embed markers in
_COMMENT_EXTS = {'.py', '.sh', '.bash', '.zsh', '.rb', '.pl'}
_XML_EXTS = {'.plist', '.xml'}
_MD_EXTS = {'.md', '.markdown', '.txt', '.rst'}
_JSON_EXTS = {'.json', '.jsonl'}

# Extensions to skip entirely during workspace scans
_SKIP_EXTS = {
    '.pyc', '.pyo', '.so', '.dylib', '.a', '.o',
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.pdf',
    '.zip', '.tar', '.gz', '.bz2', '.xz',
    '.DS_Store', '.gitkeep',
}

# Directories to skip during workspace scans
_SKIP_DIRS = {
    '.git', '__pycache__', '.venv', 'venv', 'node_modules',
    '.mypy_cache', '.pytest_cache', 'dist', 'build',
}


# ── File lifecycle states ─────────────────────────────────────────────────────

class FileLifecycle:
    """
    Lifecycle states for files tracked in the evolve system.

    OWNED    — marker present, all pkg_ids resolve to active manifests and this
               file's owned_by points to a living manifest.
    SHARED   — marker present, owned_by is active; one or more shared_with entries
               are also active.  The file is a shared dependency.
    ORPHANED — marker present but one or more pkg_ids in the marker no longer
               correspond to any known manifest.  The owning app was likely removed.
    UNOWNED  — no marker at all.  File predates the provenance system, was never
               claimed by any app, or had its marker stripped after its owner was
               removed and the file preserved (e.g. a data/state layer file).
    INELIGIBLE — marker present on a path an application may never legitimately
               own (platform telemetry, scanner state, OpenClaw-standard files;
               see applications.app_ownership_policy.can_app_own).  The marker
               is a stamping mistake to be SCRUBBED, never attached.  Recorded
               by the reconciliation ledger so the index captures WHY a marked
               file is a scrub candidate rather than an attach candidate.
    """
    OWNED      = "owned"
    SHARED     = "shared"
    ORPHANED   = "orphaned"
    UNOWNED    = "unowned"
    INELIGIBLE = "ineligible"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ProvenanceMarker:
    """
    Parsed provenance marker extracted from a file.

    pkg_refs:  List of "p-<hex>@<version>" strings (usually one, multiple for shared files).
    file_ref:  "f-<hex>@<version>" string.
    keyword:   Which form was on disk — 'pkg' (v6) or 'spec' (v7). Writers preserve this
               on merge so a v7 marker doesn't silently regress to v6 form.

    Semantic note: v7's App Spec subsumes v6's gallery package — every Spec serves
    the gallery-package role at install. `spec_refs` and `spec_ids` are aliases for
    `pkg_refs` / `pkg_ids` provided for v7 caller clarity.
    """
    pkg_refs: list[str] = field(default_factory=list)
    file_ref: str = ""
    keyword: str = "pkg"

    @property
    def pkg_ids(self) -> list[str]:
        """Return bare pkg_ids without version suffix."""
        return [r.split('@')[0] for r in self.pkg_refs]

    @property
    def spec_refs(self) -> list[str]:
        """v7 alias for pkg_refs."""
        return self.pkg_refs

    @property
    def spec_ids(self) -> list[str]:
        """v7 alias for pkg_ids."""
        return self.pkg_ids

    @property
    def file_id(self) -> str:
        """Return bare file_id without version suffix."""
        return self.file_ref.split('@')[0]

    @property
    def file_version(self) -> str | None:
        """Return the version portion of file_ref, or None if absent."""
        parts = self.file_ref.split('@', 1)
        return parts[1] if len(parts) == 2 else None

    def pkg_version(self, pkg_id: str) -> str | None:
        """Return the version for a specific pkg_id, or None if not found."""
        for ref in self.pkg_refs:
            parts = ref.split('@', 1)
            if parts[0] == pkg_id:
                return parts[1] if len(parts) == 2 else None
        return None

    def spec_version(self, spec_id: str) -> str | None:
        """v7 alias for pkg_version."""
        return self.pkg_version(spec_id)

    def is_valid(self) -> bool:
        return bool(self.pkg_refs) and bool(self.file_ref)


@dataclass
class WorkspaceFile:
    """A file found during a workspace scan with its extracted marker (if any)."""
    path: Path
    marker: ProvenanceMarker | None = None

    @property
    def has_marker(self) -> bool:
        return self.marker is not None and self.marker.is_valid()


# ── Marker formatting ─────────────────────────────────────────────────────────

def format_pkg_ref(pkg_id: str, pkg_version: str | None = None) -> str:
    """Format a single pkg reference: 'p-a3f91c8b' or 'p-a3f91c8b@2026.04.15-1.3'"""
    if pkg_version:
        return f"{pkg_id}@{pkg_version}"
    return pkg_id


def format_marker_string(
    pkg_ids: list[str],
    file_id: str,
    pkg_versions: dict[str, str] | None = None,
    file_version: str | None = None,
    keyword: str = "pkg",
) -> str:
    """
    Build the core marker string (without file-type wrapper).

    Args:
        pkg_ids:      List of pkg_ids that own/use this file.
        file_id:      The file's stable ID.
        pkg_versions: Optional dict of pkg_id → version string.
        file_version: Optional file version string.
        keyword:      'pkg' (v6, default — backwards compat) or 'spec' (v7).

    Returns:
        v6:  'evolve: pkg=p-a3f91c8b@2026.04.15-1.3 file=f-d4e8f901@2026.04.15.1'
        v7:  'evolve: spec=p-a3f91c8b@2026.05.20-1.0 file=f-d4e8f901@2026.05.20-1.0'
    """
    if keyword not in ("pkg", "spec"):
        raise ValueError(f"keyword must be 'pkg' or 'spec', got {keyword!r}")

    versions = pkg_versions or {}
    pkg_parts = []
    for pid in pkg_ids:
        ver = versions.get(pid)
        pkg_parts.append(format_pkg_ref(pid, ver))

    pkg_str = ",".join(pkg_parts)
    file_str = format_pkg_ref(file_id, file_version)
    return f"evolve: {keyword}={pkg_str} file={file_str}"


def _comment_line(marker_str: str) -> str:
    return f"# {marker_str}"


def _html_comment_line(marker_str: str) -> str:
    return f"<!-- {marker_str} -->"


# ── Marker parsing ─────────────────────────────────────────────────────────────

def _parse_marker_str(raw: str) -> ProvenanceMarker | None:
    """Extract a ProvenanceMarker from a raw string containing the marker."""
    m = _MARKER_RE.search(raw)
    if not m:
        return None
    keyword = m.group(1)  # 'pkg' or 'spec'
    pkg_str = m.group(2)
    file_str = m.group(3).rstrip(' -->')  # strip trailing comment close if present
    pkg_refs = [r.strip() for r in pkg_str.split(',') if r.strip()]
    if not pkg_refs or not file_str:
        return None
    return ProvenanceMarker(pkg_refs=pkg_refs, file_ref=file_str, keyword=keyword)


def parse_marker(file_path: Path) -> ProvenanceMarker | None:
    """
    Extract the evolve provenance marker from a file.

    Dispatches by file extension.  Returns None if the file has no marker,
    is unreadable, or is of an unsupported type.
    """
    try:
        suffix = file_path.suffix.lower()

        if suffix in _SKIP_EXTS:
            return None

        if suffix in _JSON_EXTS:
            return _parse_json_marker(file_path)

        if suffix in _XML_EXTS:
            return _parse_xml_or_comment_marker(file_path)

        # For everything else (Python, shell, Markdown, plain text, plist, etc.)
        # read the first few lines and look for the marker pattern
        return _parse_first_lines_marker(file_path)

    except (OSError, PermissionError, UnicodeDecodeError):
        return None


def _parse_first_lines_marker(file_path: Path, max_lines: int = 5) -> ProvenanceMarker | None:
    """Read the first N lines of a text file and look for the marker."""
    try:
        with file_path.open('r', encoding='utf-8', errors='replace') as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                marker = _parse_marker_str(line)
                if marker:
                    return marker
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _parse_json_marker(file_path: Path) -> ProvenanceMarker | None:
    """
    Read the '_evolve' top-level key from a JSON file.

    Accepts both 'pkg' (v6) and 'spec' (v7) sub-keys; the captured keyword
    records which form was on disk.
    """
    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return None
        evolve = data.get('_evolve')
        if not isinstance(evolve, dict):
            return None
        # Prefer 'spec' over 'pkg' if both present (v7 wins).
        if evolve.get('spec'):
            pkg_raw = evolve.get('spec', '')
            keyword = 'spec'
        else:
            pkg_raw = evolve.get('pkg', '')
            keyword = 'pkg'
        file_raw = evolve.get('file', '')
        if not pkg_raw or not file_raw:
            return None
        pkg_refs = [r.strip() for r in str(pkg_raw).split(',') if r.strip()]
        return ProvenanceMarker(pkg_refs=pkg_refs, file_ref=str(file_raw), keyword=keyword)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_xml_or_comment_marker(file_path: Path) -> ProvenanceMarker | None:
    """Scan first few lines of an XML/plist file for an HTML comment marker."""
    return _parse_first_lines_marker(file_path, max_lines=10)


# ── Marker embedding ──────────────────────────────────────────────────────────

def render_marked_text(
    file_path: Path,
    pkg_ids: list[str],
    file_id: str,
    pkg_versions: dict[str, str] | None = None,
    file_version: str | None = None,
    merge: bool = True,
    keyword: str = "pkg",
) -> str | None:
    """Compute the file's new full content with the provenance marker embedded,
    **without writing it**. Returns the new text, or ``None`` when the file
    cannot be read or is otherwise a no-op (mirrors what :func:`embed_marker`
    would silently skip).

    This is the pure read-and-compute half of :func:`embed_marker`, factored out
    so a privileged write path can compute the marked content as the ``evolve``
    user (which has ACL **read** on bot-owned files) and stage the result to
    ``/tmp`` for a narrow sudo helper to apply — see
    ``applications.marker_embed_helper``. Keeping the marker-formatting logic in
    one place means the staged content is byte-identical to what ``embed_marker``
    would have written in place. ``embed_marker`` is exactly this function plus an
    atomic write.

    Args mirror :func:`embed_marker`.
    """
    if merge:
        existing = parse_marker(file_path)
        if existing and existing.is_valid():
            # Merge: union of existing + new pkg_ids (preserve order, dedup)
            combined = list(dict.fromkeys(existing.pkg_ids + pkg_ids))
            # Merge versions: existing versions as base, new ones override
            merged_versions: dict[str, str] = {}
            for ref in existing.pkg_refs:
                parts = ref.split('@', 1)
                if len(parts) == 2:
                    merged_versions[parts[0]] = parts[1]
            if pkg_versions:
                merged_versions.update(pkg_versions)
            pkg_ids = combined
            pkg_versions = merged_versions if merged_versions else None
            # Preserve existing file_id when caller passes empty string
            # (e.g. when only adding a new pkg_id to a pre-registered file)
            if not file_id and existing.file_id:
                file_id = existing.file_id
            # Never regress v7 'spec' form to v6 'pkg' form on merge.
            if existing.keyword == "spec":
                keyword = "spec"

    marker_str = format_marker_string(pkg_ids, file_id, pkg_versions, file_version, keyword=keyword)
    suffix = file_path.suffix.lower()

    if suffix in _JSON_EXTS:
        return _render_json_marker(file_path, pkg_ids, file_id, pkg_versions, file_version, keyword=keyword)
    elif suffix in _XML_EXTS:
        return _render_line_marker(file_path, _html_comment_line(marker_str), is_xml=True)
    elif suffix in _MD_EXTS:
        return _render_line_marker(file_path, _html_comment_line(marker_str))
    else:
        # Python, shell, and everything else: use a # comment
        return _render_line_marker(file_path, _comment_line(marker_str))


def embed_marker(
    file_path: Path,
    pkg_ids: list[str],
    file_id: str,
    pkg_versions: dict[str, str] | None = None,
    file_version: str | None = None,
    merge: bool = True,
    keyword: str = "pkg",
) -> None:
    """
    Write or update the provenance marker in an existing file.

    The file must already exist.  This performs an atomic write (temp file → rename).
    If the file already has a marker on an early line, it is replaced in place.
    Otherwise the marker is prepended (after shebang for scripts).

    Args:
        file_path:    Path to the file to update.
        pkg_ids:      pkg_ids to record as owners/users of this file.
        file_id:      The file's stable ID.
        pkg_versions: Optional dict of pkg_id → version string.
        file_version: Optional file version string.
        merge:        When True (default), any pkg_ids already in the existing marker
                      are preserved — the new pkg_ids are *added* to the existing set.
                      When False, the existing marker is replaced entirely.  Use False
                      only when you intentionally want to reset all ownership (e.g.
                      after a full app rebuild that recomputes all shared_with refs).
        keyword:      'pkg' (v6, default) or 'spec' (v7). Under merge=True, if the
                      existing marker on disk uses 'spec', that takes precedence —
                      a v7 marker never silently regresses to v6 form on update.

    The in-place write here only succeeds where the caller can write the file
    (evolve-owned paths, e.g. ``workspace/evolve/``). For bot-owned paths the
    ``evolve`` admin user has ACL read but not write — those go through
    :func:`render_marked_text` + the ``marker_embed_helper`` sudo path instead.
    """
    new_text = render_marked_text(
        file_path, pkg_ids, file_id, pkg_versions, file_version,
        merge=merge, keyword=keyword,
    )
    if new_text is None:
        return
    _atomic_write(file_path, new_text, mode=0o644)


def _render_line_marker(file_path: Path, marker_line: str, is_xml: bool = False) -> str | None:
    """
    Compute the new content for a text file with a marker line embedded.

    Strategy:
    1. If the file already contains an evolve: marker in the first 5 lines, replace it.
    2. Otherwise, prepend the marker after any shebang line (#!/...).

    Returns the new full text, or ``None`` if the file cannot be read.
    """
    try:
        content = file_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None

    lines = content.splitlines(keepends=True)
    marker_line_full = marker_line + '\n'

    # Try to find and replace existing marker in first 5 lines
    for i, line in enumerate(lines[:5]):
        if _MARKER_RE.search(line):
            lines[i] = marker_line_full
            return ''.join(lines)

    # No existing marker — prepend after shebang if present
    insert_at = 0
    if lines and lines[0].startswith('#!'):
        insert_at = 1

    # For XML/plist, insert after the XML declaration if present
    if is_xml and lines:
        if lines[0].strip().startswith('<?xml'):
            insert_at = 1

    lines.insert(insert_at, marker_line_full)
    return ''.join(lines)


def _render_json_marker(
    file_path: Path,
    pkg_ids: list[str],
    file_id: str,
    pkg_versions: dict[str, str] | None,
    file_version: str | None,
    keyword: str = "pkg",
) -> str | None:
    """Compute the new content for a JSON file with the _evolve key updated.

    Honors keyword='pkg' or 'spec'. Returns the new full text, or ``None`` if
    the file cannot be read/parsed or is not a JSON object.
    """
    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    versions = pkg_versions or {}
    pkg_val = ",".join(
        format_pkg_ref(pid, versions.get(pid)) for pid in pkg_ids
    )
    file_val = format_pkg_ref(file_id, file_version)

    data['_evolve'] = {keyword: pkg_val, 'file': file_val}

    # Preserve key order: _evolve first
    ordered = {'_evolve': data.pop('_evolve')}
    ordered.update(data)

    return json.dumps(ordered, indent=2) + '\n'


# ── Marker removal ────────────────────────────────────────────────────────────

def remove_pkg_id_from_marker(file_path: Path, pkg_id: str) -> bool:
    """
    Remove a single pkg_id from a file's embedded marker.

    If *pkg_id* was the only owner, the marker is stripped entirely (leaving the
    file content intact).  Callers that care about data-layer files should check
    the file's layer before calling this and decide whether to delete or preserve
    the now-unowned file.

    Returns True if the file was modified, False if *pkg_id* was not found in the
    marker or the file could not be read/written.
    """
    existing = parse_marker(file_path)
    if not existing or not existing.is_valid():
        return False
    if pkg_id not in existing.pkg_ids:
        return False

    remaining_ids = [pid for pid in existing.pkg_ids if pid != pkg_id]

    if not remaining_ids:
        # Last owner removed — strip the marker entirely
        return _strip_marker(file_path)

    # Rebuild versions dict without the removed pkg_id
    remaining_versions: dict[str, str] = {
        ref.split('@')[0]: ref.split('@')[1]
        for ref in existing.pkg_refs
        if '@' in ref and ref.split('@')[0] != pkg_id
    }
    embed_marker(
        file_path,
        pkg_ids=remaining_ids,
        file_id=existing.file_id,
        pkg_versions=remaining_versions or None,
        file_version=existing.file_version,
        merge=False,  # We've already computed the correct final state
        # Preserve the on-disk keyword form: with merge=False the default
        # keyword="pkg" would silently regress a v7 'spec=' marker to v6 'pkg='
        # form on a partial id removal (violating embed_marker's documented
        # "never silently regress" invariant). Carry the parsed form forward.
        keyword=existing.keyword,
    )
    return True


def strip_marker(file_path: Path) -> bool:
    """
    Public entry point: strip the entire ``evolve:`` provenance marker from a file.

    Thin wrapper over :func:`_strip_marker` (which dispatches across the text and
    JSON marker forms). This is the **scrub primitive** the reconciliation UI
    calls on a ``SCRUB_CANDIDATE`` — a marker stamped on a path no application may
    legitimately own (platform telemetry, OpenClaw-standard files like AGENTS.md,
    or a marker whose owning app is gone). The file's *content* is preserved; only
    the marker is removed.

    Returns True if a marker was removed, False if the file had none (idempotent
    no-op). Write errors (notably ``PermissionError`` when the ``evolve`` user
    lacks ACL write on a bot-owned path) propagate so callers can fall back to a
    bot-user write path.
    """
    return _strip_marker(file_path)


def _strip_marker(file_path: Path) -> bool:
    """
    Remove the evolve: provenance marker from a file entirely.

    Handles all file types:
    - Text files (Python, shell, Markdown, etc.) — removes the marker line.
    - JSON files — removes the '_evolve' top-level key.

    Returns True if the file was modified, False on any read/write error.
    """
    suffix = file_path.suffix.lower()

    if suffix in _JSON_EXTS:
        return _strip_json_marker(file_path)

    # Text file: remove the first line that contains the marker
    try:
        content = file_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return False

    lines = content.splitlines(keepends=True)
    new_lines = []
    stripped = False
    for i, line in enumerate(lines):
        if not stripped and i < 5 and _MARKER_RE.search(line):
            stripped = True  # drop this line
        else:
            new_lines.append(line)

    if not stripped:
        return False

    _atomic_write(file_path, ''.join(new_lines), mode=0o644)
    return True


def _strip_json_marker(file_path: Path) -> bool:
    """Remove the '_evolve' key from a JSON file."""
    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False

    if not isinstance(data, dict) or '_evolve' not in data:
        return False

    del data['_evolve']
    _atomic_write(file_path, json.dumps(data, indent=2) + '\n', mode=0o644)
    return True


# ── Workspace scanning ────────────────────────────────────────────────────────

def scan_workspace_for_markers(workspace: Path) -> list[WorkspaceFile]:
    """
    Walk a workspace directory tree, extracting provenance markers from every
    supported file encountered.

    Returns a list of WorkspaceFile objects (all files, with marker=None for
    files that have no marker or are of an unsupported type).

    Skips: _SKIP_DIRS directories, _SKIP_EXTS file extensions, symlinks.
    """
    results: list[WorkspaceFile] = []
    for wf in _walk_workspace(workspace):
        marker = parse_marker(wf)
        results.append(WorkspaceFile(path=wf, marker=marker))
    return results


def scan_workspace_marked_only(workspace: Path) -> list[WorkspaceFile]:
    """
    Like scan_workspace_for_markers but returns only files that have a valid marker.
    Faster for orphan detection and consistency checks.
    """
    results: list[WorkspaceFile] = []
    for wf in _walk_workspace(workspace):
        marker = parse_marker(wf)
        if marker and marker.is_valid():
            results.append(WorkspaceFile(path=wf, marker=marker))
    return results


def _walk_workspace(workspace: Path) -> Iterator[Path]:
    """Yield file paths under workspace, respecting skip lists."""
    if not workspace.exists():
        return
    for root, dirs, files in os.walk(workspace):
        # Prune skip directories in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith('.')]
        for fname in files:
            fp = Path(root) / fname
            if fp.is_symlink():
                continue
            if fp.suffix.lower() in _SKIP_EXTS:
                continue
            yield fp


# ── Consistency helpers ───────────────────────────────────────────────────────

def check_file_consistency(
    registered_files: list[dict],
    workspace: Path,
) -> list[dict]:
    """
    Cross-check the manifest's file registry against embedded markers on disk.

    Args:
        registered_files: List of file dicts from manifest.files (v5 format).
        workspace:        Root of the bot's workspace.

    Returns:
        List of inconsistency dicts, each with keys:
            file_id, path, issue, expected, found
    """
    issues = []

    for entry in registered_files:
        if isinstance(entry, str):
            # v4 manifest — string paths, no provenance to check
            continue

        file_id = entry.get('file_id', '')
        rel_path = entry.get('path', '')
        expected_version = entry.get('file_version', '')
        owned_by = entry.get('owned_by', '')

        # F-C1: intentionally NOT routed through ws_rel_key. ``Path.__truediv__``
        # resets to the right operand when it is absolute, so ``workspace /
        # rel_path`` checks the real on-disk location whether ``rel_path`` is the
        # workspace-relative (migrate_v7/v13) or absolute (extend_application)
        # form — this existence probe is already robust to both. (The scanner /
        # reconciliation joins that DID need ws_rel_key compared a derived key
        # against a set, where the absolute/relative mismatch caused a miss.)
        fp = workspace / rel_path if rel_path else None

        # 1. File exists on disk?
        if not fp or not fp.exists():
            issues.append({
                'file_id': file_id,
                'path': rel_path,
                'issue': 'missing_from_disk',
                'expected': rel_path,
                'found': None,
            })
            continue

        marker = parse_marker(fp)

        # 2. Has a marker?
        if not marker or not marker.is_valid():
            issues.append({
                'file_id': file_id,
                'path': rel_path,
                'issue': 'no_marker',
                'expected': file_id,
                'found': None,
            })
            continue

        # 3. file_id matches?
        if marker.file_id != file_id:
            issues.append({
                'file_id': file_id,
                'path': rel_path,
                'issue': 'file_id_mismatch',
                'expected': file_id,
                'found': marker.file_id,
            })

        # 4. owned_by pkg_id is in marker?
        if owned_by and owned_by not in marker.pkg_ids:
            issues.append({
                'file_id': file_id,
                'path': rel_path,
                'issue': 'ownership_drift',
                'expected': owned_by,
                'found': marker.pkg_ids,
            })

        # 5. Version matches? (warning, not error — out-of-band edits are allowed)
        if expected_version and marker.file_version and marker.file_version != expected_version:
            issues.append({
                'file_id': file_id,
                'path': rel_path,
                'issue': 'version_drift',
                'expected': expected_version,
                'found': marker.file_version,
            })

    return issues

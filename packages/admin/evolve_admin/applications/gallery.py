"""
gallery.py — App Gallery I/O for the Evolve App Gallery & Forge system.

Handles reading builtin packages from the repo, imported packages from the
shared directory, detecting available updates, and building delta specs that
describe what changed between two package versions.

Gallery layout
──────────────
Builtin gallery (ships with the evolve repo):
    {repo_root}/gallery/index.json
    {repo_root}/gallery/{name}/{pkg_id}.json

Imported gallery (operator-uploaded packages):
    {shared_dir}/gallery/imported/{pkg_id}.json

The repo root is computed from this file's location:
    packages/admin/evolve_admin/applications/gallery.py
    → repo root is 5 parents up from __file__.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from .ids import compare_pkg_versions, is_major_bump
from .manifest import ApplicationManifest, list_manifests


# ── Repo root resolution ───────────────────────────────────────────────────────

# This file lives at:
#   packages/admin/evolve_admin/applications/gallery.py
# Repo root is 4 levels up from this file's parent directory:
#   applications/ → evolve_admin/ → admin/ → packages/ → repo root
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]

_BUILTIN_GALLERY_INDEX = _REPO_ROOT / "gallery" / "index.json"
_BUILTIN_GALLERY_DIR = _REPO_ROOT / "gallery"

# Required fields for any valid gallery package manifest
# identity: see resolve_app_id — AL-1.4b sweep note for this WHOLE module.
# ``gallery.py`` is the gallery CATALOG, and the catalog's key is ``pkg_id``
# (``p-<8hex>``), enforced by ``_PKG_ID_RE`` and used as the on-disk filename in
# ``gallery/<app-name>/<pkg_id>.json``, ``{shared_dir}/gallery/imported/
# <pkg_id>.json``, and as the key of the ``LEGACY_KEY_MIGRATION`` /
# ``REMOVED_PKG_MIGRATIONS`` alias tables. Every ``pkg_id`` read below is
# therefore semantically required and stays; individual sites carry the reason
# where it is not obvious from the two lines around them.
#
# Two facts that make this concrete, both verified on this base:
#   * A ``gallery/index.json`` ROW carries an ``app_id`` key of its own, holding
#     the app SCRIPT name (``app_task_manager``) rather than the package key —
#     15 of 15 builtin rows, 0 of 15 matching ``APP_ID_PATTERN``. Since PR #3681
#     the ``app_id`` field only wins when it is a conforming slug, so
#     ``resolve_app_id`` on an index row now falls through the legacy chain and
#     returns exactly the row's ``pkg_id``. The annotations here are therefore
#     "the key is semantically required", NOT "the resolver returns something
#     else" — that second hazard was closed by #3681 and is pinned by
#     ``test_al_1_4b_forge_install_export_identity.py``.
#   * For an INSTALLED manifest the resolver returns ``pkg_id`` when one is
#     present (it leads the legacy chain) but falls back to the manifest stem
#     when it is not — so it cannot be used where the empty case means
#     "not from the gallery" (AL-1.4 §6 delta 12).
_REQUIRED_PKG_FIELDS = {"pkg_id", "name", "display_name", "objective", "build_spec"}
_PKG_ID_RE = re.compile(r"^p-[0-9a-f]{8}$")


# ── Retired-pkg → surviving-pkg mapping ─────────────────────────────────────
#
# When two gallery packages are merged into one (the merged spec lives at
# the surviving pkg_id; the retired pkg_id stops shipping a manifest),
# existing installs of the retired package still carry its old pkg_id in
# their installed manifest. ``load_gallery_package`` resolves the retired
# id through this map so update-detection, snapshot retrieval, and the
# install dispatcher all transparently land on the merged spec.
#
# Shape mirrors ``alerts.catalog.LEGACY_KEY_MIGRATION``: deprecated key →
# surviving key. The surviving key MUST resolve to a real package in the
# builtin or imported gallery.
#
# Entries should be removed only when no installed bot still references
# the retired pkg_id (i.e. all installs have been re-snapshot under the
# surviving id) — keeping a retired entry indefinitely is cheap; removing
# it prematurely strands old installs.
LEGACY_KEY_MIGRATION: dict[str, str] = {
    # Merged 2026-06-05: Unified Task System (p-c20a5564, exported from
    # team-bot-a) consolidated into Task Manager (p-9bfa1c84). The merged
    # spec inherits Unified Task System's substance; the older Task
    # Manager pkg_id is preserved so existing installs auto-upgrade.
    # Rationale: internal/gallery-merge-task-manager-2026-06-05.md.
    "p-c20a5564": "p-9bfa1c84",
}


# ── F-P.12 — provenance axis (trust source of a gallery package) ────────────
#
# Distinct from the existing ``source`` field (which records storage
# location — "builtin" vs "imported"). The ``provenance`` field is the
# TRUST axis the install UI uses to decide whether to gate the install
# behind a confirmation modal.
#
#   evolve         — ships with this repo. Canonical, trusted.
#   community      — imported from an external operator. Worth a
#                    confirmation before install (unknown code, unknown
#                    intent).
#   operator-local — built/imported by THIS operator. Trusted as much as
#                    anything the operator themselves wrote.
#
# Spec: internal/note-smart-forge-and-file-provenance-2026-06-04.md flags
# this as the second axis of the gallery app classification.

GALLERY_PROVENANCE_EVOLVE = "evolve"
GALLERY_PROVENANCE_COMMUNITY = "community"
GALLERY_PROVENANCE_OPERATOR_LOCAL = "operator-local"

_VALID_PROVENANCE = frozenset({
    GALLERY_PROVENANCE_EVOLVE,
    GALLERY_PROVENANCE_COMMUNITY,
    GALLERY_PROVENANCE_OPERATOR_LOCAL,
})


def resolve_gallery_provenance(pkg_dict: dict, *, is_builtin: bool) -> str:
    """Compute the provenance for a gallery package.

    Priority order:

      1. Explicit ``provenance`` field on the package JSON (operator-
         set during publish / share). Any value not in
         ``_VALID_PROVENANCE`` is ignored.
      2. Fallback by storage location:
           - ``is_builtin=True``  → ``"evolve"`` (canonical gallery)
           - ``is_builtin=False`` → ``"community"`` (imported from
             somewhere external)

    Operator-local is reserved for explicit declarations — there's no
    automatic path to it today. Future "share with my pod only" flow
    would stamp packages with ``provenance: "operator-local"`` when
    they're imported from a sibling bot in the same pod.

    Args:
        pkg_dict: the raw gallery package manifest dict.
        is_builtin: True when this package was loaded from the
            in-repo gallery dir; False when loaded from
            ``{shared_dir}/gallery/imported/``.

    Returns: one of ``GALLERY_PROVENANCE_*`` constants.
    """
    explicit = (pkg_dict.get("provenance") or "").strip()
    if explicit in _VALID_PROVENANCE:
        return explicit
    return (
        GALLERY_PROVENANCE_EVOLVE if is_builtin
        else GALLERY_PROVENANCE_COMMUNITY
    )


# ── F-P.12.d — contributor identity ─────────────────────────────────────────
#
# Optional dict on community-sourced gallery packages identifying who
# shared it. Surfaced in the F-P.12.c install confirmation gate so
# operators can decide based on the contributor, not just the trust tier.
#
#   {
#     "name":   "Alex Developer",    # required when contributor is set
#     "handle": "alex_dev",          # optional (e.g. GitHub username)
#     "url":    "https://...",       # optional (homepage / repo / contact)
#   }
#
# evolve-sourced packages don't need a contributor block (Evolve is
# the implicit contributor). operator-local doesn't need one either
# (the operator themselves is the implicit contributor). Only
# community-sourced packages benefit from explicit attribution.


def normalize_contributor(raw: object) -> dict:
    """Normalize a raw contributor field to a sanitized dict.

    Returns ``{}`` when the input is missing, malformed, or has no
    usable ``name`` field. Otherwise returns a dict with only the
    recognized keys (``name``, ``handle``, ``url``), each stripped to
    a string. This makes downstream rendering safe — UI can render
    ``contributor.name`` without null-checks.

    The strict ``name``-or-nothing rule prevents an "imported from
    anonymous unknown" half-state in the install confirmation gate.
    """
    if not isinstance(raw, dict):
        return {}
    name = (raw.get("name") or "").strip() if isinstance(raw.get("name"), str) else ""
    if not name:
        return {}
    out: dict[str, str] = {"name": name}
    handle = raw.get("handle")
    if isinstance(handle, str) and handle.strip():
        out["handle"] = handle.strip()
    url = raw.get("url")
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
    return out


# ── Path helpers ───────────────────────────────────────────────────────────────

def _imported_gallery_dir(shared_dir: Path) -> Path:
    return shared_dir / "gallery" / "imported"


def _imported_pkg_path(pkg_id: str, shared_dir: Path) -> Path:
    return _imported_gallery_dir(shared_dir) / f"{pkg_id}.json"


# ── Builtin gallery readers ────────────────────────────────────────────────────

def _load_builtin_index() -> list[dict]:
    """Load the builtin gallery index. Returns [] on any error."""
    try:
        return json.loads(_BUILTIN_GALLERY_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def find_files_pack_dir(pkg_id: str) -> Path | None:
    """Locate ``gallery/<app-name>/files/`` for ``pkg_id`` in the
    builtin gallery checkout.

    Spec: internal/spec-files-pack-hybrid-2026-06-03.md §4.

    Returns the directory path when present (whether or not it
    contains a manifest.json — that's the caller's job to check via
    ``load_files_pack_metadata``). Returns ``None`` when no app
    directory contains a package matching ``pkg_id`` or when there's
    no ``files/`` subdirectory next to the package manifest.

    Used by forge's install dispatcher to decide between the fast
    files-pack path and the LLM-forge fallback.
    """
    try:
        for app_dir in _BUILTIN_GALLERY_DIR.iterdir():
            if not app_dir.is_dir():
                continue
            if not (app_dir / f"{pkg_id}.json").exists():
                continue
            fp_dir = app_dir / "files"
            if fp_dir.is_dir():
                return fp_dir
            return None  # package found, but no files-pack
    except Exception:
        pass
    return None


def _load_builtin_pkg(pkg_id: str) -> dict | None:
    """
    Search the builtin gallery directory for a package by pkg_id.

    The builtin gallery organises packages as:
        gallery/{app-name}/{pkg_id}.json
    We search every subdirectory for a file whose stem matches pkg_id.
    """
    try:
        for app_dir in _BUILTIN_GALLERY_DIR.iterdir():
            if not app_dir.is_dir():
                continue
            candidate = app_dir / f"{pkg_id}.json"
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _load_imported_pkg(pkg_id: str, shared_dir: Path) -> dict | None:
    """Load a single imported package manifest. Returns None on any error."""
    path = _imported_pkg_path(pkg_id, shared_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_imported_pkgs(shared_dir: Path) -> list[dict]:
    """Load all imported packages. Silently skips unreadable files."""
    d = _imported_gallery_dir(shared_dir)
    if not d.exists():
        return []
    pkgs = []
    try:
        for f in sorted(d.iterdir()):
            if f.suffix == ".json" and f.stem.startswith("p-"):
                try:
                    pkgs.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
    except Exception:
        pass
    return pkgs


# ── Public API ─────────────────────────────────────────────────────────────────

def list_gallery_packages(shared_dir: Path, bot_ids: list[str]) -> list[dict]:
    """
    Return all gallery packages (builtin + imported) enriched with installation info.

    Each returned dict is the raw gallery package record with two extra fields:
        installed_on : list[str]  — bot_ids that currently have this pkg_id installed
        source       : str        — "builtin" or "imported"

    The function loads installed manifests for all *bot_ids* to build the
    installation map.  Silently ignores unreadable manifests.

    Args:
        shared_dir: Path to the shared evolve data directory.
        bot_ids:    List of bot identifiers to check for installations.

    Returns:
        List of package dicts, builtin packages first then imported.
    """
    # Build a map of pkg_id → list of bot_ids that have it installed
    installed_map: dict[str, list[str]] = {}
    for bot_id in bot_ids:
        try:
            for manifest in list_manifests(shared_dir, bot_id):
                # identity: see resolve_app_id — catalog key on both sides: this
                # map is probed below with the gallery record's ``pkg_id``. The
                # truthiness check is "this install came from a package at all".
                if manifest.pkg_id:
                    installed_map.setdefault(manifest.pkg_id, []).append(bot_id)
        except Exception:
            pass

    packages: list[dict] = []

    # Builtin packages from the gallery index. The index carries denormalised
    # `application_tags` since the 2026-05-31 backfill (re-generated by
    # scripts/backfill_application_tags.py --rebuild-index) so we don't have
    # to crack open every per-app JSON to populate the UI's tag display.
    # `tags` is mirrored as an alias because the admin UI's renderGallery
    # has been reading `p.tags` since before the field landed in the index;
    # both keys are kept in sync so neither call site silently empties.
    for entry in _load_builtin_index():
        # identity: see resolve_app_id — ``gallery/index.json`` row key. Post
        # #3681 the resolver returns this same string for these rows (their own
        # ``app_id`` is the non-conforming script name); reading the field
        # directly keeps the ``installed_map`` lookup keyed the way the map was
        # built, independent of a future conforming ``app_id`` landing on a row.
        pkg_id = entry.get("pkg_id", "")
        record = dict(entry)
        record["source"] = "builtin"
        # F-P.12.a: trust axis. Defaults to "evolve" for builtin; the
        # operator can override via an explicit ``provenance`` field
        # on the package JSON (e.g. for a community contribution that
        # was vendored into the gallery dir but should still surface
        # with community-trust UI).
        record["provenance"] = resolve_gallery_provenance(
            record, is_builtin=True,
        )
        # F-P.12.d: contributor identity (normalized — empty dict
        # when not declared or malformed).
        record["contributor"] = normalize_contributor(record.get("contributor"))
        record["installed_on"] = installed_map.get(pkg_id, [])
        tags = list(record.get("application_tags") or [])
        record["application_tags"] = tags
        record["tags"] = tags
        packages.append(record)

    # Imported packages — full manifest dicts (not the thin index shape),
    # so `application_tags` is already populated when present.
    for pkg in _list_imported_pkgs(shared_dir):
        # identity: see resolve_app_id — imported-package catalog key, as above.
        pkg_id = pkg.get("pkg_id", "")
        record = dict(pkg)
        record["source"] = "imported"
        # F-P.12.a: imported packages default to "community" trust
        # (came from somewhere external). Explicit "operator-local"
        # override available for the "I built this myself, just
        # storing it in the imported dir" case.
        record["provenance"] = resolve_gallery_provenance(
            record, is_builtin=False,
        )
        # F-P.12.d: contributor identity.
        record["contributor"] = normalize_contributor(record.get("contributor"))
        record["installed_on"] = installed_map.get(pkg_id, [])
        tags = list(record.get("application_tags") or [])
        record["application_tags"] = tags
        record["tags"] = tags
        packages.append(record)

    return packages


def load_tags_index(shared_dir: Path | None = None) -> dict[str, list[str]]:
    """Return the reverse tag index for the builtin gallery, mapping
    `tag → [pkg_id, ...]`.

    Loaded from `gallery/tags-index.json` (regenerated by
    `scripts/backfill_application_tags.py --rebuild-index`). Returns
    `{}` if the file is missing or unreadable so callers can treat it
    as "no filterable tags" without branching.

    The `shared_dir` parameter is reserved for a future per-pod imported
    tag index; today only the builtin index is consulted.
    """
    path = _BUILTIN_GALLERY_DIR / "tags-index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): [str(p) for p in v]
        for k, v in data.items()
        if isinstance(v, list)
    }


def load_gallery_package(pkg_id: str, shared_dir: Path) -> dict | None:
    """
    Load a full gallery package manifest by pkg_id.

    Checks the builtin gallery first; falls back to the imported gallery.
    Returns None if not found in either location.

    Retired pkg_ids in ``LEGACY_KEY_MIGRATION`` resolve to their
    surviving merged package — existing installs of a retired app see
    the merged spec as an update (via ``get_update_status``) and the
    install dispatcher routes their re-installs to the surviving id.

    Args:
        pkg_id:     The stable package identifier (e.g. "p-a3f91c8b").
        shared_dir: Path to the shared evolve data directory.

    Returns:
        Package manifest dict, or None if not found.
    """
    resolved = LEGACY_KEY_MIGRATION.get(pkg_id, pkg_id)
    pkg = _load_builtin_pkg(resolved)
    if pkg is not None:
        return pkg
    return _load_imported_pkg(resolved, shared_dir)


def import_package(pkg_json: dict, shared_dir: Path) -> tuple[bool, str]:
    """
    Validate and save an operator-uploaded package manifest to the imported gallery.

    Validation rules:
    - ``pkg_id`` must be present and match the ``p-<8hex>`` pattern.
    - The package must have all required fields: name, display_name, objective, build_spec.

    Writes atomically to ``{shared_dir}/gallery/imported/{pkg_id}.json``.

    Args:
        pkg_json:   The package manifest dict (already parsed from JSON).
        shared_dir: Path to the shared evolve data directory.

    Returns:
        ``(True, "")`` on success, ``(False, reason)`` on validation failure or I/O error.
    """
    # Validate pkg_id
    # identity: see resolve_app_id — the catalog key being VALIDATED against
    # ``_PKG_ID_RE`` (``p-<8hex>``) and then used as the import filename. This
    # is the write side of the catalog contract, not an identity read.
    pkg_id = pkg_json.get("pkg_id", "")
    if not isinstance(pkg_id, str) or not _PKG_ID_RE.match(pkg_id):
        return False, f"Invalid or missing pkg_id: {pkg_id!r}"

    # Validate required fields
    missing = _REQUIRED_PKG_FIELDS - set(pkg_json.keys())
    if missing:
        return False, f"Missing required fields: {', '.join(sorted(missing))}"

    # Ensure non-empty values for string fields
    for field in _REQUIRED_PKG_FIELDS:
        val = pkg_json.get(field, "")
        if not val:
            return False, f"Field {field!r} must not be empty"

    try:
        path = _imported_pkg_path(pkg_id, shared_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, pkg_json)
    except Exception as exc:
        return False, f"Failed to save package: {exc}"

    return True, ""


# ── Removed gallery packages (migration map) ───────────────────────────────────
#
# When a gallery app is retired because Evolve grew the same capability as
# built-in functionality, its pkg_id stays in this map so existing installs
# surface a clear "this is now built-in to Evolve — uninstall the gallery
# copy" message instead of silently appearing up-to-date when the gallery
# resolver can't find the package.
#
# Same shape and intent as ``alerts.catalog.LEGACY_KEY_MIGRATION``: a single
# source of truth that the gallery consistency check, the gallery UI, and
# tests all consult.
#
# To retire a gallery app:
#   1. Delete ``gallery/<name>/<pkg_id>.json`` and its directory.
#   2. Drop the entry from ``gallery/index.json`` and ``gallery/tags-index.json``.
#   3. Add an entry below with ``display_name``, ``replaced_by`` (short label
#      for what now covers it), and ``reason`` (one-line explanation).
#   4. Document the removal in ``internal/gallery-removals-*.md``.

REMOVED_PKG_MIGRATIONS: dict[str, dict[str, str]] = {
    # 2026-06-05 — duplicated Evolve's built-in per-bot backup.py + admin-UI
    # HTTPS+PAT remote discovery/rotation flow. Two paths for one capability
    # confused operators about which to configure.
    "p-f9bce546": {
        "display_name": "Workspace Backup",
        "replaced_by": "Evolve built-in workspace backup (per-bot backup.py)",
        "reason": (
            "Evolve already runs daily git backup per bot via backup.py "
            "and exposes HTTPS+PAT remote discovery/rotation in the "
            "admin UI's integrations page."
        ),
    },
    # 2026-06-05 — duplicated Evolve's built-in three-purpose GitHub
    # integration (per-bot self-backup, general code access via MCP,
    # pod-wide developer issue tracking).
    "p-f047a60f": {
        "display_name": "GitHub Integration",
        "replaced_by": "Evolve built-in GitHub integration (admin UI → Integrations)",
        "reason": (
            "Evolve already wires GitHub in via the integrations page for "
            "all three purposes (per-bot self-backup, MCP code access, "
            "pod-wide developer issue tracking)."
        ),
    },
}


def get_removed_status(
    installed_manifest: ApplicationManifest,
) -> dict | None:
    """Return a removal-status dict if the installed app's pkg_id has been
    retired from the gallery, otherwise None.

    The returned dict carries enough context for the consistency endpoint
    and the gallery UI to render an operator-facing "this is now built-in
    to Evolve — uninstall the gallery copy" message. See
    ``REMOVED_PKG_MIGRATIONS`` above for the registry.
    """
    # identity: see resolve_app_id — catalog key: it is looked up in the
    # ``REMOVED_PKG_MIGRATIONS`` alias table, which is keyed on retired
    # ``pkg_id``s. An install with no ``pkg_id`` never came from the gallery and
    # so can never have a removal migration.
    pkg_id = installed_manifest.pkg_id
    if not pkg_id:
        return None
    entry = REMOVED_PKG_MIGRATIONS.get(pkg_id)
    if entry is None:
        return None
    return {
        "pkg_id": pkg_id,
        "display_name": entry["display_name"],
        "replaced_by": entry["replaced_by"],
        "reason": entry["reason"],
        "message": (
            f"{entry['display_name']} is now built-in to Evolve. "
            f"Uninstall the gallery copy; {entry['replaced_by']} covers it."
        ),
    }


def get_update_status(
    installed_manifest: ApplicationManifest,
    shared_dir: Path,
) -> dict | None:
    """
    Check whether the gallery has a newer version of an installed app.

    Compares the installed app's ``pkg_version`` against the gallery package's
    ``pkg_version``.  Returns a status dict when an update is available, or
    None if the app is up-to-date, has no ``pkg_id``, or the gallery package
    cannot be found.

    Args:
        installed_manifest: The installed app's ApplicationManifest.
        shared_dir:         Path to the shared evolve data directory.

    Returns:
        Dict with keys ``pkg_id``, ``current_version``, ``gallery_version``,
        ``is_breaking`` when an update is available; otherwise None.
    """
    # identity: see resolve_app_id — catalog key: fed to
    # ``load_gallery_package`` to compare installed vs gallery version. The
    # falsy guard is the documented "has no ``pkg_id``" no-update case.
    if not installed_manifest.pkg_id:
        return None

    gallery_pkg = load_gallery_package(installed_manifest.pkg_id, shared_dir)
    if not gallery_pkg:
        return None

    gallery_ver = gallery_pkg.get("pkg_version", "")
    current_ver = installed_manifest.pkg_version

    if not gallery_ver:
        return None

    if compare_pkg_versions(current_ver, gallery_ver) >= 0:
        # Already at or ahead of gallery version
        return None

    return {
        "pkg_id": installed_manifest.pkg_id,
        "current_version": current_ver,
        "gallery_version": gallery_ver,
        "is_breaking": is_breaking_update(
            {"pkg_version": current_ver},
            gallery_pkg,
        ),
    }


def build_delta_spec(old_pkg: dict, new_pkg: dict) -> dict:
    """
    Diff two package dicts and produce a structured delta spec.

    The delta spec describes what changed between an installed version of a
    gallery app and the updated gallery version.  It is passed to the forge
    engine as context so the builder can generate a targeted delta rather than
    a full rebuild.

    Args:
        old_pkg: The previously installed gallery package dict.
        new_pkg: The new (updated) gallery package dict.

    Returns:
        Dict with keys:
            from_version          : str   — old pkg_version
            to_version            : str   — new pkg_version
            build_spec_changed    : bool  — True if build_spec text differs
            exported_hooks_changed: bool  — True if exported_hooks list differs
            new_files             : list  — files in new_pkg.files not in old_pkg.files
            summary               : str   — one-line human summary
            migration_instructions: str   — guidance for the forge run
    """
    from_version = old_pkg.get("pkg_version", "")
    to_version = new_pkg.get("pkg_version", "")

    build_spec_changed = old_pkg.get("build_spec", "") != new_pkg.get("build_spec", "")

    old_hooks = set(old_pkg.get("exported_hooks") or [])
    new_hooks = set(new_pkg.get("exported_hooks") or [])
    exported_hooks_changed = old_hooks != new_hooks

    old_files = set(old_pkg.get("files") or [])
    new_files_all = set(new_pkg.get("files") or [])
    new_files = sorted(new_files_all - old_files)

    # Build a human-readable summary
    changes: list[str] = []
    if build_spec_changed:
        changes.append("build spec updated")
    if exported_hooks_changed:
        added = new_hooks - old_hooks
        removed = old_hooks - new_hooks
        parts = []
        if added:
            parts.append(f"{len(added)} hook(s) added")
        if removed:
            parts.append(f"{len(removed)} hook(s) removed")
        changes.append("exported hooks: " + ", ".join(parts) if parts else "exported hooks changed")
    if new_files:
        changes.append(f"{len(new_files)} new file(s)")

    if changes:
        summary = f"Update {from_version} → {to_version}: " + "; ".join(changes)
    else:
        summary = f"Update {from_version} → {to_version}: metadata only"

    # Build migration instructions
    instructions_parts: list[str] = []
    if build_spec_changed:
        instructions_parts.append(
            "The build spec has changed. Review the updated spec and apply targeted "
            "changes to bring the implementation in line with the new requirements."
        )
    if exported_hooks_changed:
        added = new_hooks - old_hooks
        removed = old_hooks - new_hooks
        if removed:
            instructions_parts.append(
                f"Exported hooks removed: {', '.join(sorted(removed))}. "
                "Identify and update any callers of these hooks before removing them."
            )
        if added:
            instructions_parts.append(
                f"Exported hooks added: {', '.join(sorted(added))}. "
                "Implement the new hook interfaces as specified."
            )
    if new_files:
        instructions_parts.append(
            f"New files expected: {', '.join(new_files)}. Create these files as part of the update."
        )
    if not instructions_parts:
        instructions_parts.append(
            "No structural changes detected. Apply any metadata or documentation updates."
        )

    migration_instructions = "\n\n".join(instructions_parts)

    return {
        "from_version": from_version,
        "to_version": to_version,
        "build_spec_changed": build_spec_changed,
        "exported_hooks_changed": exported_hooks_changed,
        "new_files": new_files,
        "summary": summary,
        "migration_instructions": migration_instructions,
    }


# ── Integration satisfaction checks ──────────────────────────────────────────
#
# These were closures inside preflight_check(); they live at module level so
# oauth/orchestrator.py can consult the same satisfaction logic (declared
# check_path + alternatives[]) before detouring an install to awaiting_oauth
# (gallery framework audit 2026-07-02, S1).


def _home_of(bot_id: str) -> Path:
    """Platform-keyed bot-home resolution (pwd-first; /home/{user} on Linux,
    /Users/{user} on macOS). Never construct f"/Users/{bot_id}" here — the
    bot_id may differ from the OS account name, and the literal breaks
    every openclaw.json-backed check on a Linux pod.

    Imports are lazy for the same reason _check_path_c_google_integration
    imports config lazily: keep gallery.py importable in dev/test envs.
    """
    try:
        from ..config import bot_home
        return bot_home(bot_id)
    except Exception:
        # bot_home raises (RuntimeError) when network.json is corrupt.
        # A preflight checker must degrade the way the old hardcoded path
        # did — checks report missing — not 500 the endpoint, so any
        # resolution failure falls back rather than propagating.
        # Passing bot_id to user_home intentionally relaxes its "resolved
        # account only" contract: the bot_id→account mapping lives in the
        # very network.json that just failed to load, and on macOS with
        # bot_id == account this yields exactly the old literal's path.
        from evolve_config import user_home
        return user_home(bot_id)


def _read_oc_json(bot_id: str) -> dict:
    p = _home_of(bot_id) / ".openclaw/openclaw.json"
    try:
        if p.exists():
            return json.loads(p.read_text())
    except PermissionError:
        pass
    except Exception:
        return {}
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return {}


def _read_github_token_from_git(bot_id: str, current_oc: dict) -> tuple[str, str] | None:
    """Discover a GitHub PAT from the workspace git remote URL.

    Returns (token, repo_slug) if found, or None.

    Note: openclaw rejects 'integrations' as an unknown top-level key, so this
    function intentionally does NOT write to openclaw.json. The git remote URL
    is the canonical store for the GitHub PAT.
    """
    _ws = (current_oc or {}).get("agents", {}).get("defaults", {}).get("workspace") or str(_home_of(bot_id) / ".openclaw/workspace")
    git_config = Path(_ws) / ".git" / "config"
    try:
        cfg_text = git_config.read_text()
    except Exception:
        return None

    # Handles: https://ghp_X@github.com/owner/repo[.git]
    #          https://oauth2:ghp_X@github.com/owner/repo[.git]
    #          https://x-access-token:ghp_X@github.com/owner/repo[.git]
    m = re.search(
        r"url\s*=\s*https://(?:[^:@\s]*:)?([^@\s]+)@github\.com/([^\s]+?)(?:\.git)?\s*$",
        cfg_text, re.MULTILINE,
    )
    if not m:
        return None
    token, repo_slug = m.group(1).strip(), m.group(2).strip()
    if not token or not repo_slug:
        return None
    return (token, repo_slug)


def check_integration_requirement(bot_id: str, req: dict) -> tuple[str, str]:
    """Return (state, message) for an integration requirement.

    state is "satisfied" | "missing" | "unknown".

    Tries the primary requirement first; if missing, iterates through
    any ``alternatives`` entries (each itself a full requirement dict)
    and returns satisfied if ANY of them matches. This supports the
    "flexible auth" pattern: a Gmail-using app can declare a legacy
    ``openclaw.json → integrations.gmail`` requirement with an
    alternative pointing at the path-C ``network.json → bots[bot].
    google_integration`` block, so an operator using either auth
    model satisfies the requirement.

    If the primary is missing AND no alternative matches, the
    returned message names the alternatives that were tried so the
    operator sees what auth paths the app would accept.

    Used by preflight_check() below and by the OAuth orchestrator's
    unknown-provider / declared-alternatives fallback
    (``oauth.orchestrator.evaluate_install_prerequisites``).
    """
    state, message = _check_one_integration(bot_id, req)
    if state == "satisfied":
        return state, message

    # Primary missing → try alternatives in order. First match wins.
    alternatives = req.get("alternatives") or []
    for alt in alternatives:
        alt_state, alt_message = _check_one_integration(bot_id, alt)
        if alt_state == "satisfied":
            alt_id = alt.get("id") or alt.get("display_name") or "alternative"
            return "satisfied", f"{alt_message} (via alternative '{alt_id}')"

    # None of primary or alternatives satisfied. Surface the alternative
    # IDs so the operator sees what auth paths would have been accepted.
    if alternatives:
        alt_ids = ", ".join(
            a.get("id") or a.get("display_name") or "?" for a in alternatives
        )
        message = (
            f"{message} (also tried alternatives: {alt_ids} — none satisfied)"
        )
    return state, message


def _check_one_integration(bot_id: str, req: dict) -> tuple[str, str]:
    """Return (state, message) for a single integration requirement entry.

    Handlers for first-class integrations come first; they encode the
    real on-disk shape (which often spans openclaw.json + filesystem
    skill files) rather than a single dotted path. Generic dotted-path
    traversal handles the long tail. Path-C google_integration is a
    special case because it lives in network.json (pod-wide) rather
    than the bot's openclaw.json.
    """
    integration_id = req.get("id", "")
    display_name = req.get("display_name") or integration_id
    # GitHub PAT: check .git/config remote URL (openclaw rejects "integrations" as unknown key)
    if integration_id == "github":
        result = _read_github_token_from_git(bot_id, _read_oc_json(bot_id))
        if result:
            return "satisfied", "GitHub PAT found in workspace git remote URL."
        return (
            "missing",
            "GitHub PAT not found in workspace .git/config remote URL. "
            + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
        )

    # Telegram: the bot is "configured" if EITHER the filesystem skill
    # file exists OR channels.telegram has a usable token. This matches
    # skills.inventory's de-dup logic — filesystem wins, channels is the
    # legacy/alternate location (personal_bot-style bots). The bare dotted-path
    # traversal can't express "or" so it falsely flagged atlas as missing
    # despite channels.telegram.botToken being populated.
    if integration_id == "telegram":
        oc = _read_oc_json(bot_id)
        # 1. Filesystem skill file — the canonical location for tokens set
        #    via the Skills → Telegram flow.
        skill_file = _home_of(bot_id) / ".openclaw/skills/telegram.json"
        file_ok = False
        try:
            file_ok = skill_file.exists()
        except (PermissionError, OSError):
            pass
        if not file_ok:
            # Fallback via sudo /bin/cat (evolve user can't always traverse
            # other bots' home dirs; the ACL on .openclaw/skills/ may not
            # have propagated for older bots).
            try:
                r = subprocess.run(
                    ["sudo", "/bin/test", "-f", str(skill_file)],
                    capture_output=True, timeout=5,
                )
                file_ok = r.returncode == 0
            except Exception:
                pass
        if file_ok:
            return "satisfied", (
                f"{display_name} configured via "
                f"~/.openclaw/skills/telegram.json."
            )
        # 2. channels.telegram with a token field — matches
        #    _CHANNEL_BACKED_SKILLS in skills/inventory.py.
        ch = (oc.get("channels") or {}).get("telegram")
        if isinstance(ch, dict):
            # Explicit opt-out beats presence.
            if ch.get("enabled") is False:
                return (
                    "missing",
                    f"{display_name} is disabled at 'channels.telegram.enabled = false' in openclaw.json. "
                    + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
                )
            for field in ("botToken", "token", "access_token"):
                if ch.get(field):
                    return "satisfied", (
                        f"{display_name} configured via channels.telegram.{field}."
                    )
        return (
            "missing",
            f"{display_name} not configured (no token at "
            f"~/.openclaw/skills/telegram.json or channels.telegram.botToken). "
            + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
        )

    # Otherwise honor the manifest's declared `check_path`. Two file
    # roots are supported: "openclaw.json → ..." for per-bot openclaw
    # config (the legacy bearer-token integrations live there), and
    # "network.json → ..." for pod-wide config including the path-C
    # google_integration block from PR #1862.
    check_path = (req.get("check_path") or "").strip()
    if "→" in check_path:
        head, _, tail = check_path.partition("→")
        file_part = head.strip()
        # Strip trailing annotations after a "+" so e.g.
        # "plugins.entries.google + calendar scope" → "plugins.entries.google"
        path_part = tail.strip().split("+", 1)[0].strip()

        # network.json → bots[bot].google_integration — path-C google
        # integration via service-account + domain-wide delegation.
        # The literal `[bot]` is substituted with the bot_id being
        # checked. We verify the block is well-formed (mode + subject +
        # service_account_secret_ref + scopes) and, if the requirement
        # carries `required_scopes`, that all of them appear in the
        # bot's configured scopes list.
        if file_part == "network.json" and path_part.startswith("bots[bot].google_integration"):
            state, message = _check_path_c_google_integration(
                bot_id, req, display_name
            )
            return state, message

        if file_part == "openclaw.json" and path_part:
            oc = _read_oc_json(bot_id)
            obj: object = oc
            for part in path_part.split("."):
                if not isinstance(obj, dict) or part not in obj:
                    return (
                        "missing",
                        f"{display_name} not configured (no '{path_part}' in openclaw.json). "
                        + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
                    )
                obj = obj[part]
            # Explicit opt-out beats presence (matches safe_upgrade._enabled_plugin_refs).
            if isinstance(obj, dict) and obj.get("enabled") is False:
                return (
                    "missing",
                    f"{display_name} is disabled at '{path_part}.enabled = false' in openclaw.json. "
                    + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
                )
            return "satisfied", f"{display_name} is configured at openclaw.json → {path_part}."

    return "unknown", f"Integration '{integration_id}' check not supported."


def _check_path_c_google_integration(
    b_id: str, req: dict, display_name: str,
) -> tuple[str, str]:
    """Verify the bot has a path-C google_integration block in network.json.

    Path C means service-account + domain-wide delegation per
    ``internal/spec-google-integration-paths-2026-05-30.md``. A satisfied
    check requires:

      - ``bots[b_id].google_integration`` is present
      - ``mode == "service_account_dwd"``
      - ``subject`` is set (the Workspace user the SA impersonates)
      - ``service_account_secret_ref`` is set (points at the SA JSON
        in the secrets store)
      - ``scopes`` is non-empty

    If the requirement carries ``required_scopes``, all of them must
    appear in the bot's configured ``scopes`` list — otherwise the
    scope set is insufficient for the app and we return missing.
    """
    # Lazy import: keeps gallery.py importable in dev/test envs that
    # don't have the network config helpers wired.
    try:
        from .. import config as _cfg
    except ImportError:
        return "unknown", f"{display_name} check failed: cannot import config module."

    try:
        network = _cfg.load_network()
    except Exception as exc:  # noqa: BLE001 — surfacing the read failure is the goal
        return "unknown", f"{display_name} check failed reading network.json: {exc}"

    bot_cfg = (network.get("bots") or {}).get(b_id)
    if not isinstance(bot_cfg, dict):
        return (
            "missing",
            f"{display_name} not configured (bot '{b_id}' has no entry in network.json).",
        )

    gi = bot_cfg.get("google_integration")
    if not isinstance(gi, dict):
        return (
            "missing",
            f"{display_name} not configured (no 'google_integration' block on "
            f"bot '{b_id}' in network.json). "
            + (f"Setup: {req['setup_doc']}" if req.get("setup_doc") else ""),
        )

    mode = gi.get("mode")
    if mode != "service_account_dwd":
        return (
            "missing",
            f"{display_name} present but mode is {mode!r}; path-C requires "
            f"mode == 'service_account_dwd'.",
        )

    for required_field in ("subject", "service_account_secret_ref"):
        if not gi.get(required_field):
            return (
                "missing",
                f"{display_name} block on '{b_id}' is missing required field "
                f"'{required_field}'.",
            )

    configured_scopes = gi.get("scopes") or []
    if not isinstance(configured_scopes, list) or not configured_scopes:
        return (
            "missing",
            f"{display_name} block on '{b_id}' has no scopes configured.",
        )

    required_scopes = req.get("required_scopes") or []
    if required_scopes:
        missing_scopes = [s for s in required_scopes if s not in configured_scopes]
        if missing_scopes:
            return (
                "missing",
                f"{display_name} block on '{b_id}' is missing required scopes: "
                f"{', '.join(missing_scopes)}.",
            )

    return (
        "satisfied",
        f"{display_name} configured via path-C service-account on bot '{b_id}' "
        f"(subject: {gi['subject']}).",
    )


def check_file_requirement(bot_id: str, req: dict) -> tuple[str, str]:
    """Return (state, message) for a ``requirements.files[]`` entry.

    A file requirement declares a prerequisite the app is dead without but
    that no integration/secret dotted-path can express: a bot-home-RELATIVE
    file that must exist (e.g. pm-inbox's ``~/.openclaw/
    pm-inbox-github-tokens.json``), optionally with an exact mode
    (``"mode": "0600"`` — the token-file contract).

    Entry shape::

        {
          "id":           "pm-inbox-github-tokens",   # row id (falls back to path)
          "path":         ".openclaw/pm-inbox-github-tokens.json",
          "display_name": "PM Inbox GitHub tokens file",
          "mode":         "0600",          # optional octal string, exact match
          "required":     true,
          "severity":     "runtime_warning",  # optional; "build_blocker" escalates
          "reason":       "...",
          "setup_doc":    "..."            # optional
        }

    ``path`` must stay inside the bot's home: a leading ``~/`` is accepted
    and stripped; absolute paths and any ``..`` component are refused with
    state "unknown" (which the gallery conformance gate turns into a
    shipped-package failure). As a second layer, when the path resolves on
    disk the resolved target is required to stay under the bot home, so a
    symlink planted inside ``.openclaw`` cannot redirect the probe outside
    it; if resolution isn't possible (unreadable parents), the lexical
    guards above still hold and the probe proceeds best-effort.

    Degrade order mirrors the telegram skill-file probe above: direct
    ``is_file()`` under the evolve read ACL first, then the ``sudo
    /bin/test -f`` fallback for pre-ACL bots. The mode check is
    advisory-on-unverifiable: if the file exists but stat is not permitted,
    the requirement is satisfied with a "(mode unverified)" note rather
    than false-flagging a healthy bot.
    """
    raw = str(req.get("path") or "").strip()
    display_name = req.get("display_name") or req.get("id") or raw or "file"
    setup_hint = f" Setup: {req['setup_doc']}" if req.get("setup_doc") else ""

    if not raw:
        return "unknown", f"File requirement '{display_name}' has no 'path'."

    rel = raw[2:] if raw.startswith("~/") else raw
    if not rel or rel.startswith(("/", "~")):
        return (
            "unknown",
            f"File requirement '{display_name}' path {raw!r} must be "
            f"bot-home-relative (absolute and home-qualified paths are refused).",
        )
    if ".." in Path(rel).parts:
        return (
            "unknown",
            f"File requirement '{display_name}' path {raw!r} must not "
            f"contain '..' components (must stay inside the bot's home).",
        )

    home = _home_of(bot_id)
    p = home / rel

    # Second-layer escape guard: if the path resolves on disk, its resolved
    # target must stay under the (resolved) bot home — a symlink component
    # inside .openclaw must not redirect the probe (or the sudo-backed
    # fallback) outside the home. Best-effort: resolution failures (missing
    # or unreadable parents) fall through to the lexical guards already
    # applied above rather than refusing a legitimate constrained read.
    try:
        resolved = p.resolve()
        home_resolved = home.resolve()
        if resolved != home_resolved and not resolved.is_relative_to(home_resolved):
            return (
                "unknown",
                f"File requirement '{display_name}' path {raw!r} resolves "
                f"outside the bot's home (symlink escape refused).",
            )
    except (OSError, RuntimeError):
        pass

    exists = False
    try:
        exists = p.is_file()
    except (PermissionError, OSError):
        pass
    if not exists:
        # Fallback via sudo /bin/test — same pattern as the telegram
        # skill-file probe: the evolve read ACL may not have propagated
        # for pre-ACL bots. Without a grant this fails closed (missing).
        try:
            r = subprocess.run(
                ["sudo", "/bin/test", "-f", str(p)],
                capture_output=True, timeout=5,
            )
            exists = r.returncode == 0
        except Exception:
            pass
    if not exists:
        return (
            "missing",
            f"{display_name} not found at ~/{rel}.{setup_hint}",
        )

    mode_want_raw = str(req.get("mode") or "").strip()
    if mode_want_raw:
        try:
            mode_want = int(mode_want_raw, 8)
        except ValueError:
            return (
                "unknown",
                f"File requirement '{display_name}' declares unparseable "
                f"mode {mode_want_raw!r} (expected an octal string like \"0600\").",
            )
        try:
            import stat as _stat
            mode_actual = _stat.S_IMODE(p.stat().st_mode)
        except (PermissionError, OSError):
            return (
                "satisfied",
                f"{display_name} present at ~/{rel} (mode unverified — "
                f"stat not permitted for the admin user).",
            )
        if mode_actual != mode_want:
            return (
                "missing",
                f"{display_name} present at ~/{rel} but mode is "
                f"{oct(mode_actual)[2:].zfill(4)} — expected {mode_want_raw}. "
                f"Fix: chmod {mode_want_raw} the file as the bot user.{setup_hint}",
            )
        return "satisfied", f"{display_name} present at ~/{rel} (mode {mode_want_raw})."

    return "satisfied", f"{display_name} present at ~/{rel}."


def check_messaging_channel_requirement(bot_id: str, req: dict) -> tuple[str, str]:
    """Return (state, message) for a ``requirements.messaging_channel[]`` entry.

    Expresses the delivery apps' real prerequisite — "this bot can send its
    person a message" — which every user-facing scheduled app assumed in
    build_spec prose only (gallery framework audit 2026-07-02, S6).

    Resolution mirrors the delivery convention's ``resolve_route``
    (internal/spec-gallery-delivery-convention-2026-06-11.md) without forking a
    third copy of either half:

      * enabled channels come from the SAME shared reader the coherence
        gate and briefing activation use (``evolve_admin.channels.
        enabled_messaging_channels_from_config`` over ``read_oc_config``,
        vocabulary = ``MESSAGING_INTEGRATION_IDS``);
      * the recorded route is ``network.json → bots[bot].primary_user.
        external_ids`` — the convention's single source of truth for where
        the bot's person lives.

    Verdicts:

      * any enabled messaging channel → satisfied (route status is noted in
        the message: no recorded route means scheduled sends will be
        SKIPPED until one is recorded — honest-but-satisfied, the channel
        itself is connected);
      * openclaw.json unreadable + a recorded route → satisfied ("trust
        external_ids alone rather than refusing", the convention's own
        degrade for exactly this ambiguity);
      * openclaw.json readable with NO enabled messaging channel → missing,
        even when a route is recorded — a recorded address does not make
        the bot able to send, and the convention's send would fail loudly
        every window (a disabled channel must never satisfy this check).
    """
    display_name = req.get("display_name") or "Messaging channel"
    setup_hint = f" Setup: {req['setup_doc']}" if req.get("setup_doc") else ""

    cfg: dict | None = None
    try:
        from ..skills._oc_install_common import read_oc_config
        cfg, _err = read_oc_config(bot_id)
    except Exception:
        cfg = None

    enabled: set[str] = set()
    if cfg is not None:
        try:
            from ..channels import enabled_messaging_channels_from_config
            enabled = enabled_messaging_channels_from_config(cfg)
        except Exception:
            enabled = set()

    # The recorded route only counts when it names a messaging-capable
    # channel — the SAME vocabulary the delivery convention's resolve_route
    # walks (its CHANNEL_PRIORITY), so a stray non-channel external_ids key
    # (or an id on a transport the bot can't send) never satisfies this
    # check. Data source is one shared set (coherence_pass_a's
    # MESSAGING_INTEGRATION_IDS, exposed via evolve_admin.channels).
    route_channels: list[str] = []
    try:
        from .. import config as _cfg
        from .coherence_pass_a import MESSAGING_INTEGRATION_IDS
        network = _cfg.load_network()
        from .. import external_ids as _external_ids
        ids = _external_ids.read_external_ids(
            ((network.get("bots") or {}).get(bot_id) or {}).get("primary_user")
        )
        route_channels = sorted(
            ch for ch in ids if ch in MESSAGING_INTEGRATION_IDS
        )
    except Exception:
        route_channels = []

    if enabled:
        chan_list = ", ".join(sorted(enabled))
        if route_channels:
            return (
                "satisfied",
                f"{display_name} connected ({chan_list}); delivery route to "
                f"the primary user recorded ({', '.join(route_channels)}).",
            )
        return (
            "satisfied",
            f"{display_name} connected ({chan_list}). No primary_user "
            f"delivery route recorded in network.json yet — scheduled "
            f"sends will be skipped until one is recorded.",
        )

    if cfg is None and route_channels:
        # openclaw.json unreadable: same degrade as the delivery
        # convention's enabled_channels() → None — trust the recorded
        # route rather than refusing.
        return (
            "satisfied",
            f"{display_name}: openclaw.json unreadable; trusting the "
            f"recorded delivery route ({', '.join(route_channels)}).",
        )

    if route_channels:
        # Readable config, no enabled messaging channel, but a route IS
        # recorded — the send would be attempted on the recorded channel
        # and fail loudly every window.
        return (
            "missing",
            f"No enabled messaging channel in openclaw.json (a delivery "
            f"route is recorded for {', '.join(route_channels)}, but that "
            f"channel is not enabled on this bot — sends would fail)."
            f"{setup_hint}",
        )

    return (
        "missing",
        f"No connected messaging channel: no channels.*.enabled entry in "
        f"openclaw.json and no primary_user delivery route recorded in "
        f"network.json. Connect a channel (e.g. Telegram via Skills) "
        f"before installing this app.{setup_hint}",
    )


def installed_state(dep_pkg_id: str, bot_id: str, shared_dir: Path) -> str:
    """
    Determine the install state of a gallery package on a bot.

    Shared by preflight_check's dependency axis and the install-chain
    orchestrator's already-installed skip (audit slate S5a) — both must
    agree on what "installed" means or a chain could re-install an app
    preflight considers satisfied.

    Returns one of:
      "installed"    — forge-built manifest with matching pkg_id, in a
                       post-build status (active / approved)
      "installing"   — forge job in progress (queued/running/awaiting_approval)
                       or the manifest is still seeded-but-unbuilt (status=updating)
      "failed"       — most recent forge job failed or was rejected
      "not_installed"— no manifest found at all

    pkg_ids are canonicalized through ``LEGACY_KEY_MIGRATION`` on BOTH sides
    of the comparison: a bot that installed a package before it was merged
    carries the retired id in its manifest, while the chain's closure carries
    the surviving id (closure entries come from ``load_gallery_package``,
    which resolves aliases). Without canonicalizing the manifest side, that
    bot reads ``not_installed`` and the chain would silently build a second
    copy of an app it already runs (audit S5a adversarial finding, on the
    real Task Manager migration entry).
    """
    canon = LEGACY_KEY_MIGRATION.get(dep_pkg_id, dep_pkg_id)

    # Look for any manifest on this bot whose (canonicalized) pkg_id matches.
    from .manifest import list_manifests as _list
    try:
        for m in _list(shared_dir, bot_id):
            # identity: see resolve_app_id — catalog key canonicalized through
            # the ``LEGACY_KEY_MIGRATION`` alias table, which is keyed on
            # ``pkg_id`` on BOTH sides (see the docstring above: canonicalizing
            # only one side is the audit S5a double-install bug).
            m_canon = LEGACY_KEY_MIGRATION.get(m.pkg_id, m.pkg_id) if m.pkg_id else ""
            if m_canon != canon:
                continue
            # Matching pkg_id found — check forge job state
            job = m.install_job or {}
            job_status = job.get("status", "") if isinstance(job, dict) else ""
            if job_status in ("queued", "running", "awaiting_approval"):
                return "installing"
            if job_status in ("failed", "rejected"):
                return "failed"
            # Post-install canonical status set by forge_engine._apply_forge_output
            # is "approved" (the manifest stays in that status indefinitely — no
            # later "active" transition exists in code). "active" is the rollback
            # target for improvement jobs (see forge_jobs.auto_reject_stale). Both
            # mean "the app is built and usable as a dependency"; the check used
            # to match only "active", which silently treated every freshly-installed
            # app as "installing" → blocked any dependent EA Pack-style install
            # until the operator manually flipped status. See the 2026-06-01
            # EA Pack / task-manager dependency regression.
            if m.status in ("active", "approved"):
                return "installed"
            return "installing"
    except Exception:
        pass

    # No matching manifest — but a live (non-terminal) forge job for this
    # (canonical pkg, bot) means an install is already mid-flight in the
    # pre-seed window: the manifest is only written at Step 1 inside
    # run_forge_job, so between another install's installed_state check and
    # that seed there is nothing on disk to see. Consulting active jobs here
    # lets an install chain PARK on the in-flight install instead of racing a
    # duplicate build over the same (bot, app) slot (audit S5a adversarial
    # finding — cross-chain shared-dependency double-build).
    try:
        from .forge_jobs import TERMINAL_JOB_STATES, list_jobs_for_app
        for pid in {canon, dep_pkg_id}:
            for j in list_jobs_for_app(pid, bot_id, shared_dir):
                if j.status not in TERMINAL_JOB_STATES:
                    return "installing"
    except Exception as exc:
        # Best-effort: a jobs-dir read failure must not crash preflight or
        # the chain advancer — fall through to not_installed (the caller
        # already treats that conservatively as a build_blocker).
        import logging as _log_mod
        _log_mod.getLogger(__name__).debug(
            "installed_state: active-job scan failed for %s on %s: %s",
            dep_pkg_id, bot_id, exc,
        )
    return "not_installed"


def preflight_check(pkg_id: str, bot_id: str, shared_dir: Path) -> dict:
    """
    Run pre-install checks for a gallery package on a specific bot.

    Checks two independent axes:
      1. App dependencies  — other gallery apps that must already be forge-installed
         on this bot (from pkg.app_dependencies).
      2. Technical requirements — integrations, secrets, system tools, and Python
         packages the running app will need (from pkg.requirements).

    Each item carries a ``state`` and a ``severity``:
      state:    "satisfied" | "not_installed" | "installing" | "failed" |
                "scanner_only" | "missing" | "unknown"
      severity: "build_blocker"   — must be resolved before forge can run
                "runtime_warning" — app will build but won't work until resolved
                "info"            — informational only

    Returns::

        {
          "pkg_id":         str,
          "bot_id":         str,
          "ready_to_build": bool,   # False if any build_blocker is unsatisfied
          "ready_to_run":   bool,   # False if any requirement is unsatisfied
          "app_dependencies": [
              {
                "pkg_id":       str,
                "display_name": str,
                "required":     bool,
                "reason":       str,
                "state":        str,
                "severity":     str,
                "message":      str,
              }, ...
          ],
          "requirements": [
              {
                "type":     "integration" | "secret" | "system" |
                            "python_package" | "file" | "messaging_channel",
                "id":       str,
                "required": bool,
                "state":    str,
                "severity": str,
                "message":  str,
                "setup_doc": str,   # present for integrations
              }, ...
          ],
        }
    """
    pkg = load_gallery_package(pkg_id, shared_dir)
    if pkg is None:
        return {
            "pkg_id": pkg_id,
            "bot_id": bot_id,
            "error": f"Package not found: {pkg_id}",
            "ready_to_build": False,
            "ready_to_run": False,
            "app_dependencies": [],
            "requirements": [],
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _installed_state(dep_pkg_id: str) -> str:
        return installed_state(dep_pkg_id, bot_id, shared_dir)

    def _check_system(req: dict) -> tuple[str, str]:
        name = req.get("name", "")
        check_cmd = req.get("check", f"which {name}")
        # Manifest-supplied probe string — same trust level as
        # install_cfg.command, so it goes through the same 2.9 gate and
        # executes as an argv vector (no shell).
        from .install_helpers import gate_exec_command
        argv, gate_reason = gate_exec_command(check_cmd)
        if argv is None:
            return (
                "unknown",
                f"Could not check system tool '{name}': check command "
                f"refused by security gate ({gate_reason}).",
            )
        try:
            r = subprocess.run(argv, capture_output=True, timeout=5)
            if r.returncode == 0:
                return "satisfied", f"System tool '{name}' available."
            return "missing", f"System tool '{name}' not found (check: {check_cmd})."
        except Exception as exc:
            return "unknown", f"Could not check system tool '{name}': {exc}"

    def _check_python_package(req: dict) -> tuple[str, str]:
        import_name = req.get("import", "")
        pip_name = req.get("pip_name", import_name)
        if not import_name:
            return "unknown", "No import name specified."
        try:
            __import__(import_name.replace("-", "_"))
            return "satisfied", f"Python package '{pip_name}' importable."
        except ImportError:
            return "missing", f"Python package '{pip_name}' not installed (import: {import_name})."
        except Exception as exc:
            return "unknown", f"Could not check package '{pip_name}': {exc}"

    def _check_secret(req: dict) -> tuple[str, str]:
        """Walk openclaw.json using a simple dot-path after the → separator.

        For integrations.github.token specifically: checks .git/config remote URL
        rather than openclaw.json, since openclaw rejects 'integrations' as an
        unknown top-level key and writing it crashes the gateway.
        """
        storage = req.get("storage", "")
        key_label = req.get("key", storage)
        if "→" not in storage:
            return "unknown", f"Cannot parse storage path for secret '{key_label}'."
        path_part = storage.split("→", 1)[1].strip()  # e.g. "integrations.github.token"
        # GitHub PAT: canonical store is .git/config remote URL, not openclaw.json
        if path_part == "integrations.github.token":
            oc = _read_oc_json(bot_id)
            if _read_github_token_from_git(bot_id, oc):
                return "satisfied", f"Secret '{key_label}' found in workspace git remote URL."
            return "missing", f"Secret '{key_label}' not found in workspace .git/config remote URL."
        oc = _read_oc_json(bot_id)
        obj: object = oc
        for part in path_part.split("."):
            if not isinstance(obj, dict) or part not in obj:
                return "missing", f"Secret '{key_label}' not found at path '{path_part}'."
            obj = obj[part]
        if obj:
            return "satisfied", f"Secret '{key_label}' present."
        return "missing", f"Secret '{key_label}' present but empty."

    # ── Check app dependencies ────────────────────────────────────────────────

    dep_results: list[dict] = []
    has_build_blocker = False

    for dep in pkg.get("app_dependencies", []):
        # identity: see resolve_app_id — catalog key: ``_installed_state``
        # compares it against installed manifests' canonicalized ``pkg_id``.
        dep_pkg_id = dep.get("pkg_id", "")
        required = dep.get("required", True)
        state = _installed_state(dep_pkg_id)

        if state == "installed":
            severity = "info"
            message = f"{dep.get('display_name', dep_pkg_id)} is installed."
        elif state == "installing":
            severity = "build_blocker" if required else "runtime_warning"
            message = (
                f"{dep.get('display_name', dep_pkg_id)} is currently being installed. "
                "Approve that forge job first, then install this app."
            )
        elif state == "failed":
            severity = "build_blocker" if required else "runtime_warning"
            message = (
                f"{dep.get('display_name', dep_pkg_id)} forge job failed or was rejected. "
                "Reinstall it before proceeding."
            )
        else:  # not_installed or scanner_only
            severity = "build_blocker" if required else "runtime_warning"
            message = (
                f"{dep.get('display_name', dep_pkg_id)} must be installed first. "
                + (dep.get("reason", ""))
            )

        if severity == "build_blocker" and state != "installed":
            has_build_blocker = True

        dep_results.append({
            "pkg_id":       dep_pkg_id,
            "display_name": dep.get("display_name", dep_pkg_id),
            "required":     required,
            "reason":       dep.get("reason", ""),
            "state":        state,
            "severity":     severity,
            "message":      message,
        })

    # ── Check technical requirements ──────────────────────────────────────────

    req_results: list[dict] = []
    has_runtime_gap = False

    reqs: dict = pkg.get("requirements", {})

    # An OPTIONAL requirement that's missing is by definition not a gap:
    # "optional" means the app functions without it. We surface optional+missing
    # items in the UI as informational ("available enhancement, not configured")
    # but they don't flip ready_to_run.
    for req in reqs.get("integrations", []):
        state, message = check_integration_requirement(bot_id, req)
        required = req.get("required", True)
        severity = "runtime_warning" if required else "info"
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":      "integration",
            "id":        req.get("id", ""),
            "required":  required,
            "state":     state,
            "severity":  severity,
            "message":   message,
            "setup_doc": req.get("setup_doc", ""),
        })

    for req in reqs.get("secrets", []):
        state, message = _check_secret(req)
        required = req.get("required", True)
        severity = "runtime_warning" if required else "info"
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":     "secret",
            "id":       req.get("key", ""),
            "required": required,
            "state":    state,
            "severity": severity,
            "message":  message,
        })

    for req in reqs.get("system", []):
        state, message = _check_system(req)
        required = req.get("required", True)
        # Missing system tools are build blockers — forge can't write valid scripts
        # without knowing what binaries are available.
        severity = "build_blocker" if required else "runtime_warning"
        if state != "satisfied" and severity == "build_blocker":
            has_build_blocker = True
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":     "system",
            "id":       req.get("name", ""),
            "required": required,
            "state":    state,
            "severity": severity,
            "message":  message,
        })

    for req in reqs.get("python_packages", []):
        state, message = _check_python_package(req)
        required = req.get("required", True)
        severity = "runtime_warning" if required else "info"
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":     "python_package",
            "id":       req.get("pip_name", req.get("import", "")),
            "required": required,
            "state":    state,
            "severity": severity,
            "message":  message,
        })

    # Bot-home files the app is dead without (audit 2026-07-02 S6, e.g.
    # pm-inbox's GitHub tokens file). Default severity is runtime_warning
    # (the app builds fine and its scripts degrade loudly on the missing
    # file); a package that genuinely cannot forge without the file may
    # declare "severity": "build_blocker" to gate the install instead.
    for req in reqs.get("files", []):
        state, message = check_file_requirement(bot_id, req)
        required = req.get("required", True)
        severity = "runtime_warning" if required else "info"
        if required and req.get("severity") == "build_blocker":
            severity = "build_blocker"
        if state != "satisfied" and severity == "build_blocker":
            has_build_blocker = True
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":      "file",
            "id":        req.get("id") or str(req.get("path") or ""),
            "required":  required,
            "state":     state,
            "severity":  severity,
            "message":   message,
            "setup_doc": req.get("setup_doc", ""),
        })

    # "Any connected messaging channel" — the delivery apps' prerequisite
    # (audit 2026-07-02 S6). A list for shape-consistency with the other
    # requirement categories; in practice a package declares one entry.
    for req in reqs.get("messaging_channel", []):
        state, message = check_messaging_channel_requirement(bot_id, req)
        required = req.get("required", True)
        severity = "runtime_warning" if required else "info"
        if state != "satisfied" and required:
            has_runtime_gap = True
        req_results.append({
            "type":      "messaging_channel",
            "id":        req.get("id") or "messaging_channel",
            "required":  required,
            "state":     state,
            "severity":  severity,
            "message":   message,
            "setup_doc": req.get("setup_doc", ""),
        })

    # ── scheduled_actions[] coverage check ────────────────────────────────────
    # If build_spec describes a LaunchDaemon / cron in prose but the manifest
    # declares no forge-installable scheduled_actions[] entry, the install
    # would appear successful while no cron is ever scheduled (the
    # Atlas Daily Digest failure mode, 2026-06-04). Refuse the install with
    # a clear remediation message so the manifest author fixes the structured
    # field before any tokens are burned on a forge run that will silently
    # produce a dead app.
    try:
        from .scheduled_actions_validator import validate_scheduled_actions
        sa_result = validate_scheduled_actions(pkg)
    except Exception as _sa_exc:
        # Validator import / execution failure must not block a working
        # install. Log to stderr and continue — the forge-side check at
        # ``run_forge_job`` is the durable backstop.
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(
            "preflight_check: scheduled_actions validator failed for %s: %s",
            pkg_id, _sa_exc,
        )
        sa_result = None

    if sa_result and not sa_result["ok"]:
        has_build_blocker = True
        req_results.append({
            "type":     "scheduled_actions",
            "id":       "structured_actions_declaration",
            "required": True,
            "state":    "missing",
            "severity": sa_result["severity"],
            "message":  sa_result["message"],
        })

    # apps-inherit-bot-llm validation — refuse manifests that declare
    # per-app LLM credentials (``api_key_source``), an invalid/missing
    # ``transport``, or a credential template in ``files[]``. See
    # internal/spec-apps-inherit-bot-llm-2026-06-06.md.
    try:
        from .apps_inherit_bot_llm_validator import validate_apps_inherit_bot_llm
        aibl_result = validate_apps_inherit_bot_llm(pkg)
    except Exception as _aibl_exc:
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(
            "preflight_check: apps-inherit-bot-llm validator failed for %s: %s",
            pkg_id, _aibl_exc,
        )
        aibl_result = None

    if aibl_result and not aibl_result["ok"]:
        has_build_blocker = True
        req_results.append({
            "type":     "apps_inherit_bot_llm",
            "id":       "principle_declaration",
            "required": True,
            "state":    "violation",
            "severity": aibl_result["severity"],
            "message":  aibl_result["message"],
        })

    # privacy{} + audience_scoping{} validation (manifest-v7 Slice 2,
    # internal/spec-manifest-v7-slicing-2026-06-10.md §4.1) — structural keys
    # when a block is declared, trigger-audience conformance against
    # role_capabilities, and the group-surface consent-notice gate.
    try:
        from .privacy_scoping_validator import validate_privacy_scoping
        ps_result = validate_privacy_scoping(pkg)
    except Exception as _ps_exc:
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(
            "preflight_check: privacy_scoping validator failed for %s: %s",
            pkg_id, _ps_exc,
        )
        ps_result = None

    if ps_result and not ps_result["ok"]:
        has_build_blocker = True
        req_results.append({
            "type":     "privacy_scoping",
            "id":       "privacy_audience_declaration",
            "required": True,
            "state":    "violation",
            "severity": ps_result["severity"],
            "message":  ps_result["message"],
        })

    # Surface the consent notice to the operator at install time (slicing
    # spec §4.1 "install surfaces the consent notice"). Informational —
    # the operator approving the install is approving this exact text
    # being shown to the app's audience.
    _consent = ""
    if isinstance(pkg.get("privacy"), dict):
        _raw = pkg["privacy"].get("consent_notice")
        if isinstance(_raw, str):
            _consent = _raw.strip()
    if _consent:
        req_results.append({
            "type":     "privacy_consent",
            "id":       "consent_notice",
            "required": False,
            "state":    "satisfied",
            "severity": "info",
            "message":  f"Consent notice this app shows its audience: “{_consent}”",
        })

    return {
        "pkg_id":           pkg_id,
        "bot_id":           bot_id,
        "ready_to_build":   not has_build_blocker,
        "ready_to_run":     not has_build_blocker and not has_runtime_gap,
        "app_dependencies": dep_results,
        "requirements":     req_results,
    }


def is_breaking_update(old_pkg: dict, new_pkg: dict) -> bool:
    """
    Return True if the update from *old_pkg* to *new_pkg* is a major version bump.

    A major bump signals cross-app impact — exported hook changes, shared file
    schema changes, etc.  Uses :func:`ids.is_major_bump` for the comparison.

    Args:
        old_pkg: Package dict representing the currently installed version.
        new_pkg: Package dict representing the available gallery version.

    Returns:
        True if new_pkg.pkg_version represents a major bump over old_pkg.pkg_version.
    """
    return is_major_bump(
        old_pkg.get("pkg_version"),
        new_pkg.get("pkg_version", ""),
    )

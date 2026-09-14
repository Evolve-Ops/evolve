"""arbiter.appliers.manifest_update — Patch an app manifest on a bot.

Manifests live at::

    {shared_dir}/applications/{bot_id}/{app_id}.json

Owned by the ``evolve`` user (the shared_dir's owner), so writes don't
need /tmp staging + sudo — atomic temp-file + rename is enough.

L2 wires only the ``set_fields`` operation, which is the one
``test_gate_backfill`` needs in order to mechanically write a
``test_exemption_reason`` for the obvious "manual cron, no logic" case
(see spec-app-testing-2026-05-07.md §5).

The other operations on ManifestUpdate (``add_tag``, ``broaden_scope``,
…) describe capability-surface edits whose semantics are app-specific
and route through forge / the manifest editor in the admin UI; they
fall outside the scope of an autonomous applier and return a clear
not-implemented error here. Adding them is a follow-up if/when a
generator emits one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import ManifestUpdate


# Resolved at apply-time so tests can swap shared_dir without re-importing.
_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    """Override the shared_dir used by the applier (tests + alternate pods)."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _manifest_path(bot_id: str, app_id: str) -> Path:
    return _SHARED_DIR / "applications" / bot_id / f"{app_id}.json"


def _read_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"manifest {path} must contain a JSON object")
    return data


_NOT_IMPLEMENTED_OPS = (
    "add_tag",
    "remove_tag",
    "broaden_scope",
    "narrow_scope",
    "add_capability",
    "remove_capability",
    "update_keywords",
)


_MISSING = object()


class ManifestUpdateApplier:
    """Apply a ManifestUpdate.

    Supports the ``set_fields`` and ``add_files`` operations. Capability
    surface ops (add_tag, broaden_scope, …) return ok=False with a
    not-implemented message; they route through forge / the manifest
    editor in the admin UI."""

    def capture_snapshot(self, action: ManifestUpdate, bot_id: str) -> dict:
        """Snapshot the prior values of every key the apply will touch."""
        path = _manifest_path(bot_id, action.app_id)
        if not path.exists():
            return {
                "action_kind": "ManifestUpdate",
                "bot_id": bot_id,
                "app_id": action.app_id,
                "manifest_path": str(path),
                "file_existed_before": False,
                "prior_values": {},
                "absent_keys": [],
            }
        try:
            data = _read_manifest(path)
        except (OSError, ValueError, json.JSONDecodeError):
            # Couldn't read; revert won't have anything to restore. The
            # apply step will fail-fast on the same read.
            return {
                "action_kind": "ManifestUpdate",
                "bot_id": bot_id,
                "app_id": action.app_id,
                "manifest_path": str(path),
                "file_existed_before": True,
                "read_failed": True,
                "prior_values": {},
                "absent_keys": [],
            }

        prior_values: dict[str, Any] = {}
        absent_keys: list[str] = []
        for k in (action.fields or {}).keys():
            v = data.get(k, _MISSING)
            if v is _MISSING:
                absent_keys.append(k)
            else:
                prior_values[k] = v
        return {
            "action_kind": "ManifestUpdate",
            "bot_id": bot_id,
            "app_id": action.app_id,
            "manifest_path": str(path),
            "file_existed_before": True,
            "prior_values": prior_values,
            "absent_keys": absent_keys,
        }

    def apply(self, action: ManifestUpdate, bot_id: str) -> ApplyResult:
        path = _manifest_path(bot_id, action.app_id)

        if action.operation in _NOT_IMPLEMENTED_OPS:
            return ApplyResult(
                ok=False,
                details={
                    "error": f"operation {action.operation!r} not implemented",
                    "manifest_path": str(path),
                },
                message=(
                    f"ManifestUpdate operation {action.operation!r} is not "
                    "auto-applied. Capability-surface edits route through "
                    "forge / the manifest editor in the admin UI."
                ),
            )

        if action.operation not in ("set_fields", "add_files"):
            return ApplyResult(
                ok=False,
                details={"error": f"unknown operation {action.operation!r}"},
                message=f"ManifestUpdate: unknown operation {action.operation!r}",
            )

        if not path.exists():
            return ApplyResult(
                ok=False,
                details={"manifest_path": str(path)},
                message=f"manifest not found: {path}",
            )

        if not action.fields:
            return ApplyResult(
                ok=True,
                details={"no_op": True, "manifest_path": str(path)},
                message=(
                    f"{action.operation} with empty fields dict: nothing "
                    f"to do for {action.app_id}"
                ),
            )

        try:
            data = _read_manifest(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return ApplyResult(
                ok=False,
                details={"manifest_path": str(path), "error": str(e)},
                message=f"failed to read manifest: {e}",
            )

        if action.operation == "set_fields":
            for k, v in action.fields.items():
                data[k] = v
            details = {
                "manifest_path": str(path),
                "operation": "set_fields",
                "fields_set": sorted(action.fields.keys()),
            }
            message = f"set {sorted(action.fields.keys())} on {bot_id}/{action.app_id}"
        else:
            # add_files — append new files to data["files"], deduped against
            # the manifest's CURRENT value (read above). Re-read at apply
            # time means a proposal sitting in pending/ for days won't
            # clobber concurrent file additions made by other paths
            # (manifest reflex, scanner, etc.).
            #
            # Caller must pass action.fields as {"files": [...]}. Other
            # keys are silently ignored — we don't want add_files to
            # quietly mutate test_command or anything else.
            new_files = action.fields.get("files")
            if not isinstance(new_files, list) or not all(isinstance(p, str) for p in new_files):
                return ApplyResult(
                    ok=False,
                    details={
                        "manifest_path": str(path),
                        "fields_received": list(action.fields.keys()),
                    },
                    message=(
                        f"add_files requires fields={{'files': [str, ...]}}; "
                        f"got fields={action.fields!r}"
                    ),
                )

            existing = data.get("files") or []
            # The manifest's `files` may be list[str] (v4) or list[dict]
            # (v5+ provenance records). For comparison we need the path
            # set; for the merged write we preserve the existing entries
            # in their original shape and append plain-string entries
            # for the new ones (the scanner's Phase 5 stamping later
            # promotes string entries to dict shape). This mirrors how
            # the manifest_reflex_runner's update path handles it.
            existing_paths: set[str] = set()
            for entry in existing:
                if isinstance(entry, str):
                    existing_paths.add(entry.strip())
                elif isinstance(entry, dict):
                    p = (entry.get("path") or "").strip()
                    if p:
                        existing_paths.add(p)

            merged = list(existing)
            added: list[str] = []
            for fp in new_files:
                fp = fp.strip()
                if not fp:
                    continue
                if fp in existing_paths:
                    continue
                merged.append(fp)
                existing_paths.add(fp)
                added.append(fp)

            data["files"] = merged

            if not added:
                # Everything we were asked to add was already present —
                # success, but no-op. Operator might want to know so
                # they don't approve duplicates expecting a change.
                return ApplyResult(
                    ok=True,
                    details={
                        "manifest_path": str(path),
                        "operation": "add_files",
                        "added": [],
                        "already_present": [fp.strip() for fp in new_files if fp.strip()],
                    },
                    message=(
                        f"add_files no-op: all paths already in "
                        f"{bot_id}/{action.app_id}.files"
                    ),
                )

            details = {
                "manifest_path": str(path),
                "operation": "add_files",
                "added": added,
            }
            message = (
                f"appended {added} to {bot_id}/{action.app_id}.files"
            )

        try:
            atomic_write_json(path, data, sort_keys=True)
        except OSError as e:
            return ApplyResult(
                ok=False,
                details={"manifest_path": str(path), "error": str(e)},
                message=f"failed to write manifest: {e}",
            )

        return ApplyResult(ok=True, details=details, message=message)

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        path_str = snapshot.get("manifest_path", "")
        if not path_str:
            return RevertResult(ok=False, message="snapshot missing manifest_path")
        path = Path(path_str)

        if not snapshot.get("file_existed_before"):
            return RevertResult(
                ok=False,
                message=(
                    "manifest did not exist before apply; refusing to delete "
                    "to avoid data loss — operator must clean up manually"
                ),
            )

        if not path.exists():
            return RevertResult(
                ok=False,
                details={"manifest_path": str(path)},
                message=f"manifest no longer exists at {path}; cannot revert",
            )

        try:
            data = _read_manifest(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return RevertResult(
                ok=False,
                details={"manifest_path": str(path), "error": str(e)},
                message=f"failed to read manifest for revert: {e}",
            )

        prior_values = snapshot.get("prior_values") or {}
        absent_keys = snapshot.get("absent_keys") or []

        for k, v in prior_values.items():
            data[k] = v
        for k in absent_keys:
            data.pop(k, None)

        try:
            atomic_write_json(path, data, sort_keys=True)
        except OSError as e:
            return RevertResult(
                ok=False,
                details={"manifest_path": str(path), "error": str(e)},
                message=f"failed to write manifest on revert: {e}",
            )

        return RevertResult(
            ok=True,
            details={
                "manifest_path": str(path),
                "restored_keys": sorted(prior_values.keys()),
                "removed_keys": sorted(absent_keys),
            },
            message=f"reverted manifest changes on {bot_id}/{snapshot.get('app_id', '?')}",
        )


register_applier("ManifestUpdate", ManifestUpdateApplier())

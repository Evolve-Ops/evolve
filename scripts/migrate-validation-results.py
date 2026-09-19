#!/usr/bin/env python3
"""
migrate-validation-results.py — One-shot migration for the forge → validation rename.

Idempotent. Safe to re-run.

Performs:
  1. For each <id>.json in {shared}/proposals/forge-results/: if there is no
     matching file in validation-results/, move it across (rewriting the
     forge_notes field to validation_notes).
  2. For each <id>.json already in validation-results/: if it still has a
     forge_notes field, rewrite to validation_notes.
  3. If forge-results/ ends up empty, remove it.

Run as the evolve service user, since shared dir files are evolve-owned.

Usage:
    python3 migrate-validation-results.py [--shared-dir /Users/Shared/evolve] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _rewrite_field(data: dict) -> bool:
    """If `forge_notes` is present and `validation_notes` is not, rename it.
    Returns True if the dict was modified."""
    if "forge_notes" in data and "validation_notes" not in data:
        data["validation_notes"] = data.pop("forge_notes")
        return True
    if "forge_notes" in data and "validation_notes" in data:
        # Both present (shouldn't happen, but be safe): drop the legacy one.
        data.pop("forge_notes")
        return True
    return False


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def migrate(shared_dir: Path, dry_run: bool = False) -> int:
    proposals = shared_dir / "proposals"
    legacy_dir = proposals / "forge-results"
    new_dir = proposals / "validation-results"

    moved = 0
    field_rewrites = 0
    skipped = 0

    if legacy_dir.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(legacy_dir.glob("*.json")):
            dest = new_dir / src.name
            if dest.exists():
                # Newer file wins; just remove the legacy copy.
                if dry_run:
                    print(f"[dry-run] rm {src} (already in new dir)")
                else:
                    src.unlink()
                skipped += 1
                continue
            try:
                data = json.loads(src.read_text())
            except (json.JSONDecodeError, OSError) as e:
                print(f"[skip] {src.name}: cannot parse ({e})", file=sys.stderr)
                continue
            _rewrite_field(data)
            if dry_run:
                print(f"[dry-run] mv {src} → {dest}")
            else:
                _atomic_write(dest, data)
                src.unlink()
            moved += 1

        # Tidy up empty legacy dir.
        try:
            if not any(legacy_dir.iterdir()):
                if dry_run:
                    print(f"[dry-run] rmdir {legacy_dir}")
                else:
                    legacy_dir.rmdir()
        except OSError:
            pass

    # Field rewrite for files that already lived in the new dir
    if new_dir.exists():
        for path in sorted(new_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if _rewrite_field(data):
                if dry_run:
                    print(f"[dry-run] rewrite forge_notes → validation_notes in {path.name}")
                else:
                    _atomic_write(path, data)
                field_rewrites += 1

    print(
        f"Migration complete: {moved} file(s) moved, "
        f"{field_rewrites} file(s) had field rewritten, "
        f"{skipped} legacy duplicate(s) removed."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--shared-dir", type=Path, default=Path("/Users/Shared/evolve"),
        help="Pod shared directory (default: /Users/Shared/evolve)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    args = parser.parse_args()

    if not args.shared_dir.exists():
        print(f"ERROR: shared dir does not exist: {args.shared_dir}", file=sys.stderr)
        return 1

    return migrate(args.shared_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

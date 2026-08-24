#!/usr/bin/env python3
"""Bump stored charter fingerprints to match deployed charter YAML.

Run this after a charter PR lands and the new code reaches the mini.
The registry's load-time fingerprint check will refuse to load any
generator whose stored record fingerprint disagrees with the deployed
charter. This tool re-reads each charter, recomputes its fingerprint,
and updates the matching ``{shared_dir}/generators/<id>.json`` record
in place — preserving track_record, config, and state.

Usage:
    sudo -u evolve python3 tools/bump_charter_fingerprints.py \\
        --shared-dir /Users/Shared/evolve

By default lists what would change without writing. Pass --apply to write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    from registry.charter_loader import compute_charter_fingerprint

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-dir", type=Path, required=True)
    parser.add_argument(
        "--generators-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "generators",
    )
    parser.add_argument("--apply", action="store_true", help="Write changes")
    args = parser.parse_args()

    records_dir = args.shared_dir / "generators"
    if not records_dir.exists():
        print(f"records dir does not exist: {records_dir}", file=sys.stderr)
        return 1

    changed = 0
    skipped = 0
    for entry in sorted(args.generators_dir.iterdir()):
        if not entry.is_dir():
            continue
        charter_path = entry / "charter.yaml"
        if not charter_path.exists():
            charter_path = entry / "charter.yml"
        if not charter_path.exists():
            continue

        gen_id = entry.name
        record_path = records_dir / f"{gen_id}.json"
        if not record_path.exists():
            print(f"{gen_id}: no record on disk yet — first run will create it")
            continue

        new_fp = compute_charter_fingerprint(
            charter_path.read_text(encoding="utf-8")
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        old_fp = record.get("charter_fingerprint", "")

        if old_fp == new_fp:
            skipped += 1
            continue

        print(f"{gen_id}: {old_fp[:12]}… → {new_fp[:12]}…")
        if args.apply:
            record["charter_fingerprint"] = new_fp
            record_path.write_text(json.dumps(record, indent=2))
        changed += 1

    if not args.apply and changed:
        print(f"\n{changed} record(s) would change. Re-run with --apply.")
    elif args.apply:
        print(f"\n{changed} record(s) updated, {skipped} unchanged.")
    else:
        print(f"\nAll {skipped} record(s) already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

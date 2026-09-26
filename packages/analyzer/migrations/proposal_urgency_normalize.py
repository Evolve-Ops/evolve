"""migrations.proposal_urgency_normalize — Normalize legacy urgency values.

One-shot migration: rewrites on-disk proposal JSONs whose ``urgency`` field
carries a value that isn't in ``schema.proposal.Urgency``. The canonical
taxonomy is the source of truth; legacy values are mapped to their closest
canonical equivalent.

Background. Three generators emitted urgencies outside the seven-element
Urgency Literal:

* ``audit_poller`` + ``test_failure_responder`` wrote ``needs_attention``;
  these surfaces are operational (failing tests, broken sudoers/daemons),
  so they map to ``operational_urgent``.
* ``app_birth_detector`` wrote ``discretionary`` for orphan-cluster
  promotion proposals; these are structural improvements, so they map
  to ``improvement``.
* ``budget_hawk`` wrote ``cost_hygiene`` for three tuning proposals
  (summarizer floor, app-cost imbalance, classifier threshold); these
  aren't alerts, just cleanup, so they map to ``hygiene``.

After this migration runs (and the writer code is fixed in the same PR),
every proposal on disk carries a canonical urgency. The companion
regression test ``test_proposal_urgency_canonical.py`` keeps drift from
recurring.

Idempotent: files already carrying a canonical urgency are left alone.
A proposal with no ``urgency`` key at all (one such file exists on the
test pod from before the field was introduced) is round-tripped through
``Proposal.from_dict → to_dict`` so the on-disk JSON gets the schema's
default ``"improvement"`` written explicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator, get_args

from evolve_util import atomic_write_json as _atomic_write
from schema.proposal import Proposal, Urgency


# Drift-detector: the canonical urgency set, derived from the Literal so
# adding a new Urgency value flows here automatically.
CANONICAL_URGENCIES: frozenset[str] = frozenset(get_args(Urgency))

# Closest-canonical mapping for the three legacy values that were
# observed in writer code (PR #1168, #1172, and budget_hawk).
LEGACY_URGENCY_MAP: dict[str, str] = {
    "needs_attention": "operational_urgent",
    "discretionary": "improvement",
    "cost_hygiene": "hygiene",
}


def is_canonical(value: object) -> bool:
    """True if *value* is a string in the canonical Urgency set."""
    return isinstance(value, str) and value in CANONICAL_URGENCIES


def iter_invalid(proposals_root: Path) -> Iterator[tuple[Path, object]]:
    """Yield (path, urgency_value) for every JSON whose urgency isn't canonical.

    Walks every ``*.json`` under ``proposals_root`` (any subdir depth).
    A missing ``urgency`` key counts as invalid — it's tolerated by
    ``Proposal.from_dict`` (defaults to ``improvement``) but is still
    schema drift on disk and gets a round-trip to make the field
    explicit during migration.
    """
    if not proposals_root.exists():
        return
    for path in sorted(proposals_root.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        urgency = data.get("urgency")
        if urgency is None or not is_canonical(urgency):
            yield path, urgency


def migrate_file(path: Path) -> tuple[str, str] | None:
    """Rewrite a single proposal's urgency to a canonical value.

    Returns ``(old_value, new_value)`` if the file was rewritten, ``None``
    if it was already canonical. Raises if the file can't be read or
    written.
    """
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    old_value = data.get("urgency")
    if is_canonical(old_value):
        return None

    if isinstance(old_value, str) and old_value in LEGACY_URGENCY_MAP:
        new_value = LEGACY_URGENCY_MAP[old_value]
    else:
        # Missing or otherwise unrecognized — round-trip through the
        # schema so the default ("improvement") lands explicitly.
        new_value = "improvement"

    # Round-trip through Proposal to revalidate the rest of the record
    # and to write back a fully-shaped dict (e.g. the missing-urgency
    # case may also be missing other newer optional fields that
    # to_dict() will now include).
    proposal = Proposal.from_dict(data)
    proposal.urgency = new_value  # type: ignore[assignment]
    _atomic_write(path, proposal.to_dict(), mode=0o644)
    return (str(old_value) if old_value is not None else "<missing>", new_value)


def migrate_directory(
    proposals_root: Path, *, dry_run: bool = False
) -> dict:
    """Walk ``proposals_root`` and normalize every non-canonical urgency.

    Returns:
        {
            "scanned": int,
            "already_canonical": int,
            "migrated": int,
            "errors": [(path, reason), ...],
            "changes": [(path, old_value, new_value), ...],
        }
    """
    scanned = 0
    already_canonical = 0
    migrated = 0
    errors: list[tuple[str, str]] = []
    changes: list[tuple[str, str, str]] = []

    if not proposals_root.exists():
        return {
            "scanned": 0,
            "already_canonical": 0,
            "migrated": 0,
            "errors": [],
            "changes": [],
        }

    for path in sorted(proposals_root.rglob("*.json")):
        scanned += 1
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            errors.append((str(path), f"read/parse failed: {e}"))
            continue

        if is_canonical(data.get("urgency")):
            already_canonical += 1
            continue

        if dry_run:
            old_value = data.get("urgency")
            new_value = LEGACY_URGENCY_MAP.get(
                old_value if isinstance(old_value, str) else "", "improvement"
            )
            changes.append(
                (
                    str(path),
                    str(old_value) if old_value is not None else "<missing>",
                    new_value,
                )
            )
            migrated += 1
            continue

        try:
            result = migrate_file(path)
        except (OSError, KeyError, ValueError, TypeError) as e:
            errors.append((str(path), f"migrate failed: {e}"))
            continue

        if result is not None:
            old_value, new_value = result
            changes.append((str(path), old_value, new_value))
            migrated += 1

    return {
        "scanned": scanned,
        "already_canonical": already_canonical,
        "migrated": migrated,
        "errors": errors,
        "changes": changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize legacy proposal urgency values to the canonical "
            "schema.proposal.Urgency taxonomy."
        )
    )
    parser.add_argument(
        "--proposals-root",
        type=Path,
        default=Path("/Users/Shared/evolve/proposals"),
        help="Root directory containing proposal JSON files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing any files.",
    )
    args = parser.parse_args(argv)

    report = migrate_directory(args.proposals_root, dry_run=args.dry_run)

    print(f"Scanned:           {report['scanned']}")
    print(f"Already canonical: {report['already_canonical']}")
    print(f"Migrated:          {report['migrated']}")
    if report["changes"]:
        print("Changes:")
        for path, old_value, new_value in report["changes"]:
            print(f"  {path}: {old_value!r} -> {new_value!r}")
    if report["errors"]:
        print(f"Errors:            {len(report['errors'])}")
        for path, reason in report["errors"]:
            print(f"  {path}: {reason}")
    if args.dry_run:
        print("(dry run — no files written)")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

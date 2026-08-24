#!/usr/bin/env python3
"""
migrate_v7_backfill.py — Backfill fields dropped by earlier v7-arc migrations.

Background
----------
The v13 → v7-arc migration (``migrate_v7.py``) shipped with several known
drops, each fixed forward in a subsequent PR:

  - PR #1469 (2026-05-23) — ``identity`` passthrough.
  - PR #1471 (2026-05-23) — ``constraints`` / ``test_cases`` /
    ``example_triggers`` / ``scheduled_actions`` / ``owner`` /
    ``inputs`` / ``outputs`` passthrough.
  - PR #1602 (2026-05-26) — ``description`` passthrough.
  - Tier-A publisher fix (2026-06) — schema-5 ``requirements.integrations[]``
    → ``dependencies.integrations[]``; ``interface_contract`` and
    ``build_spec`` ``## FILE:`` blocks → ``blueprint.files[]``.
  - Manifest-v7 Slice 2 / schema v24 (2026-06-10) — ``privacy{}`` /
    ``audience_scoping{}`` re-stamped through the same inference the
    current migration runs when a Spec lacks them, plus a non-mutating
    trigger-audience conformance report (slicing spec §4.3 / §6.2).

Each fix added logic to ``_extract_spec``, but none backfilled Specs
already migrated before its fix. The visible result on the production pod
is empty tile descriptions, empty view-modal Identity sections, empty
``blueprint.files[]``, empty ``dependencies.integrations[]``, and missing
``scheduled_actions[]`` on every Spec migrated before the corresponding
fix landed.

What this script does
---------------------
Walks two source paths in parallel; both feed the same per-Spec merge:

  - **Migration-backup walk** — for the latest ``migration_backup`` run on
    disk, iterates each ``v13_source`` operation and reads the original
    v13 JSON from ``originals/<hash>.json``. This covers Instance-migrated
    Specs (the ``gallery/local/`` tier) which were tracked by backup ops
    at migration time.
  - **Repo-gallery walk** — for every ``<repo>/gallery/<name>/p-*.json``
    in the in-repo gallery (default: the deploy checkout's ``gallery/``
    dir, from ``platform_profile.deploy_checkout_default``),
    looks up the matching Spec by ``pkg_id``. This covers BUILTIN-migrated
    Specs (the ``gallery/builtin/`` tier) whose source isn't in any
    migration backup — ``migrate_gallery_package`` records ops as
    ``kind: gallery_spec`` with no v13 source attached. The repo source
    is always the latest version, so re-running picks up any post-migration
    edits to the in-repo Spec.

Both paths patch the corresponding Spec at
``{shared_dir}/gallery/{local,builtin,imported}/<spec_id>/<spec_version>.json``
to recover dropped fields. Two recovery strategies:

  - **Passthrough fields** (``description``, ``identity``, ``constraints``,
    ``test_cases``, ``example_triggers``, ``scheduled_actions``, ``owner``,
    ``inputs``, ``outputs``, plus ``success_criteria.observable_outcomes`` /
    ``failure_signals`` / ``minimum_bar``): copy v13 value verbatim when the
    Spec doesn't carry one.
  - **Translated fields** (``blueprint.files[]``,
    ``dependencies.integrations[]``): re-run the current PR-1 readers
    (``_build_blueprint``, ``_build_integrations``) against the v13 source
    when the Spec's array is empty. Schema-5 lineage sources put their
    roster in ``interface_contract`` + ``build_spec`` and their integrations
    in ``requirements.integrations[]`` — the early migrator ignored both.

Conservative by design: only ever fills empty / missing fields. Never
overwrites existing values. Operators who want to refresh a populated
field should re-run the full migration.

Idempotent: re-running is a no-op once every Spec carries all available
fields.

Usage
-----
``PYTHONPATH`` is required because ``evolve_admin`` isn't installed
site-wide on the mini — it's loaded out of the deploy checkout. Mirrors
``migrate_v7.py``'s own invocation pattern.

::

    # dry-run first (shows what would change, writes nothing)
    sudo -u evolve PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \\
        python3 -m evolve_admin.applications.migrate_v7_backfill \\
        --shared-dir /Users/Shared/evolve --dry-run

    # apply
    sudo -u evolve PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \\
        python3 -m evolve_admin.applications.migrate_v7_backfill \\
        --shared-dir /Users/Shared/evolve --apply

identity: see resolve_app_id — the second MIGRATION module the AL-1.4b grep
gate exempts (build-AL-1.4-app-id-canonical.md §3). Every ``spec_id`` /
``pkg_id`` here names the gallery version-line being backfilled INTO
(``gallery/<tier>/<spec_id>/<version>.json``), matched against an in-repo
source's own ``pkg_id``. See ``_iter_repo_gallery_sources`` for the one place
that picks a source's id and why the resolver is deliberately not used there.

"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json

from .migrate_v7 import (
    MigrationResult,
    _build_blueprint,
    _build_integrations,
    _infer_audience_scoping,
    _infer_privacy,
)

# Top-level fields that earlier migrations dropped and the new migrator now
# passes through verbatim. Backfill copies v13[field] → spec[field] when the
# Spec doesn't carry one (truthy check — empty strings/dicts/lists are
# treated as "missing" so a partial earlier write doesn't shadow the v13 source).
PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "description",
    "identity",
    "constraints",
    "test_cases",
    "example_triggers",
    "scheduled_actions",
    "owner",
    "inputs",
    "outputs",
)

# Subfields under success_criteria that earlier migrators dropped while keeping
# the parent block. Backfilled into the existing success_criteria dict (or a
# fresh dict if absent).
SUCCESS_CRITERIA_SUBFIELDS: tuple[str, ...] = (
    "observable_outcomes",
    "failure_signals",
    "minimum_bar",
)

# Fields the PR-1 readers translate from schema-5 enrichments. Re-run the
# current readers against v13 when the Spec's array is empty.
TRANSLATED_FIELDS: tuple[str, ...] = (
    "blueprint.files",
    "dependencies.integrations",
)

# v24 (manifest-v7 Slice 2) — blocks re-stamped through the same inference
# the current migration runs (``_infer_privacy`` / ``_infer_audience_scoping``)
# when the Spec lacks them, so Specs migrated before Slice 2 agree with
# post-v24 ones (slicing spec §4.3). Fill-if-missing only, like everything
# else here — a populated block is operator-meaningful and preserved.
INFERRED_BLOCK_FIELDS: tuple[str, ...] = (
    "privacy",
    "audience_scoping",
)

# Combined list — used by the guardrail test to detect when this module
# gains a new backfill target without the tests being updated.
BACKFILLABLE_FIELDS: tuple[str, ...] = (
    PASSTHROUGH_FIELDS
    + tuple(f"success_criteria.{s}" for s in SUCCESS_CRITERIA_SUBFIELDS)
    + TRANSLATED_FIELDS
    + INFERRED_BLOCK_FIELDS
)


@dataclass
class BackfillResult:
    spec_path: Path
    spec_id: str
    fields_added: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    # Non-mutating observations (e.g. trigger audiences that don't conform
    # to role_capabilities). Printed by the walk loops; never written.
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.fields_added) and not self.skipped_reason


def _find_spec_path(
    shared_dir: Path,
    spec_id: str,
    *,
    tier: Optional[str] = None,
) -> Path | None:
    """Resolve a Spec's on-disk path across the local/builtin/imported tiers.

    Returns the path with the highest version (lexicographic), or None if the
    Spec directory doesn't exist in any tier.

    By default walks all tiers in order ``local → builtin → imported``,
    returning the first match. When ``tier`` is provided, restricts the search
    to that single tier — used by the repo-gallery walk to target the
    ``gallery/builtin/`` tier directly, since the in-repo source is the
    canonical source of truth for builtin Specs and the unscoped lookup
    would short-circuit on a local Instance copy of the same spec_id.
    """
    tiers = (tier,) if tier else ("local", "builtin", "imported")
    for t in tiers:
        tier_root = shared_dir / "gallery" / t
        if t == "imported":
            # imported has an extra source_pod_id level: imported/<pod>/<spec>/
            if not tier_root.exists():
                continue
            for pod_dir in tier_root.iterdir():
                cand = pod_dir / spec_id
                if cand.is_dir():
                    versions = sorted(cand.glob("*.json"))
                    if versions:
                        return versions[-1]
            continue
        cand = tier_root / spec_id
        if cand.is_dir():
            versions = sorted(cand.glob("*.json"))
            if versions:
                return versions[-1]
    return None


def _run_has_v13_sources(run: Path) -> bool:
    """True when the run's manifest records at least one v13_source op."""
    try:
        manifest = json.loads((run / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        (op.get("context") or {}).get("kind") == "v13_source"
        for op in manifest.get("operations", [])
    )


def _latest_backup_run(shared_dir: Path) -> Path | None:
    """Return the timestamped dir of the most recent migration run that
    actually captured v13 sources.

    Multiple runs accumulate under ``migration_backup/v13_to_v7_arc/``;
    prefer the latest because earlier runs may have been rolled back and
    replaced. But a re-run on an already-migrated pod records ZERO
    operations (nothing left to migrate) — picking such an empty run
    starves the whole backfill while printing a clean-looking
    "changed=0, skipped=0" (observed on the reference pod: the
    2026-06-06 no-op run shadowed the 2026-05-23 run holding all 63
    originals). Skip source-less runs; fall back to the plain latest
    only when no run carries sources (preserves the old behavior for
    the "no backups at all" message path).
    """
    root = shared_dir / "migration_backup" / "v13_to_v7_arc"
    if not root.exists():
        return None
    runs = sorted(p for p in root.iterdir() if p.is_dir())
    if not runs:
        return None
    for run in reversed(runs):
        if _run_has_v13_sources(run):
            return run
    return runs[-1]


def _iter_v13_sources(backup_run: Path):
    """Yield (spec_id, v13_dict) for each v13_source op in the run's manifest."""
    manifest_path = backup_run / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[backfill] error reading {manifest_path}: {e}", file=sys.stderr)
        return

    for op in manifest.get("operations", []):
        ctx = op.get("context") or {}
        if ctx.get("kind") != "v13_source":
            continue
        spec_id = ctx.get("spec_id")
        backup_rel = op.get("backup")
        if not (spec_id and backup_rel):
            continue
        original_path = backup_run / backup_rel
        try:
            v13 = json.loads(original_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"[backfill] skipping {spec_id}: can't read backup {original_path}: {e}",
                  file=sys.stderr)
            continue
        yield spec_id, v13


def backfill_one(
    spec_path: Path,
    spec: dict,
    v13: dict,
    dry_run: bool,
) -> BackfillResult:
    """Merge missing fields from v13 into spec; write atomically (unless dry-run).

    Three recovery strategies run in sequence; each only acts when the
    corresponding Spec target is empty/absent so the merge is idempotent
    and never overwrites operator-authored content:

      1. Top-level passthroughs (description, identity, constraints, …).
      2. ``success_criteria`` subfields (observable_outcomes, …).
      3. Translated fields — ``blueprint.files[]`` and
         ``dependencies.integrations[]`` re-derived via the current
         ``_build_blueprint`` / ``_build_integrations`` readers when the
         Spec's array is empty.
      4. v24 inferred blocks — ``privacy{}`` / ``audience_scoping{}``
         re-stamped through the same inference the current migration runs
         when the Spec lacks them (slicing spec §4.3).

    A fifth, non-mutating pass checks every ``event_triggers[].audience``
    against ``audience_scoping.role_capabilities`` (the Slice-1
    free-string gap) and reports non-conforming values via
    ``result.warnings`` — the operator pins the vocabulary; the backfill
    never invents role keys.
    """
    result = BackfillResult(spec_path=spec_path, spec_id=spec.get("spec_id", "?"))

    # Strategy 1 — top-level passthroughs.
    for fld in PASSTHROUGH_FIELDS:
        if spec.get(fld):
            continue  # already present
        v13_val = v13.get(fld)
        if not v13_val:
            continue  # v13 didn't have it either
        spec[fld] = v13_val
        result.fields_added.append(fld)

    # Strategy 2 — success_criteria subfields. v13's success_criteria may
    # carry observable_outcomes / failure_signals / minimum_bar that the
    # early migrator dropped from spec.success_criteria. Backfill them into
    # whatever success_criteria block the Spec already has (or create one).
    sc_v13 = v13.get("success_criteria")
    if isinstance(sc_v13, dict):
        sc_spec = spec.get("success_criteria")
        if not isinstance(sc_spec, dict):
            sc_spec = {}
            spec["success_criteria"] = sc_spec
        for sub in SUCCESS_CRITERIA_SUBFIELDS:
            if sc_spec.get(sub):
                continue
            v13_val = sc_v13.get(sub)
            if not v13_val:
                continue
            sc_spec[sub] = v13_val
            result.fields_added.append(f"success_criteria.{sub}")

    # Strategy 3 — translated fields. Re-run the PR-1 readers against v13
    # to derive the populated form. Only patches when the existing array is
    # empty; non-empty arrays are operator-meaningful and preserved.
    bp = spec.get("blueprint")
    if not isinstance(bp, dict):
        bp = {"files": []}
        spec["blueprint"] = bp
    if not bp.get("files"):
        scratch = MigrationResult(source_path=spec_path, dry_run=dry_run)
        new_bp = _build_blueprint(v13, scratch)
        if new_bp.get("files"):
            bp["files"] = new_bp["files"]
            result.fields_added.append("blueprint.files")

    deps = spec.get("dependencies")
    if not isinstance(deps, dict):
        # Don't synthesize the full dependencies skeleton here — the Spec
        # was migrated; it must already have a dependencies block. If it
        # doesn't, leave it alone and let the operator re-migrate.
        pass
    elif not deps.get("integrations"):
        new_integrations = _build_integrations(v13)
        if new_integrations:
            deps["integrations"] = new_integrations
            result.fields_added.append("dependencies.integrations")

    # Strategy 4 — v24 inferred blocks. Same inference as the current
    # migration so pre- and post-v24 Specs agree. The inference functions
    # write review warnings into a scratch MigrationResult; surface them
    # alongside the conformance warnings below.
    scratch_v24 = MigrationResult(source_path=spec_path, dry_run=dry_run)
    if not spec.get("privacy"):
        spec["privacy"] = _infer_privacy(v13, scratch_v24)
        result.fields_added.append("privacy")
    if not spec.get("audience_scoping"):
        spec["audience_scoping"] = _infer_audience_scoping(v13, scratch_v24)
        result.fields_added.append("audience_scoping")
    result.warnings.extend(scratch_v24.warnings)

    # Pass 5 — trigger-audience vocabulary conformance (non-mutating).
    # Existing triggers were authored while audience was an unpinned free
    # string (Slice-1 gap, slicing spec §6.2); report any value that
    # doesn't name a role_capabilities key so the operator can fix the
    # trigger or extend the role map. Never auto-add roles — that would
    # mint a security boundary nobody declared.
    scoping = spec.get("audience_scoping")
    rc = scoping.get("role_capabilities") if isinstance(scoping, dict) else None
    role_keys = set(rc) if isinstance(rc, dict) else set()
    triggers = spec.get("event_triggers")
    if isinstance(triggers, list):
        for idx, trigger in enumerate(triggers):
            if not isinstance(trigger, dict):
                continue
            audience = trigger.get("audience")
            if isinstance(audience, str) and audience and audience not in role_keys:
                trigger_id = trigger.get("id") or f"#{idx}"
                result.warnings.append(
                    f"event_triggers[{trigger_id}].audience {audience!r} does "
                    f"not name a role_capabilities key "
                    f"({', '.join(sorted(role_keys)) or 'none declared'}) — "
                    f"fix the trigger or add the role"
                )

    if result.fields_added and not dry_run:
        # mode=0o644 preserves the pre-consolidation perms (write_text +
        # umask); the canonical mkstemp default of 0o600 would narrow them.
        _atomic_write_json(spec_path, spec, mode=0o644)
    return result


def run_backfill(shared_dir: Path, dry_run: bool) -> tuple[int, int, int]:
    """Walk the latest migration_backup run; backfill every matching Spec.

    Returns (specs_changed, specs_skipped, errors).
    """
    backup_run = _latest_backup_run(shared_dir)
    if backup_run is None:
        print(f"[backfill] no migration_backup runs under {shared_dir}; nothing to do")
        return (0, 0, 0)
    print(f"[backfill] using backup run: {backup_run.name} (dry_run={dry_run})")

    changed = skipped = errors = 0
    for spec_id, v13 in _iter_v13_sources(backup_run):
        spec_path = _find_spec_path(shared_dir, spec_id)
        if spec_path is None:
            print(f"  - {spec_id}: SKIP (Spec not found in gallery — superseded run?)")
            skipped += 1
            continue
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! {spec_id} @ {spec_path}: error reading: {e}", file=sys.stderr)
            errors += 1
            continue

        res = backfill_one(spec_path, spec, v13, dry_run=dry_run)
        if res.changed:
            verb = "WOULD ADD" if dry_run else "added"
            print(f"  ✓ {spec_id}: {verb} {', '.join(res.fields_added)} "
                  f"→ {spec_path.relative_to(shared_dir)}")
            changed += 1
        else:
            skipped += 1
        for w in res.warnings:
            print(f"    ⚠ {spec_id}: {w}")

    print(f"[backfill] done — changed={changed}, skipped={skipped}, errors={errors}")
    return (changed, skipped, errors)


def _iter_repo_gallery_sources(repo_gallery: Path):
    """Yield (spec_id, source_dict, source_path) for every in-repo gallery file.

    Layout (canonical): ``<repo_gallery>/<app_name>/p-<id>.json`` — one
    Spec per app directory. The pkg_id inside the JSON wins for spec_id;
    the filename is informational.

    identity: see resolve_app_id — NOT swept (AL-1.4b). The value returned is
    a SPEC-ID, used as the gallery directory key
    (``gallery/<tier>/<spec_id>/<version>.json``), so the chain is
    deliberately ``pkg_id`` then ``spec_id`` and nothing else. Every in-repo
    gallery source also carries a top-level ``id`` holding the app SCRIPT
    name (``app_task_manager``) — ``resolve_app_id`` ranks that ABOVE
    ``spec_id``, so a source missing ``pkg_id`` would silently backfill into
    a ``gallery/local/app_task_manager/`` directory no reader looks in,
    instead of the explicit SKIP below. (This is the same trap #3681 found in
    ``gallery/index.json``'s ``app_id`` key.)
    """
    for src_path in sorted(repo_gallery.glob("*/p-*.json")):
        try:
            src = json.loads(src_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! {src_path.name}: read error: {e}", file=sys.stderr)
            continue
        spec_id = (src.get("pkg_id") or src.get("spec_id") or "").strip()
        if not spec_id:
            print(f"  - {src_path.name}: SKIP (no pkg_id/spec_id in source)")
            continue
        yield spec_id, src, src_path


def run_backfill_from_repo_gallery(
    shared_dir: Path,
    repo_gallery: Path,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Walk the in-repo gallery directly; backfill matching Specs in
    ``{shared_dir}/gallery/{local,builtin,imported}``.

    This is the BUILTIN gallery analog of ``run_backfill``. The migration
    backup the latter walks only contains ``v13_source`` ops (from the
    Instance-migration code path); the gallery-migration code path
    (``migrate_gallery_package``) records ops as ``kind: gallery_spec`` with
    no v13 source attached, so the migration_backup flow can't reach those
    Specs. This walks the in-repo source directly instead.

    Bonus: the in-repo source is always the latest version, not a historical
    snapshot from the day of migration — so if an operator updates a gallery
    Spec source, the next backfill picks up the fresh content.

    Returns ``(specs_changed, specs_skipped, errors)``.
    """
    if not repo_gallery.is_dir():
        print(f"[backfill] repo gallery not found: {repo_gallery}; "
              "skipping gallery-source path")
        return (0, 0, 0)
    print(f"[backfill] walking repo gallery: {repo_gallery} (dry_run={dry_run})")

    changed = skipped = errors = 0
    for spec_id, src, src_path in _iter_repo_gallery_sources(repo_gallery):
        # Restrict the lookup to the builtin tier. The unscoped lookup walks
        # local → builtin → imported and short-circuits on the first match —
        # which means a Spec installed as a local Instance copy (e.g. Journal
        # on a bot) would shadow its `gallery/builtin/` counterpart and the
        # builtin file would never get backfilled. The in-repo source is the
        # canonical source of truth for the builtin tier; target it directly.
        spec_path = _find_spec_path(shared_dir, spec_id, tier="builtin")
        if spec_path is None:
            print(f"  - {spec_id}: SKIP (no migrated Spec in gallery/builtin — not yet deployed?)")
            skipped += 1
            continue
        try:
            spec = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! {spec_id} @ {spec_path}: error reading: {e}", file=sys.stderr)
            errors += 1
            continue

        res = backfill_one(spec_path, spec, src, dry_run=dry_run)
        if res.changed:
            verb = "WOULD ADD" if dry_run else "added"
            print(f"  ✓ {spec_id}: {verb} {', '.join(res.fields_added)} "
                  f"→ {spec_path.relative_to(shared_dir)}")
            changed += 1
        else:
            skipped += 1
        for w in res.warnings:
            print(f"    ⚠ {spec_id}: {w}")

    print(f"[backfill] repo-gallery done — "
          f"changed={changed}, skipped={skipped}, errors={errors}")
    return (changed, skipped, errors)


def _repo_gallery_default_path() -> Path:
    """The in-repo gallery dir under the platform's deploy checkout.

    Derived from ``platform_profile.get_profile().deploy_checkout_default``
    — the deploy checkout the repo-puller maintains and every daemon loads
    from. The ``gallery/`` dir is a child of that checkout. On macOS the
    checkout is a *sibling* of the shared dir (``/Users/Shared/evolve`` ↔
    ``/Users/Shared/evolve-repo``), but on Linux it is a *child*
    (``/var/lib/evolve`` shared dir ↔ ``/var/lib/evolve/repo`` checkout).
    Deriving from ``dirname(shared_dir) + "evolve-repo"`` only held on
    macOS — on Linux it pointed at a nonexistent ``/var/lib/evolve-repo``.
    """
    from platform_profile import get_profile

    return Path(get_profile().deploy_checkout_default) / "gallery"


def _default_repo_gallery() -> Optional[Path]:
    """Default repo-gallery location, or ``None`` when it isn't on disk.

    Wraps :func:`_repo_gallery_default_path` with an existence gate so the
    backfill degrades gracefully (finds no builtin gallery) rather than
    failing when the checkout's ``gallery/`` dir is absent.
    """
    cand = _repo_gallery_default_path()
    return cand if cand.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shared-dir", required=True, type=Path,
                   help="Pod shared dir (typically /Users/Shared/evolve)")
    p.add_argument("--repo-gallery", type=Path, default=None,
                   help=("In-repo gallery dir (default: the deploy checkout's gallery/ dir, "
                         "from platform_profile.deploy_checkout_default). "
                         "Walks <repo-gallery>/<name>/p-*.json and backfills the corresponding "
                         "gallery/builtin/<spec_id>/* Specs from each in-repo source. This is the "
                         "path the BUILTIN gallery needs — Instance-migrated Specs use the "
                         "migration_backup path which runs in parallel."))
    p.add_argument("--no-repo-gallery", action="store_true",
                   help="Skip the repo-gallery walk; only run the migration_backup path.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Show what would change; write nothing")
    mode.add_argument("--apply", action="store_true",
                      help="Apply the backfill (atomic per-Spec)")
    args = p.parse_args(argv)

    if not args.shared_dir.is_dir():
        print(f"--shared-dir not a directory: {args.shared_dir}", file=sys.stderr)
        return 2

    # Path 1 — migration_backup walk (handles Instance-migrated Specs in
    # gallery/local/ that were tracked via v13_source backup ops).
    inst_changed, _, inst_errors = run_backfill(args.shared_dir, dry_run=args.dry_run)

    # Path 2 — repo-gallery walk (handles BUILTIN-migrated Specs whose
    # source isn't in any migration_backup; we go to the source of truth).
    gal_changed = gal_errors = 0
    if not args.no_repo_gallery:
        repo_gallery = args.repo_gallery or _default_repo_gallery()
        if repo_gallery is None:
            print("[backfill] no --repo-gallery provided and no default found "
                  f"(tried {_repo_gallery_default_path()}); "
                  "skipping repo-gallery path")
        else:
            gal_changed, _, gal_errors = run_backfill_from_repo_gallery(
                args.shared_dir, repo_gallery, dry_run=args.dry_run,
            )

    total_changed = inst_changed + gal_changed
    total_errors = inst_errors + gal_errors
    print(f"[backfill] total — changed={total_changed}, errors={total_errors}")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())

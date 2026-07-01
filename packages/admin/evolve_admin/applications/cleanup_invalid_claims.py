"""
cleanup_invalid_claims.py — one-time, reversible sweep that removes already-
persisted ``invalid_claim`` entries from v7-arc Instance ``realized_files[]``.

The situation this cleans
─────────────────────────
Before the writer-hygiene gate (F-B1) landed, the ``realized_files[]`` write
sites (``migrate_v7._build_realized_files`` from a legacy ``files[]``, and
``extend_application``) had no ownership check, so they minted claims onto paths
no application may ever own — manifest-store self-references, cryptographic
salt/secret material, append-only runtime logs, telemetry indices. The recon
ledger correctly *flags* each such persisted claim as ``invalid_claim`` on every
scan (``recon_ledger`` pass 2 keystone), but the only remediation
(``remove_from_realized_files``) has no auto-apply endpoint — so an invalid
entry, once written, persists across every scan until removed. The write-site
gate stops NEW invalid claims; this CLI clears the ones already on disk.

What it removes — and only what it removes
──────────────────────────────────────────
The authoritative set is the ``invalid_claim`` bucket of the materialized
reconciliation ledger (:func:`recon_ledger.build_recon_ledger`). This CLI does
NOT re-derive a second never-ownable list — it asks the ledger which claims are
``invalid_claim`` and removes EXACTLY those ``realized_files[]`` entries,
matched on the SAME canonical workspace-relative key
(:func:`path_keys.ws_rel_key`) the ledger used to build its join. Membership in
that ledger bucket is the over-removal guard: a ``missing_marker`` (a real file
the app legitimately owns), ``owned_ok``, ``missing_file``, or any other bucket
is NEVER touched.

Safety
──────
* **Dry-run by default.** Without ``--apply`` nothing is written; the sweep
  prints the per-bot, per-instance removal plan and exits 0. The plan it prints
  is byte-for-byte the plan ``--apply`` executes (both call
  :func:`build_removal_plan`), so an operator can review the dry-run output and
  trust ``--apply`` does exactly that and no more.
* **Reversible.** Removing a claim edits only the Instance JSON's
  ``realized_files[]`` (plus a documenting ``change_log`` entry) — it NEVER
  touches the file on disk. A wrongly-removed claim can be re-added (or
  re-discovered by the scanner / orphan-reconcile).
* **Atomic.** Each Instance is rewritten via ``evolve_util.atomic_write_json``
  (temp-file + rename), mode ``0o644`` so the bot user can still read its own
  manifest after the evolve-owned rewrite — the same contract
  ``manifest_hygiene`` uses for the orphan-attach write. No sudo / ``/tmp``
  staging is needed: the bot's ``workspace/manifests/`` carries an evolve write
  ACL (``grant_write_recursive``).
* Per-Instance try/except: one unwritable / malformed Instance is tallied as a
  failure and reported — it never sinks the whole sweep.

CLI
───
    python3 -m evolve_admin.applications.cleanup_invalid_claims --shared-dir <dir>
        # dry-run: per-bot per-instance removal plan, exits 0, changes nothing
    python3 -m evolve_admin.applications.cleanup_invalid_claims --shared-dir <dir> --apply
        # removes every invalid_claim entry; reports removed/instances/failed
    ... --bot <id> [--bot <id> ...]      # target specific bots (default: all)

Pure Python, no LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from platform_profile import get_profile as _get_profile

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso

from ..config import bot_home, load_network
from .path_keys import ws_rel_key as _ws_rel_key
from .recon_ledger import (
    Bucket,
    LedgerRow,
    ReconResult,
    build_recon_ledger,
)

_PROFILE = _get_profile()


# ── Plan model (one entry per realized_files[] claim to remove) ────────────────

@dataclass
class ClaimRemoval:
    """One ``realized_files[]`` entry slated for removal from one Instance."""
    bot_id: str
    instance_id: str
    instance_path: str
    stored_path: str        # the realized_files entry's ``path`` value, as stored
    canonical_key: str      # ws_rel_key form == the ledger row's path
    file_id: str
    reason: str             # ledger reason code (non_ownable_claim)


@dataclass
class RemovalPlan:
    """The full set of removals across all swept bots/instances."""
    removals: list[ClaimRemoval] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.removals)

    def by_bot(self) -> dict[str, list[ClaimRemoval]]:
        out: dict[str, list[ClaimRemoval]] = {}
        for r in self.removals:
            out.setdefault(r.bot_id, []).append(r)
        return out

    def by_instance(self) -> dict[str, list[ClaimRemoval]]:
        """Group by the Instance JSON path (the unit of a single atomic write)."""
        out: dict[str, list[ClaimRemoval]] = {}
        for r in self.removals:
            out.setdefault(r.instance_path, []).append(r)
        return out


def _workspace(bot_id: str) -> Path:
    return bot_home(bot_id) / ".openclaw" / "workspace"


def _iter_v7_instances(bot_id: str) -> list[tuple[Path, dict]]:
    """Yield ``(on_disk_path, instance_dict)`` for every v7-arc Instance in
    *bot_id*'s manifests dir.

    Mirrors ``recon_ledger._load_bot_instances`` (same dir, same dot/underscore
    skip, same v7-arc filter, same graceful PermissionError degrade) so the
    cleanup acts on EXACTLY the manifest set the ledger reconciled — but keeps
    the REAL file path each dict was read from. We rewrite that exact path rather
    than re-deriving ``{instance_id}.json``, so the cleanup is correct even if a
    manifest's filename ever differs from its ``instance_id`` (it never targets a
    non-existent path).
    """
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    out: list[tuple[Path, dict]] = []
    try:
        if not mdir.is_dir():
            return []
        json_files = sorted(mdir.glob("*.json"))
    except (PermissionError, OSError):
        return []
    for f in json_files:
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("manifest_shape") == "v7-arc":
            out.append((f, data))
    return out


# ── Plan construction (the SINGLE source of truth for dry-run AND apply) ───────

def build_removal_plan(
    shared_dir: Path,
    bot_ids: list[str],
    *,
    recon: Optional[ReconResult] = None,
) -> tuple[RemovalPlan, ReconResult]:
    """Build the removal plan over *bot_ids*.

    The ledger (built fresh in memory, ``persist=False``, unless one is passed
    in) is the authority: its ``invalid_claim`` bucket gives the canonical keys
    to remove. We then walk each bot's Instances and select the
    ``realized_files[]`` entries whose ``ws_rel_key`` is in that bot's
    invalid-key set — the SAME canonicalization (and the SAME instance set, via
    ``_load_bot_instances``) the ledger used to flag them, so a selected entry
    is provably one the ledger calls ``invalid_claim`` and nothing else.
    """
    result = recon or build_recon_ledger(shared_dir, bot_ids, persist=False)

    # invalid canonical keys per bot, with the ledger's reason/file_id for the
    # report. The ledger guarantees one row per path per bot, so a key maps to
    # exactly one bucket — a key present here is invalid_claim and not, say,
    # missing_marker.
    invalid_by_bot: dict[str, dict[str, LedgerRow]] = {}
    for row in result.rows(Bucket.INVALID_CLAIM):
        invalid_by_bot.setdefault(row.bot_id, {})[row.path] = row

    plan = RemovalPlan()
    for bot_id in bot_ids:
        invalid_keys = invalid_by_bot.get(bot_id)
        if not invalid_keys:
            continue
        workspace = _workspace(bot_id)
        for ipath, inst in _iter_v7_instances(bot_id):
            instance_id = inst.get("instance_id") or ipath.stem
            for rf in inst.get("realized_files") or []:
                if not isinstance(rf, dict):
                    continue
                stored_path = rf.get("path") or ""
                if not stored_path:
                    continue
                key = _ws_rel_key(stored_path, workspace)
                row = invalid_keys.get(key)
                if row is None:
                    # Not flagged invalid_claim by the ledger → KEEP. This is the
                    # over-removal guard: owned_ok / missing_marker / missing_file
                    # / attach claims never match an invalid_claim key.
                    continue
                plan.removals.append(ClaimRemoval(
                    bot_id=bot_id,
                    instance_id=instance_id,
                    instance_path=str(ipath),
                    stored_path=stored_path,
                    canonical_key=key,
                    file_id=(rf.get("file_id") or row.file_id or ""),
                    reason=row.reason,
                ))
    return plan, result


# ── Apply (atomic per-Instance rewrite) ────────────────────────────────────────

@dataclass
class RemovalReport:
    removed: int = 0
    instances_touched: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def apply_removal_plan(
    plan: RemovalPlan,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> RemovalReport:
    """Execute *plan*: for each affected Instance, drop the planned
    ``realized_files[]`` entries and atomically rewrite the Instance JSON.

    The write is re-derived from the plan's canonical keys (recomputed from the
    freshly-read on-disk entries), so apply removes EXACTLY the keys the dry-run
    listed. One bad Instance is tallied and skipped; the sweep continues.
    """
    report = RemovalReport()
    for ipath_str, removals in plan.by_instance().items():
        ipath = Path(ipath_str)
        bot_id = removals[0].bot_id
        workspace = _workspace(bot_id)
        keys_to_remove = {r.canonical_key for r in removals}
        try:
            instance = json.loads(ipath.read_text())
        except (OSError, json.JSONDecodeError) as e:
            report.failed += len(removals)
            report.failures.append((ipath_str, f"read failed: {e}"))
            continue

        realized = instance.get("realized_files") or []
        kept: list = []
        dropped: list[dict] = []
        for rf in realized:
            if (
                isinstance(rf, dict)
                and (rf.get("path") or "")
                and _ws_rel_key(rf.get("path") or "", workspace) in keys_to_remove
            ):
                dropped.append(rf)
            else:
                kept.append(rf)

        if not dropped:
            # Plan said to remove from here but nothing matched on re-read (the
            # manifest changed since the plan was built). Skip — never write a
            # no-op, and surface it (counted as failed so the summary and the
            # non-zero exit agree) so the operator can re-run the plan.
            report.failed += len(removals)
            report.failures.append((ipath_str, "no matching entry on re-read"))
            continue

        instance["realized_files"] = kept
        instance.setdefault("change_log", []).append({
            "entry_id": f"cleanup-{_now_iso()}",
            "timestamp": _now_iso(),
            "kind": "invalid_claims_removed",
            "who": "evolve",
            "description": (
                f"Removed {len(dropped)} never-ownable path(s) from "
                f"realized_files[] (recon-ledger invalid_claim cleanup)."
            ),
            "file_changes": [
                {"action": "unclaimed", "path": rf.get("path", ""),
                 "file_id": rf.get("file_id", "")}
                for rf in dropped
            ],
        })

        try:
            # mode=0o644: the Instance lives in the bot's workspace and must stay
            # readable by the bot user after evolve's rewrite (mkstemp's default
            # 0o600 would lock the bot out) — same contract as manifest_hygiene.
            _atomic_write_json(ipath, instance, mode=0o644)
        except OSError as e:
            report.failed += len(dropped)
            report.failures.append((ipath_str, f"write failed: {e}"))
            continue

        report.removed += len(dropped)
        report.instances_touched += 1
        for rf in dropped:
            log(f"  remove  [{bot_id}] {instance.get('instance_id', '?')}  {rf.get('path','')}")
    return report


# ── CLI ────────────────────────────────────────────────────────────────────────

def _discover_bot_ids(shared_dir: Path) -> list[str]:
    """All bot ids from ``{shared_dir}/network.json`` (mirrors recon_ledger)."""
    try:
        network = load_network(shared_dir / "network.json")
    except Exception:
        return []
    bots_field = network.get("bots") or {}
    if isinstance(bots_field, dict):
        return list(bots_field.keys())
    if isinstance(bots_field, list):
        return [b["id"] for b in bots_field if isinstance(b, dict) and "id" in b]
    return []


def _print_dry_run(plan: RemovalPlan) -> None:
    print("Invalid-claim cleanup — DRY RUN (no manifests changed)\n")
    if plan.total == 0:
        print("  No invalid_claim entries found. Nothing to remove.")
        return
    by_bot = plan.by_bot()
    for bot_id in sorted(by_bot):
        bot_removals = by_bot[bot_id]
        print(f"  {bot_id}  ({len(bot_removals)} claim(s) would be removed)")
        # Group by instance for legibility.
        by_inst: dict[str, list[ClaimRemoval]] = {}
        for r in bot_removals:
            by_inst.setdefault(r.instance_id, []).append(r)
        for instance_id in sorted(by_inst):
            print(f"    {instance_id}")
            for r in by_inst[instance_id]:
                print(f"        - {r.canonical_key}   ({r.reason})")
        print()
    print(f"  TOTAL would remove: {plan.total}")
    print("\n  Re-run with --apply to remove these claims (reversible; the file "
          "on disk is never touched).")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Remove already-persisted invalid_claim entries from v7-arc "
                    "Instance realized_files[] (the recon ledger's invalid_claim "
                    "bucket). Dry-run by default; reversible.",
    )
    p.add_argument(
        "--bot", action="append", default=[], dest="bots", metavar="BOT_ID",
        help="Bot to sweep (repeatable). Default: all bots in network.json.",
    )
    default_shared_dir = _PROFILE.shared_dir_default
    p.add_argument(
        "--shared-dir", default=default_shared_dir,
        help=f"Pod shared dir (default: {default_shared_dir})",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually remove the claims. Without this flag the sweep is a "
             "dry-run and changes nothing.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bots or _discover_bot_ids(shared_dir)
    if not bot_ids:
        print("ERROR: no bots to sweep (pass --bot or populate network.json)",
              file=sys.stderr)
        return 1

    plan, _result = build_removal_plan(shared_dir, bot_ids)

    if not args.apply:
        _print_dry_run(plan)
        return 0

    print(f"Invalid-claim cleanup — APPLY  ({plan.total} claim(s))\n")
    report = apply_removal_plan(plan, log=print)
    print(
        f"\n  removed {report.removed}   "
        f"instances touched {report.instances_touched}   failed {report.failed}"
    )
    if report.failures:
        print("\n  Instances that could not be cleaned (left untouched):")
        for path, why in report.failures:
            print(f"    {path}  — {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

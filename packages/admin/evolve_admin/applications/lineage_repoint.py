"""
lineage_repoint.py — dry-run-first CLI that re-points stale ``_evolve``
provenance markers carrying a *retired* spec_id onto the live successor that
inherited the file, recording the supersession lineage as it goes.

The situation this cleans
─────────────────────────
A monolithic app is **split** into N successor apps under fresh spec_ids. The
files on disk still carry the OLD (monolithic) spec_id in their ``_evolve``
marker, but no ``provenance.prior_spec_ids[]`` lineage records the split — so
the retired marker id resolves to *no* live Instance (``spec_lineage.resolve_spec``
returns ``None``). The file looks orphaned.

On bot ``atlas`` the monolithic "Atlas digest" app (retired ids ``p-c07ba3b2``
/ ``p-6e6c474a``) was split into three successors — Capture Pipeline
(``p-555f7671``), Daily Digest (``p-7b26ba5e``), and Article Capture
(``p-df9d99a3``). Several live scripts/config files still carry the retired
ids. (After the #3330 recon-ledger join fix these files are no longer mis-
bucketed as ``scrub_candidate`` — the successors' ``realized_files[]`` already
re-claim most of them, so the recon ledger calls them ``owned_ok`` — but their
*markers* are still provenance-stale: they name a spec_id that resolves to
nothing, and the ``prior_spec_ids[]`` breadcrumb is missing. This tool closes
that gap, which makes the ownership robust to ``realized_files`` drift and lets
the marker resolve on its own.)

Scope vs ``strip_stale_markers``
────────────────────────────────
This tool handles **app-ownable** files whose marker resolves to **no live
Instance at all** (every id is retired/lineage-less) — the re-pointable orphans.
A stale marker on a *never-ownable* path (OC identity/task substrate, the
platform ``evolve/`` telemetry tree, ``evolve-backup``) is ``strip_stale_markers``'
domain and is skipped here (the same ``can_app_own`` keystone the recon ledger
uses). A marker that still carries even one *live* id already resolves (owned /
attach) — also skipped; the retired extras on such a file are cosmetic cruft, a
separate lower-value "marker tidy" concern, not a re-point need.

What it does — per FILE, deterministically (the split is 1→N)
─────────────────────────────────────────────────────────────
For each app-ownable marked file whose marker resolves to **no live Instance**
(neither a current id nor an existing ``prior_spec_ids[]`` chain entry):

  * **DATA POLICY — generated dated output under ``digest/``** (``digest/*.md``,
    ``digest/source_health-*.json``) is telemetry *output*, never realized
    *source* → **STRIP-KEEP** (strip the stale marker, keep the file). Same
    class as the reserved-file scrubs ``strip_stale_markers`` handles — NOT a
    re-point.

  * else **bind to the single live successor whose ``realized_files[]`` lists
    this exact path**:
      - exactly one such successor → **RE-POINT**: record the retired spec_id(s)
        into that successor's ``prior_spec_ids[]`` (``record_spec_supersession``)
        AND rewrite the on-disk marker to the successor's current spec_id. Both
        moves are additive / reversible (the marker move is a swap, the lineage
        append removable).
      - zero, or more than one, claiming successor → **NEEDS-OPERATOR-DECISION**:
        we do NOT guess. The candidates (with their ``identity.purpose`` for
        context) are surfaced for the operator to bind by hand.

Binding is on the deterministic ``realized_files[]`` claim ONLY. ``purpose`` is
surfaced as operator context on a needs-decision row but is never auto-bound on
(too fuzzy to mutate provenance from).

Safety / reversibility
──────────────────────
* **Dry-run by default.** Without ``--apply`` nothing is written; the full
  per-file plan is printed and the tool exits 0. ``compute_plan`` is pure (no
  writes) — the dry-run path never calls a mutator.
* RE-POINT is additive + a marker *move*: the lineage append is removable and
  the marker rewrite swaps the retired id for the live one (the dry-run plan
  records the original marker so an operator can reverse by hand). STRIP-KEEP
  preserves file content (only the marker key/line is removed), mirroring
  ``strip_stale_markers``.
* Idempotent — a re-run finds nothing (the lineage now resolves the old id, so
  the marker is no longer orphaned; a re-pointed marker carries the live id; a
  stripped marker no longer parses).
* File access (CLAUDE.md): successor manifests under ``workspace/manifests/``
  carry the evolve write ACL → atomic in-place write. The workspace
  scripts/config/digest files are bot-owned → the documented ``/tmp``-stage +
  ``sudo /bin/cp`` + ``chown`` back + preserve-mode fallback. One unwritable
  file is tallied as a failure and reported; it never sinks the sweep.

CLI
───
    python3 -m evolve_admin.applications.lineage_repoint --bot atlas
        # dry-run: full per-file plan, exits 0, changes nothing
    python3 -m evolve_admin.applications.lineage_repoint --bot atlas --apply
        # records lineage + re-points markers (+ strips digest-output markers)

Pure Python, no LLM.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from platform_profile import get_profile as _get_profile

from evolve_util import atomic_write_json as _atomic_write_json

from ..config import bot_home, load_network
from .app_ownership_policy import can_app_own
from .provenance import (
    embed_marker,
    parse_marker,
    remove_pkg_id_from_marker,
    scan_workspace_marked_only,
)
from .recon_ledger import _ws_rel_key
from .spec_lineage import build_spec_index, current_spec_id

_PROFILE = _get_profile()


# ── Actions + outcomes (machine-stable) ────────────────────────────────────────

REPOINT = "re-point"
STRIP_KEEP = "strip-keep"
NEEDS_DECISION = "needs-decision"
ACTIONS: tuple[str, ...] = (REPOINT, STRIP_KEEP, NEEDS_DECISION)

APPLIED = "applied"
SKIPPED = "skipped"
FAILED = "failed"


# ── Data policy ────────────────────────────────────────────────────────────────

def is_digest_output(rel_path: str) -> bool:
    """True for a generated dated OUTPUT file under ``digest/``.

    These are telemetry outputs (daily ``*.md`` digests, ``source_health-*.json``
    health snapshots), NOT realized app *source* — the data policy strips their
    stale marker (keeping the file) rather than re-pointing it. Scoped to the
    ``digest/`` directory: app source lives under ``scripts/`` / ``atlas/``,
    never ``digest/``.
    """
    return rel_path.split("/", 1)[0] == "digest"


# ── Instance loading (with manifest paths — we write the successor back) ───────

@dataclass
class _Inst:
    """A loaded v7-arc Instance plus the manifest file it came from."""
    path: Path
    data: dict

    @property
    def spec_id(self) -> Optional[str]:
        return current_spec_id(self.data)

    @property
    def purpose(self) -> str:
        idn = self.data.get("identity")
        if isinstance(idn, dict):
            p = idn.get("purpose")
            if isinstance(p, str):
                return p
        return ""


def load_instances(bot_id: str) -> list[_Inst]:
    """Load every v7-arc Instance JSON in *bot_id*'s manifests dir, WITH paths.

    Mirrors ``recon_ledger._load_bot_instances`` but keeps the source file path
    so a re-point can write the successor manifest back. Same defensive guards
    (ACL-clamped ``.openclaw`` raises ``PermissionError`` on Linux/Py3.12 —
    degrade to "no instances" rather than crash).
    """
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    out: list[_Inst] = []
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
            out.append(_Inst(path=f, data=data))
    return out


def build_claim_index(
    instances: list[_Inst], workspace: Path
) -> dict[str, list[_Inst]]:
    """Map canonical workspace-relative path → the Instances that claim it in
    ``realized_files[]``.

    Keyed via the same ``_ws_rel_key`` the recon join uses, so a claim recorded
    as absolute (extend_application) or workspace-relative (migrate_v7/v13) meets
    the disk-walk key. A path claimed by >1 Instance is the ambiguous 1→N case
    (a file two successors both list) → it lands in needs-decision.
    """
    out: dict[str, list[_Inst]] = {}
    for inst in instances:
        for rf in inst.data.get("realized_files") or []:
            if not isinstance(rf, dict):
                continue
            path = rf.get("path") or ""
            if not path:
                continue
            key = _ws_rel_key(path, workspace)
            bucket = out.setdefault(key, [])
            if inst not in bucket:
                bucket.append(inst)
    return out


# ── Plan (PURE — no writes) ────────────────────────────────────────────────────

@dataclass
class PlanRow:
    """One file's disposition. Exactly one row per marked-with-stale-id file."""
    bot_id: str
    path: str                       # workspace-relative
    abs_path: str
    action: str                     # one of ACTIONS
    orphaned_ids: list[str] = field(default_factory=list)
    bind_spec_id: Optional[str] = None   # successor current spec_id (re-point)
    bind_manifest: Optional[str] = None  # successor manifest filename (re-point)
    candidates: list[str] = field(default_factory=list)  # needs-decision
    reason: str = ""

    def describe_action(self) -> str:
        if self.action == REPOINT:
            return f"re-point→{self.bind_spec_id}"
        if self.action == STRIP_KEEP:
            return "strip-keep"
        return "NEEDS-OPERATOR-DECISION"


def _orphaned_marker_ids(marker, spec_index: dict[str, dict]) -> list[str]:
    """The marker spec_ids that resolve to NO live Instance.

    A spec_id resolves when it is some Instance's current id OR sits in some
    Instance's ``prior_spec_ids[]`` — exactly what ``build_spec_index`` keys on
    (current id wins over a retired one on collision). An id absent from that
    index is genuinely retired with no lineage: the re-point target. An id that
    is simultaneously live somewhere is therefore NEVER treated as orphaned.
    """
    return [sid for sid in marker.spec_ids if spec_index.get(sid) is None]


def compute_plan(bot_id: str) -> list[PlanRow]:
    """Build the per-file re-point plan for *bot_id*. Pure: reads only.

    Walks every marked file in the bot workspace; emits a :class:`PlanRow` only
    for files whose marker carries at least one orphaned (retired, lineage-less)
    spec_id. Files with a healthy marker are skipped.
    """
    instances = load_instances(bot_id)
    spec_index = build_spec_index([i.data for i in instances])
    workspace = bot_home(bot_id) / ".openclaw" / "workspace"
    claims = build_claim_index(instances, workspace)

    rows: list[PlanRow] = []
    try:
        marked = scan_workspace_marked_only(workspace)
    except (PermissionError, OSError):
        return rows

    for wf in marked:
        if not wf.has_marker:
            continue
        marker = wf.marker
        assert marker is not None
        abs_path = str(wf.path)
        rel = _ws_rel_key(abs_path, workspace)
        # The manifests/ index dir is not an ownable artifact (mirrors recon).
        if rel.split("/", 1)[0] == "manifests":
            continue

        # KEYSTONE: only app-ownable paths are in scope. A stale marker on a
        # never-ownable path (OC identity/task substrate, the platform evolve/
        # telemetry tree, evolve-backup) is strip_stale_markers' domain — it
        # gets scrubbed, never bound to an app. Mirrors recon_ledger's
        # can_app_own keystone so the two tools never contend for the same file.
        if not can_app_own(rel):
            continue

        orphaned = _orphaned_marker_ids(marker, spec_index)
        # Only target a marker that resolves to NO live Instance at all (every
        # id orphaned). A marker that still carries even one LIVE id already
        # resolves — the retired extras are cosmetic cruft, not a re-point need
        # (and a shared file like operator.json that lists live + retired ids is
        # owned via the live id). This is exactly the recon ledger's
        # attach-vs-unresolvable split: marker resolves → owned/attach (skip);
        # resolves to nothing → our domain.
        live_ids = [s for s in marker.spec_ids if s not in orphaned]
        if live_ids or not orphaned:
            continue  # resolves via a live id, or healthy — nothing to do

        # DATA POLICY: generated dated digest output → strip-keep (not re-point).
        if is_digest_output(rel):
            rows.append(PlanRow(
                bot_id=bot_id, path=rel, abs_path=abs_path, action=STRIP_KEEP,
                orphaned_ids=orphaned,
                reason="generated dated digest output — strip stale marker, keep file",
            ))
            continue

        # Bind to the single successor whose realized_files[] lists this path.
        claimants = claims.get(rel, [])
        live_claimants = [c for c in claimants if c.spec_id]
        if len(live_claimants) == 1:
            succ = live_claimants[0]
            rows.append(PlanRow(
                bot_id=bot_id, path=rel, abs_path=abs_path, action=REPOINT,
                orphaned_ids=orphaned, bind_spec_id=succ.spec_id,
                bind_manifest=succ.path.name,
                reason=f"sole realized_files claimant: {succ.path.name}",
            ))
        else:
            cands = [
                f"{c.spec_id} ({c.path.name})" for c in live_claimants
            ]
            why = (
                "no live successor claims this path"
                if not live_claimants
                else f"{len(live_claimants)} successors claim this path"
            )
            rows.append(PlanRow(
                bot_id=bot_id, path=rel, abs_path=abs_path,
                action=NEEDS_DECISION, orphaned_ids=orphaned,
                candidates=cands, reason=why,
            ))

    rows.sort(key=lambda r: (ACTIONS.index(r.action), r.path))
    return rows


# ── Apply (the mutation path) ──────────────────────────────────────────────────

@dataclass
class ApplyReport:
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    needs_decision: int = 0
    touched: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _transform_marker_in_place(
    path: Path, *, repoint_to: Optional[str], strip_ids: list[str]
) -> bool:
    """Apply the marker transform to *path* in place (no sudo).

    RE-POINT (``repoint_to`` set): add the successor's current spec_id FIRST
    (``embed_marker`` merge — preserves the existing ``file`` id and keyword),
    THEN remove every orphaned id. Add-before-remove never transits an empty
    marker (which would strip the marker entirely).

    STRIP-KEEP (``repoint_to`` None): remove every orphaned id; removing the
    last id strips the marker, preserving file content.

    Returns True if anything changed. May raise ``OSError`` / ``PermissionError``
    from the underlying atomic write (the caller catches and retries via sudo).
    """
    changed = False
    if repoint_to is not None:
        before = parse_marker(path)
        if before is None or not before.is_valid():
            return False
        if repoint_to not in before.spec_ids:
            embed_marker(path, pkg_ids=[repoint_to], file_id="", merge=True)
            changed = True
    for sid in strip_ids:
        if remove_pkg_id_from_marker(path, sid):
            changed = True
    return changed


def _transform_marker_via_sudo(
    path: Path, bot_id: str, *, repoint_to: Optional[str], strip_ids: list[str]
) -> str:
    """Fallback for bot-owned workspace files the ``evolve`` user cannot rewrite
    in place. Stage in ``/tmp`` (evolve-writable), apply the same transform,
    ``sudo /bin/cp`` back, ``chown`` to the bot, restore the original mode.

    Returns :data:`APPLIED` / :data:`SKIPPED` / :data:`FAILED`. Mirrors
    ``strip_stale_markers._strip_via_sudo`` so the perms contract stays in one
    shape.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (PermissionError, OSError):
        r = subprocess.run(
            ["sudo", _PROFILE.cat, str(path)], capture_output=True, text=True
        )
        if r.returncode != 0:
            return FAILED
        content = r.stdout

    try:
        orig_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        orig_mode = 0o644

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-repoint-{bot_id}-", suffix=path.suffix
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        if not _transform_marker_in_place(
            tmp_path, repoint_to=repoint_to, strip_ids=strip_ids
        ):
            return SKIPPED
        cp = subprocess.run(
            ["sudo", _PROFILE.cp, tmp, str(path)], capture_output=True, text=True
        )
        if cp.returncode != 0:
            return FAILED
        subprocess.run(
            ["sudo", _PROFILE.chown, f"{bot_id}:staff", str(path)],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["sudo", _PROFILE.chmod, format(orig_mode, "03o"), str(path)],
            capture_output=True, text=True,
        )
        return APPLIED
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _apply_marker(
    path: Path, bot_id: str, *, repoint_to: Optional[str], strip_ids: list[str]
) -> str:
    """In-place first, sudo-staging fallback. Returns an outcome constant."""
    try:
        changed = _transform_marker_in_place(
            path, repoint_to=repoint_to, strip_ids=strip_ids
        )
        return APPLIED if changed else SKIPPED
    except (PermissionError, OSError):
        try:
            return _transform_marker_via_sudo(
                path, bot_id, repoint_to=repoint_to, strip_ids=strip_ids
            )
        except Exception:
            return FAILED


def _save_successor_manifest(inst: _Inst) -> None:
    """Atomic write-back of a successor manifest (records the lineage append).

    Manifests under ``workspace/manifests/`` carry the evolve write ACL, so the
    same-dir temp+rename works in place; on the rare pre-first-scan bot without
    that ACL we fall back to manifest.py's /tmp-stage + sudo path (mirrors
    ``native_write._write_instance_json``).
    """
    try:
        _atomic_write_json(inst.path, inst.data, mode=0o644)
    except (PermissionError, FileNotFoundError):
        from .manifest import _write_manifest_bytes
        _write_manifest_bytes(
            inst.path, json.dumps(inst.data, indent=2).encode("utf-8")
        )


def apply_plan(
    rows: list[PlanRow],
    bot_id: str,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> ApplyReport:
    """Execute a re-point plan.

    Order of operations:
      1. Record supersession on each successor Instance (accumulate ALL retired
         ids that bind to it across the re-point rows), then write each touched
         successor manifest ONCE. Lineage first so that even if a later marker
         rewrite fails the retired id already resolves via the chain.
      2. Re-point / strip each on-disk marker.

    needs-decision rows are reported and left untouched. Per-file try/except:
    one unwritable file is a tallied failure; the sweep continues.
    """
    from .spec_lineage import record_spec_supersessions

    report = ApplyReport()
    instances = {i.path.name: i for i in load_instances(bot_id)}

    # ── Phase 1: lineage on successors (dedup retired ids per successor) ───────
    by_successor: dict[str, set[str]] = {}
    for r in rows:
        if r.action == REPOINT and r.bind_manifest:
            by_successor.setdefault(r.bind_manifest, set()).update(r.orphaned_ids)
    for manifest_name, retired_ids in by_successor.items():
        inst = instances.get(manifest_name)
        if inst is None:
            report.failures.append((manifest_name, "successor manifest vanished"))
            report.failed += 1
            continue
        record_spec_supersessions(inst.data, sorted(retired_ids))
        try:
            _save_successor_manifest(inst)
            log(f"  lineage [{bot_id}] {manifest_name} += {sorted(retired_ids)}")
        except Exception as e:  # never sink the sweep
            report.failures.append((manifest_name, f"manifest write: {e}"))
            report.failed += 1

    # ── Phase 2: re-point / strip the on-disk markers ─────────────────────────
    for r in rows:
        if r.action == NEEDS_DECISION:
            report.needs_decision += 1
            log(f"  decide  [{bot_id}] {r.path} ({'; '.join(r.candidates) or 'no candidates'})")
            continue
        path = Path(r.abs_path) if r.abs_path else None
        if path is None:
            report.failed += 1
            report.failures.append((r.path, "no abs_path on plan row"))
            continue
        repoint_to = r.bind_spec_id if r.action == REPOINT else None
        outcome = _apply_marker(
            path, bot_id, repoint_to=repoint_to, strip_ids=r.orphaned_ids
        )
        if outcome == APPLIED:
            report.applied += 1
            report.touched.append(r.path)
            log(f"  {r.action:<10} [{bot_id}] {r.path} -> {r.describe_action()}")
        elif outcome == SKIPPED:
            report.skipped += 1
            log(f"  skip       [{bot_id}] {r.path} (marker already clean)")
        else:
            report.failed += 1
            report.failures.append((r.path, "unwritable"))
            log(f"  FAIL       [{bot_id}] {r.path} (could not rewrite marker)")
    return report


# ── CLI ─────────────────────────────────────────────────────────────────────────

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


def _print_dry_run(bot_id: str, rows: list[PlanRow]) -> None:
    print(f"Lineage re-point — DRY RUN [{bot_id}] (no files changed)\n")
    if not rows:
        print("  No stale-marker files found. Nothing to re-point.")
        return
    counts = {a: sum(1 for r in rows if r.action == a) for a in ACTIONS}
    print(
        f"  {counts[REPOINT]} re-point · {counts[STRIP_KEEP]} strip-keep · "
        f"{counts[NEEDS_DECISION]} needs-decision\n"
    )
    for r in rows:
        print(f"  {r.path}")
        print(f"      action:  {r.describe_action()}")
        print(f"      marker:  retired {', '.join(r.orphaned_ids)}")
        if r.action == NEEDS_DECISION:
            if r.candidates:
                print(f"      candidates: {'; '.join(r.candidates)}")
            print(f"      reason:  {r.reason} — NOT guessed; bind by hand")
        else:
            print(f"      reason:  {r.reason}")
        print()
    if counts[REPOINT] and counts[NEEDS_DECISION]:
        print(
            "  NOTE: applying the re-points records retired→successor lineage,\n"
            "  which may also auto-resolve a needs-decision row that shares the\n"
            "  same retired id (its on-disk marker is never rewritten). Bind any\n"
            "  needs-decision file you care about BEFORE --apply if you want an\n"
            "  explicit owner.\n"
        )
    print("  Re-run with --apply to record lineage + re-point the markers.")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Re-point stale evolve provenance markers (retired spec_id) "
                    "onto the live successor that inherited the file, recording "
                    "the supersession lineage. Dry-run by default.",
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
        help="Actually record lineage + re-point markers. Without this flag the "
             "sweep is a dry-run and changes nothing.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bots or _discover_bot_ids(shared_dir)
    if not bot_ids:
        print("ERROR: no bots to sweep (pass --bot or populate network.json)",
              file=sys.stderr)
        return 1

    exit_code = 0
    for bot_id in bot_ids:
        rows = compute_plan(bot_id)
        if not args.apply:
            _print_dry_run(bot_id, rows)
            continue
        print(f"Lineage re-point — APPLY [{bot_id}]  ({len(rows)} row(s))\n")
        report = apply_plan(rows, bot_id, log=print)
        print(
            f"\n  applied {report.applied}   skipped {report.skipped}   "
            f"needs-decision {report.needs_decision}   failed {report.failed}"
        )
        if report.failures:
            print("\n  Rows that could not be applied (left untouched):")
            for path, why in report.failures:
                print(f"    {path}  — {why}")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

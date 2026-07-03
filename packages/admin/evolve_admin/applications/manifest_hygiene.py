"""
manifest_hygiene.py — Auto-reconcile orphan files surfaced by Reflect.

When a bot writes a file with a stamped spec marker but skips the
``extend_application`` write path, the file lands on disk with a marker
pointing at a spec_id while no Instance's ``realized_files[]`` references
it. Reflect surfaces these as ``orphan_file`` findings (see ``reflect.py``).

This module closes the gap mechanically, and it does so through the SAME
resolution authority the classifier uses. ``reflect()`` is a thin reader of
``recon_ledger.build_recon_ledger`` (see ``reflect.py``): its ``orphan_file``
findings ARE the recon ledger's ``attach_candidate`` bucket — lineage-resolved
(a retired ``spec_id`` in a marker still resolves to its live Instance via
``spec_lineage.resolve_spec``) and ownership-policy filtered (never-ownable
paths are routed to ``scrub_candidate``, never offered for attach). Each
finding's ``spec_id`` is the *resolved current* spec_id the classifier bound
the marker to. This module consumes that output: for each orphan it maps the
already-resolved spec_id to its owning Instance and appends the file to that
Instance's ``realized_files[]``.

Because classification and action share one resolver, the invariant holds: a
file the classifier placed in ``attach_candidate`` is ALWAYS attachable here —
``no_instance_for_spec_id`` is impossible for a real candidate (it can only
appear if a *targeted* request names a path that is not an attach candidate).
This is the fix for two historical bugs in the same family:

  1. A second, non-lineage resolver (``spec_index.get(primary)`` over current
     ids only) returned ``no_instance_for_spec_id`` for a candidate the
     classifier had resolved via lineage.
  2. The action resolved the owning manifest through ``instance_id`` (→
     ``manifests/{instance_id}.json``), so a ``discovered`` app — manifest+Spec
     on disk, but no materialized Instance yet (``instance_id is None``,
     ``realized_files == []``) — failed with ``no_instance_for_spec_id`` even
     though the classifier had placed its files in ``attach_candidate``. The
     action now resolves the owning manifest to its on-disk PATH (mapping the
     resolved Instance dict back to the file it loaded from, by identity), so a
     null instance_id no longer blocks the attach. Attaching appends to
     ``realized_files[]`` only — it never flips ``definition_status``, so a
     discovered app stays discovered.

The reconcile is deterministic — there's no judgment call when the marker
is unambiguous. Edge cases (multiple spec_ids in one marker, >1 Instance
sharing one current spec_id, file already in ``realized_files[]``) are
surfaced via per-spec counts in the returned ``ReconcileResult`` so the
operator can see exactly what was resolved versus left alone.

Multi-spec policy
-----------------
A marker can carry multiple spec_ids when a file is shared between Specs
(see ``provenance.py`` and ``ProvenanceMarker.pkg_ids``). For v1 we treat
the FIRST spec_id as the canonical owner — that's the entry the file gets
added to. Secondary spec_ids are recorded in the result for visibility
but don't trigger a second write. This matches the contract of
``extend_application``, which always stamps a single owning spec_id.

CLI:
    python3 -m evolve_admin.applications.manifest_hygiene --bot-id team_bot_a [--apply]

Default is dry-run: prints what would happen but doesn't mutate. Pass
``--apply`` to perform the writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso

from ..config import bot_home, load_network
from .reflect import reflect


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ResolvedOrphan:
    """One orphan file that auto-reconcile attached to an Instance."""
    file_path: str
    spec_id: str
    instance_id: str
    secondary_spec_ids: list[str] = field(default_factory=list)


@dataclass
class AmbiguousOrphan:
    """An orphan whose primary spec_id maps to multiple Instances on this bot.

    With the v7-arc one-Instance-per-Spec invariant this should never happen,
    but we surface it explicitly rather than picking arbitrarily.
    """
    file_path: str
    spec_id: str
    instance_ids: list[str]


@dataclass
class UnmatchedOrphan:
    """An orphan that could not be attached.

    Under the shared-resolver contract a real ``attach_candidate`` always
    resolves, so the only live reason is ``not_attach_candidate`` — a *targeted*
    request named a path the classifier does not place in the attach bucket
    (already attached, reclassified to scrub, or no longer present). The legacy
    ``no_instance_for_spec_id`` / ``no_spec_id_in_marker`` reasons are kept for
    the defensive (should-be-impossible) paths and back-compat.
    """
    file_path: str
    spec_id: str
    reason: str  # "not_attach_candidate" | "no_instance_for_spec_id" | "no_spec_id_in_marker"


@dataclass
class ReconcileResult:
    """Aggregate over a single bot's reconcile pass."""
    bot_id: str
    applied: bool                            # False = dry-run, True = writes happened
    resolved: list[ResolvedOrphan] = field(default_factory=list)
    ambiguous: list[AmbiguousOrphan] = field(default_factory=list)
    unmatched: list[UnmatchedOrphan] = field(default_factory=list)
    skipped_already_listed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts_by_spec(self) -> dict[str, dict[str, int]]:
        """Per spec_id → {resolved, ambiguous, unmatched} counts for UI rollup."""
        out: dict[str, dict[str, int]] = {}
        for r in self.resolved:
            bucket = out.setdefault(r.spec_id, {"resolved": 0, "ambiguous": 0, "unmatched": 0})
            bucket["resolved"] += 1
        for a in self.ambiguous:
            bucket = out.setdefault(a.spec_id, {"resolved": 0, "ambiguous": 0, "unmatched": 0})
            bucket["ambiguous"] += 1
        for u in self.unmatched:
            bucket = out.setdefault(u.spec_id or "(none)", {"resolved": 0, "ambiguous": 0, "unmatched": 0})
            bucket["unmatched"] += 1
        return out

    def summary(self) -> str:
        mode = "APPLIED" if self.applied else "DRY-RUN"
        return (
            f"[{self.bot_id}] {mode} "
            f"resolved={len(self.resolved)} "
            f"ambiguous={len(self.ambiguous)} "
            f"unmatched={len(self.unmatched)} "
            f"already_listed={len(self.skipped_already_listed)}"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _app_label(inst: dict, path: Path) -> str:
    """Operator-facing identifier for the app a manifest describes.

    The materialized ``instance_id`` when present, else the manifest ``id`` /
    file stem — a ``discovered`` app (manifest+Spec exist, no materialized
    Instance) carries ``instance_id == None``, so its label falls back to the
    stable manifest id. Surfaced in the success toast and the ambiguous list.
    """
    return inst.get("instance_id") or inst.get("id") or path.stem


def _normalize_path(p: str, workspace: Path) -> str:
    """Comparable key for matching an orphan file_path (Reflect emits these as
    absolute on-disk strings) against a ``realized_files[]`` entry (stored
    absolute by extend_application, or workspace-relative by migrate_v7/v13).

    A relative entry is anchored to *workspace* BEFORE resolving, so the key is
    never bound to the daemon CWD — the ``admin-ui`` LaunchDaemon sets no
    ``WorkingDirectory`` (CWD ``/``), so ``Path("scripts/x.py").resolve()`` would
    yield ``/scripts/x.py`` and silently break the idempotency match, appending
    a duplicate realized_files entry on every re-run (the #3303 join-key class).
    Mirrors ``sync._abs`` / ``recon_ledger._ws_rel_key``."""
    path = Path(p)
    if not path.is_absolute():
        path = workspace / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


# ── Core ──────────────────────────────────────────────────────────────────────

def reconcile_orphan_markers(
    bot_id: str,
    *,
    apply: bool = False,
    shared_dir: Path = Path("/Users/Shared/evolve"),
    paths: Optional[list[str]] = None,
) -> ReconcileResult:
    """Attach the files Reflect surfaces as ``attach_candidate`` to the
    manifest the classifier already resolved their marker to.

    Reflect is a thin reader of the recon ledger, so its ``orphan_file``
    findings ARE the ledger's ``attach_candidate`` bucket and each finding's
    ``spec_id`` is the *resolved current* spec_id the classifier bound the
    marker to (lineage-aware: a retired marker id already resolved to its live
    app). This function maps that already-resolved spec_id to its owning
    manifest — through the SAME ``build_spec_index`` the classifier used, then
    back to that manifest's on-disk Path by dict identity — so it never
    re-resolves from a second, divergent index. Resolving to the Path (not the
    ``instance_id``) means a ``discovered`` app with no materialized Instance
    (``instance_id is None``) is still attachable. That single resolution
    authority is what guarantees a candidate is always attachable.

    Args:
        bot_id: Bot to reconcile.
        apply: When False (default), report only — no writes. When True,
            mutate the owning manifest JSONs via atomic temp-file + rename
            (``realized_files[]`` append only; ``definition_status`` untouched).
        shared_dir: Pod shared dir. Forwarded to ``reflect()``.
        paths: Optional targeted subset. When ``None`` (default), every
            attach candidate is processed ("Attach all"). When a list, only the
            candidates whose path matches are attached (per-row Attach / a batch
            of specific rows); a requested path that is not an attach candidate
            (already attached, reclassified, or gone) is surfaced in
            ``unmatched`` with reason ``not_attach_candidate``. Paths may be
            absolute or workspace-relative — both forms normalize to the same
            key (CWD-free; see ``_normalize_path`` / #3303).

    Returns:
        ReconcileResult with per-orphan classification:
          - resolved: orphan attached to an Instance (apply=True actually
            wrote; apply=False would have written)
          - ambiguous: the resolved current spec_id is shared by >1 Instance on
            this bot (violates the v7-arc one-Instance-per-Spec invariant) — we
            refuse to pick one arbitrarily
          - unmatched: a *targeted* path that is not an attach candidate
          - skipped_already_listed: file path was already in the target
            Instance's realized_files[] (idempotent re-run case)

    Idempotency: after a successful apply the file is claimed+marked → the recon
    ledger classifies it OWNED_OK (not attach_candidate) → Reflect no longer
    surfaces it, so a re-run is a no-op.
    """
    result = ReconcileResult(bot_id=bot_id, applied=apply)
    # Workspace root for CWD-free path normalization (anchors relative
    # realized_files[] entries before resolving — see _normalize_path / #3303).
    workspace = bot_home(bot_id) / ".openclaw" / "workspace"

    # Step 1: get Reflect findings — the thin reader of the recon ledger, the
    # SAME classification surface the UI renders. orphan_file == attach_candidate.
    reflect_result = reflect(bot_id, shared_dir)
    result.warnings.extend(reflect_result.warnings)

    orphans = [f for f in reflect_result.findings if f.kind == "orphan_file"]

    # Step 1b: optional targeted filter. None → attach all. A list → only the
    # requested paths; a requested path that is not an attach candidate is
    # surfaced as unmatched/not_attach_candidate (stale UI row, already attached,
    # reclassified to scrub) rather than silently doing nothing.
    if paths is not None:
        requested = {_normalize_path(p, workspace) for p in paths if p}
        by_norm = {_normalize_path(o.file_path, workspace): o for o in orphans}
        orphans = [o for o in orphans
                   if _normalize_path(o.file_path, workspace) in requested]
        for norm in sorted(requested - set(by_norm)):
            result.unmatched.append(UnmatchedOrphan(
                file_path=norm,
                spec_id="",
                reason="not_attach_candidate",
            ))

    if not orphans:
        return result

    # Step 2: load manifests ONCE via the recon ledger's own loader (the same
    # instance set the classifier walked), PAIRED with their on-disk Path, and
    # build the lineage-aware index. We resolve the owning manifest + detect
    # ambiguity from this single source.
    #
    # The classifier places a file in attach_candidate whenever its marker
    # resolves through ``build_spec_index`` to a live app. That set INCLUDES a
    # ``discovered`` app — manifest+Spec on disk, but no materialized Instance
    # yet (``instance_id is None``, ``realized_files == []``). The old action
    # resolved the owner to ``instance_id`` and then to ``manifests/{id}.json``;
    # a null instance_id broke both, so a candidate the modal showed fell to
    # ``unmatched`` ("No longer an attach candidate"). We instead resolve the
    # owning manifest to its PATH by mapping the resolved Instance dict back to
    # the file it was loaded from (identity), through the very index the
    # classifier used. classify == act: every attach_candidate is attachable,
    # including discovered apps and prior-spec-chain markers.
    from .provenance import parse_marker
    from .recon_ledger import _load_bot_instances_with_paths
    from .spec_lineage import build_spec_index, current_spec_id

    pairs = _load_bot_instances_with_paths(bot_id)
    instances = [inst for _, inst in pairs]
    spec_index = build_spec_index(instances)  # lineage-aware spec_id -> Instance
    # Map a resolved Instance dict back to its on-disk manifest Path by object
    # identity — works whether or not the manifest carries an instance_id.
    path_by_identity = {id(inst): p for p, inst in pairs}
    inst_by_path: dict[Path, dict] = {p: inst for p, inst in pairs}
    # current spec_id -> [manifest Path, ...]: the one-app-per-Spec ambiguity
    # check, keyed on every v7-arc manifest (materialized OR discovered), not
    # just those with a non-null instance_id — two manifests sharing a current
    # spec_id is ambiguous regardless of materialization.
    by_current: dict[str, list[Path]] = {}
    for p, inst in pairs:
        sid = current_spec_id(inst)
        if sid:
            by_current.setdefault(sid, []).append(p)

    # Buffer mutations per owning manifest Path (string key) so a single
    # manifest JSON read+write captures every orphan bound to it. Without
    # buffering, N orphans on one manifest would mean N read-mutate-write cycles.
    pending_writes: dict[str, list[ResolvedOrphan]] = {}

    for orphan in orphans:
        # The classifier already resolved this marker (lineage-aware) to a live
        # app: finding.spec_id is the spec_id it bound to (the resolved CURRENT
        # id, or — in the degenerate no-current-id case — the matched marker id).
        resolved_spec = orphan.spec_id or ""

        # Ambiguity gate FIRST: >1 manifest shares this as a CURRENT spec_id.
        # The lineage index (build_spec_index) collapses that to one manifest by
        # last-writer-wins, so the classifier silently picks one — we refuse to
        # attach to a possibly WRONG app and surface it for triage instead.
        current_owner_paths = by_current.get(resolved_spec, [])
        if len(current_owner_paths) > 1:
            result.ambiguous.append(AmbiguousOrphan(
                file_path=orphan.file_path,
                spec_id=resolved_spec,
                instance_ids=[
                    _app_label(inst_by_path[p], p) for p in current_owner_paths
                ],
            ))
            continue

        # Resolve the owning manifest through the SAME index the classifier used
        # (build_spec_index covers current AND retired prior_spec_ids), then map
        # the resolved Instance dict back to its on-disk Path by identity. The
        # action lands on exactly the manifest the classifier resolved — never a
        # second, divergent map — and works even when ``instance_id`` is None (a
        # discovered app), the gap that turned attach_candidate into unmatched.
        owner_inst = spec_index.get(resolved_spec)
        owner_path = (
            path_by_identity.get(id(owner_inst)) if owner_inst is not None else None
        )
        if owner_inst is None or owner_path is None:
            # INVARIANT: unreachable for a real attach_candidate (its spec_id is
            # a key in the very index the classifier resolved through, and that
            # Instance came from a manifest with a known Path). Surface
            # defensively rather than crash if a future skew ever produced one.
            result.unmatched.append(UnmatchedOrphan(
                file_path=orphan.file_path,
                spec_id=resolved_spec,
                reason="no_instance_for_spec_id",
            ))
            continue

        owner_label = _app_label(owner_inst, owner_path)

        # secondary_spec_ids: the marker's other spec_ids, for visibility only
        # (no second write). The matched id is the marker id that resolves to the
        # SAME Instance (by identity, lineage-aware) — so a retired primary isn't
        # mislabeled a secondary regardless of current-vs-prior resolution.
        secondaries: list[str] = []
        try:
            marker = parse_marker(Path(orphan.file_path))
        except Exception as e:
            result.warnings.append(
                f"marker re-parse failed for {orphan.file_path}: {e}")
            marker = None
        if marker and marker.is_valid():
            matched_sid = None
            for s in marker.spec_ids:
                if spec_index.get(s) is owner_inst:
                    matched_sid = s
                    break
            secondaries = [s for s in marker.spec_ids if s != matched_sid]

        # Buffer per owning manifest Path (not instance_id — discovered apps have
        # none), so all orphans bound to one manifest share a single read+write.
        pending_writes.setdefault(str(owner_path), []).append(ResolvedOrphan(
            file_path=orphan.file_path,
            spec_id=resolved_spec,
            instance_id=owner_label,
            secondary_spec_ids=secondaries,
        ))

    # Step 4: read each affected manifest, append realized_files[] entries,
    # write back. One read+write per manifest regardless of how many orphans
    # target it. The key is the manifest's on-disk Path (resolved above), so a
    # discovered app whose instance_id is None is written to its real file —
    # appending realized_files[] only; ``definition_status`` is left untouched,
    # so attaching a file does NOT promote a discovered app to defined.
    session_marker = f"manifest_hygiene:auto_reconcile@{_now_iso()}"
    for path_str, orphan_attaches in pending_writes.items():
        ipath = Path(path_str)
        try:
            instance = json.loads(ipath.read_text())
        except (OSError, json.JSONDecodeError) as e:
            result.warnings.append(
                f"failed to read manifest {ipath.name}: {e}; "
                f"orphans for this app skipped"
            )
            continue

        realized = instance.setdefault("realized_files", [])
        # Build a path-set of paths already on this Instance to keep the
        # append idempotent.
        already_listed = {
            _normalize_path(rf.get("path", ""), workspace)
            for rf in realized
            if isinstance(rf, dict)
        }

        # Pull the Instance's spec_version so the file_id we record carries
        # the version suffix. Falls back to "" if absent (rare for real v7).
        provenance = instance.get("provenance") or {}
        spec_version = provenance.get("spec_version") or ""

        actually_added: list[ResolvedOrphan] = []
        for attach in orphan_attaches:
            norm = _normalize_path(attach.file_path, workspace)
            if norm in already_listed:
                result.skipped_already_listed.append(attach.file_path)
                continue

            # Pull the file_id from the on-disk marker. The marker is the
            # source of truth — we record what's actually stamped, not a
            # freshly minted id (which would conflict with the marker).
            file_ref = ""
            try:
                marker = parse_marker(Path(attach.file_path))
                if marker and marker.is_valid():
                    file_ref = marker.file_ref
            except Exception:
                pass
            if not file_ref:
                # Fall back to a placeholder file_id; Reflect's next pass
                # will surface this as missing_marker if no marker exists.
                file_ref = f"f-unknown@{spec_version}" if spec_version else "f-unknown"

            realized.append({
                "logical_name": Path(attach.file_path).stem or "unnamed",
                "path": attach.file_path,
                "file_id": file_ref,
                "marker_state": "OWNED",
                "created_in_session": session_marker,
            })
            already_listed.add(norm)
            actually_added.append(attach)
            result.resolved.append(attach)

        if apply and actually_added:
            try:
                # mode=0o644: the instance file lives in the bot's workspace
                # and must stay readable by the bot user after evolve's
                # rewrite (mkstemp default 0o600 would lock the bot out).
                _atomic_write_json(ipath, instance, mode=0o644)
            except OSError as e:
                # The realized_files we already pushed onto `result.resolved`
                # don't exist on disk if the write failed — demote them to
                # warnings so the operator isn't misled.
                result.warnings.append(
                    f"write failed for manifest {ipath.name}: {e}; "
                    f"{len(actually_added)} attachments NOT persisted"
                )
                # Roll back the in-memory accounting for this Instance only.
                for att in actually_added:
                    if att in result.resolved:
                        result.resolved.remove(att)

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Auto-reconcile orphan-marker files into the Instance whose "
            "spec_id matches their primary marker. Idempotent — re-runs "
            "skip files already in realized_files[]."
        )
    )
    p.add_argument(
        "--bot-id",
        action="append",
        default=[],
        help="Bot to reconcile (repeatable). Default: all bots in network.json.",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Persist changes (default: dry-run, prints what would happen).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON on stdout.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bot_id
    if not bot_ids:
        try:
            network = load_network(shared_dir / "network.json")
            bots_field = network.get("bots") or {}
            if isinstance(bots_field, dict):
                bot_ids = list(bots_field.keys())
            elif isinstance(bots_field, list):
                bot_ids = [b["id"] for b in bots_field if isinstance(b, dict) and "id" in b]
        except Exception as e:
            print(f"ERROR: couldn't load network.json: {e}", file=sys.stderr)
            return 1
    if not bot_ids:
        print("ERROR: no bots to reconcile (pass --bot-id or populate network.json)",
              file=sys.stderr)
        return 1

    all_results: list[ReconcileResult] = []
    for bot_id in bot_ids:
        try:
            res = reconcile_orphan_markers(bot_id, apply=args.apply, shared_dir=shared_dir)
        except Exception as e:
            print(f"[{bot_id}] ERROR: {e}", file=sys.stderr)
            continue
        all_results.append(res)

    if args.json:
        payload = {
            "shared_dir": str(shared_dir),
            "applied": args.apply,
            "bots": [
                {
                    "bot_id": r.bot_id,
                    "applied": r.applied,
                    "resolved": [asdict(x) for x in r.resolved],
                    "ambiguous": [asdict(x) for x in r.ambiguous],
                    "unmatched": [asdict(x) for x in r.unmatched],
                    "skipped_already_listed": r.skipped_already_listed,
                    "warnings": r.warnings,
                    "counts_by_spec": r.counts_by_spec(),
                }
                for r in all_results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Manifest hygiene reconcile — {'APPLY' if args.apply else 'DRY-RUN'}\n")
    for r in all_results:
        print(r.summary())
        for w in r.warnings:
            print(f"  WARN: {w}")
        for entry in r.resolved[:20]:
            sec = f" (+{len(entry.secondary_spec_ids)} secondary)" if entry.secondary_spec_ids else ""
            print(f"  → {entry.file_path}  →  {entry.instance_id} [{entry.spec_id}]{sec}")
        if len(r.resolved) > 20:
            print(f"  ... and {len(r.resolved) - 20} more resolved")
        for entry in r.ambiguous:
            print(f"  ? {entry.file_path}  spec={entry.spec_id}  candidates={entry.instance_ids}")
        for entry in r.unmatched[:10]:
            print(f"  ✗ {entry.file_path}  spec={entry.spec_id}  reason={entry.reason}")
        if len(r.unmatched) > 10:
            print(f"  ... and {len(r.unmatched) - 10} more unmatched")
        print()

    total_resolved = sum(len(r.resolved) for r in all_results)
    total_unmatched = sum(len(r.unmatched) for r in all_results)
    total_ambiguous = sum(len(r.ambiguous) for r in all_results)
    print(
        f"Totals: resolved={total_resolved} "
        f"ambiguous={total_ambiguous} unmatched={total_unmatched}"
    )
    if not args.apply and total_resolved > 0:
        print("\n(Dry-run. Re-run with --apply to persist.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

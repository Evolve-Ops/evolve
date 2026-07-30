"""
recon_ledger.py — The materialized app-identity reconciliation ledger.

Spec: docs/spec-app-identity-ledger-2026-06-27.md (§2, §3 the five buckets,
§4.C, §5 row ``apps-recon-ledger``, §6 invariants); sub-spec of
docs/spec-apps-meta-2026-06-13.md.

This is the single source of truth that JOINS the four inputs the rest of the
app-identity stack each looked at in isolation:

  1. **Disk markers** — every ``evolve:`` provenance marker on a bot's
     workspace files (``provenance.scan_workspace_marked_only``). The legacy
     ``file_index`` was blind to these: it only indexed files a manifest
     already listed, so a stamped-but-unregistered file (an orphan, or a
     scanner mis-stamp on telemetry) was invisible to it.
  2. **The registry** — every v7-arc Instance's ``realized_files[]`` (what each
     installed app claims to own).
  3. **Lineage** — ``spec_lineage.resolve_spec`` / ``build_spec_index``, so a
     marker carrying a *retired* ``spec_id`` still maps to the live Instance
     that superseded it instead of looking orphaned.
  4. **The ownership policy** — ``app_ownership_policy.can_app_own``, so a
     marker on a never-ownable path (platform telemetry, ``AGENTS.md``, …) is
     classified as something to SCRUB, never to attach.

The join classifies **every** file into exactly one of six buckets (spec §3):

    owned_ok          marker resolves (directly or via lineage) to a live
                      Instance that claims the path
    attach_candidate  ownable path, marker present, resolves to a live app,
                      but no Instance claims it — a real bot-authored file the
                      bot forgot to register
    scrub_candidate   marker on a ``can_app_own == False`` path (lifecycle
                      ``ineligible``), OR a spec_id unresolvable even via
                      lineage (lifecycle ``orphaned`` — the owning app is gone)
    invalid_claim     an Instance *claims* a path that ``can_app_own == False``
                      (a manifest-store self-reference, a generated runtime /
                      secret file, a telemetry index, …). The recommended verb
                      is "remove the path from realized_files[]", NEVER "stamp a
                      marker" — stamping would corrupt a secret or store file.
    missing_marker    an Instance claims an OWNABLE path, the file is on disk,
                      but no marker is present → Fix=stamp is CORRECT
    missing_file      an Instance claims an OWNABLE path, but no file exists on
                      disk → operator manual remove/restore

Two keystones, both rooted in ``can_app_own``:
  * The MARKERS side (pass 1): a marker on a never-ownable path is ``scrub`` —
    ``can_app_own`` is checked BEFORE any attach decision, so an ineligible path
    can never reach ``attach_candidate``. This makes the scanner's
    audit-telemetry / ``AGENTS.md`` mis-stamps *strippable* instead of attachable.
  * The CLAIMS side (pass 2): a *claim* on a never-ownable path is
    ``invalid_claim`` — ``can_app_own`` is checked BEFORE the
    missing_marker/missing_file split, so a never-ownable claim can never reach
    the stampable ``missing_marker`` bucket. This is what kills the corrupting
    "Fix=stamp a marker on a secret/store file" affordance.

Output
──────
:func:`build_recon_ledger` returns a typed :class:`ReconResult` (bucket → list
of :class:`LedgerRow`) plus a reverse spec index
(``spec_id -> {current_version, prior_spec_ids[], owning_instance, files[]}``)
and persists both atomically to ``{shared_dir}/recon_ledger.json``.

This module is the producer. The downstream chips consume it:
``apps-reflect-thin-reader`` turns ``reflect.py`` into a thin reader over this
result; ``apps-ui-scrub-action`` drives the operator scrub action off the
``scrub_candidate`` bucket. They import the helpers and result types from here
(NOT the reverse — keeping the dependency one-directional avoids the cycle).

Pure Python, no LLM. The workspace walk reuses ``provenance``'s skip-dir /
skip-ext sets via ``scan_workspace_marked_only``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json

from ..config import bot_home, load_network
from .app_ownership_policy import can_app_own
# ``_ws_rel_key`` now lives in the shared ``path_keys`` module so ``scanner``
# can adopt the SAME canonicalizer without the
# ``recon_ledger → app_ownership_policy → scanner`` import cycle (F-C1 / F-C2).
# Re-exported under its historical private name for existing importers
# (``lineage_repoint``).
from .path_keys import ws_rel_key as _ws_rel_key
from .provenance import (
    FileLifecycle,
    ProvenanceMarker,
    scan_workspace_marked_only,
)
from .spec_lineage import build_spec_index, current_spec_id, prior_spec_ids


# ── Bucket names (the closed five-set from spec §3) ───────────────────────────

class Bucket:
    OWNED_OK         = "owned_ok"
    ATTACH_CANDIDATE = "attach_candidate"
    SCRUB_CANDIDATE  = "scrub_candidate"
    INVALID_CLAIM    = "invalid_claim"
    MISSING_MARKER   = "missing_marker"
    MISSING_FILE     = "missing_file"


BUCKETS: tuple[str, ...] = (
    Bucket.OWNED_OK,
    Bucket.ATTACH_CANDIDATE,
    Bucket.SCRUB_CANDIDATE,
    Bucket.INVALID_CLAIM,
    Bucket.MISSING_MARKER,
    Bucket.MISSING_FILE,
)


# ── Result types (consumed by apps-reflect-thin-reader / apps-ui-scrub-action) ─

@dataclass
class LedgerRow:
    """One reconciled file. Exactly one row per file path per bot.

    Fields:
        path:       Workspace-relative path (the form ``can_app_own`` and the
                    scanner's evidence both use). Stable across machines.
        bot_id:     The bot whose workspace the file lives in.
        bucket:     One of :data:`BUCKETS`.
        spec_id:    The resolved *current* spec_id this file belongs to, when it
                    could be determined — via lineage for a marked file, or the
                    claiming Instance's binding for a registry-only file. ``None``
                    when no live spec could be tied to the file (e.g. a
                    ``scrub_candidate`` whose marker spec_id is unresolvable).
        file_id:    The marker's file_id (marked files) or the realized_file's
                    file_id (registry-only files); ``None`` when unknown.
        reason:     Short machine-stable reason code explaining the bucket (see
                    the ``_REASON_*`` constants). Distinguishes the two
                    ``scrub_candidate`` arms (``ineligible_path`` vs
                    ``unresolvable_spec``) and records lineage hits.
        lifecycle:  A :class:`provenance.FileLifecycle` value recording the file's
                    state — including the new ``ineligible`` value for
                    policy-scrub rows, which is the keystone the spec calls out.
        abs_path:   Absolute on-disk path, for callers that act on the file.
    """
    path: str
    bot_id: str
    bucket: str
    spec_id: Optional[str] = None
    file_id: Optional[str] = None
    reason: str = ""
    lifecycle: str = ""
    abs_path: str = ""


@dataclass
class SpecIndexEntry:
    """Reverse spec index value: everything a caller needs to go spec → files
    in O(1) without re-walking instances."""
    current_version: Optional[str] = None
    prior_spec_ids: list[str] = field(default_factory=list)
    owning_instance: Optional[str] = None
    bot_id: Optional[str] = None
    files: list[str] = field(default_factory=list)


@dataclass
class ReconResult:
    """The materialized reconciliation join over one or more bots."""
    bots: list[str] = field(default_factory=list)
    buckets: dict[str, list[LedgerRow]] = field(default_factory=dict)
    spec_index: dict[str, SpecIndexEntry] = field(default_factory=dict)
    files_scanned: int = 0
    warnings: list[str] = field(default_factory=list)

    # ── Bucket helpers ────────────────────────────────────────────────────────

    def rows(self, bucket: str) -> list[LedgerRow]:
        """All rows in *bucket* (empty list for an unknown/empty bucket)."""
        return self.buckets.get(bucket, [])

    def all_rows(self) -> list[LedgerRow]:
        out: list[LedgerRow] = []
        for b in BUCKETS:
            out.extend(self.buckets.get(b, []))
        return out

    def counts(self) -> dict[str, int]:
        return {b: len(self.buckets.get(b, [])) for b in BUCKETS}

    def lookup_spec(self, spec_id: str) -> Optional[SpecIndexEntry]:
        """Resolve *spec_id* through the reverse index, honoring lineage: a
        retired spec_id maps to the entry of the Instance that superseded it."""
        if not spec_id:
            return None
        entry = self.spec_index.get(spec_id)
        if entry is not None:
            return entry
        for e in self.spec_index.values():
            if spec_id in e.prior_spec_ids:
                return e
        return None

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema": "recon-ledger-v1",
            "bots": list(self.bots),
            "counts": self.counts(),
            "files_scanned": self.files_scanned,
            "buckets": {
                b: [asdict(r) for r in self.buckets.get(b, [])] for b in BUCKETS
            },
            "spec_index": {
                sid: asdict(entry) for sid, entry in self.spec_index.items()
            },
            "warnings": list(self.warnings),
        }


# ── Reason codes (machine-stable; surfaced in LedgerRow.reason) ───────────────

_REASON_OWNED              = "claimed_and_marked"
_REASON_OWNED_VIA_LINEAGE  = "claimed_marker_resolved_via_lineage"
_REASON_ATTACH             = "marker_resolves_no_claim"
_REASON_SCRUB_INELIGIBLE   = "ineligible_path"
_REASON_SCRUB_UNRESOLVABLE = "unresolvable_spec"
_REASON_INVALID_CLAIM      = "non_ownable_claim"
_REASON_MISSING_MARKER     = "claimed_no_marker"
_REASON_MISSING_FILE       = "claimed_absent_on_disk"


# ── Registry loading (recon_ledger owns these; reflect becomes a thin reader) ─

def _load_bot_instances_with_paths(bot_id: str) -> list[tuple[Path, dict]]:
    """Load all v7-arc Instance JSONs in *bot_id*'s manifests dir, each paired
    with the on-disk ``Path`` it was read from.

    This is the single source for the manifests walk. :func:`_load_bot_instances`
    (below) is the bare-dict view the classifier consumes; callers that must
    WRITE a manifest back take this view because they need the source path. In
    particular ``manifest_hygiene.reconcile_orphan_markers`` attaches a file to
    a ``discovered`` app whose manifest+Spec exist but that has no materialized
    Instance yet — ``instance_id is None`` — so the manifest's file name cannot
    be derived from the (null) id and the resolved ``Path`` is the only handle
    on the file. Both views share ONE walk so the two cannot drift apart (the
    classify-vs-act split this stack has been bitten by repeatedly).
    """
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    out: list[tuple[Path, dict]] = []
    # Guard the dir probe + glob: on Linux/Py3.12 a bare .is_dir()/glob on a
    # path under a bot's ACL-clamped .openclaw raises PermissionError (the
    # #3184 deploy-abort class). Degrade to "no instances" rather than crash.
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


def _load_bot_instances(bot_id: str) -> list[dict]:
    """Load all v7-arc Instance JSONs in *bot_id*'s manifests dir.

    Mirrors the loader reflect.py used before it became a thin reader of this
    ledger: scan ``{bot_home}/.openclaw/workspace/manifests/*.json``, skip
    dot/underscore index files, keep only ``manifest_shape == "v7-arc"``. The
    bare-dict view over :func:`_load_bot_instances_with_paths` (one walk).
    """
    return [data for _, data in _load_bot_instances_with_paths(bot_id)]


def _build_expected_files(
    instances: list[dict], workspace: Path
) -> dict[str, tuple[dict, dict]]:
    """Map canonical workspace-relative path → (Instance, realized_file entry)
    for every file any Instance claims. Keyed via :func:`_ws_rel_key` so the
    registry and the disk walk meet on the same key regardless of whether
    ``realized_files`` stored an absolute (extend_application) or
    workspace-relative (migrate_v7/v13) path — and WITHOUT binding a relative
    path to the daemon CWD (#3303).
    """
    out: dict[str, tuple[dict, dict]] = {}
    for inst in instances:
        for rf in inst.get("realized_files") or []:
            if not isinstance(rf, dict):
                continue
            path = rf.get("path") or ""
            if not path:
                continue
            out[_ws_rel_key(path, workspace)] = (inst, rf)
    return out


def _rf_file_id(rf: dict) -> Optional[str]:
    """Bare file_id from a realized_file entry (strips any ``@version`` suffix)."""
    fid = rf.get("file_id") or ""
    if not fid:
        return None
    return fid.split("@", 1)[0]


# ── Reverse spec index ────────────────────────────────────────────────────────

def build_reverse_spec_index(
    instances: list[dict], bot_id: str
) -> dict[str, SpecIndexEntry]:
    """``spec_id -> SpecIndexEntry`` keyed by each Instance's *current* spec_id.

    ``prior_spec_ids[]`` is carried inside the value (so a retired id still
    resolves via :meth:`ReconResult.lookup_spec`). ``files[]`` is the workspace
    path list from the Instance's ``realized_files[]`` — already O(1) for a
    spec→files lookup, the thing :func:`build_spec_index` alone does not give.
    """
    index: dict[str, SpecIndexEntry] = {}
    for inst in instances:
        sid = current_spec_id(inst)
        if not sid:
            continue
        prov_raw = inst.get("provenance")
        prov = prov_raw if isinstance(prov_raw, dict) else {}
        files = [
            rf.get("path") or ""
            for rf in (inst.get("realized_files") or [])
            if isinstance(rf, dict) and (rf.get("path") or "")
        ]
        index[sid] = SpecIndexEntry(
            current_version=prov.get("spec_version"),
            prior_spec_ids=list(prior_spec_ids(inst)),
            owning_instance=inst.get("instance_id"),
            bot_id=bot_id,
            files=files,
        )
    return index


# ── Classification (the join) ─────────────────────────────────────────────────

def _resolve_marker_spec(
    marker: ProvenanceMarker, spec_index: dict[str, dict]
) -> tuple[Optional[str], bool]:
    """Resolve a marker's spec_ids through *spec_index* (lineage-aware).

    Returns ``(current_spec_id, via_lineage)``: the live Instance's *current*
    spec_id the marker ties to (``None`` if none resolves), and whether the
    match came through a retired ``prior_spec_ids[]`` entry rather than a direct
    current-id hit.
    """
    for sid in marker.spec_ids:
        inst = spec_index.get(sid)
        if inst is None:
            continue
        resolved = current_spec_id(inst)
        via_lineage = resolved is not None and sid != resolved
        return resolved or sid, via_lineage
    return None, False


def _classify_bot(bot_id: str, result: ReconResult) -> None:
    """Reconcile one bot's markers + registry into *result* (appends rows)."""
    instances = _load_bot_instances(bot_id)
    spec_index = build_spec_index(instances)  # lineage-aware spec_id -> Instance
    workspace = bot_home(bot_id) / ".openclaw" / "workspace"
    # Key the registry side on the SAME canonical workspace-relative form the
    # disk walk uses (via the shared workspace handle) so the join meets (#3303).
    expected = _build_expected_files(instances, workspace)

    # Merge the reverse spec index for this bot.
    result.spec_index.update(build_reverse_spec_index(instances, bot_id))

    # Guarded dir probe — a bare .is_dir() on an ACL-clamped .openclaw raises
    # PermissionError on Linux/Py3.12 (#3184 class). Treat clamped-but-present
    # as present so the walk still runs (and degrades gracefully).
    try:
        workspace_present = workspace.is_dir()
    except (PermissionError, OSError):
        workspace_present = True
    if not workspace_present:
        result.warnings.append(f"[{bot_id}] workspace dir missing: {workspace}")
        # Registry may still claim files (all missing_file) — fall through so
        # those still surface.

    def _add(row: LedgerRow) -> None:
        result.buckets.setdefault(row.bucket, []).append(row)

    seen: set[str] = set()

    # ── Pass 1: every marked file on disk ─────────────────────────────────────
    if workspace_present:
        for wf in scan_workspace_marked_only(workspace):
            if not wf.has_marker:
                continue
            abs_path = str(wf.path)
            # Canonical workspace-relative key — the join axis AND the display
            # path (LedgerRow.path is workspace-relative). Lexical, CWD-free.
            rel = _ws_rel_key(abs_path, workspace)
            # The manifests/ index dir is not an ownable artifact — skip it so
            # Instance JSONs aren't reconciled as files of themselves. (Mirrors
            # the same guard the legacy reflect walk used.)
            if rel.split("/", 1)[0] == "manifests":
                continue

            seen.add(rel)
            result.files_scanned += 1
            marker = wf.marker
            assert marker is not None  # has_marker guarantees this
            claimed = expected.get(rel)

            # KEYSTONE: a marker on a never-ownable path is always a scrub,
            # never owned/attach — even if some Instance erroneously claims it.
            if not can_app_own(rel):
                _add(LedgerRow(
                    path=rel, bot_id=bot_id, bucket=Bucket.SCRUB_CANDIDATE,
                    spec_id=None, file_id=marker.file_id or None,
                    reason=_REASON_SCRUB_INELIGIBLE,
                    lifecycle=FileLifecycle.INELIGIBLE, abs_path=abs_path,
                ))
                continue

            resolved_spec, via_lineage = _resolve_marker_spec(marker, spec_index)

            if claimed is not None:
                # Claimed + marked → owned. Lineage resolution records the live
                # current spec_id even when the marker carries the retired one
                # (the false-orphan the legacy file_index would have reported).
                inst, rf = claimed
                _add(LedgerRow(
                    path=rel, bot_id=bot_id, bucket=Bucket.OWNED_OK,
                    spec_id=resolved_spec or current_spec_id(inst),
                    file_id=marker.file_id or _rf_file_id(rf),
                    reason=_REASON_OWNED_VIA_LINEAGE if via_lineage else _REASON_OWNED,
                    lifecycle=FileLifecycle.OWNED, abs_path=abs_path,
                ))
                continue

            if resolved_spec is not None:
                # Ownable, marker resolves to a live app, but no Instance lists
                # this path: the bot authored it and forgot to register it.
                _add(LedgerRow(
                    path=rel, bot_id=bot_id, bucket=Bucket.ATTACH_CANDIDATE,
                    spec_id=resolved_spec, file_id=marker.file_id or None,
                    reason=_REASON_ATTACH, lifecycle=FileLifecycle.UNOWNED,
                    abs_path=abs_path,
                ))
            else:
                # Ownable + marked, but the spec_id resolves to no live
                # Instance even through lineage: the owning app is gone. Scrub.
                _add(LedgerRow(
                    path=rel, bot_id=bot_id, bucket=Bucket.SCRUB_CANDIDATE,
                    spec_id=None, file_id=marker.file_id or None,
                    reason=_REASON_SCRUB_UNRESOLVABLE,
                    lifecycle=FileLifecycle.ORPHANED, abs_path=abs_path,
                ))

    # ── Pass 2: registry claims with no marker found in pass 1 ────────────────
    # A claimed path that DID have a valid marker was handled in pass 1 (it
    # appears in ``seen``). What remains is claimed-but-unmarked: either the
    # file is on disk without a marker (missing_marker) or absent (missing_file).
    for rel, (inst, rf) in expected.items():
        if rel in seen:
            continue
        # Reconstruct the true on-disk absolute from the workspace-relative key.
        # ``workspace / rel`` is CWD-free; for a foreign-absolute key (kept
        # absolute by _ws_rel_key) pathlib's join yields that absolute as-is.
        abs_path = str(workspace / rel)
        spec_id = current_spec_id(inst)
        file_id = _rf_file_id(rf)

        # CLAIMS-SIDE KEYSTONE: an Instance claims a path that no application may
        # own (manifest-store self-reference, generated runtime/secret file,
        # telemetry index, …). This is a manifest-hygiene error — the path must
        # be REMOVED from realized_files[], never stamped with a marker. Checked
        # BEFORE the missing_marker/missing_file split so a never-ownable claim
        # can never reach the stampable ``missing_marker`` bucket, whose Fix=stamp
        # would corrupt a secret/store file. Mirrors pass 1's marker-side keystone.
        if not can_app_own(rel):
            _add(LedgerRow(
                path=rel, bot_id=bot_id, bucket=Bucket.INVALID_CLAIM,
                spec_id=spec_id, file_id=file_id,
                reason=_REASON_INVALID_CLAIM,
                lifecycle=FileLifecycle.INELIGIBLE, abs_path=abs_path,
            ))
            continue

        if not Path(abs_path).exists():
            _add(LedgerRow(
                path=rel, bot_id=bot_id, bucket=Bucket.MISSING_FILE,
                spec_id=spec_id, file_id=file_id,
                reason=_REASON_MISSING_FILE, lifecycle=FileLifecycle.ORPHANED,
                abs_path=abs_path,
            ))
        else:
            result.files_scanned += 1
            _add(LedgerRow(
                path=rel, bot_id=bot_id, bucket=Bucket.MISSING_MARKER,
                spec_id=spec_id, file_id=file_id,
                reason=_REASON_MISSING_MARKER, lifecycle=FileLifecycle.OWNED,
                abs_path=abs_path,
            ))


# ── Public API ────────────────────────────────────────────────────────────────

def _ledger_path(shared_dir: Path) -> Path:
    return shared_dir / "recon_ledger.json"


def build_recon_ledger(
    shared_dir: Path,
    bot_ids: list[str],
    *,
    persist: bool = True,
) -> ReconResult:
    """Build the materialized reconciliation ledger over *bot_ids* and (by
    default) persist it atomically to ``{shared_dir}/recon_ledger.json``.

    The result classifies every marked file + every registry claim across all
    bots into the five buckets, and carries the reverse spec index. Persistence
    uses ``evolve_util.atomic_write_json`` (temp-file + rename), the same
    contract ``file_index.py`` uses — the ledger lives under ``{shared_dir}``,
    which is evolve-owned, so no sudo / /tmp staging is needed.

    Set ``persist=False`` for a pure in-memory build (tests, dry-runs).
    """
    result = ReconResult(bots=list(bot_ids))
    # Pre-seed empty bucket lists so the shape is stable for consumers.
    for b in BUCKETS:
        result.buckets.setdefault(b, [])

    for bot_id in bot_ids:
        try:
            _classify_bot(bot_id, result)
        except Exception as e:  # never let one bot's bad state sink the ledger
            result.warnings.append(f"[{bot_id}] reconcile failed: {e}")

    if persist:
        try:
            path = _ledger_path(shared_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, result.to_dict())
        except Exception as e:
            result.warnings.append(f"persist failed: {e}")

    return result


def load_recon_ledger(shared_dir: Path) -> dict:
    """Load the persisted ledger dict, or ``{}`` if absent/unreadable.

    Returns the raw serialized form (``to_dict()`` shape). Consumers that want
    typed rows can rehydrate via :func:`rows_from_dict`.
    """
    try:
        return json.loads(_ledger_path(shared_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def rows_from_dict(ledger: dict, bucket: str) -> list[LedgerRow]:
    """Rehydrate ``LedgerRow`` objects for *bucket* from a loaded ledger dict."""
    raw = (ledger.get("buckets") or {}).get(bucket) or []
    rows: list[LedgerRow] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        rows.append(LedgerRow(
            path=d.get("path", ""),
            bot_id=d.get("bot_id", ""),
            bucket=d.get("bucket", bucket),
            spec_id=d.get("spec_id"),
            file_id=d.get("file_id"),
            reason=d.get("reason", ""),
            lifecycle=d.get("lifecycle", ""),
            abs_path=d.get("abs_path", ""),
        ))
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────

def _discover_bot_ids(shared_dir: Path) -> list[str]:
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


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Build the app-identity reconciliation ledger (five-bucket join)."
    )
    p.add_argument(
        "--bot-id", action="append", default=[],
        help="Bot to reconcile (repeatable). Default: all bots in network.json.",
    )
    # Platform-correct shared-dir default (macOS /Users/Shared/evolve vs Linux
    # /var/lib/evolve) — never hardcode the macOS literal.
    from platform_profile import get_profile
    default_shared_dir = get_profile().shared_dir_default
    p.add_argument(
        "--shared-dir", default=default_shared_dir,
        help=f"Pod shared dir (default: {default_shared_dir})",
    )
    p.add_argument(
        "--no-persist", action="store_true",
        help="Build in memory only; do not write recon_ledger.json.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the full ledger as JSON on stdout.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bot_id or _discover_bot_ids(shared_dir)
    if not bot_ids:
        print("ERROR: no bots to reconcile (pass --bot-id or populate network.json)",
              file=sys.stderr)
        return 1

    result = build_recon_ledger(shared_dir, bot_ids, persist=not args.no_persist)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print("Reconciliation ledger\n")
    for b in BUCKETS:
        print(f"  {b:<18} {len(result.buckets.get(b, [])):>5}")
    print(f"\n  files_scanned      {result.files_scanned:>5}")
    print(f"  specs indexed      {len(result.spec_index):>5}")
    for w in result.warnings:
        print(f"  WARN: {w}")
    if not args.no_persist:
        print(f"\nPersisted → {_ledger_path(shared_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

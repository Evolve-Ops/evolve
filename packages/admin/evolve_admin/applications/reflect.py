"""
reflect.py — Manifest hygiene for v7-arc Instances, as a thin reader of the
recon ledger.

Per docs/spec-app-identity-ledger-2026-06-27.md (§3 five buckets, §4.C, §5 row
``apps-reflect-thin-reader``, §6 "reconciliation has one authority"). Reflect is
the "bot forgot to use extend_application" safety net: it surfaces
manifest-hygiene findings without applying changes. The operator (or a future
arbiter-integration layer) decides what to act on.

**Thin reader.** Reflect no longer re-walks the workspace and re-derives
ownership on its own. That independent classification was the bug behind the
false orphans: a stamped-but-unregistered file was reported as an *attachable*
``orphan_file`` even when its path could never be owned by an application
(audit telemetry under ``evolve/audit_outbox/...``, the bot's ``AGENTS.md``).
Ownership now has ONE authority — ``recon_ledger.build_recon_ledger`` — which
already folds in the ownership policy (``can_app_own`` → SCRUB) and lineage
(``resolve_spec`` → a retired ``spec_id`` still resolves to its live Instance).
Reflect translates that ledger's five buckets into the ``ReflectFinding`` shape
the ``/api/bots/<id>/reflect`` endpoint + ``apps.js`` already consume.

Bucket → finding mapping (recon §3 → reflect kind):

  owned_ok          → not a finding. (Supplementary: an owned file still
                      carrying a v6 ``pkg=`` marker becomes ``stale_pkg_marker``
                      — see below.)
  attach_candidate  → ``orphan_file`` + action ``attach_to_instance_or_archive``.
                      The genuine "bot authored an ownable file and forgot to
                      register it" case — the only one for which an Attach is
                      offered.
  scrub_candidate   → ``scrub_candidate`` + action ``strip_marker``. A marker on
                      a never-ownable path (platform telemetry, ``AGENTS.md``, …
                      lifecycle ``ineligible``) OR a marker whose spec_id resolves
                      to no live Instance even via lineage (lifecycle
                      ``orphaned``). NEVER offered for attach — the marker should
                      be stripped, not registered. This is the keystone fix.
  invalid_claim     → ``invalid_claim`` + action ``remove_from_realized_files``.
                      An Instance CLAIMS a path no application may own (a
                      manifest-store self-reference, a generated runtime/secret
                      file, a telemetry index). NEVER offered for stamp — the path
                      should be removed from the manifest, not marked (stamping a
                      secret/store file corrupts it). The claims-side twin of
                      scrub_candidate, and the safety fix that keeps these paths
                      out of the stampable ``missing_marker`` bucket.
  missing_marker    → ``missing_marker`` + action ``stamp_marker``. The claimed
                      path is OWNABLE (passed ``can_app_own``) and on disk.
  missing_file      → ``missing_disk_file`` + action ``manual_remove_or_restore``.

**stale_pkg_marker.** The recon ledger answers *ownership*, not marker *form*:
a claimed+marked file is OWNED_OK whether its marker is the v7 ``spec=`` or the
legacy v6 ``pkg=`` form. To preserve the v6→v7 rewrite affordance (and the
existing UI / test), Reflect keeps this one check as a narrow supplementary pass
over the OWNED_OK rows only — re-reading the marker on each *owned* file (not a
full walk) to spot any still-stale ``pkg=`` form. EACCES on that re-read is
swallowed (Linux/Py3.12 .openclaw clamp).

Output: list of ReflectFinding records, surfaced via:
  - Python API: `reflect(bot_id, shared_dir).findings`
  - CLI: `python3 -m evolve_admin.applications.reflect --bot-id team_bot_a [--json]`

No automatic action is taken. Per the manifest-v7 spec §8.1: "Reflect doesn't
apply changes unilaterally — it produces proposals routed through the existing
arbiter Proposal store, with approval_audience: pod_operator." The arbiter
integration is a follow-up; for now the CLI prints findings + a proposed action
JSON the operator can review.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..config import bot_home, load_network
from .recon_ledger import (
    Bucket,
    ReconResult,
    SpecIndexEntry,
    build_recon_ledger,
)
from .recon_ledger import _load_bot_instances as _recon_load_bot_instances


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ReflectFinding:
    """One manifest-hygiene issue Reflect surfaced.

    ``reason`` / ``lifecycle`` are carried straight from the recon ledger row
    (machine-stable reason code + ``provenance.FileLifecycle`` value). They are
    primarily meaningful for ``scrub_candidate`` findings — the future
    ``apps-ui-scrub-action`` chip uses them to label the Strip-marker chip
    ("never-ownable path" vs "owning app gone"). Empty for the kinds that don't
    need them; harmless extra fields for older consumers.
    """
    kind: str                          # "orphan_file" | "missing_marker" | "stale_pkg_marker" | "missing_disk_file" | "scrub_candidate" | "invalid_claim"
    bot_id: str
    file_path: str
    spec_id: Optional[str] = None
    instance_id: Optional[str] = None
    description: str = ""
    proposed_action: dict = field(default_factory=dict)
    reason: str = ""
    lifecycle: str = ""


@dataclass
class ReflectResult:
    """Aggregate over a single bot's Reflect pass."""
    bot_id: str
    files_scanned: int = 0
    instances_checked: int = 0
    findings: list[ReflectFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        by_kind: dict[str, int] = {}
        for f in self.findings:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none"
        return (
            f"[{self.bot_id}] files={self.files_scanned} "
            f"instances={self.instances_checked} findings={len(self.findings)} "
            f"({breakdown})"
        )


# ── Instance loading (re-exported for sync.py; single-sourced on recon_ledger) ─

def _load_bot_instances(bot_id: str) -> list[dict]:
    """Load all v7-arc Instance JSONs in the bot's manifests dir.

    Delegates to ``recon_ledger._load_bot_instances`` so the loader (and its
    Linux/Py3.12 EACCES guard on the ``.openclaw`` dir probe) is single-sourced
    with the reconciliation authority. ``sync.py`` imports this name.
    """
    return _recon_load_bot_instances(bot_id)


# ── Bucket → finding translation ──────────────────────────────────────────────

def _instance_for_spec(recon: ReconResult, spec_id: Optional[str]) -> Optional[SpecIndexEntry]:
    """Resolve a row's spec_id to its owning Instance entry (lineage-aware)."""
    if not spec_id:
        return None
    return recon.lookup_spec(spec_id)


def _orphan_finding(bot_id: str, row) -> ReflectFinding:
    """attach_candidate → the genuine 'forgot to register' orphan."""
    return ReflectFinding(
        kind="orphan_file",
        bot_id=bot_id,
        file_path=row.abs_path or row.path,
        spec_id=row.spec_id,
        description=(
            f"File has a marker resolving to spec={row.spec_id} but no "
            "Instance.realized_files[] references it. Likely the bot wrote it "
            "without calling extend_application."
        ),
        proposed_action={
            "kind": "attach_to_instance_or_archive",
            "spec_id": row.spec_id,
            "marker_file_id": row.file_id,
        },
        reason=row.reason,
        lifecycle=row.lifecycle,
    )


def _scrub_finding(bot_id: str, row) -> ReflectFinding:
    """scrub_candidate → a marker that must be stripped, never attached.

    Two arms (distinguished by ``row.reason``/``row.lifecycle``):
      - ineligible_path  — the path can never be owned by an application
                           (platform telemetry, AGENTS.md, OC-reserved files).
      - unresolvable_spec — the marker's spec_id resolves to no live Instance
                           even via lineage; the owning app is gone.
    """
    if row.reason == "ineligible_path":
        description = (
            "File carries an application marker, but its path can never be "
            "owned by an application (platform telemetry / OpenClaw-standard "
            "file). The marker should be stripped — not attached to any app."
        )
    else:
        description = (
            "File's marker spec_id resolves to no live Instance, even through "
            "lineage — the owning application is gone. The stale marker should "
            "be stripped."
        )
    return ReflectFinding(
        kind="scrub_candidate",
        bot_id=bot_id,
        file_path=row.abs_path or row.path,
        spec_id=row.spec_id,
        description=description,
        proposed_action={
            "kind": "strip_marker",
            "file_id": row.file_id,
            "reason": row.reason,
            "lifecycle": row.lifecycle,
        },
        reason=row.reason,
        lifecycle=row.lifecycle,
    )


def _invalid_claim_finding(bot_id: str, row, entry: Optional[SpecIndexEntry]) -> ReflectFinding:
    """invalid_claim → an Instance claims a path no application may own.

    The remediation is to REMOVE the path from the Instance's realized_files[],
    never to stamp a marker — the path is a manifest-store self-reference, a
    generated runtime/secret file, or a telemetry index, and stamping it would
    corrupt the file. This is the claims-side twin of ``scrub_candidate``; the
    proposed_action is deliberately NOT a stampable kind, so the existing
    apply-fix endpoint (which whitelists ``stamp_marker`` /
    ``rewrite_marker_to_spec``) rejects it by construction.
    """
    instance_id = entry.owning_instance if entry else None
    return ReflectFinding(
        kind="invalid_claim",
        bot_id=bot_id,
        file_path=row.abs_path or row.path,
        spec_id=row.spec_id,
        instance_id=instance_id,
        description=(
            f"Instance {instance_id} claims this path in realized_files[], but it "
            "can never be owned by an application (manifest-store file, generated "
            "runtime/secret file, or telemetry index). The path should be removed "
            "from the manifest — NOT stamped with a marker, which would corrupt it."
        ),
        proposed_action={
            "kind": "remove_from_realized_files",
            "instance_id": instance_id,
            "spec_id": row.spec_id,
            "file_id": row.file_id,
        },
        reason=row.reason,
        lifecycle=row.lifecycle,
    )


def _missing_marker_finding(bot_id: str, row, entry: Optional[SpecIndexEntry]) -> ReflectFinding:
    """missing_marker → claimed on disk but no marker stamped."""
    instance_id = entry.owning_instance if entry else None
    spec_version = entry.current_version if entry else None
    return ReflectFinding(
        kind="missing_marker",
        bot_id=bot_id,
        file_path=row.abs_path or row.path,
        spec_id=row.spec_id,
        instance_id=instance_id,
        description=(
            f"File appears in Instance {instance_id}'s realized_files[] but has "
            "no marker on disk. Ownership isn't traceable from on-disk inspection."
        ),
        proposed_action={
            "kind": "stamp_marker",
            "spec_id": row.spec_id,
            "spec_version": spec_version,
            "file_id": row.file_id,
        },
        reason=row.reason,
        lifecycle=row.lifecycle,
    )


def _missing_file_finding(bot_id: str, row, entry: Optional[SpecIndexEntry]) -> ReflectFinding:
    """missing_file → an Instance claims a path with no file on disk."""
    instance_id = entry.owning_instance if entry else None
    return ReflectFinding(
        kind="missing_disk_file",
        bot_id=bot_id,
        file_path=row.abs_path or row.path,
        spec_id=row.spec_id,
        instance_id=instance_id,
        description=(
            f"Instance {instance_id} claims this path in realized_files[] but no "
            "file exists on disk. Either the file was deleted without updating "
            "the manifest, or it never landed."
        ),
        proposed_action={
            "kind": "manual_remove_or_restore",
            "instance_id": instance_id,
            "spec_id": row.spec_id,
            "file_id": row.file_id,
        },
        reason=row.reason,
        lifecycle=row.lifecycle,
    )


def _stale_pkg_finding(
    bot_id: str, row, entry: Optional[SpecIndexEntry]
) -> Optional[ReflectFinding]:
    """owned_ok supplementary check: an owned file still on the v6 ``pkg=`` form.

    The recon ledger classifies a claimed+marked file as owned regardless of
    marker keyword, so this is the one piece the ledger doesn't surface. We
    re-read the marker on the *owned* file only (not a full walk) to preserve
    the v6→v7 rewrite affordance. Returns None for the common v7 case or when
    the file/marker can't be read (EACCES, deleted between passes, …).
    """
    from .provenance import parse_marker

    abs_path = row.abs_path or row.path
    try:
        marker = parse_marker(Path(abs_path))
    except Exception:
        return None
    if not marker or not marker.is_valid() or marker.keyword != "pkg":
        return None

    instance_id = entry.owning_instance if entry else None
    spec_version = entry.current_version if entry else None
    return ReflectFinding(
        kind="stale_pkg_marker",
        bot_id=bot_id,
        file_path=abs_path,
        spec_id=row.spec_id,
        instance_id=instance_id,
        description=(
            "File has a v6 `pkg=` marker; expected v7 `spec=`. Likely missed by "
            "rewrite_markers (skip rules, permission gap, or added post-migration)."
        ),
        proposed_action={
            "kind": "rewrite_marker_to_spec",
            "spec_id": row.spec_id,
            "spec_version": spec_version,
            "file_id": row.file_id,
        },
    )


# ── Public API ────────────────────────────────────────────────────────────────

def reflect(bot_id: str, shared_dir: Path = Path("/Users/Shared/evolve")) -> ReflectResult:
    """Surface a bot's v7-arc manifest-hygiene findings (read-only).

    Thin reader of ``recon_ledger.build_recon_ledger``: the ledger is the single
    reconciliation authority (it folds in the ownership policy + lineage), and
    this function only translates its five buckets into ``ReflectFinding``s.

    The single-bot ledger is built ``persist=False`` — reflect is read-only and
    must not clobber the canonical pod-wide ``recon_ledger.json`` that the
    recon producer owns.

    Returns a ReflectResult with findings; the caller decides what to act on.
    """
    result = ReflectResult(bot_id=bot_id)

    # Count instances for the UI header + the "not migrated yet" warning, using
    # the same loader the ledger uses (EACCES-guarded).
    instances = _load_bot_instances(bot_id)
    result.instances_checked = len(instances)
    if not instances:
        result.warnings.append(
            f"no v7-arc Instances in {bot_home(bot_id) / '.openclaw' / 'workspace' / 'manifests'}; "
            "Reflect needs the bot to be migrated to v7-arc first"
        )
        # Don't return early: the ledger still classifies any stray markers
        # (e.g. scrub candidates) so they remain visible even pre-migration.

    recon = build_recon_ledger(shared_dir, [bot_id], persist=False)
    result.files_scanned = recon.files_scanned
    # The ledger prefixes per-bot warnings with "[bot_id] "; carry them through.
    result.warnings.extend(recon.warnings)

    # attach_candidate → orphan_file (the genuine forgot-to-register case).
    for row in recon.rows(Bucket.ATTACH_CANDIDATE):
        result.findings.append(_orphan_finding(bot_id, row))

    # scrub_candidate → strip-marker finding (NEVER an attachable orphan).
    for row in recon.rows(Bucket.SCRUB_CANDIDATE):
        result.findings.append(_scrub_finding(bot_id, row))

    # invalid_claim → remove-from-manifest finding (NEVER a stampable marker).
    for row in recon.rows(Bucket.INVALID_CLAIM):
        entry = _instance_for_spec(recon, row.spec_id)
        result.findings.append(_invalid_claim_finding(bot_id, row, entry))

    # missing_marker → stamp-marker finding.
    for row in recon.rows(Bucket.MISSING_MARKER):
        entry = _instance_for_spec(recon, row.spec_id)
        result.findings.append(_missing_marker_finding(bot_id, row, entry))

    # missing_file → operator remove/restore finding.
    for row in recon.rows(Bucket.MISSING_FILE):
        entry = _instance_for_spec(recon, row.spec_id)
        result.findings.append(_missing_file_finding(bot_id, row, entry))

    # owned_ok → no finding, except a supplementary v6 pkg= form check.
    for row in recon.rows(Bucket.OWNED_OK):
        entry = _instance_for_spec(recon, row.spec_id)
        stale = _stale_pkg_finding(bot_id, row, entry)
        if stale is not None:
            result.findings.append(stale)

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Reflect — manifest hygiene scan for v7-arc Instances"
    )
    p.add_argument(
        "--bot-id",
        action="append",
        default=[],
        help="Bot to scan (repeatable). Default: all bots in network.json.",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit findings as JSON on stdout instead of human-readable summary.",
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
        print("ERROR: no bots to scan (pass --bot-id or populate network.json)",
              file=sys.stderr)
        return 1

    all_results: list[ReflectResult] = []
    for bot_id in bot_ids:
        try:
            res = reflect(bot_id, shared_dir)
        except Exception as e:
            print(f"[{bot_id}] ERROR: {e}", file=sys.stderr)
            continue
        all_results.append(res)

    if args.json:
        payload = {
            "shared_dir": str(shared_dir),
            "bots": [
                {
                    "bot_id": r.bot_id,
                    "files_scanned": r.files_scanned,
                    "instances_checked": r.instances_checked,
                    "findings": [asdict(f) for f in r.findings],
                    "warnings": r.warnings,
                }
                for r in all_results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Human-readable
    print("Reflect scan — manifest hygiene\n")
    for r in all_results:
        print(r.summary())
        for w in r.warnings:
            print(f"  WARN: {w}")
        for f in r.findings[:30]:
            print(f"  [{f.kind}] {f.file_path}")
            if f.spec_id:
                print(f"    spec={f.spec_id}", end="")
                if f.instance_id:
                    print(f"  instance={f.instance_id}", end="")
                print()
            print(f"    {f.description}")
        if len(r.findings) > 30:
            print(f"  ... and {len(r.findings) - 30} more findings")
        print()

    total = sum(len(r.findings) for r in all_results)
    print(f"Total findings: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

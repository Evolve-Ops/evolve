"""
migrate_v7.py — One-shot migration from schema v13 (single-file manifest) to v7-arc
(App Spec + App Instance + Provenance + Lessons separation).

SKELETON: function signatures, data flow, and key logic. Full implementation deferred.
Run with --dry-run during development to validate before destructive changes.

Per docs/spec-manifest-v7-2026-05-20.md §10.

Migration scope:
  1. Per-Instance: each /Users/{bot}/.openclaw/workspace/manifests/<app_id>.json
     splits into:
       - App Spec at {shared_dir}/gallery/local/<spec_id>/<spec_version>.json
       - App Instance rewritten in place (or renamed to <instance_id>.json)
       - Empty Lessons stub at {shared_dir}/lessons/<bot_id>/<spec_id>.json
  2. Gallery: existing gallery packages promoted to Specs at
     {shared_dir}/gallery/builtin/<spec_id>/<spec_version>.json
  3. File markers: `pkg=p-... file=f-...` rewritten to `spec=p-... file=f-...@<version>`
     using the canonical YYYY.MM.DD-major.minor format.

Usage:
    # Dry-run (read-only; runs as any user with read access):
    sudo -u evolve PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \
        python3 -m evolve_admin.applications.migrate_v7

    # Apply (destructive; MUST run as root):
    sudo PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \
        python3 -m evolve_admin.applications.migrate_v7 --apply

    # Rollback (also destructive):
    sudo PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \
        python3 -m evolve_admin.applications.migrate_v7 --rollback <timestamp> --apply

Why root for --apply / --rollback:
    The migration writes Instance JSONs into each bot's
    /Users/<bot>/.openclaw/workspace/manifests/ directory and unlinks the
    v13 source. On this pod's setup, some bots' manifests/ dirs have evolve
    write ACL (team_bot_a, evolve); the rest only grant read. Running as root
    bypasses ACL entirely — appropriate for a one-shot operator command.
    Running as evolve will fail for any bot lacking a write ACL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from evolve_util import atomic_write_json
from evolve_util import now_iso as _now_iso

from ..config import bot_home
# can_app_own is the AUTHORITATIVE "may an application own this path?" gate
# (app_ownership_policy.py — the ONE shared predicate). Top-level import is
# cycle-safe: app_ownership_policy imports scanner at module load, and scanner
# imports neither migrate_v7 nor extend_application. We gate realized_files[]
# population here so never-ownable paths (manifest-store self-refs, secrets,
# append-only logs, telemetry indexes) carried in a legacy ``files[]`` are
# never minted into a v7-arc Instance's claims (F-B1 writer-hygiene; the
# read/classify side already routes through this same predicate).
from .app_ownership_policy import can_app_own

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION_V7 = 14
MANIFEST_SHAPE = "v7-arc"
INITIAL_SPEC_VERSION = "2026.05.20-1.0"

# Canonical date-prefixed semver (YYYY.MM.DD-major.minor) — the grammar of
# both Spec versions (gallery file stems) and gallery-package pkg_version
# strings. native_write imports these so the version grammar has one home.
CANONICAL_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d+)\.(\d+)$")


def version_sort_key(version: str) -> tuple[int, int, int, int, int]:
    """Numeric sort key for a canonical version string (so ``1.10`` > ``1.2``).
    Non-conformant / empty strings sort lowest."""
    m = CANONICAL_VERSION_RE.match(version or "")
    if not m:
        return (0, 0, 0, 0, 0)
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


# Provenance fields stamped onto a builtin Spec by ``migrate_gallery_package``,
# recording the repo gallery package version + content fingerprint it was
# seeded from. ``reseed_builtin_specs`` compares these against the live repo
# package so a bumped gallery package propagates to the bound builtin Spec
# without a manual ``migrate_all`` re-run. (The 2026-06-12 U1 morning-briefing
# delivery bug was a builtin Spec stranded at a pre-#2695 package because
# nothing re-seeded it — see docs/decision-add-bot-m4-u1-proof-2026-06-11.md
# Resolution note + #2792.)
SEEDED_FROM_PKG_VERSION_KEY = "seeded_from_pkg_version"
SEEDED_FROM_PKG_SHA256_KEY = "seeded_from_pkg_sha256"

# v13 status values that don't map cleanly to the v7 enum
# (active | paused | draft | deprecated). 'approved' is the most common — it
# was the operator's lifecycle stamp for "running and accepted." Maps to active.
_STATUS_MIGRATION_MAP = {
    "approved": "active",
}


def _migrate_status(v13_status: str | None) -> str:
    """Map a v13 status string to its v7 equivalent. Unknown values pass through."""
    if not v13_status:
        return "active"
    return _STATUS_MIGRATION_MAP.get(v13_status, v13_status)


# ── ID generation ────────────────────────────────────────────────────────────

_SPEC_ID_RE = re.compile(r"^p-[a-f0-9]{8}$")


def _new_spec_id() -> str:
    """Mint a new spec_id (8 hex chars, p- prefix). Stable across versions."""
    return f"p-{secrets.token_hex(4)}"


def _resolve_spec_id(v13: dict, result: "MigrationResult") -> str:
    """
    Choose a spec_id for the migrated Spec.

    Per spec §10.1, the legacy v13 pkg_id IS the new spec_id (the 'p-' prefix
    is preserved by design — every Spec serves the gallery-package role at
    install). Only mint a fresh ID when the legacy pkg_id is missing or
    non-conformant. This keeps file markers and provenance refs stable across
    the migration so rewrite_markers is a keyword/version rewrite rather than
    an ID translation.
    """
    legacy = (v13.get("pkg_id") or "").strip()
    if legacy and _SPEC_ID_RE.match(legacy):
        return legacy
    minted = _new_spec_id()
    if legacy:
        result.warnings.append(
            f"legacy pkg_id {legacy!r} doesn't match canonical pattern; "
            f"minted fresh spec_id {minted!r} (markers will need translation)"
        )
    return minted


def _new_instance_id() -> str:
    """Mint a new instance_id (8 hex chars, i- prefix). Bot-local."""
    return f"i-{secrets.token_hex(4)}"


def _new_lessons_id() -> str:
    return f"l-{secrets.token_hex(4)}"


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class MigrationResult:
    """Outcome of migrating a single source artifact."""
    source_path: Path
    dry_run: bool
    spec_path: Optional[Path] = None
    instance_path: Optional[Path] = None
    lessons_path: Optional[Path] = None
    spec_id: Optional[str] = None
    instance_id: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors


@dataclass
class AggregateResult:
    """Aggregate over a full migration run."""
    instance_results: list[MigrationResult] = field(default_factory=list)
    gallery_results: list[MigrationResult] = field(default_factory=list)
    marker_files_rewritten: int = 0
    marker_warnings: list[str] = field(default_factory=list)
    spec_id_map: dict[str, str] = field(default_factory=dict)  # old pkg_id → new spec_id
    backup_timestamp: str = ""  # populated on --apply runs; empty on dry-run

    def summary(self) -> str:
        ok_inst = sum(1 for r in self.instance_results if r.succeeded)
        ok_gal = sum(1 for r in self.gallery_results if r.succeeded)
        marker_line = f"Markers:   {self.marker_files_rewritten} files rewritten"
        if self.marker_warnings:
            marker_line += f" ({len(self.marker_warnings)} warning(s))"
        return (
            f"Instances: {ok_inst}/{len(self.instance_results)} migrated\n"
            f"Gallery:   {ok_gal}/{len(self.gallery_results)} migrated\n"
            + marker_line
        )


# ── Backup mechanism (for safe --apply with auto-rollback) ───────────────────

BACKUP_MANIFEST_VERSION = "v13_to_v7_arc"


@dataclass
class BackupRun:
    """
    Tracks a single migration run's destructive operations so they can be
    undone. Initialized at the start of --apply, finalized when the run
    completes (or partially, on crash — the manifest.json is rewritten after
    each operation so rollback works even if the script aborts mid-run).

    Layout:
        {shared_dir}/migration_backup/v13_to_v7_arc/<timestamp>/
        ├── manifest.json       # ordered list of operations (rewritten per op)
        └── originals/
            └── <hash>.json     # copy of each v13 file before unlink

    Markers are intentionally NOT backed up — bot workspaces are git-tracked
    (per CLAUDE.md backup design), so marker rewrites can be reverted via
    `git checkout` on the bot's workspace if needed. This keeps the backup
    footprint reasonable (178 marker rewrites in the test pod's case).
    """
    backup_dir: Path
    operations: list[dict] = field(default_factory=list)
    started_at: str = ""

    @classmethod
    def create(cls, shared_dir: Path) -> "BackupRun":
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = shared_dir / "migration_backup" / BACKUP_MANIFEST_VERSION / ts
        (backup_dir / "originals").mkdir(parents=True, exist_ok=True)
        return cls(backup_dir=backup_dir, started_at=_now_iso())

    @property
    def timestamp(self) -> str:
        return self.backup_dir.name

    def backup_source(self, source_path: Path) -> Path:
        """Copy a soon-to-be-destroyed source file into the backup dir."""
        # Hash by absolute path so each source has a unique backup filename.
        h = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:16]
        backup_path = self.backup_dir / "originals" / f"{h}.json"
        shutil.copy2(source_path, backup_path)
        return backup_path

    def record_unlink(self, source_path: Path, backup_path: Path, context: dict) -> None:
        """Record that source_path was unlinked; backup_path holds its content."""
        self.operations.append({
            "action": "restore",
            "target": str(source_path),
            "backup": str(backup_path.relative_to(self.backup_dir)),
            "context": context,
        })
        self._write_manifest()

    def record_creation(self, target_path: Path, context: dict) -> None:
        """Record that target_path was created (didn't exist before)."""
        self.operations.append({
            "action": "delete",
            "target": str(target_path),
            "context": context,
        })
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Atomic manifest write — rewritten after each operation so a crash
        mid-run still leaves a usable rollback record."""
        data = {
            "timestamp": self.timestamp,
            "version": BACKUP_MANIFEST_VERSION,
            "started_at": self.started_at,
            "updated_at": _now_iso(),
            "operations": self.operations,
        }
        _write_json_mkdirs(self.backup_dir / "manifest.json", data)


# ── Per-Instance migration ───────────────────────────────────────────────────

def migrate_instance(
    v13_manifest_path: Path,
    shared_dir: Path,
    bot_id: str,
    dry_run: bool = True,
    *,
    backup: Optional[BackupRun] = None,
) -> MigrationResult:
    """
    Migrate one v13 per-bot manifest to v7-arc shape.

    Steps:
      1. Read the v13 JSON.
      2. Extract Spec-shape fields into a new App Spec; write to
         {shared_dir}/gallery/local/<spec_id>/<spec_version>.json.
      3. Build the new App Instance shape (provenance, realized_files,
         configured_schedules, learned_config, usage_metadata, change_log empty).
      4. Write the Instance to its target path (rename to <instance_id>.json).
      5. Create an empty Lessons stub at
         {shared_dir}/lessons/<bot_id>/<spec_id>.json.

    The change_log starts empty — pre-migration history is intentionally lost
    (a watermark). Spec_id is minted fresh; pkg_id from the v13 manifest is
    recorded in the result's spec_id_map for marker rewriting.

    Manual-review flags (added as warnings, not errors):
      - apps with prose hints suggesting event-driven behavior in build_spec
      - personal-bot apps where the inferred privacy defaults may be too lax
    """
    result = MigrationResult(source_path=v13_manifest_path, dry_run=dry_run)

    try:
        v13 = json.loads(v13_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        result.errors.append(f"failed to read source: {e}")
        return result

    spec_id = _resolve_spec_id(v13, result)
    instance_id = _new_instance_id()
    result.spec_id = spec_id
    result.instance_id = instance_id

    # ── Step 1: build App Spec ──
    app_spec = _extract_spec(v13, spec_id, result)
    spec_path = (
        shared_dir / "gallery" / "local" / spec_id / f"{INITIAL_SPEC_VERSION}.json"
    )
    result.spec_path = spec_path

    # ── Step 2: build App Instance ──
    # bot_home() resolves the actual macOS home — bot_id may differ from the
    # account name (team_bot_b/personal_bot_user case); the helper handles that disambiguation.
    app_instance = _extract_instance(v13, instance_id, spec_id, bot_id, result)
    bot_workspace = bot_home(bot_id) / ".openclaw" / "workspace"
    instance_dir = bot_workspace / "manifests"
    instance_path = instance_dir / f"{instance_id}.json"
    result.instance_path = instance_path

    # ── Step 3: build empty Lessons stub ──
    # Lessons live under {shared_dir}/lessons/<bot_id>/, NOT per-bot workspace.
    # The bot's .openclaw/ dir is owned by the bot user; evolve only has ACL
    # read on it, so it can't mkdir a new subdir there. {shared_dir} is owned
    # by evolve (matches the signal-store layout per CLAUDE.md). Lessons are
    # inherently pod-shareable artifacts anyway, so pod-shared storage matches
    # their semantic.
    lessons = _empty_lessons_stub(spec_id, bot_id, shared_dir)
    lessons_dir = shared_dir / "lessons" / bot_id
    lessons_path = lessons_dir / f"{spec_id}.json"
    result.lessons_path = lessons_path

    if dry_run:
        return result

    # ── Step 4: write all three files (atomic via /tmp staging) ──
    # Order: backup source → write artifacts → unlink source. If any write
    # fails partway, the original survives (we haven't unlinked yet) and the
    # backup gives the operator a recovery hook either way.
    try:
        backup_path: Optional[Path] = None
        if backup is not None:
            backup_path = backup.backup_source(v13_manifest_path)

        _write_json_mkdirs(spec_path, app_spec)
        if backup is not None:
            backup.record_creation(spec_path, {"kind": "spec", "spec_id": spec_id})

        _write_json_mkdirs(instance_path, app_instance)
        if backup is not None:
            backup.record_creation(instance_path, {"kind": "instance", "instance_id": instance_id, "bot_id": bot_id})

        _write_json_mkdirs(lessons_path, lessons)
        if backup is not None:
            backup.record_creation(lessons_path, {"kind": "lessons", "spec_id": spec_id, "bot_id": bot_id})

        # Step 5: remove the original v13 file only after all three writes
        # succeed. Backup already captured its contents (if backup is set);
        # rollback uses that to restore.
        v13_manifest_path.unlink()
        if backup is not None and backup_path is not None:
            backup.record_unlink(
                v13_manifest_path,
                backup_path,
                {"kind": "v13_source", "bot_id": bot_id, "spec_id": spec_id},
            )
    except OSError as e:
        result.errors.append(f"failed to write artifacts: {e}")

    return result


def _extract_spec(
    v13: dict,
    spec_id: str,
    result: MigrationResult,
    *,
    spec_version: str = INITIAL_SPEC_VERSION,
) -> dict:
    """
    Build an App Spec from v13 fields. Atlas-gap fields are inferred (with
    warnings flagging manual review where defaults may be wrong). The
    spec_id is resolved by _resolve_spec_id at the caller — pass-through here.

    ``spec_version`` defaults to the migration watermark; the native-write
    path (native_write.py, Slice 3a) passes a freshly minted install-date
    version instead. Same extraction either way — one shape, two callers.
    """
    spec: dict[str, Any] = {
        "spec_id": spec_id,
        "spec_version": spec_version,
        "name": v13.get("name") or v13.get("display_name") or "untitled",
        "app_version": v13.get("app_version") or "0.1.0",
        "schema_version": SCHEMA_VERSION_V7,
        "manifest_shape": MANIFEST_SHAPE,
        "objective": _build_objective(v13),
        "success_criteria": _build_success_criteria(v13),
        "blueprint": _build_blueprint(v13, result),
        "dependencies": _build_dependencies(v13),
        "audience_scoping": _infer_audience_scoping(v13, result),
        "approval_audience": v13.get("approval_audience") or "pod_operator",
        "tags": v13.get("tags") or [],
    }

    # Atlas-gap inferences (warnings for manual review)
    if event_triggers := _infer_event_triggers(v13, result):
        spec["event_triggers"] = event_triggers
    if schedules := _build_schedules(v13):
        spec["schedules"] = schedules
    if bot_guidance := _extract_bot_guidance(v13, result):
        spec["bot_guidance"] = bot_guidance
    if privacy := _infer_privacy(v13, result):
        spec["privacy"] = privacy
    if scope_excludes := v13.get("scope_excludes"):
        spec["scope_excludes"] = scope_excludes

    # Preserve the v13 `identity` block. v7's `objective` is the canonical
    # purpose statement going forward, but identity carried richer prose
    # (purpose / scope_includes / scope_excludes / user) the operator wrote.
    # Dropping it silently in the migration left the admin UI's view modal
    # empty for migrated apps. Pass it through verbatim; consumers that
    # prefer objective can ignore identity.
    if identity := v13.get("identity"):
        spec["identity"] = identity

    # Preserve v13's `description`. Same drop-on-migration as `identity` above:
    # hydrate_v7_arc_instance reads spec["description"] when building the
    # Instance view for the UI, and the tile/card pulls from there. Without
    # passthrough every migrated app would lose its operator-authored summary.
    # The server has a fallback to identity.purpose, which masks this bug for
    # most apps — but Specs migrated before #1469 lack identity too, so both
    # fallbacks fail and the tile renders empty.
    if description := v13.get("description"):
        spec["description"] = description

    # S2.13 — preserve high-content operator-authored fields the audit
    # surfaced. Per real-world quantification on the test pod:
    #   constraints (full block, not just .privacy) — 78% populated
    #   test_cases                                  — 76%
    #   example_triggers                            — 76%
    #   owner / inputs / outputs                    — 13% each
    #   scheduled_actions                           —  8%
    # All passthrough; consumers that don't care can ignore them.
    for field in ("constraints", "test_cases", "example_triggers",
                  "owner", "inputs", "outputs", "scheduled_actions"):
        value = v13.get(field)
        if value:
            spec[field] = value

    return spec


def _extract_instance(
    v13: dict,
    instance_id: str,
    spec_id: str,
    bot_id: str,
    result: MigrationResult,
    *,
    spec_version: str = INITIAL_SPEC_VERSION,
    installed_by: str = "migration",
    history_reason: str = "migration_from_v13",
) -> dict:
    """Build an App Instance from v13's per-bot fields.

    The keyword knobs exist for the native-write path (native_write.py,
    Slice 3a): a freshly forged/scanned app records ``installed_by``
    "forge_engine"/"scanner" with reason "initial_install" and the minted
    install-date spec_version. Defaults preserve migration behavior.
    """
    instance: dict[str, Any] = {
        "instance_id": instance_id,
        "bot_id": bot_id,
        "schema_version": SCHEMA_VERSION_V7,
        "manifest_shape": MANIFEST_SHAPE,
        "provenance": {
            "spec_id": spec_id,
            "spec_version": spec_version,
            "source_pod_id": None,  # locally-original — migration produces local Specs only
            "source_bot_id": None,
            "installed_at": v13.get("created_at") or _now_iso(),
            "installed_by": installed_by,
            "forked_from": None,
            # Append-only supersession chain (oldest→newest), NOT including the
            # current spec_id above. Populated by spec_lineage.record_spec_
            # supersession when an app is rebuilt under a fresh spec_id (forge
            # re-create / scanner re-discovery); spec_lineage.resolve_spec
            # consults it so a marker carrying a retired spec_id still resolves
            # to this live Instance. Born empty on a fresh mint. Optional /
            # back-compat: absent reads as []. See docs/spec-manifest-v7-2026-
            # 05-20.md §5.
            "prior_spec_ids": [],
        },
        "spec_version_history": [
            {
                "version": spec_version,
                "adopted_at": _now_iso(),
                "reason": history_reason,
            }
        ],
        "dependency_check_at_install": {
            "checked_at": _now_iso(),
            "all_required_satisfied": True,  # assume satisfied; pre-migration apps were running
            "details": [],  # TODO: backfill from a Forge dependency-resolution pass
            "optional_missing": [],
        },
        "realized_files": _build_realized_files(
            v13, result, default_version=spec_version,
        ),
        "configured_schedules": _build_configured_schedules(v13),
        "learned_config": v13.get("usage") or {},  # v10 'usage' was a free-form bag
        "usage_metadata": {
            "invocation_count": v13.get("invocation_count", 0),
            "last_run": v13.get("last_run") or v13.get("last_test_run", "1970-01-01T00:00:00Z"),
            "error_count": v13.get("error_count", 0),
            "last_error_at": v13.get("last_error_at", "1970-01-01T00:00:00Z"),
        },
        "change_log": [],  # intentionally empty — pre-migration history is the watermark
        "status": _migrate_status(v13.get("status")),
        "last_reflect_at": _now_iso(),
    }

    # S2.13 — preserve per-bot v13 fields the audit surfaced:
    #   evidence_files          — 73% populated; operator-curated paths
    #   last_audit              — 89%; Tier-3 audit_runner.py state
    #   last_structural_verify  — 89%; Tier-2 audit_runner.py state
    #   audit_trail_path        — bot-local path to per-app audit log
    # Pass through so daemons (audit_runner, scanner) can resume their state
    # without forced re-runs.
    for field in ("evidence_files", "last_audit",
                  "last_structural_verify", "audit_trail_path"):
        value = v13.get(field)
        if value:
            instance[field] = value

    return instance


def _empty_lessons_stub(
    spec_id: str,
    bot_id: str,
    shared_dir: Path,
    *,
    spec_version: str = INITIAL_SPEC_VERSION,
) -> dict:
    """Initial Lessons file — empty lessons[] but valid structure."""
    return {
        "lessons_id": _new_lessons_id(),
        "spec_id": spec_id,
        "spec_version_observed": spec_version,
        "source_pod_id": _resolve_local_pod_id(shared_dir),
        "source_bot_id": bot_id,
        "observation_window": {
            "start": _now_iso(),
            "end": _now_iso(),
            "instance_runs": 0,
        },
        "lessons": [],
        "redaction_applied": False,
    }


# ── Spec field builders ──────────────────────────────────────────────────────

def _build_objective(v13: dict) -> dict:
    """v13 has scalar 'objective' (string); v7 wraps in {primary, sub_objectives}."""
    primary = (v13.get("objective") or v13.get("purpose") or "").strip()
    if not primary:
        primary = v13.get("description") or "untitled"
    return {
        "primary": primary,
        "sub_objectives": v13.get("goals") or [],
    }


def _build_success_criteria(v13: dict) -> dict:
    """
    v13's success_criteria object is loosely structured — across real manifests
    the keys vary: behavioral/user_visible/observable/observable_outcomes/
    metrics/failure_signals/minimum_bar. v7 canonicalizes on
    behavioral + observable but we preserve the other fields so operator-
    authored content survives the migration and the UI sees them.
    """
    sc = v13.get("success_criteria") or {}
    out: dict[str, Any] = {
        "behavioral": sc.get("behavioral") or sc.get("user_visible") or [],
        "observable": (
            sc.get("observable")
            or sc.get("observable_outcomes")
            or sc.get("metrics")
            or []
        ),
    }
    # Preserve any other v13 fields the schema allows (additionalProperties: true).
    for k in ("observable_outcomes", "failure_signals", "minimum_bar"):
        if k in sc and sc[k]:
            out[k] = sc[k]
    return out


def _normalize_v13_file_entry(entry: Any) -> dict:
    """
    v13 manifests carry `files` as either list[str] (v4 shape — plain paths)
    or list[dict] (v5+ shape — full provenance records). Coerce both to a dict
    with the keys the migration helpers expect.
    """
    if isinstance(entry, str):
        return {"path": entry, "description": "", "file_id": ""}
    if isinstance(entry, dict):
        return entry
    return {"path": "", "description": "", "file_id": ""}


# Script command parser — pulls a script path out of an `interface_contract.cli[].command`.
# Real shapes seen on gallery schema-5 specs:
#   "python3 scripts/email_sync.py sync"
#   "python3 scripts/calendar_summary.py preview"
#   "/usr/bin/python3 scripts/x.py thread THREAD_ID"
#   "bash scripts/cron.sh"
_CLI_SCRIPT_RE = re.compile(
    r"^\s*"
    r"(?:/\S+/)?"                  # optional interpreter abspath prefix
    r"(?:python\d?|bash|sh|node|ruby)"  # interpreter
    r"\s+"
    r"(\S+\.(?:py|sh|js|ts|rb))"   # captured script path
    r"(?:\s|$)"
)

# `## FILE: <path>` blocks in build_spec markdown — used by gallery specs to
# embed config/plist/cron file templates that should land on disk at install.
_FILE_BLOCK_RE = re.compile(r"^## FILE:\s+(\S+)", re.MULTILINE)


def _file_role_for_path(path: str) -> str:
    """Map a path's extension to a v7 blueprint role."""
    ext = Path(path).suffix.lower()
    if ext in (".py", ".sh", ".bash", ".js", ".ts", ".rb"):
        return "vital_to_blueprint"
    if ext in (".plist", ".xml"):
        # Installer artifacts (LaunchDaemon plists, etc.) — recreated at install.
        return "vital_to_blueprint"
    if ext in (".jsonl", ".json", ".csv", ".db"):
        return "instance_specific"
    if ext in (".md", ".txt", ".rst"):
        return "reference_only"
    return "vital_to_blueprint"


def _file_entry(path: str, intent: str, *, role: Optional[str] = None) -> dict:
    """Build a v7 `blueprint.files[*]` entry from a path + intent."""
    ext = Path(path).suffix.lower()
    return {
        "logical_name": Path(path).stem or "unnamed",
        "role": role or _file_role_for_path(path),
        "intent": intent or f"File at {path}",
        "language": ext.lstrip(".") or "unknown",
        "expected_location": path,
    }


def _build_blueprint(v13: dict, result: MigrationResult) -> dict:
    """
    Build `blueprint.files[]` from whichever source the v13 manifest carries.

    v13 / schema-5 manifests scatter file information across four places, in
    declining order of structure:

      1. Top-level `files: [{path, description, …}]` — v4/v5+ provenance shape.
         This is what `migrate_instance` always has and what `_build_realized_files`
         reads. Gallery specs in the schema-5 lineage leave this EMPTY and put
         their roster in interface_contract / build_spec instead.
      2. `interface_contract.cli[].command` — `python3 scripts/X.py sub` style
         entries. The script path is the part we want.
      3. `interface_contract.data_files[].path` — output JSON / data declarations.
      4. `## FILE: <path>` blocks in `build_spec` — embedded config/plist/cron
         templates the build provisions verbatim.

    All four sources are unioned and de-duplicated by path. Without this union,
    gallery-shape Specs lose their entire file roster on migration (the bug
    behind the empty `blueprint.files[]` on every `/Users/Shared/evolve/gallery/builtin/*`
    Spec on the production pod).
    """
    out_files: dict[str, dict] = {}  # path → entry, preserves order

    # Source 1 — top-level files[] (v4/v5+ provenance shape).
    for raw in v13.get("files") or []:
        f = _normalize_v13_file_entry(raw)
        path = (f.get("path") or "").strip()
        if not path:
            continue
        intent = f.get("description") or ""
        ext = Path(path).suffix.lower()
        if not ext:
            result.warnings.append(
                f"role inferred as 'vital_to_blueprint' for unknown extension: {path}"
            )
        out_files[path] = _file_entry(path, intent)

    # Source 2 — interface_contract.cli[].command → script paths.
    ic = v13.get("interface_contract") or {}
    cli_subs: dict[str, list[str]] = {}  # script path → list of subcommand tails
    for entry in ic.get("cli") or []:
        if not isinstance(entry, dict):
            continue
        cmd = (entry.get("command") or "").strip()
        m = _CLI_SCRIPT_RE.match(cmd)
        if not m:
            continue
        script_path = m.group(1)
        tail = cmd[m.end():].strip()
        cli_subs.setdefault(script_path, []).append(tail or "default")
    for script_path, subs in cli_subs.items():
        if script_path in out_files:
            continue
        # De-dup subcommand tails while preserving order.
        seen: set[str] = set()
        unique_subs = [s for s in subs if not (s in seen or seen.add(s))]
        intent = f"CLI script. Subcommands: {', '.join(unique_subs)}."
        out_files[script_path] = _file_entry(script_path, intent)

    # Source 3 — interface_contract.data_files[].path.
    for entry in ic.get("data_files") or []:
        if not isinstance(entry, dict):
            continue
        path = (entry.get("path") or "").strip()
        if not path or path in out_files:
            continue
        intent = (entry.get("description") or "").strip() or f"Data file at {path}"
        out_files[path] = _file_entry(path, intent)

    # Source 4 — `## FILE: <path>` blocks embedded in build_spec markdown.
    build_spec = v13.get("build_spec") or ""
    for path in _FILE_BLOCK_RE.findall(build_spec):
        if path in out_files:
            continue
        out_files[path] = _file_entry(path, f"Embedded template rendered at install: {path}")

    return {"files": list(out_files.values())}


def _build_dependencies(v13: dict) -> dict:
    """
    v13 has flat lists of dependencies; v7 splits into seven kinds.
    Heuristics:
      - Treat v13 'dependencies' (list of strings) as python_packages by default.
      - v13 'app_dependencies' (v6+) → apps.
      - v13 'requirements.integrations[]' (schema-5+ gallery shape) → integrations.
    """
    return {
        "apps": _migrate_app_dependencies(v13.get("app_dependencies") or []),
        "python_packages": [
            {"name": pkg, "required": True}
            for pkg in (v13.get("dependencies") or [])
            if isinstance(pkg, str)
        ],
        "system_packages": [],
        "oc_plugins": [],
        "oc_skills": [],
        "integrations": _build_integrations(v13),
        "credentials": [],
    }


def _build_integrations(v13: dict) -> list[dict]:
    """
    Translate v13 schema-5+ `requirements.integrations[]` into the v7
    integrationDependency shape (integration_id / scopes / required / purpose).

    Schema-5 entries carry richer fields than v7 captures natively:

        {
          "id": "gmail",
          "display_name": "Gmail",
          "required": true,
          "check_path": "openclaw.json → integrations.gmail",
          "setup_doc": "docs/integrations/gmail.md",
          "reason": "Reads email via Gmail API",
          "required_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
          "alternatives": [{id, required, check_path, required_scopes, setup_doc, reason}]
        }

    Mapping:
      - integration_id ← id
      - scopes         ← required_scopes (empty list if absent)
      - required       ← required (default True)
      - purpose        ← reason; check_path / setup_doc / alternatives summary
                         are appended so the operator's install dialog still
                         has actionable text without inventing a new v7 field.

    Alternatives are *not* forked into separate dependency entries — they're
    install-time substitutes, not co-required channels. They survive as a
    one-line summary inside `purpose`.
    """
    out: list[dict] = []
    req = v13.get("requirements")
    if not isinstance(req, dict):
        return out
    for entry in req.get("integrations") or []:
        if not isinstance(entry, dict):
            continue
        iid = (entry.get("id") or "").strip()
        if not iid:
            continue

        scopes_raw = entry.get("required_scopes")
        scopes = [s for s in (scopes_raw or []) if isinstance(s, str) and s.strip()]

        purpose_parts: list[str] = []
        if reason := (entry.get("reason") or "").strip():
            purpose_parts.append(reason)
        if check_path := (entry.get("check_path") or "").strip():
            purpose_parts.append(f"Check: {check_path}.")
        if setup_doc := (entry.get("setup_doc") or "").strip():
            purpose_parts.append(f"Setup: {setup_doc}.")
        alts = entry.get("alternatives") or []
        if isinstance(alts, list) and alts:
            alt_summary = ", ".join(
                (a.get("id") or "").strip() or "?"
                for a in alts
                if isinstance(a, dict) and (a.get("id") or "").strip()
            )
            if alt_summary:
                purpose_parts.append(f"Alternatives: {alt_summary}.")

        out.append({
            "integration_id": iid,
            "scopes": scopes,
            "required": bool(entry.get("required", True)),
            "purpose": " ".join(purpose_parts)
                       or f"Required by app (integration: {iid}).",
        })
    return out


def _migrate_app_dependencies(raw: list) -> list[dict]:
    """
    v13 `app_dependencies` is either:
      - list[str]  — bare pkg_ids (older shape)
      - list[dict] — {pkg_id, display_name, required, reason} (ea-pack shape)

    Coerce both into the v7 shape {spec_id, required, purpose}. Reject
    anything that doesn't match the spec_id pattern (e.g. display names
    accidentally stored in this field).
    """
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            if _SPEC_ID_RE.match(entry):
                out.append({"spec_id": entry, "required": True})
        elif isinstance(entry, dict):
            pkg_id = (entry.get("pkg_id") or entry.get("spec_id") or "").strip()
            if not _SPEC_ID_RE.match(pkg_id):
                continue
            out.append({
                "spec_id": pkg_id,
                "required": bool(entry.get("required", True)),
                "purpose": (entry.get("reason") or entry.get("purpose") or "").strip()
                           or f"Required by app (originally referenced as {entry.get('display_name', pkg_id)!r})",
            })
    return out


def _infer_audience_scoping(v13: dict, result: MigrationResult) -> dict:
    """
    Default to operator_only for personal-bot apps; named_users for team bots.
    Flag for manual review either way — this is a security boundary the
    operator must confirm.

    A DECLARED v24 ``audience_scoping{}`` block on the source manifest is
    authoritative and passes through verbatim — post-Slice-2 manifests
    carry blocks the operator (or forge Critique) authored; inference
    must never clobber them. This matters for the Slice-3a native-write
    path especially: drafts are seeded at mint time and may be edited
    before the build completes.

    The inferred block itself is the canonical default from
    ``privacy_scoping_validator`` — one source of truth shared with the
    v24 backfill pass and forge's fresh-install seeding, so pre- and
    post-v24 artifacts agree (slicing spec §4.3).
    """
    declared = v13.get("audience_scoping")
    if isinstance(declared, dict) and declared:
        return declared

    # TODO: derive default from bot's network.json role (personal vs. team)
    result.warnings.append(
        "audience_scoping defaulted to operator_only — REVIEW before deploy"
    )
    from .privacy_scoping_validator import default_audience_scoping_block
    return default_audience_scoping_block()


def _infer_event_triggers(v13: dict, result: MigrationResult) -> list:
    """
    v13 doesn't have first-class event triggers. Heuristic: scan build_spec
    prose for trigger-like phrasing ('when the user sends', 'on incoming', etc.)
    and flag for manual conversion.
    """
    build_spec = (v13.get("build_spec") or "").lower()
    trigger_hints = ["when the user", "on incoming", "when a message", "event:"]
    if any(h in build_spec for h in trigger_hints):
        result.warnings.append(
            "build_spec prose suggests event-driven behavior — "
            "MANUAL CONVERSION required for event_triggers[]"
        )
    return []  # Always empty after migration; operator fills in per app.


def _build_schedules(v13: dict) -> list:
    """v13 has `crons: [{schedule, command}]`. Map directly."""
    out = []
    for cron in v13.get("crons") or []:
        out.append({
            "id": cron.get("name") or "unnamed",
            "cron_intent": cron.get("description") or "",
            "cron_default": cron.get("schedule") or "",
            "invokes": Path(cron.get("command", "")).stem or "unknown",
        })
    return out


def _extract_bot_guidance(v13: dict, result: MigrationResult) -> list:
    """
    Extract markdown sections from v13's build_spec that look like AGENTS.md
    guidance (heading + paragraphs). Uses the existing provisioner regex
    pattern as a starting point.

    TODO: import the actual provisioner regex from provisioner.py to ensure
    parity with what's currently being spliced into AGENTS.md today.
    """
    build_spec = v13.get("build_spec") or ""
    # Naive: any '## ' heading followed by content
    import re
    pattern = re.compile(r"(## [^\n]+)\n(.*?)(?=\n## |\Z)", re.DOTALL)
    blocks = []
    for m in pattern.finditer(build_spec):
        blocks.append({
            "section": m.group(1).strip(),
            "content": m.group(2).strip(),
        })
    if blocks:
        result.warnings.append(
            f"extracted {len(blocks)} bot_guidance section(s) from build_spec — "
            "verify against current AGENTS.md splices"
        )
    return blocks


def _infer_privacy(v13: dict, result: MigrationResult) -> dict:
    """
    v13's constraints.privacy is prose; v7's privacy block is structured.

    A DECLARED v24 ``privacy{}`` block on the source manifest is
    authoritative and passes through verbatim (same rationale as
    ``_infer_audience_scoping`` above — never clobber authored blocks).

    Real-world v13 manifests ship constraints.privacy as either a string OR
    a list of bullet-style strings. v7's consent_notice is a single string —
    we join list-form prose with newline-prefixed bullets for readability.
    """
    declared = v13.get("privacy")
    if isinstance(declared, dict) and declared:
        return declared

    constraints = v13.get("constraints") or {}
    privacy_raw = constraints.get("privacy")
    if isinstance(privacy_raw, list):
        privacy_prose = "\n".join(
            f"- {item}" for item in privacy_raw if isinstance(item, str) and item.strip()
        )
    elif isinstance(privacy_raw, str):
        privacy_prose = privacy_raw
    else:
        privacy_prose = ""

    from .privacy_scoping_validator import default_privacy_block

    if not privacy_prose:
        return default_privacy_block()
    result.warnings.append(
        "privacy block built from prose — REVIEW user_data_collected and consent_notice"
    )
    block = default_privacy_block()
    # TODO: extract user_data_collected via LLM at share-time
    block["consent_notice"] = privacy_prose[:2000]
    return block


# Canonical file_id shapes accepted by the v7 schema
_FILE_ID_CANONICAL_RE = re.compile(
    r"^f-[a-f0-9]{8}@[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]+\.[0-9]+$"
)
_FILE_ID_BARE_RE = re.compile(r"^f-[a-f0-9]{8}$")
_FILE_ID_OLD_DOT_RE = re.compile(
    r"^(f-[a-f0-9]{8})@([0-9]{4}\.[0-9]{2}\.[0-9]{2})\.([0-9]+)$"
)


def _normalize_file_id(
    raw: str,
    path: str,
    result: MigrationResult,
    *,
    default_version: str = INITIAL_SPEC_VERSION,
) -> str:
    """
    Coerce a v13 file_id string into the v7 canonical shape
    'f-<8hex>@<YYYY.MM.DD>-<major>.<minor>'.

    Handles four real-world inputs:
      - canonical v7 form               → returned as-is
      - bare 'f-<hex>'                  → append @<INITIAL_SPEC_VERSION>
      - 'f-<hex>@<YYYY.MM.DD>.<n>'      → rewrite to '<id>@<date>-<n>.0'
      - non-conformant (human labels)   → mint fresh, warn (the v13 manifest
                                           stored arbitrary strings in this slot
                                           rather than scanner-minted IDs)
      - empty                           → mint fresh, warn

    The minted-fresh case is flagged for scanner re-verification — the file's
    actual on-disk marker may carry a different ID we should reconcile to.
    """
    if not raw:
        minted = f"f-{secrets.token_hex(4)}@{default_version}"
        result.warnings.append(f"minted file_id for {path} (was empty); rescan to verify marker")
        return minted

    if _FILE_ID_CANONICAL_RE.match(raw):
        return raw

    if _FILE_ID_BARE_RE.match(raw):
        return f"{raw}@{default_version}"

    if m := _FILE_ID_OLD_DOT_RE.match(raw):
        # 'f-abc12345@2026.05.20.1' → 'f-abc12345@2026.05.20-1.0'
        return f"{m.group(1)}@{m.group(2)}-{m.group(3)}.0"

    minted = f"f-{secrets.token_hex(4)}@{default_version}"
    result.warnings.append(
        f"minted file_id for {path} (was non-conformant {raw!r}); rescan to verify marker"
    )
    return minted


def _build_realized_files(
    v13: dict,
    result: MigrationResult,
    *,
    default_version: str = INITIAL_SPEC_VERSION,
) -> list:
    """
    Build realized_files[] from v13's files[]. Carry over existing marker
    file_ids when they're conformant; mint new ones (with warnings) for the
    rest. Post-migration scanner pass should reconcile minted IDs against the
    actual on-disk markers.

    Handles both v4 (list[str]) and v5+ (list[dict]) shapes via
    _normalize_v13_file_entry.
    """
    out = []
    for raw in v13.get("files") or []:
        f = _normalize_v13_file_entry(raw)
        path = f.get("path", "")
        # Writer-hygiene gate (F-B1): a never-ownable path (manifest-store
        # self-ref, secret/salt material, append-only log, telemetry index,
        # OC-standard file) must never be minted into realized_files[]. The
        # legacy ``files[]`` of an on-disk manifest can carry such paths; left
        # ungated they regenerate an invalid claim on every scan. The shared
        # can_app_own predicate is the same gate the read/classify side uses
        # (recon_ledger invalid_claim), so the writer and reader agree.
        if not can_app_own(path):
            result.warnings.append(
                f"dropped never-ownable path from realized_files: {path!r} "
                f"(ownership_policy)"
            )
            continue
        file_id = _normalize_file_id(
            f.get("file_id") or "", path, result,
            default_version=default_version,
        )
        out.append({
            "logical_name": Path(path).stem or "unnamed",
            "path": path,
            "file_id": file_id,
            "marker_state": "OWNED",
            "created_in_session": "",
        })
    return out


def _build_configured_schedules(v13: dict) -> list:
    """v13 crons → v7 configured_schedules (linked back to Spec.schedules[].id)."""
    out = []
    for cron in v13.get("crons") or []:
        out.append({
            "spec_schedule_id": cron.get("name") or "unnamed",
            "resolved_cron": cron.get("schedule") or "",
            "configured_at": v13.get("created_at") or _now_iso(),
            "user_adjustments": [],
        })
    return out


def _resolve_local_pod_id(shared_dir: Path) -> str:
    """
    Read pod_id from network.json.

    Pod identity lives at top-level `networkId`; the `pod` block carries
    other fields (admins, ssh_target, passphrases) but not the ID itself.
    """
    try:
        network = json.loads((shared_dir / "network.json").read_text())
        return network.get("networkId") or "pod-unknown"
    except (OSError, json.JSONDecodeError):
        return "pod-unknown"


# ── Gallery migration ────────────────────────────────────────────────────────

def migrate_gallery_package(
    pkg_path: Path,
    shared_dir: Path,
    dry_run: bool = True,
    *,
    backup: Optional[BackupRun] = None,
) -> MigrationResult:
    """
    Promote a built-in gallery package to a v7 Spec.

    Existing gallery package at /repo/gallery/<name>/<pkg_id>.json
    becomes a Spec at {shared_dir}/gallery/builtin/<spec_id>/<spec_version>.json.

    pkg_id is reused as spec_id (no renaming) since the legacy 'p-' prefix
    serves both roles. The original repo gallery file is NOT touched; this
    is purely additive (a NEW Spec in shared_dir).

    If a backup is supplied, the new Spec path is recorded so --rollback can
    delete it. Defense in depth — even though gallery migration is additive
    rather than destructive, residual Specs after a partial-failure run are
    confusing and worth cleaning up.
    """
    result = MigrationResult(source_path=pkg_path, dry_run=dry_run)
    try:
        pkg = json.loads(pkg_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        result.errors.append(f"failed to read source: {e}")
        return result

    spec_id = pkg.get("pkg_id") or _new_spec_id()
    result.spec_id = spec_id
    spec_path = shared_dir / "gallery" / "builtin" / spec_id / f"{INITIAL_SPEC_VERSION}.json"
    result.spec_path = spec_path

    # Gallery packages are already Spec-shaped (objective + build_spec) but missing
    # Atlas-gap fields. Reuse the same extraction helper as Instance migration.
    spec = _extract_spec(pkg, spec_id, result)
    # Record which repo package version + content this builtin was seeded from
    # so the deploy-time re-seed (reseed_builtin_specs) can detect a newer
    # repo package and propagate it.
    _stamp_seed_provenance(spec, pkg)

    if not dry_run:
        try:
            _write_json_mkdirs(spec_path, spec)
            if backup is not None:
                backup.record_creation(
                    spec_path,
                    {"kind": "gallery_spec", "spec_id": spec_id, "source": str(pkg_path)},
                )
        except OSError as e:
            result.errors.append(f"failed to write Spec: {e}")
    return result


# ── Builtin Spec re-seed (deploy-time gallery propagation) ───────────────────

def _canonical_pkg_hash(pkg: dict) -> str:
    """Stable content fingerprint of a gallery package dict — key-order- and
    whitespace-independent — so a same-``pkg_version`` source edit still
    triggers a re-seed."""
    blob = json.dumps(pkg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stamp_seed_provenance(spec: dict, pkg: dict) -> None:
    """Stamp the seed-provenance fields (version + content hash) onto a Spec
    extracted from gallery package ``pkg``. Single home for the stamp so the
    one-shot migration and the deploy-time re-seed produce identical
    provenance."""
    version = (pkg.get("pkg_version") or pkg.get("gallery_version") or "").strip()
    if version:
        spec[SEEDED_FROM_PKG_VERSION_KEY] = version
    spec[SEEDED_FROM_PKG_SHA256_KEY] = _canonical_pkg_hash(pkg)


def _read_json_dict_or_none(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _reseed_decision(
    existing: Optional[dict], repo_version: str, repo_hash: str,
) -> tuple[bool, str]:
    """Decide whether a builtin Spec needs re-seeding from its repo package.

    Returns ``(needs_reseed, reason)``. Missing builtins are intentionally
    LEFT ALONE (a package added after the one-shot migration is minted at
    install time — the same as today); only an *existing* builtin that has
    fallen behind its repo package is regenerated.
    """
    if existing is None:
        # New package, never seeded — out of scope. Install-time mint owns it.
        return False, "no existing builtin (install-time mint owns it)"
    rec_version = (existing.get(SEEDED_FROM_PKG_VERSION_KEY) or "").strip()
    rec_hash = existing.get(SEEDED_FROM_PKG_SHA256_KEY) or ""
    if not rec_version:
        # Pre-this-change builtin (the stranded class #2792 fixed by hand) —
        # re-seed once to stamp provenance and pick up any repo-side drift.
        return True, "builtin predates seed-provenance (legacy)"
    if repo_version:
        cmp_repo = version_sort_key(repo_version)
        cmp_rec = version_sort_key(rec_version)
        if cmp_repo > cmp_rec:
            return True, f"repo pkg_version {repo_version} newer than seeded {rec_version}"
        if cmp_repo < cmp_rec:
            # Deploy checkout is behind the builtin (rare: a rollback or a
            # mid-tick race). Don't downgrade the builtin.
            return False, f"repo pkg_version {repo_version} older than seeded {rec_version}"
    # Equal (or one side unversioned) — re-seed only if the source drifted.
    if repo_hash and rec_hash and repo_hash != rec_hash:
        return True, "same pkg_version, source content changed"
    return False, "up to date"


@dataclass
class GalleryReseedResult:
    """Outcome of a ``reseed_builtin_specs`` pass."""
    reseeded: list[str] = field(default_factory=list)   # spec_ids regenerated
    skipped: list[str] = field(default_factory=list)    # spec_ids already current
    errors: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)   # per-package decision records

    def summary(self) -> str:
        return (
            f"Builtin re-seed: {len(self.reseeded)} updated, "
            f"{len(self.skipped)} current, {len(self.errors)} error(s)"
        )


def reseed_builtin_specs(
    shared_dir: Path,
    *,
    gallery_root: Optional[Path] = None,
    dry_run: bool = False,
) -> GalleryReseedResult:
    """Regenerate builtin Specs whose repo gallery package moved past what the
    builtin was last seeded from.

    The propagation gap this closes: ``migrate_gallery_package`` seeds a
    builtin Spec at ``gallery/builtin/<spec_id>/{INITIAL_SPEC_VERSION}.json``
    once (during the one-shot ``migrate_all``); a gallery install then *binds*
    to that pre-existing builtin and never re-reads the repo package (see
    ``native_write.mint_v7_arc_app`` step 2). So a repo-side gallery edit
    (e.g. #2695's delivery-endpoint migration) never reaches a deployed pod's
    builtin Spec — freshly-forged bots keep inheriting the stale Spec until
    someone manually re-runs the migration. Root cause of the 2026-06-12 U1
    morning-briefing delivery bug; see
    docs/decision-add-bot-m4-u1-proof-2026-06-11.md Resolution note + #2792.

    For each ``gallery/<name>/<pkg_id>.json`` repo package this:
      * resolves the bound builtin Spec
        (``gallery/builtin/<pkg_id>/{INITIAL_SPEC_VERSION}.json``);
      * re-seeds it — by re-running ``migrate_gallery_package``, which
        overwrites the fixed-version builtin file in place — when the builtin
        carries no seed-provenance (a pre-this-change builtin, the stranded
        class), the repo ``pkg_version`` is newer than the recorded one, or
        the recorded version matches but the source content drifted;
      * leaves it untouched otherwise (idempotent steady state).

    NEVER touches ``gallery/local/`` — operator-edited / instance-migrated
    Specs live there and are authoritative; only the builtin tier (the
    repo-seeded tier) is regenerated. A *missing* builtin is left to the
    install-time mint, so this can't change the local-vs-builtin binding for
    packages added after migration.

    Runs as the ``evolve`` user (the repo-puller's identity): the builtin dir
    is evolve-owned, so ``migrate_gallery_package``'s plain
    ``atomic_write_json`` succeeds without sudo. Best-effort and per-package
    isolated — one unreadable package never blocks the rest. No-op when the
    pod has no builtin tier yet (un-migrated pod).
    """
    result = GalleryReseedResult()
    if gallery_root is None:
        # packages/admin/evolve_admin/applications/migrate_v7.py → repo root.
        gallery_root = Path(__file__).resolve().parents[4] / "gallery"
    if not gallery_root.is_dir():
        return result

    builtin_root = shared_dir / "gallery" / "builtin"
    if not builtin_root.is_dir():
        # Un-migrated pod — there is no builtin tier to keep in sync.
        return result

    for pkg_path in sorted(gallery_root.glob("*/*.json")):
        try:
            pkg = json.loads(pkg_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            result.errors.append(f"{pkg_path.name}: unreadable ({e})")
            continue
        if not isinstance(pkg, dict):
            continue
        spec_id = (pkg.get("pkg_id") or "").strip()
        if not _SPEC_ID_RE.match(spec_id):
            # No stable id to map to a builtin Spec (migrate_all would mint a
            # random one — wrong for an idempotent re-seed). Skip.
            continue

        repo_version = (pkg.get("pkg_version") or pkg.get("gallery_version") or "").strip()
        repo_hash = _canonical_pkg_hash(pkg)
        builtin_path = builtin_root / spec_id / f"{INITIAL_SPEC_VERSION}.json"
        existing = _read_json_dict_or_none(builtin_path)

        needs, reason = _reseed_decision(existing, repo_version, repo_hash)
        record = {
            "spec_id": spec_id,
            "source": str(pkg_path),
            "reason": reason,
            "from_version": (existing or {}).get(SEEDED_FROM_PKG_VERSION_KEY),
            "to_version": repo_version,
        }
        if not needs:
            result.skipped.append(spec_id)
            result.details.append({**record, "action": "skip"})
            continue
        if dry_run:
            result.reseeded.append(spec_id)
            result.details.append({**record, "action": "would-reseed"})
            continue

        res = migrate_gallery_package(pkg_path, shared_dir, dry_run=False)
        if res.succeeded:
            result.reseeded.append(spec_id)
            result.details.append({**record, "action": "reseeded"})
        else:
            result.errors.append(f"{spec_id}: {'; '.join(res.errors)}")
            result.details.append({**record, "action": "error"})

    return result


# ── Marker file rewriting ────────────────────────────────────────────────────

def rewrite_markers(
    workspace_root: Path,
    spec_id_map: dict[str, str],
    dry_run: bool = True,
    *,
    warnings: Optional[list[str]] = None,
) -> int:
    """
    Rewrite file markers from v6 `evolve: pkg=p-... file=f-...` to the v7
    `evolve: spec=p-...@<v7-version> file=f-...@<v7-version>` form.

    Walks the workspace tree, parses each file's existing marker via
    provenance.parse_marker, and re-stamps using provenance.embed_marker
    with keyword='spec'. The original pkg_ids carry through unchanged unless
    spec_id_map provides a translation (used only when a fresh spec_id was
    minted during Instance migration — by default the legacy pkg_id IS the
    spec_id, so the map is identity for most files).

    Files already in v7 form (keyword='spec') are skipped. Files without a
    marker are skipped (UNOWNED lifecycle per provenance.py).

    Permission/IO failures are accumulated in the optional `warnings` list
    (or stderr if not provided) and do not abort the walk — best-effort.

    Returns count of files actually rewritten (or, in dry_run mode, the
    count of files that would have been rewritten).
    """
    from .provenance import (
        _SKIP_DIRS,
        _SKIP_EXTS,
        embed_marker,
        parse_marker,
    )

    rewritten = 0
    warn_buf = warnings if warnings is not None else []

    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_EXTS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue

        try:
            marker = parse_marker(path)
        except Exception as e:
            warn_buf.append(f"parse failed for {path}: {e}")
            continue

        if not marker or not marker.is_valid():
            continue
        if marker.keyword == "spec":
            # Already v7 form — skip.
            continue

        # Translate pkg_ids through spec_id_map (identity for preserved legacy IDs).
        new_pkg_ids = [spec_id_map.get(pid, pid) for pid in marker.pkg_ids]
        new_versions = {pid: INITIAL_SPEC_VERSION for pid in new_pkg_ids}

        if dry_run:
            rewritten += 1
            continue

        try:
            embed_marker(
                path,
                pkg_ids=new_pkg_ids,
                file_id=marker.file_id,
                pkg_versions=new_versions,
                file_version=INITIAL_SPEC_VERSION,
                merge=False,  # we have authoritative data; replace cleanly
                keyword="spec",
            )
            rewritten += 1
        except (OSError, PermissionError) as e:
            # Bot-owned files may not be writable by the migration user;
            # accumulate and continue — the operator can re-run as the bot
            # user (or via sudo) once the gap is reported.
            warn_buf.append(f"rewrite failed for {path}: {e}")
            continue

    if warnings is None and warn_buf:
        # No caller buffer; surface to stderr.
        for w in warn_buf:
            print(f"[rewrite_markers] {w}", file=sys.stderr)

    return rewritten


# ── Rollback ─────────────────────────────────────────────────────────────────

@dataclass
class RollbackResult:
    restored: int = 0
    deleted: int = 0
    missing: list[str] = field(default_factory=list)   # backup files referenced but not present
    skipped: list[str] = field(default_factory=list)   # already-restored / already-deleted

    def summary(self) -> str:
        line = f"Rollback: {self.restored} files restored, {self.deleted} files deleted"
        if self.missing:
            line += f", {len(self.missing)} backup files missing"
        if self.skipped:
            line += f", {len(self.skipped)} ops skipped (already in target state)"
        return line


def rollback_migration(
    shared_dir: Path,
    timestamp: str,
    dry_run: bool = True,
) -> RollbackResult:
    """
    Restore the pod from a migration backup created at <timestamp>.

    Reads {shared_dir}/migration_backup/v13_to_v7_arc/<timestamp>/manifest.json
    and reverses each operation in LIFO order:

      - "delete" ops (artifacts the migration created): remove the target file.
      - "restore" ops (v13 sources that the migration unlinked): copy the
        backup content back to the target path.

    Idempotent: re-running rollback after a partial run is safe. Missing
    backup files are recorded but do not abort the run.

    Markers are NOT rolled back here — bot workspaces are git-tracked, so use
    `git checkout` inside the bot's workspace to revert marker rewrites.
    """
    result = RollbackResult()
    backup_dir = (
        shared_dir / "migration_backup" / BACKUP_MANIFEST_VERSION / timestamp
    )
    manifest_path = backup_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"can't read backup manifest at {manifest_path}: {e}") from e

    # Reverse order so the v13 source is restored AFTER the v7 artifacts are
    # deleted (the original is in the same dir as the new instance_id.json —
    # they wouldn't collide by name but deleting v7 first keeps the dir clean
    # at each step).
    for op in reversed(manifest.get("operations", [])):
        action = op["action"]
        target = Path(op["target"])
        if action == "delete":
            if not target.exists():
                result.skipped.append(str(target))
                continue
            if not dry_run:
                target.unlink()
            result.deleted += 1
        elif action == "restore":
            backup_rel = op["backup"]
            backup_path = backup_dir / backup_rel
            if not backup_path.exists():
                result.missing.append(str(backup_path))
                continue
            if target.exists() and target.read_bytes() == backup_path.read_bytes():
                # Already restored.
                result.skipped.append(str(target))
                continue
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, target)
            result.restored += 1
        else:
            # Unknown action; record but don't fail.
            result.skipped.append(f"unknown action {action!r} for {target}")

    return result


# ── Atomic write helper ──────────────────────────────────────────────────────

def _write_json_mkdirs(path: Path, data: dict) -> None:
    """Atomic JSON write into a directory that may not exist yet.

    Migration targets (spec/instance/lessons/gallery dirs) are created on
    first write, so mkdir the parent before delegating to the shared
    atomic writer. mode=0o644: --apply runs as root and bots must be able
    to read their own instance files (a post-migration pass chowns them).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, mode=0o644)


# ── Post-migration ownership fix ─────────────────────────────────────────────

def _chown_paths_to_evolve(
    paths: list[Path],
    *,
    log: Callable[[str], None] = print,
) -> int:
    """Recursively chown each path to ``evolve:wheel`` when running as root.

    Background: ``--apply`` requires running as root (S2.8 — bots' workspace
    manifest dirs have inconsistent ACLs across the pod). Any file the
    migration writes under ``{shared_dir}/`` then lands owned by
    ``root:wheel`` instead of ``evolve:wheel``. The admin server runs as
    ``evolve`` and uses simple ``Path.write_text`` for follow-on writes
    (Lessons compression, share endpoint distillation, etc.), so the
    root-owned files would block those writes with PermissionError.

    We caught this live: after the first --apply on the mini, the share
    endpoint failed with "Permission denied" until we manually
    ``sudo chown -R evolve:wheel /Users/Shared/evolve/gallery/local
    /Users/Shared/evolve/lessons``. This helper makes that step part of
    the migration itself.

    No-op when:
      - not running as root (chown would refuse / hit OSError anyway)
      - ``evolve`` user doesn't exist (e.g. dev laptop running tests)
      - the path doesn't exist (gallery-only run skips lessons/, etc.)

    Errors during the walk are logged at WARN and skipped — partial
    chown is still better than no chown, and a hard failure here would
    mask the migration result.

    Returns the count of paths chowned (for logging).
    """
    import pwd
    import grp

    if os.geteuid() != 0:
        # Sanity: --apply enforces root, but this function may be called
        # from other paths in the future. Make the guard explicit.
        return 0

    try:
        evolve_uid = pwd.getpwnam("evolve").pw_uid
    except KeyError:
        log("WARN: 'evolve' user not found on this host; skipping post-migration chown")
        return 0
    try:
        wheel_gid = grp.getgrnam("wheel").gr_gid
    except KeyError:
        wheel_gid = -1  # -1 leaves group unchanged

    chowned = 0
    for root_path in paths:
        if not root_path.exists():
            continue
        try:
            os.chown(root_path, evolve_uid, wheel_gid)
            chowned += 1
        except OSError as e:
            log(f"WARN: chown {root_path} failed: {e}")
            continue
        if root_path.is_dir():
            for sub in root_path.rglob("*"):
                try:
                    os.chown(sub, evolve_uid, wheel_gid)
                    chowned += 1
                except OSError:
                    # Best-effort. Skip silently inside the walk — one
                    # log line per top-level path is enough.
                    pass
    return chowned


# ── Orchestration ────────────────────────────────────────────────────────────

def migrate_all(
    shared_dir: Path,
    bot_ids: list[str],
    gallery_only: bool = False,
    bot_only: bool = False,
    dry_run: bool = True,
) -> AggregateResult:
    """
    Run the full migration. Order:
      1. Gallery (so Spec files exist before Instances reference them).
      2. Per-bot Instances.
      3. Marker rewriting across all bot workspaces.

    Each step is independent; partial failures don't block the others (warnings
    accumulate on the result). When dry_run is False, a BackupRun is initialized
    so the destructive instance migrations can be rolled back via
    rollback_migration().
    """
    agg = AggregateResult()
    backup: Optional[BackupRun] = None
    if not dry_run:
        backup = BackupRun.create(shared_dir)
        agg.backup_timestamp = backup.timestamp
        print(f"Backups will be written to: {backup.backup_dir}", file=sys.stderr)

    if not bot_only:
        gallery_root = Path(__file__).resolve().parent.parent.parent.parent.parent / "gallery"
        for pkg_path in gallery_root.glob("*/*.json"):
            res = migrate_gallery_package(
                pkg_path, shared_dir, dry_run=dry_run, backup=backup
            )
            agg.gallery_results.append(res)
            if res.spec_id:
                agg.spec_id_map[pkg_path.stem] = res.spec_id

    if not gallery_only:
        for bot_id in bot_ids:
            manifest_dir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
            if not manifest_dir.is_dir():
                continue
            for mf in manifest_dir.glob("*.json"):
                # Skip non-manifest files that share the directory:
                #   .scan-status.json — scanner's per-bot state (re-init on next scan)
                #   _history/...      — archived manifests (the dir itself isn't matched
                #                       by *.json but be defensive in case a stray *.json
                #                       lands there)
                # Matches the filter in list_manifests (applications/manifest.py:820).
                if mf.name.startswith(".") or mf.name.startswith("_"):
                    continue
                res = migrate_instance(
                    mf, shared_dir, bot_id, dry_run=dry_run, backup=backup
                )
                agg.instance_results.append(res)
                if res.spec_id and mf.stem:
                    agg.spec_id_map[mf.stem] = res.spec_id

        # Marker rewrite — only if we touched at least one Instance
        if agg.instance_results:
            for bot_id in bot_ids:
                ws = bot_home(bot_id) / ".openclaw" / "workspace"
                if ws.is_dir():
                    agg.marker_files_rewritten += rewrite_markers(
                        ws,
                        agg.spec_id_map,
                        dry_run=dry_run,
                        warnings=agg.marker_warnings,
                    )

    return agg


# ── CLI ──────────────────────────────────────────────────────────────────────

def _list_bots_from_network(shared_dir: Path) -> list[str]:
    """
    Read bot IDs from network.json per the explicit-pod-membership convention.

    The `bots` field may be either a dict keyed by bot_id (current pod shape,
    e.g. {"team_bot_a": {...}, "evolve": {...}, ...}) or a list of {id: ...} dicts
    (older shape). Both are accepted.
    """
    try:
        network = json.loads((shared_dir / "network.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    bots = network.get("bots", {})
    if isinstance(bots, dict):
        return list(bots.keys())
    if isinstance(bots, list):
        return [b["id"] for b in bots if isinstance(b, dict) and "id" in b]
    return []


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="v13 → v7-arc manifest migration")
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--bot-id",
        action="append",
        default=[],
        help="Migrate only this bot (repeatable). Default: all bots in network.json.",
    )
    p.add_argument("--gallery-only", action="store_true", help="Skip per-bot migration")
    p.add_argument("--bot-only", action="store_true", help="Skip gallery migration")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write files. Without this flag, runs as --dry-run.",
    )
    p.add_argument(
        "--rollback",
        metavar="TIMESTAMP",
        help="Restore from a previous --apply run (backup_timestamp). "
             "Reverses created artifacts and restores unlinked v13 manifests. "
             "Combine with --apply to actually perform the restore; otherwise dry-run.",
    )
    p.add_argument(
        "--reseed-builtins",
        action="store_true",
        help="Re-seed builtin Specs from repo gallery packages whose pkg_version "
             "moved past the bound builtin (the deploy-time propagation healer the "
             "repo-puller runs each tick). Runs as evolve; combine with --apply to "
             "write, otherwise dry-run.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    dry_run = not args.apply

    # ── Builtin re-seed mode ──
    # Runs as the evolve user (writes only the evolve-owned builtin tier), so
    # it sits BEFORE the root check below — re-seed must never demand root.
    if args.reseed_builtins:
        if dry_run:
            print("RESEED DRY RUN — no files will be written. Re-run with --apply.")
        rs = reseed_builtin_specs(shared_dir, dry_run=dry_run)
        print(rs.summary())
        for d in rs.details:
            if d["action"] in ("reseeded", "would-reseed", "error"):
                print(f"  {d['action']}: {d['spec_id']} — {d['reason']}")
        for e in rs.errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1 if rs.errors else 0

    # ── Root check for destructive ops ──
    # Bot manifests/ dirs have inconsistent ACLs across the pod — some bots
    # grant evolve write, some grant only read. Root bypasses ACL entirely
    # and is the right privilege for a one-shot operator action.
    if args.apply and os.geteuid() != 0:
        print(
            "ERROR: --apply requires running as root.\n"
            "       Some bots' .openclaw/workspace/manifests/ dirs are not\n"
            "       writable by the evolve user. Root bypasses ACL entirely.\n"
            "\n"
            "       Correct invocation:\n"
            "           sudo PYTHONPATH=/Users/Shared/evolve-repo/packages/admin \\\n"
            "               python3 -m evolve_admin.applications.migrate_v7 --apply\n"
            "\n"
            "       (NOT: sudo -u evolve python3 -m evolve_admin.applications.migrate_v7 --apply)\n",
            file=sys.stderr,
        )
        return 1

    # ── Rollback mode ──
    if args.rollback:
        if dry_run:
            print(f"ROLLBACK DRY RUN — no files will be changed. Re-run with --apply to perform.")
        else:
            print(f"ROLLBACK from {args.rollback} — restoring...")
        try:
            res = rollback_migration(shared_dir, args.rollback, dry_run=dry_run)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(res.summary())
        if res.missing:
            print(f"\nMissing backup files ({len(res.missing)}):")
            for m in res.missing[:10]:
                print(f"  {m}")
        return 0

    # ── Migration mode ──
    bot_ids = args.bot_id or _list_bots_from_network(shared_dir)
    if dry_run:
        print("DRY RUN — no files will be written. Re-run with --apply.")

    agg = migrate_all(
        shared_dir=shared_dir,
        bot_ids=bot_ids,
        gallery_only=args.gallery_only,
        bot_only=args.bot_only,
        dry_run=dry_run,
    )

    print(agg.summary())
    if agg.backup_timestamp:
        print(f"Backup timestamp: {agg.backup_timestamp}")
        print(f"To rollback:  python3 -m evolve_admin.applications.migrate_v7 --rollback {agg.backup_timestamp} --apply")

    # ── Post-migration: chown shared_dir back to evolve ──
    # --apply runs as root, so any new files/dirs under {shared_dir}/ land
    # owned by root:wheel. The admin server (which runs as evolve) needs
    # write access for share, Lessons compression, and Reflect writes —
    # without this the share endpoint silently 500s on first use. No-op on
    # dry-run and on hosts without an evolve user (e.g. dev laptop).
    if not dry_run:
        chowned = _chown_paths_to_evolve([
            shared_dir / "gallery" / "local",
            shared_dir / "gallery" / "builtin",
            shared_dir / "lessons",
            shared_dir / "migration_backup",
        ])
        if chowned:
            print(f"Restored ownership on {chowned} path(s) to evolve:wheel")
    print()

    # Surface warnings + errors
    for res in agg.instance_results + agg.gallery_results:
        if res.warnings or res.errors:
            print(f"\n{res.source_path}:")
            for w in res.warnings:
                print(f"  WARN: {w}")
            for e in res.errors:
                print(f"  ERROR: {e}")

    if agg.marker_warnings:
        print(f"\nMarker rewrite warnings ({len(agg.marker_warnings)}):")
        for w in agg.marker_warnings[:20]:
            print(f"  {w}")
        if len(agg.marker_warnings) > 20:
            print(f"  ... and {len(agg.marker_warnings) - 20} more")

    any_errors = any(r.errors for r in agg.instance_results + agg.gallery_results)
    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(main())

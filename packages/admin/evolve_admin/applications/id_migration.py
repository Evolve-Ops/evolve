"""id_migration — the one-shot ``app_id`` census + backfill (AL-1.4a).

docs/build-AL-1.4-app-id-canonical.md §2. Walks every identity-bearing
artifact on the pod — per-bot manifests (legacy single-file and v7-arc
Instances) and gallery Specs across all tiers — and reports, or writes, the
canonical ``app_id`` each one is entitled to.

WHY THE TABLE EXISTS. Nothing consumes ``{shared_dir}/apps/id-migration.json``
in 1.4a; it exists so 1.4c can drop the legacy fallback *safely*, having first
proved what the fallback is still carrying. In 1.4a the ``map`` is an IDENTITY
map by construction — ``app_id`` is stamped as the id the legacy chain already
resolves to, because anything else would re-identify apps whose ids are
already written into filenames, workspace markers, cron labels and the
plugin's app-integrity coverage keys. The value of this run is therefore the
CENSUS in ``entries``:

  * ``stamped`` / ``already``  — has (or just got) a conforming ``app_id``.
  * ``non_conforming``         — its legacy id is not a valid slug, so no
                                 ``app_id`` was written. 1.4c cannot drop the
                                 fallback while any of these exist; each needs
                                 an operator-conferred id.
  * ``no_id``                  — no identity field at all (a malformed
                                 artifact; reported, never repaired here).
  * ``draft``                  — a scanner draft holding a ``draft_id``.
                                 Correctly has no ``app_id`` (design §3).

Each Instance entry also records the ``spec_id`` it is bound to. That is what
AL-1.5/1.4c needs to collapse instance identity onto spec identity (design §3:
an instance is the pair ``(app_id, bot_id)`` where ``app_id`` is the spec's) —
a rewrite 1.4a deliberately does NOT perform, because it would change what
every resolver returns for every v7-arc instance on the pod.

Idempotent: a second ``--apply`` stamps nothing and reports the same census.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). This module is the MIGRATION TABLE generator, so
# naming the legacy fields is its subject matter, not a stray read. ``spec_id``
# is a first-class column of every Instance row on purpose (module docstring):
# it records the Spec each Instance is bound to, which is what lets AL-1.5/1.4c
# collapse instance identity onto the Spec's app_id from data rather than by
# guesswork.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from evolve_util import atomic_write_json, now_iso

from .app_identity import (
    APP_ID_FIELD,
    canonical_app_id,
    draft_id_of,
    resolve_legacy_app_id,
)
from .manifest import applications_dir

MIGRATION_TABLE_VERSION = 1

KIND_MANIFEST = "manifest"      # legacy single-file manifest
KIND_INSTANCE = "instance"      # v7-arc Instance
KIND_SPEC = "spec"              # gallery App Spec

STATUS_ALREADY = "already"
STATUS_STAMPED = "stamped"
STATUS_NON_CONFORMING = "non_conforming"
STATUS_NO_ID = "no_id"
STATUS_DRAFT = "draft"

# Statuses that leave the artifact without an app_id — i.e. the legacy
# fallback is still load-bearing for it and 1.4c cannot drop it yet.
BLOCKING_STATUSES = (STATUS_NON_CONFORMING, STATUS_NO_ID)


def migration_table_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "apps" / "id-migration.json"


@dataclass
class Entry:
    path: str
    kind: str
    bot_id: str
    legacy_id: str
    app_id: str
    status: str
    spec_id: str = ""
    draft_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "bot_id": self.bot_id,
            "legacy_id": self.legacy_id,
            "app_id": self.app_id,
            "status": self.status,
            # identity: see resolve_app_id — a migration-table COLUMN: the Spec each Instance is bound to, recorded so 1.4c/AL-1.5 can collapse instance identity from data.
            "spec_id": self.spec_id,
            "draft_id": self.draft_id,
        }


@dataclass
class MigrationReport:
    entries: list[Entry] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    table_path: str = ""
    dry_run: bool = True

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out

    @property
    def mapping(self) -> dict[str, str]:
        """``{legacy_id: app_id}`` — the table the brief specifies."""
        return {
            e.legacy_id: e.app_id
            for e in self.entries
            if e.legacy_id and e.app_id
        }

    @property
    def blocking(self) -> list[Entry]:
        return [e for e in self.entries if e.status in BLOCKING_STATUSES]


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iter_manifest_paths(shared_dir: Path, bot_id: str) -> Iterator[Path]:
    """Per-bot manifest files, skipping scanner state and the archive dir."""
    caps_dir = applications_dir(Path(shared_dir), bot_id)
    if not caps_dir.is_dir():
        return
    for p in sorted(caps_dir.glob("*.json")):
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        yield p


def _iter_spec_paths(shared_dir: Path) -> Iterator[Path]:
    """Gallery Spec files across every tier.

    Mirrors the tiers ``native_write.find_existing_spec`` searches:
    ``gallery/local/<spec_id>/<version>.json``,
    ``gallery/builtin/<spec_id>/<version>.json`` and
    ``gallery/imported/<pod>/<spec_id>/<version>.json``. The flat legacy
    ``gallery/imported/<pkg_id>.json`` files are gallery *packages*, not Specs,
    and are deliberately skipped.
    """
    gallery = Path(shared_dir) / "gallery"
    for tier in ("local", "builtin"):
        root = gallery / tier
        if root.is_dir():
            for spec_dir in sorted(root.iterdir()):
                if spec_dir.is_dir():
                    yield from sorted(spec_dir.glob("*.json"))
    imported = gallery / "imported"
    if imported.is_dir():
        for pod_dir in sorted(imported.iterdir()):
            if not pod_dir.is_dir():
                continue
            for spec_dir in sorted(pod_dir.iterdir()):
                if spec_dir.is_dir():
                    yield from sorted(spec_dir.glob("*.json"))


def _classify(path: Path, data: dict, kind: str, bot_id: str) -> Entry:
    prov = data.get("provenance")
    spec_id = ""
    # identity: see resolve_app_id — this classifier reads provenance.spec_id (the Spec BINDING) to decide an artifact's KIND, not its id.
    if isinstance(prov, dict) and isinstance(prov.get("spec_id"), str):
        spec_id = prov["spec_id"].strip()
    elif kind == KIND_SPEC and isinstance(data.get("spec_id"), str):
        spec_id = data["spec_id"].strip()

    entry = Entry(
        path=str(path), kind=kind, bot_id=bot_id,
        legacy_id=resolve_legacy_app_id(data), app_id="",
        status=STATUS_NO_ID, spec_id=spec_id, draft_id=draft_id_of(data),
    )

    existing = data.get(APP_ID_FIELD)
    if isinstance(existing, str) and existing.strip():
        entry.app_id = existing.strip()
        entry.status = STATUS_ALREADY
        return entry
    if entry.draft_id:
        # The mint declined to confer identity on this one (design §3). A
        # draft still carries a legacy ``id`` — it is the filename stem — so
        # this check must come BEFORE the legacy resolution below, or the
        # census would "helpfully" stamp every draft on the pod.
        entry.status = STATUS_DRAFT
        return entry
    if not entry.legacy_id:
        entry.status = STATUS_NO_ID
        return entry
    proposed = canonical_app_id(entry.legacy_id, context=str(path))
    if proposed:
        entry.app_id = proposed
        entry.status = STATUS_STAMPED
    else:
        entry.status = STATUS_NON_CONFORMING
    return entry


def _shape_of(data: dict) -> str:
    # identity: see resolve_app_id — a v7-arc SHAPE probe: instance_id's presence is the discriminator, not the id it holds.
    if data.get("manifest_shape") == "v7-arc" or data.get("instance_id"):
        return KIND_INSTANCE
    return KIND_MANIFEST


def build_report(
    shared_dir: Path, bot_ids: list[str], *, apply: bool = False,
) -> MigrationReport:
    """Census every identity-bearing artifact; optionally stamp ``app_id``.

    ``apply=False`` (the default) touches nothing. ``apply=True`` writes the
    stamp into artifacts whose status is ``stamped`` and writes the table.
    """
    shared = Path(shared_dir)
    report = MigrationReport(dry_run=not apply)

    targets: list[tuple[Path, str]] = [(p, bot) for bot in bot_ids
                                       for p in _iter_manifest_paths(shared, bot)]
    targets += [(p, "") for p in _iter_spec_paths(shared)]

    for path, bot_id in targets:
        data = _read_json_dict(path)
        if data is None:
            report.errors.append(f"unreadable or non-object JSON: {path}")
            continue
        kind = KIND_SPEC if not bot_id else _shape_of(data)
        entry = _classify(path, data, kind, bot_id)
        report.entries.append(entry)
        if apply and entry.status == STATUS_STAMPED:
            data[APP_ID_FIELD] = entry.app_id
            try:
                _write_artifact(path, data)
                report.written.append(str(path))
            except OSError as exc:
                report.errors.append(f"write failed for {path}: {exc}")
                entry.status = STATUS_NON_CONFORMING

    if apply:
        table_path = migration_table_path(shared)
        try:
            table_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(table_path, _table(report), mode=0o644)
            report.table_path = str(table_path)
        except OSError as exc:
            report.errors.append(f"table write failed for {table_path}: {exc}")
    else:
        report.table_path = str(migration_table_path(shared))
    return report


def _write_artifact(path: Path, data: dict) -> None:
    """Write a stamped artifact back, via the manifest write path.

    Manifests can live in a bot-owned tree the ``evolve`` user cannot write
    directly on a pre-first-scan bot, so this routes through
    ``manifest._write_manifest_bytes`` (direct write, then /tmp staging +
    ``sudo /bin/cp``) rather than ``Path.write_text``.
    """
    from .manifest import _write_manifest_bytes
    _write_manifest_bytes(path, json.dumps(data, indent=2).encode("utf-8"))


def _table(report: MigrationReport) -> dict[str, Any]:
    return {
        "version": MIGRATION_TABLE_VERSION,
        "generated_at": now_iso(),
        "generated_by": "evolve-admin application migrate-ids",
        "note": (
            "AL-1.4a census. 'map' is {legacy_id: app_id} and is an identity "
            "map by construction in 1.4a — the stamp writes the id the legacy "
            "chain already resolved to. The census in 'entries' is the point: "
            "'non_conforming' and 'no_id' rows are what still needs the legacy "
            "fallback 1.4c wants to remove, and each instance row carries the "
            "spec_id AL-1.5/1.4c will collapse its identity onto."
        ),
        "map": report.mapping,
        "entries": [e.to_dict() for e in report.entries],
    }

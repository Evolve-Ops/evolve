"""Self-healing of a corrupt ``manifest_shape`` discriminator.

Regression for the 2026-06-11 ledger incident: ``task-manager.json`` carried
``manifest_shape: "v20"`` — a schema-version string that leaked into the shape
discriminator (the manifest's ``schema_version`` was 20 at write time). A
non-canonical value orphans the manifest from BOTH:

  * the legacy→v7-arc promotion path (it only promotes ``manifest_shape == ""``), and
  * every v7/Reflect reader (reflect.py, pass_runner, audit, spec_drift, adopt,
    extend_application — all branch on ``manifest_shape == "v7-arc"``).

``migrate_manifest`` runs on every ``list_manifests`` / ``load_manifest`` read, so
normalizing there self-heals the whole fleet on the next scan. ``manifest_shape``
is a closed enum (``VALID_MANIFEST_SHAPES``); anything outside it is coerced back
to a canonical value inferred from structure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SHAPE_LEGACY,
    MANIFEST_SHAPE_V7_ARC,
    MANIFEST_SHAPE_V7_ARC_PRE,
    VALID_MANIFEST_SHAPES,
    _normalize_manifest_shape,
    migrate_manifest,
)


# ── Pure-helper unit coverage ────────────────────────────────────────────────

def test_canonical_values_pass_through_untouched():
    for shape in ("", "v7-arc", "v7-arc-pre"):
        data = {"manifest_shape": shape, "files": [{"path": "x"}]}
        assert _normalize_manifest_shape(data) is False
        assert data["manifest_shape"] == shape


def test_v20_on_legacy_structure_coerces_to_legacy():
    # Mirrors the real ledger task-manager.json: inline files, top-level id,
    # no instance_id / realized_files.
    data = {
        "id": "task-manager",
        "name": "Task Manager",
        "manifest_shape": "v20",
        "schema_version": 24,
        "files": [{"path": "scripts/tasks.py"}],
    }
    assert _normalize_manifest_shape(data) is True
    assert data["manifest_shape"] == MANIFEST_SHAPE_LEGACY


def test_v20_on_v7_arc_structure_coerces_to_v7_arc():
    # A corrupt value on a real Instance (carries instance_id / realized_files)
    # must heal to "v7-arc", not legacy — otherwise the Instance stays orphaned.
    for marker in ("instance_id", "realized_files"):
        data = {"manifest_shape": "v20", marker: "task-manager" if marker == "instance_id" else [{"path": "x"}]}
        assert _normalize_manifest_shape(data) is True
        assert data["manifest_shape"] == MANIFEST_SHAPE_V7_ARC


def test_null_shape_coerces_to_legacy():
    data = {"id": "x", "manifest_shape": None, "files": []}
    assert _normalize_manifest_shape(data) is True
    assert data["manifest_shape"] == MANIFEST_SHAPE_LEGACY


def test_normalize_is_idempotent():
    data = {"id": "x", "manifest_shape": "v20", "files": []}
    assert _normalize_manifest_shape(data) is True
    # Second pass: now canonical, no further change.
    assert _normalize_manifest_shape(data) is False


def test_v7_arc_pre_is_in_valid_set():
    assert MANIFEST_SHAPE_V7_ARC_PRE == "v7-arc-pre"
    assert VALID_MANIFEST_SHAPES == {"", "v7-arc", "v7-arc-pre"}


# ── End-to-end through migrate_manifest (the universal read-time pass) ────────

def test_migrate_manifest_repairs_v20_on_disk(tmp_path):
    path = tmp_path / "task-manager.json"
    path.write_text(
        json.dumps(
            {
                "id": "task-manager",
                "name": "Task Manager",
                "manifest_shape": "v20",
                "schema_version": 20,
                "files": [{"path": "scripts/tasks.py"}],
            }
        )
    )
    migrate_manifest(path)
    healed = json.loads(path.read_text())
    assert healed["manifest_shape"] == MANIFEST_SHAPE_LEGACY
    # Schema bump still happens; the rest of the migration is unaffected.
    assert healed["schema_version"] >= 24

    # Idempotent: a second migrate leaves the (now canonical) shape alone.
    before = path.read_text()
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["manifest_shape"] == MANIFEST_SHAPE_LEGACY
    assert json.loads(before)["manifest_shape"] == MANIFEST_SHAPE_LEGACY

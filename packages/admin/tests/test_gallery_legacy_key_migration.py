"""Gallery LEGACY_KEY_MIGRATION — retired pkg_id → surviving pkg_id resolution.

Mirrors the pattern in ``alerts.catalog.LEGACY_KEY_MIGRATION``. When a
gallery package is merged into another, the retired pkg_id stops shipping
a manifest, but existing installs still carry it. ``load_gallery_package``
resolves the retired id transparently so update-detection, snapshot
retrieval, and the install dispatcher all land on the merged spec.

The first live entry — and the one this test exercises end-to-end against
the real builtin gallery — is the 2026-06-05 merger of the standalone
Unified Task System (``p-c20a5564``) into Task Manager (``p-9bfa1c84``).
Rationale: internal/gallery-merge-task-manager-2026-06-05.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.gallery import (  # noqa: E402
    LEGACY_KEY_MIGRATION,
    load_gallery_package,
)


# ── The 2026-06-05 task-manager merge — exercised against real gallery ─────


def test_unified_task_system_resolves_to_task_manager(tmp_path: Path):
    """Looking up the retired Unified Task System pkg_id returns the
    merged Task Manager spec from the real builtin gallery."""
    pkg = load_gallery_package("p-c20a5564", tmp_path)
    assert pkg is not None, (
        "p-c20a5564 must resolve to the merged Task Manager package; "
        "if this fails, either LEGACY_KEY_MIGRATION lost its entry or "
        "the surviving p-9bfa1c84 manifest is missing from the gallery."
    )
    # The retired id resolves to the SURVIVING package — its own
    # pkg_id is the surviving one, not the retired one.
    assert pkg["pkg_id"] == "p-9bfa1c84"
    assert pkg["display_name"] == "Task Manager"


def test_task_manager_still_resolves_to_itself(tmp_path: Path):
    """The surviving pkg_id continues to resolve identically — the
    migration map MUST NOT shadow the surviving id."""
    pkg = load_gallery_package("p-9bfa1c84", tmp_path)
    assert pkg is not None
    assert pkg["pkg_id"] == "p-9bfa1c84"
    assert pkg["display_name"] == "Task Manager"


def test_migration_map_contains_unified_task_system_entry():
    """Direct assertion on the map so a future edit that drops the
    entry by accident fails loudly."""
    assert LEGACY_KEY_MIGRATION.get("p-c20a5564") == "p-9bfa1c84"


def test_migration_map_targets_must_resolve(tmp_path: Path):
    """Every value in the map must point at a real gallery package —
    a retired id that resolves to a missing package strands installs."""
    for retired, surviving in LEGACY_KEY_MIGRATION.items():
        # We look the surviving id up directly (bypassing the map) to
        # confirm the manifest is actually present.
        pkg = load_gallery_package(surviving, tmp_path)
        assert pkg is not None, (
            f"LEGACY_KEY_MIGRATION points {retired!r} → {surviving!r}, "
            f"but {surviving!r} does not resolve to a package."
        )
        assert pkg["pkg_id"] == surviving


# ── Synthetic / isolated checks — independent of the live gallery ──────────


def test_lookup_returns_none_for_unknown_pkg(tmp_path: Path):
    """Sanity check: an unknown pkg_id returns None (and does NOT raise)
    even when the migration map is non-empty."""
    pkg = load_gallery_package("p-deadbeef", tmp_path)
    assert pkg is None


def test_migration_resolves_through_imported_gallery(tmp_path: Path):
    """If the surviving pkg lives in the imported gallery (not the
    builtin), migration still resolves through to it.

    Exercised via a temporary entry: write a fake imported package,
    monkey-patch the migration map to point a fake retired id at it,
    and confirm load_gallery_package finds it.
    """
    imported = tmp_path / "gallery" / "imported"
    imported.mkdir(parents=True)
    (imported / "p-aaaaaaaa.json").write_text(json.dumps({
        "pkg_id": "p-aaaaaaaa",
        "name": "fake",
        "display_name": "Fake Merged",
        "objective": "test",
        "build_spec": "...",
    }))
    # Use pytest's monkeypatching idiom directly via dict manipulation
    # to avoid coupling to a monkeypatch fixture; restore in finally.
    original = LEGACY_KEY_MIGRATION.get("p-bbbbbbbb")
    LEGACY_KEY_MIGRATION["p-bbbbbbbb"] = "p-aaaaaaaa"
    try:
        pkg = load_gallery_package("p-bbbbbbbb", tmp_path)
        assert pkg is not None
        assert pkg["pkg_id"] == "p-aaaaaaaa"
        assert pkg["display_name"] == "Fake Merged"
    finally:
        if original is None:
            LEGACY_KEY_MIGRATION.pop("p-bbbbbbbb", None)
        else:
            LEGACY_KEY_MIGRATION["p-bbbbbbbb"] = original


def test_migration_no_double_hop():
    """The map is single-hop only — chaining A→B→C is NOT supported.
    This test exists so a future commit that tries to add chained
    entries trips a deliberate guard instead of silently producing
    surprising resolution.

    The check: no value in the map appears as a key in the map.
    """
    values = set(LEGACY_KEY_MIGRATION.values())
    keys = set(LEGACY_KEY_MIGRATION.keys())
    overlap = values & keys
    assert not overlap, (
        f"LEGACY_KEY_MIGRATION must be single-hop. Found values that "
        f"are also keys: {sorted(overlap)}. Collapse the chain to "
        f"direct mappings (retired_a → final_surviving, "
        f"retired_b → final_surviving)."
    )

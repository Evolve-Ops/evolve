"""F-P.12.a — gallery provenance (trust axis) tests.

Distinct from the smart-forge per-file provenance (which lives on
manifest.files[]). This provenance is at the gallery-PACKAGE level
and tracks who the package came from for trust-decision purposes
(install confirmation gates, UI badges, etc.).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.gallery import (  # noqa: E402
    GALLERY_PROVENANCE_COMMUNITY,
    GALLERY_PROVENANCE_EVOLVE,
    GALLERY_PROVENANCE_OPERATOR_LOCAL,
    list_gallery_packages,
    resolve_gallery_provenance,
)


# ── resolve_gallery_provenance — pure helper ────────────────────────────────


def test_builtin_default_is_evolve():
    """Built-in gallery packages (in-repo) default to evolve trust."""
    pkg = {"pkg_id": "p-aaaaaaaa"}
    assert resolve_gallery_provenance(pkg, is_builtin=True) == GALLERY_PROVENANCE_EVOLVE


def test_imported_default_is_community():
    """Imported packages default to community trust — they came from
    somewhere external (operator-pasted, gallery-import, etc.)."""
    pkg = {"pkg_id": "p-aaaaaaaa"}
    assert resolve_gallery_provenance(pkg, is_builtin=False) == GALLERY_PROVENANCE_COMMUNITY


def test_explicit_evolve_override_on_imported():
    """Operator can override an imported package to evolve trust —
    useful when vendoring a community package into the repo without
    moving it to the builtin gallery dir."""
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": "evolve"}
    assert resolve_gallery_provenance(pkg, is_builtin=False) == GALLERY_PROVENANCE_EVOLVE


def test_explicit_community_override_on_builtin():
    """And the reverse: a community contribution vendored into the
    builtin gallery dir but still surfaces with community trust."""
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": "community"}
    assert resolve_gallery_provenance(pkg, is_builtin=True) == GALLERY_PROVENANCE_COMMUNITY


def test_explicit_operator_local_override():
    """Operator-local is reserved for explicit declarations only —
    no auto path to it today. Verify it sticks when set."""
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": "operator-local"}
    assert resolve_gallery_provenance(pkg, is_builtin=False) == GALLERY_PROVENANCE_OPERATOR_LOCAL


def test_unknown_provenance_value_ignored():
    """Garbage in `provenance` falls back to the storage-location
    default. Keeps the trust gate from silently failing open on a typo."""
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": "trustworthy"}
    assert resolve_gallery_provenance(pkg, is_builtin=False) == GALLERY_PROVENANCE_COMMUNITY


def test_empty_provenance_falls_back():
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": ""}
    assert resolve_gallery_provenance(pkg, is_builtin=True) == GALLERY_PROVENANCE_EVOLVE
    pkg = {"pkg_id": "p-aaaaaaaa", "provenance": "   "}
    assert resolve_gallery_provenance(pkg, is_builtin=False) == GALLERY_PROVENANCE_COMMUNITY


# ── list_gallery_packages — provenance flows through to records ─────────────


@pytest.fixture
def fake_shared_dir(tmp_path: Path):
    """Wire up an imported-gallery dir with one package so we can
    exercise the list_gallery_packages code path."""
    imported = tmp_path / "gallery" / "imported"
    imported.mkdir(parents=True)
    (imported / "p-11111111.json").write_text(json.dumps({
        "pkg_id": "p-11111111",
        "name": "imported-app",
        "display_name": "Imported App",
        "objective": "test",
        "build_spec": "...",
    }))
    return tmp_path


def test_list_packages_attaches_provenance_to_imported_records(fake_shared_dir):
    pkgs = list_gallery_packages(fake_shared_dir, bot_ids=[])
    # We may also pick up builtins from the real repo — filter to imported.
    imported = [p for p in pkgs if p.get("source") == "imported"]
    assert any(p["pkg_id"] == "p-11111111" for p in imported)
    target = next(p for p in imported if p["pkg_id"] == "p-11111111")
    assert target["provenance"] == GALLERY_PROVENANCE_COMMUNITY


def test_list_packages_imported_with_explicit_operator_local(
    fake_shared_dir,
):
    """Operator stamps a package they imported themselves as
    operator-local — list_gallery_packages honors it."""
    # Add a second package with explicit operator-local marking.
    imported = fake_shared_dir / "gallery" / "imported"
    (imported / "p-22222222.json").write_text(json.dumps({
        "pkg_id": "p-22222222",
        "name": "self-imported",
        "display_name": "Self Imported",
        "objective": "test",
        "build_spec": "...",
        "provenance": "operator-local",
    }))
    pkgs = list_gallery_packages(fake_shared_dir, bot_ids=[])
    target = next(p for p in pkgs if p["pkg_id"] == "p-22222222")
    assert target["provenance"] == GALLERY_PROVENANCE_OPERATOR_LOCAL
    # source stays as the storage indicator.
    assert target["source"] == "imported"


def test_list_packages_builtin_provenance_defaults_to_evolve(fake_shared_dir):
    """Real-repo built-in gallery records get provenance=evolve."""
    pkgs = list_gallery_packages(fake_shared_dir, bot_ids=[])
    builtin = [p for p in pkgs if p.get("source") == "builtin"]
    # The real gallery has at least one builtin record (task-manager, etc.)
    if builtin:
        for p in builtin:
            assert p["provenance"] == GALLERY_PROVENANCE_EVOLVE

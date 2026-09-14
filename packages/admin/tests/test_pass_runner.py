"""Pass runner tests — v7-arc hydration + multi-pass dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.pass_runner import (  # noqa: E402
    hydrate_if_needed,
    is_v7_arc_instance,
    run_all_pure_passes,
)


# ── is_v7_arc_instance ──────────────────────────────────────────────

def test_v13_manifest_is_not_v7_arc() -> None:
    assert is_v7_arc_instance({"id": "x", "schema_version": 20}) is False


def test_manifest_with_v7_arc_shape_detected() -> None:
    assert is_v7_arc_instance({"manifest_shape": "v7-arc"}) is True


def test_none_is_not_v7_arc() -> None:
    assert is_v7_arc_instance(None) is False   # type: ignore[arg-type]


def test_non_dict_is_not_v7_arc() -> None:
    assert is_v7_arc_instance("string") is False   # type: ignore[arg-type]


# ── hydrate_if_needed ───────────────────────────────────────────────

def test_non_v7_arc_passes_through_unchanged() -> None:
    manifest = {"id": "x", "schema_version": 20, "files": []}
    out = hydrate_if_needed(manifest, Path("/nowhere"))
    assert out is manifest   # same object — no copy


def test_v7_arc_without_shared_dir_passes_through() -> None:
    """Defensive: when caller doesn't provide shared_dir, return as-is
    rather than crash. Better than NoneType errors in prod."""
    manifest = {"manifest_shape": "v7-arc", "id": "i-abc"}
    out = hydrate_if_needed(manifest, None)
    assert out is manifest


def test_v7_arc_hydrates_when_spec_available(tmp_path: Path) -> None:
    """When the Spec is on disk under shared_dir/gallery/, hydration
    overlays its presentation fields onto the instance."""
    # Build a Spec under gallery/local/<spec_id>/<version>.json.
    spec_id = "my-spec"
    spec_version = "1.0.0"
    gallery = tmp_path / "gallery" / "local" / spec_id
    gallery.mkdir(parents=True)
    spec = {
        "id": spec_id, "version": spec_version,
        "name": "Spec Name",
        "description": "Spec-side description",
        "tags": ["test", "spec"],
        "objective": "spec objective",
        "success_criteria": {"observable_outcomes": ["did the thing"]},
    }
    (gallery / f"{spec_version}.json").write_text(json.dumps(spec))

    instance = {
        "manifest_shape": "v7-arc",
        "id": "i-12345",
        "provenance": {"spec_id": spec_id, "spec_version": spec_version},
        "files": [{"path": "scripts/main.py", "layer": "code"}],
    }
    hydrated = hydrate_if_needed(instance, tmp_path)
    # Presentation fields overlaid from Spec.
    assert hydrated.get("description") == "Spec-side description"
    # Instance identity preserved.
    assert hydrated.get("id") == "i-12345"
    # Instance's own files[] preserved.
    assert len(hydrated.get("files", [])) >= 1


def test_v7_arc_without_provenance_returns_unchanged() -> None:
    instance = {"manifest_shape": "v7-arc", "id": "i-x"}
    out = hydrate_if_needed(instance, Path("/anywhere"))
    # No provenance.spec_id → hydrate_v7_arc_instance returns instance
    # unchanged per its docstring.
    assert out == instance


# ── run_all_pure_passes ─────────────────────────────────────────────

def test_run_all_pure_passes_returns_three_keys(tmp_path: Path) -> None:
    manifest = {
        "id": "x", "schema_version": 20,
        "description": "A test app.",
        "files": [],
    }
    out = run_all_pure_passes(manifest)
    assert set(out.keys()) == {"pass_a", "pass_c1", "orphan"}


def test_run_all_pure_passes_skips_c1_without_workspace_root() -> None:
    manifest = {
        "id": "x", "schema_version": 20,
        "scheduled_actions": [{
            "id": "a", "summary": "summarizes things",
            "implemented_by": "scripts/missing.py",
        }],
    }
    out = run_all_pure_passes(manifest)
    # No workspace_root → C1 returns empty (skipped).
    assert out["pass_c1"] == []


def test_run_all_pure_passes_runs_c1_with_workspace_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "missing.py").write_text("import os\n")
    manifest = {
        "id": "x", "schema_version": 20,
        "scheduled_actions": [{
            "id": "a", "summary": "summarizes things",
            "implemented_by": "scripts/missing.py",
        }],
    }
    out = run_all_pure_passes(manifest, workspace_root=tmp_path)
    # C1 should produce a finding (summary says 'summarizes' but no LLM
    # imports in the script).
    assert any("LLM" in (f.message or "") or "anthropic" in (f.message or "")
                for f in out["pass_c1"])


def test_run_all_pure_passes_hydrates_v7_arc_first(tmp_path: Path) -> None:
    """End-to-end: a v7-arc instance with a Spec on disk gets hydrated
    before Pass A runs, so Pass A sees the description and can fire
    its recurring-without-trigger check."""
    spec_id = "broken-app"
    spec_version = "1.0.0"
    gallery = tmp_path / "gallery" / "local" / spec_id
    gallery.mkdir(parents=True)
    (gallery / f"{spec_version}.json").write_text(json.dumps({
        "id": spec_id, "version": spec_version,
        "name": "Broken App",
        # The Spec's description claims daily behavior — Pass A C-A1
        # should fire because the instance has no triggers.
        "description": "Sends a daily briefing every morning.",
    }))

    instance = {
        "manifest_shape": "v7-arc",
        "id": "i-broken",
        "provenance": {"spec_id": spec_id, "spec_version": spec_version},
        "scheduled_actions": [],
        "crons": [],
    }
    out = run_all_pure_passes(instance, shared_dir=tmp_path)
    # After hydration Pass A sees the description and fires C-A1.
    assert any(f.id == "C-A1" for f in out["pass_a"])


def test_run_all_pure_passes_unhydrated_v7_arc_misses_findings(
    tmp_path: Path,
) -> None:
    """Regression-guard: without shared_dir, the v7-arc instance stays
    unhydrated and Pass A sees no description, so no C-A1.
    Documents the silent-miss this PR fixes."""
    instance = {
        "manifest_shape": "v7-arc",
        "id": "i-broken",
        "provenance": {"spec_id": "x", "spec_version": "1.0.0"},
        "scheduled_actions": [],
        "crons": [],
    }
    out = run_all_pure_passes(instance, shared_dir=None)
    # No description visible → C-A1 doesn't fire.
    assert not any(f.id == "C-A1" for f in out["pass_a"])

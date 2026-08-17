"""F-P.9 — Stage 0f (files-pack snapshot) integration tests.

Stage 0f extends ``build_export_draft`` with an optional files-pack
snapshot step that runs after Stage 0d. When ``include_files_pack``
is set AND the scanned manifest carries an installed ``pkg_id``,
the export pipeline produces a draft that ships both the manifest
AND a candidate files-pack — first-time gallery additions skip
straight to manifest+files class per the hybrid gallery model
(docs/note-hybrid-gallery-2026-06-03.md).

These tests monkeypatch the LLM-touching deriver / extractor /
round-trip stages so the assertion focuses on Stage 0f behaviour:
when it fires, when it skips, how it surfaces failures to the
operator-review surface (Stage 0e).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import export_engine  # noqa: E402


@pytest.fixture
def stub_pre_stages(monkeypatch):
    """Replace the LLM-driven stages (0b, 0c, 0d) with cheap stubs so
    the test focuses on Stage 0f behaviour."""

    # Stage 0b — build_spec derivation.
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.derive_build_spec",
        lambda *a, **kw: MagicMock(
            build_spec="stub build spec",
            model="stub-model",
            input_chars=0,
            output_chars=0,
        ),
    )
    # Stage 0c — interface_contract derivation.
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.derive_interface_contract",
        lambda *a, **kw: MagicMock(
            contract={"cli": {"commands": []}},
            model="stub-model",
            input_chars=0,
            output_chars=0,
            extraction_failed=False,
            used_fallback=False,
        ),
    )
    # Stage 0d — round-trip dry-run + structural diff.
    monkeypatch.setattr(
        "evolve_admin.applications.export_engine.round_trip_validate",
        lambda *a, **kw: MagicMock(
            verdict="pass",
            structural_findings=[],
            dry_run_files=[],
            dry_run_missing=[],
            dry_run_extra=[],
            dry_run_failed=False,
            model="stub-model",
            input_chars=0,
            output_chars=0,
        ),
    )


@pytest.fixture
def pod_layout(tmp_path: Path, monkeypatch):
    """Pod with a source bot whose installed app pkg_id matches what
    Stage 0f will snapshot."""
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts/foo.py").write_text("print('ok')\n")
    os.chmod(workspace / "scripts/foo.py", 0o644)
    # Installed manifest tracks pkg_id (the marker the scanner reads).
    (workspace / "manifests/foo.json").write_text(json.dumps({
        "id": "foo-app", "name": "Foo App",
        "bot_id": bot_user,
        "pkg_id": "p-installed", "pkg_version": "2026.06.03-1.0",
        "files": [{"path": "scripts/foo.py", "sha256": "x"}],
    }))

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {bot_user: {"user": bot_user}}},
    )
    return {
        "pod": pod, "workspace": workspace, "bot_user": bot_user,
    }


def _scanned_manifest(*, with_pkg_id: bool = True) -> dict:
    """Minimal scanned-manifest shape sufficient for the orchestrator."""
    m = {
        "id": "foo-app",
        "name": "Foo App",
        "files": [{"path": "scripts/foo.py", "sha256": "x"}],
    }
    if with_pkg_id:
        m["pkg_id"] = "p-installed"
    return m


# ── Default behaviour: Stage 0f off ─────────────────────────────────────────


def test_stage_0f_off_by_default(stub_pre_stages, pod_layout, tmp_path):
    """Without ``include_files_pack``, the draft stops at Stage 0d. No
    files_pack block; existing scanned-export consumers unaffected."""
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
    )
    assert draft["export_stage"] == "0d"
    assert "files_pack" not in draft
    assert "files_pack_skipped" not in draft.get("export_meta", {})


# ── Stage 0f happy path ─────────────────────────────────────────────────────


def test_stage_0f_snapshots_when_enabled(stub_pre_stages, pod_layout, tmp_path):
    """include_files_pack=True + scanned manifest carries pkg_id →
    Stage 0f calls the engine and attaches files_pack to draft."""
    out_dir = tmp_path / "stage0f-out"
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
        files_pack_out_dir=out_dir,
    )
    assert draft["export_stage"] == "0f"
    fp = draft["files_pack"]
    assert fp["format_version"] == "1.0"
    assert fp["files_count"] == 1
    assert fp["snapshot_source_pkg_version"] == "2026.06.03-1.0"
    assert len(fp["sha256"]) == 64
    assert fp["staging_dir"] == str(out_dir)
    # The snapshot landed on disk in the staging dir.
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "scripts/foo.py").is_file()


def test_stage_0f_carries_per_file_review_fields(
    stub_pre_stages, pod_layout, tmp_path,
):
    """The export_meta block carries the engine's per_file + skipped +
    personalization_totals so the operator-review UI (Stage 0e) can
    render the same surface as the F-P.7.b promote modal."""
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
        files_pack_out_dir=tmp_path / "out",
    )
    em = draft["export_meta"]
    assert "files_pack_per_file" in em
    assert isinstance(em["files_pack_per_file"], list)
    assert len(em["files_pack_per_file"]) == 1
    assert em["files_pack_per_file"][0]["path"] == "scripts/foo.py"
    assert "files_pack_personalization_totals" in em
    assert "files_pack_skipped_files" in em


def test_stage_0f_passes_snapshot_kwargs_through(
    stub_pre_stages, pod_layout, tmp_path,
):
    """snapshot_kwargs reaches the engine; here disabling auto_detect
    leaves the file content verbatim."""
    # Make the source file contain a workspace path so we can detect
    # whether substitution ran.
    src = pod_layout["workspace"] / "scripts/foo.py"
    src.write_text(
        f"WORKSPACE = '/Users/{pod_layout['bot_user']}/.openclaw/workspace'\n"
    )
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
        files_pack_out_dir=tmp_path / "out",
        snapshot_kwargs={"auto_detect": False},
    )
    snapped = (tmp_path / "out" / "scripts/foo.py").read_text()
    assert pod_layout["bot_user"] in snapped  # not substituted
    assert "{workspace}" not in snapped


# ── Skipping / failure paths ────────────────────────────────────────────────


def test_stage_0f_skipped_when_no_installed_pkg_id(
    stub_pre_stages, pod_layout, tmp_path,
):
    """When scanned_manifest has no pkg_id, Stage 0f can't snapshot —
    the draft must surface that as a documented skip in export_meta
    so the operator-review UI can show the reason."""
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(with_pkg_id=False),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
        files_pack_out_dir=tmp_path / "out",
    )
    assert draft["export_stage"] == "0d"  # didn't advance past 0d
    assert "files_pack" not in draft
    skipped = draft["export_meta"].get("files_pack_skipped", "")
    assert skipped.startswith("no_installed_pkg_id:")


def test_stage_0f_failure_logged_in_export_meta(
    stub_pre_stages, pod_layout, tmp_path, monkeypatch,
):
    """When the snapshot engine errors (e.g. workspace path missing),
    the failure goes into export_meta so the operator-review surface
    can show it without re-running the whole pipeline."""
    # Force the engine to fail by monkey-patching it directly inside
    # the export_engine module.
    monkeypatch.setattr(
        "evolve_admin.applications.snapshot_engine.snapshot_installed_app",
        lambda **kw: {"ok": False, "error": "stub_simulated_failure"},
    )
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
        files_pack_out_dir=tmp_path / "out",
    )
    assert draft["export_stage"] == "0d"
    assert "files_pack" not in draft
    assert draft["export_meta"]["files_pack_failed"] == "stub_simulated_failure"


def test_stage_0f_defaults_to_tempdir_when_out_dir_omitted(
    stub_pre_stages, pod_layout,
):
    """No files_pack_out_dir → engine writes to a daemon-allocated
    tempdir; the path lands in files_pack.staging_dir."""
    draft = export_engine.build_export_draft(
        bot_id=pod_layout["bot_user"],
        scanned_manifest=_scanned_manifest(),
        workspace=pod_layout["workspace"],
        include_files_pack=True,
    )
    assert draft["export_stage"] == "0f"
    staging = Path(draft["files_pack"]["staging_dir"])
    assert staging.is_dir()
    # Prefix carries the new pkg_id so operators can spot stale dirs.
    assert "export-stage0f-" in staging.name

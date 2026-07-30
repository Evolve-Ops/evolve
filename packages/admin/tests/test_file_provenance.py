"""Tests for the smart-forge per-file provenance model.

Spec: docs/note-smart-forge-and-file-provenance-2026-06-04.md.

Covers:
  - ApplicationManifest.files_partition() inference rule
    (no files-pack → all forge; with files-pack + in pack → bundled;
    with files-pack + NOT in pack → forge).
  - Explicit ``provenance`` field on file entries overrides inference.
  - install_files_pack_to_workspace honors the allowed_paths filter.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    FILE_PROVENANCE_BUNDLED,
    FILE_PROVENANCE_FORGE,
)
from evolve_admin.applications.files_pack import (  # noqa: E402
    FILES_PACK_FORMAT_VERSION,
    FilesPackFile,
    FilesPackMetadata,
    install_files_pack_to_workspace,
)


def _file_entry(path: str, *, provenance: str | None = None) -> dict:
    """A typical schema-v5+ file dict; provenance optional."""
    entry: dict = {
        "path": path,
        "file_id": "f-" + path.replace("/", "_"),
        "owned_by": "p-test",
        "purpose": "test",
    }
    if provenance:
        entry["provenance"] = provenance
    return entry


def _empty_manifest(**kwargs) -> ApplicationManifest:
    """Tiny manifest factory — only fills the required fields."""
    return ApplicationManifest(
        id="test-app",
        name="Test App",
        bot_id="team-bot-a",
        **kwargs,
    )


# ── files_partition() — inference rule ──────────────────────────────────────


def test_partition_no_files_pack_marks_everything_forge():
    """Pre-files-pack manifests have no ``files_pack`` block; every
    file should be classified as forge so the LLM-forge path runs."""
    m = _empty_manifest(files=[
        _file_entry("scripts/foo.py"),
        _file_entry("scripts/bar.sh"),
    ])
    result = m.files_partition()
    assert result["bundled"] == []
    assert result["forge"] == ["scripts/foo.py", "scripts/bar.sh"]


def test_partition_with_files_pack_marks_pack_paths_bundled():
    """When files_pack is stamped AND the path is in the files-pack
    paths, infer bundled. Paths not in the pack → forge."""
    m = _empty_manifest(
        files=[
            _file_entry("scripts/foo.py"),
            _file_entry("scripts/bar.sh"),
            _file_entry("HEARTBEAT.template.md"),
        ],
        files_pack={"format_version": "1.0", "sha256": "x"},
    )
    pack_paths = {"scripts/foo.py", "scripts/bar.sh"}
    result = m.files_partition(files_pack_paths=pack_paths)
    assert result["bundled"] == ["scripts/foo.py", "scripts/bar.sh"]
    assert result["forge"] == ["HEARTBEAT.template.md"]


def test_partition_files_pack_block_alone_doesnt_imply_bundled():
    """A stamped files_pack block but no pack_paths argument (or empty
    set) should still classify every file as forge — the path-membership
    check is the safety net against marking a file bundled when its
    content doesn't actually live in the pack."""
    m = _empty_manifest(
        files=[_file_entry("scripts/foo.py")],
        files_pack={"format_version": "1.0", "sha256": "x"},
    )
    # No pack_paths supplied → can't confirm membership → forge.
    result = m.files_partition()
    assert result["bundled"] == []
    assert result["forge"] == ["scripts/foo.py"]
    # Empty set → same behaviour.
    result = m.files_partition(files_pack_paths=set())
    assert result["bundled"] == []
    assert result["forge"] == ["scripts/foo.py"]


# ── files_partition() — explicit provenance ─────────────────────────────────


def test_partition_explicit_provenance_overrides_inference():
    """An explicit ``provenance`` field always wins. Even with no
    files-pack stamped, a file marked bundled goes in the bundled list
    (the dispatcher will then detect the missing pack and error)."""
    m = _empty_manifest(files=[
        _file_entry("scripts/foo.py", provenance=FILE_PROVENANCE_BUNDLED),
        _file_entry("scripts/bar.sh", provenance=FILE_PROVENANCE_FORGE),
    ])
    result = m.files_partition()
    assert result["bundled"] == ["scripts/foo.py"]
    assert result["forge"] == ["scripts/bar.sh"]


def test_partition_explicit_provenance_beats_pack_membership():
    """Even when a path IS in the files-pack, an explicit ``forge``
    annotation wins — operator override case."""
    m = _empty_manifest(
        files=[
            _file_entry("scripts/foo.py", provenance=FILE_PROVENANCE_FORGE),
            _file_entry("scripts/bar.sh"),  # inferred bundled
        ],
        files_pack={"format_version": "1.0", "sha256": "x"},
    )
    pack_paths = {"scripts/foo.py", "scripts/bar.sh"}
    result = m.files_partition(files_pack_paths=pack_paths)
    assert result["bundled"] == ["scripts/bar.sh"]
    assert result["forge"] == ["scripts/foo.py"]


def test_partition_unknown_provenance_falls_back_to_inference():
    """Garbage in ``provenance`` (typo, future value) → fall back to
    the inference rule for that entry. Keeps the schema forward-
    compatible without silently mis-routing files."""
    m = _empty_manifest(
        files=[
            _file_entry("scripts/foo.py", provenance="something-new"),
            _file_entry("scripts/bar.sh"),
        ],
        files_pack={"format_version": "1.0", "sha256": "x"},
    )
    pack_paths = {"scripts/foo.py", "scripts/bar.sh"}
    result = m.files_partition(files_pack_paths=pack_paths)
    # Both inferred to bundled (both in pack, no explicit overrides).
    assert set(result["bundled"]) == {"scripts/foo.py", "scripts/bar.sh"}
    assert result["forge"] == []


# ── files_partition() — list-of-strings (v4) compat ─────────────────────────


def test_partition_handles_legacy_string_entries():
    """v4-shape files (bare strings, no provenance) should still
    partition cleanly via inference. Pre-existing v4 manifests don't
    need migration to use the smart-forge dispatcher."""
    m = _empty_manifest(
        files=["scripts/foo.py", "scripts/bar.sh"],
        files_pack={"format_version": "1.0", "sha256": "x"},
    )
    pack_paths = {"scripts/foo.py"}
    result = m.files_partition(files_pack_paths=pack_paths)
    assert result["bundled"] == ["scripts/foo.py"]
    assert result["forge"] == ["scripts/bar.sh"]


def test_partition_skips_malformed_entries():
    """Malformed entries (empty path, None, non-dict/non-str) should
    be silently skipped so the partition can't blow up on bad data."""
    m = _empty_manifest(files=[
        {"path": ""},
        None,
        42,
        _file_entry("scripts/foo.py"),
    ])
    result = m.files_partition()
    assert result["bundled"] == []
    assert result["forge"] == ["scripts/foo.py"]


# ── install_files_pack_to_workspace — allowed_paths filter ──────────────────


@pytest.fixture
def packed_workspace(tmp_path: Path):
    """A files-pack on disk + a target workspace so install can
    actually copy. Returns paths + a parsed metadata object."""
    pack = tmp_path / "files"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts/foo.py").write_text("print('foo')\n")
    (pack / "scripts/bar.sh").write_text("#!/bin/bash\necho bar\n")
    (pack / "HEARTBEAT.md").write_text("# Heartbeat template\n")
    os.chmod(pack / "scripts/foo.py", 0o644)
    os.chmod(pack / "scripts/bar.sh", 0o755)
    os.chmod(pack / "HEARTBEAT.md", 0o644)

    meta = FilesPackMetadata(
        format_version=FILES_PACK_FORMAT_VERSION,
        snapshot_source={"pkg_id": "p-test", "pkg_version": "1.0",
                         "snapshot_at": "2026-06-04T00:00:00Z"},
        files=[
            FilesPackFile(path="scripts/foo.py", mode="0644",
                          sha256="x", size_bytes=0, placeholders=[]),
            FilesPackFile(path="scripts/bar.sh", mode="0755",
                          sha256="x", size_bytes=0, placeholders=[]),
            FilesPackFile(path="HEARTBEAT.md", mode="0644",
                          sha256="x", size_bytes=0, placeholders=[]),
        ],
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"pack_dir": pack, "metadata": meta, "workspace": workspace}


def test_install_writes_all_files_when_allowed_paths_omitted(
    packed_workspace,
):
    """Backward compat: when allowed_paths is None, every file in the
    pack gets installed (pre-existing all-or-nothing behaviour)."""
    install_files_pack_to_workspace(
        packed_workspace["metadata"],
        packed_workspace["pack_dir"],
        packed_workspace["workspace"],
        context={},
    )
    assert (packed_workspace["workspace"] / "scripts/foo.py").is_file()
    assert (packed_workspace["workspace"] / "scripts/bar.sh").is_file()
    assert (packed_workspace["workspace"] / "HEARTBEAT.md").is_file()


def test_install_filters_to_allowed_paths_only(packed_workspace):
    """When allowed_paths is set, only those files get installed —
    smart-forge dispatcher's bundled-subset path."""
    install_files_pack_to_workspace(
        packed_workspace["metadata"],
        packed_workspace["pack_dir"],
        packed_workspace["workspace"],
        context={},
        allowed_paths={"scripts/foo.py"},
    )
    assert (packed_workspace["workspace"] / "scripts/foo.py").is_file()
    assert not (packed_workspace["workspace"] / "scripts/bar.sh").exists()
    assert not (packed_workspace["workspace"] / "HEARTBEAT.md").exists()


def test_install_empty_allowed_paths_writes_nothing(packed_workspace):
    """allowed_paths=set() (vs. None) is the "everything is forge"
    case — explicit empty filter writes zero files. Distinct from
    allowed_paths=None which means "no filter, install everything"."""
    result = install_files_pack_to_workspace(
        packed_workspace["metadata"],
        packed_workspace["pack_dir"],
        packed_workspace["workspace"],
        context={},
        allowed_paths=set(),
    )
    assert result.files_written == []
    assert result.bytes_total == 0
    # And nothing landed in the workspace.
    assert not (packed_workspace["workspace"] / "scripts/foo.py").exists()


def test_install_allowed_paths_with_unknown_path_just_skips(
    packed_workspace,
):
    """An allowed_paths entry that isn't in the pack metadata is
    silently ignored — caller's responsibility to keep the set
    aligned. No spurious error."""
    install_files_pack_to_workspace(
        packed_workspace["metadata"],
        packed_workspace["pack_dir"],
        packed_workspace["workspace"],
        context={},
        allowed_paths={"scripts/foo.py", "not/in/pack.py"},
    )
    assert (packed_workspace["workspace"] / "scripts/foo.py").is_file()
    assert not (packed_workspace["workspace"] / "not/in/pack.py").exists()

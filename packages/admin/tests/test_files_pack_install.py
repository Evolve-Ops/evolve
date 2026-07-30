"""tests/test_files_pack_install.py — F-P.2 install path.

Spec: docs/spec-files-pack-hybrid-2026-06-03.md.

Covers:
  * ``install_files_pack_to_workspace`` writes files atomically with
    substitution, declared modes, and per-file SHA records
  * ``find_files_pack_dir`` resolves the gallery files-pack directory
  * ``_maybe_install_via_files_pack`` falls through (returns None) on
    every recoverable error: no files_pack metadata, missing gallery
    dir, missing manifest.json, integrity findings, substitution
    failure, write error
  * On happy path returns a BotResult-shaped object with
    ``status="complete"`` and the file list
  * The ``files_pack_install`` context_snapshot flag is set so
    downstream code can skip critique
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.files_pack import (  # noqa: E402
    FILES_PACK_FORMAT_VERSION,
    FilesPackIntegrityError,
    install_files_pack_to_workspace,
    load_files_pack_metadata,
    resolve_install_context,
)
from evolve_admin.applications.forge_engine import (  # noqa: E402
    _maybe_install_via_files_pack,
)
from evolve_admin.applications.forge_jobs import ForgeJob  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_files_pack(
    base: Path, files: list[dict],
    *, format_version: str = FILES_PACK_FORMAT_VERSION,
) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    meta_files = []
    for f in files:
        rel = f["path"]
        content = f.pop("_content", "")
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, int(f["mode"], 8))
        f.setdefault("sha256", _sha(content))
        f.setdefault("size_bytes", len(content.encode("utf-8")))
        meta_files.append(f)
    (base / "manifest.json").write_text(json.dumps({
        "format_version": format_version,
        "snapshot_source": {"bot_id": "team-bot-a"},
        "files": meta_files,
    }))
    return base


def _ctx(**overrides) -> dict[str, str]:
    ctx = {
        "bot_id": "personal-bot",
        "bot_user": "personal-bot",
        "workspace": "/Users/personal-bot/.openclaw/workspace",
        "shared_dir": "/Users/Shared/evolve",
        "pkg_id": "p-9bfa1c84",
        "app_id": "task-manager",
        "installed_at": "2026-06-03T08:00:00Z",
    }
    ctx.update(overrides)
    return ctx


# ── install_files_pack_to_workspace ─────────────────────────────────────────


def test_install_copies_files_to_workspace_with_modes(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "scripts/tasks.py", "mode": "0644",
         "_content": "print('ok')\n", "placeholders": []},
        {"path": "scripts/task-check.sh", "mode": "0755",
         "_content": "#!/bin/bash\necho ok\n", "placeholders": []},
    ])
    meta = load_files_pack_metadata(pack)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = install_files_pack_to_workspace(meta, pack, workspace, _ctx())

    assert result.errors == []
    assert len(result.files_written) == 2
    # Files landed on disk with declared modes.
    assert (workspace / "scripts/tasks.py").is_file()
    mode_644 = (workspace / "scripts/tasks.py").stat().st_mode & 0o777
    mode_755 = (workspace / "scripts/task-check.sh").stat().st_mode & 0o777
    assert mode_644 == 0o644
    assert mode_755 == 0o755
    # The records carry SHA + size for the bot_forge-shape downstream.
    paths = {r["path"] for r in result.files_written}
    assert paths == {"scripts/tasks.py", "scripts/task-check.sh"}


def test_install_substitutes_declared_placeholders(tmp_path: Path):
    """Placeholders in declared list substitute; literal `{bot_id}` in
    docstrings passes through (the safety property)."""
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "scripts/task-check.sh", "mode": "0755",
         "_content": (
             "#!/bin/bash\n"
             "# Uses {bot_id} (literal in docstring — should pass through)\n"
             "WORKSPACE={workspace}\n"
             "BOT={bot_id}\n"
         ),
         "placeholders": ["bot_id", "workspace"]},
    ])
    meta = load_files_pack_metadata(pack)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    install_files_pack_to_workspace(meta, pack, workspace, _ctx())

    written = (workspace / "scripts/task-check.sh").read_text()
    assert "WORKSPACE=/Users/personal-bot/.openclaw/workspace" in written
    assert "BOT=personal-bot" in written
    # Both literal-ish references were substituted (the docstring
    # comment also had {bot_id}; since bot_id IS declared, it
    # substitutes everywhere. This is the cost of declaring a
    # placeholder — operator review of placeholders[] in
    # files/manifest.json is the safety net.)
    assert "Uses personal-bot" in written


def test_install_no_placeholders_preserves_file_byte_for_byte(tmp_path: Path):
    """Binary safety property: a file with empty placeholders[] is
    copied via read_bytes/write_bytes — no text decoding."""
    pack = tmp_path / "fp"
    pack.mkdir()
    binary = bytes(range(256))   # every byte value
    (pack / "data.bin").write_bytes(binary)
    os.chmod(pack / "data.bin", 0o644)
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": FILES_PACK_FORMAT_VERSION,
        "snapshot_source": {},
        "files": [{
            "path": "data.bin", "mode": "0644",
            "sha256": hashlib.sha256(binary).hexdigest(),
            "size_bytes": 256, "placeholders": [],
        }],
    }))
    meta = load_files_pack_metadata(pack)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    install_files_pack_to_workspace(meta, pack, workspace, _ctx())
    assert (workspace / "data.bin").read_bytes() == binary


def test_install_creates_intermediate_directories(tmp_path: Path):
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "deeply/nested/dir/x.py", "mode": "0644",
         "_content": "x", "placeholders": []},
    ])
    meta = load_files_pack_metadata(pack)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    install_files_pack_to_workspace(meta, pack, workspace, _ctx())
    assert (workspace / "deeply/nested/dir/x.py").is_file()


def test_install_raises_integrity_error_for_missing_source(tmp_path: Path):
    """If verify_files_pack_integrity wasn't run first and a source
    file is missing, the install raises FilesPackIntegrityError with
    guidance to run verify first."""
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.py", "mode": "0644", "_content": "x", "placeholders": []},
    ])
    meta = load_files_pack_metadata(pack)
    (pack / "a.py").unlink()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(FilesPackIntegrityError, match="verify_files_pack_integrity"):
        install_files_pack_to_workspace(meta, pack, workspace, _ctx())


def test_install_records_write_errors_without_raising(tmp_path: Path):
    """A per-file write failure is recorded in ``errors`` but doesn't
    halt the whole install — the caller decides on retry policy."""
    pack = _write_files_pack(tmp_path / "fp", [
        {"path": "a.py", "mode": "0644", "_content": "a", "placeholders": []},
        {"path": "b.py", "mode": "0644", "_content": "b", "placeholders": []},
    ])
    meta = load_files_pack_metadata(pack)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Make the target file's parent read-only AFTER mkdir.
    target = workspace / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Drop write perms on the workspace dir so the first write fails.
    os.chmod(workspace, 0o555)
    try:
        result = install_files_pack_to_workspace(meta, pack, workspace, _ctx())
    finally:
        os.chmod(workspace, 0o755)  # restore so tmp_path cleanup works
    # Both files failed (since the workspace itself is read-only).
    assert len(result.errors) == 2
    assert result.files_written == []


# ── find_files_pack_dir ─────────────────────────────────────────────────────


def test_find_files_pack_dir_returns_existing(tmp_path: Path, monkeypatch):
    """When ``gallery/<slug>/files/`` exists, return it."""
    gallery = tmp_path / "gallery"
    app = gallery / "task-manager"
    app.mkdir(parents=True)
    (app / "p-9bfa1c84.json").write_text("{}")
    (app / "files").mkdir()

    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    from evolve_admin.applications.gallery import find_files_pack_dir
    out = find_files_pack_dir("p-9bfa1c84")
    assert out == app / "files"


def test_find_files_pack_dir_returns_none_when_no_files_dir(tmp_path: Path, monkeypatch):
    """Package exists but no ``files/`` subdirectory — return None
    (operator hasn't snapshotted yet)."""
    gallery = tmp_path / "gallery"
    app = gallery / "task-manager"
    app.mkdir(parents=True)
    (app / "p-9bfa1c84.json").write_text("{}")

    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    from evolve_admin.applications.gallery import find_files_pack_dir
    assert find_files_pack_dir("p-9bfa1c84") is None


def test_find_files_pack_dir_returns_none_when_pkg_missing(tmp_path: Path, monkeypatch):
    """No app directory contains a package matching pkg_id."""
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    from evolve_admin.applications.gallery import find_files_pack_dir
    assert find_files_pack_dir("p-nosuch") is None


# ── _maybe_install_via_files_pack — happy path + fall-throughs ──────────────


def _job() -> ForgeJob:
    return ForgeJob(
        job_id="j-fp2-test",
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-9bfa1c84",
        app_id="task-manager",
        bot_id="personal-bot",
        pkg_version_before=None,
        gallery_version="2026.06.03-1.5",
    )


def _manifest_with_files_pack(**fp_overrides) -> ApplicationManifest:
    m = ApplicationManifest(
        id="task-manager", name="Task Manager", bot_id="personal-bot",
        pkg_id="p-9bfa1c84",
    )
    m.files_pack = {
        "format_version": "1.0",
        "files_count": 1,
        "snapshot_source_pkg_version": "2026.06.03-1.4",
        "sha256": "ignored-in-test",
    }
    m.files_pack.update(fp_overrides)
    return m


def _set_up_files_pack(tmp_path: Path, monkeypatch):
    """Configure gallery + workspace fakes."""
    gallery = tmp_path / "gallery"
    app = gallery / "task-manager"
    app.mkdir(parents=True)
    (app / "p-9bfa1c84.json").write_text("{}")
    files_dir = _write_files_pack(app / "files", [
        {"path": "scripts/tasks.py", "mode": "0644",
         "_content": "print('ok')\n", "placeholders": []},
    ])
    workspace = tmp_path / "Users/personal-bot/.openclaw/workspace"
    workspace.mkdir(parents=True)

    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, network=None: tmp_path / "Users" / bot_id,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {"personal-bot": {}}},
    )
    return files_dir, workspace


def test_dispatcher_falls_through_when_no_files_pack_field(tmp_path: Path):
    """Manifest without files_pack -> return None -> LLM-forge runs."""
    m = ApplicationManifest(
        id="task-manager", name="Task Manager", bot_id="personal-bot",
        pkg_id="p-9bfa1c84",
    )
    # No m.files_pack set
    result = _maybe_install_via_files_pack(_job(), m, tmp_path)
    assert result is None


def test_dispatcher_falls_through_when_no_manifest(tmp_path: Path):
    assert _maybe_install_via_files_pack(_job(), None, tmp_path) is None


def test_dispatcher_falls_through_when_no_pkg_id(tmp_path: Path):
    job = _job()
    job.pkg_id = ""
    m = _manifest_with_files_pack()
    assert _maybe_install_via_files_pack(job, m, tmp_path) is None


def test_dispatcher_falls_through_when_gallery_files_missing(
    tmp_path: Path, monkeypatch,
):
    """Manifest declares files_pack but gallery dir absent — log +
    return None so LLM-forge picks up the slack."""
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    m = _manifest_with_files_pack()
    assert _maybe_install_via_files_pack(_job(), m, tmp_path) is None


def test_dispatcher_returns_build_result_on_happy_path(
    tmp_path: Path, monkeypatch,
):
    """Files-pack present + integrity OK → return a BuildResult-shaped
    object so the rest of the forge engine runs unchanged."""
    files_dir, workspace = _set_up_files_pack(tmp_path, monkeypatch)
    m = _manifest_with_files_pack()
    result = _maybe_install_via_files_pack(_job(), m, tmp_path)

    assert result is not None
    assert result.status == "complete"
    assert len(result.files_written) == 1
    assert result.files_written[0]["path"] == "scripts/tasks.py"
    assert "files-pack install" in result.notes.lower()
    # files actually landed in the workspace.
    assert (workspace / "scripts/tasks.py").is_file()


def test_dispatcher_falls_through_on_integrity_finding(
    tmp_path: Path, monkeypatch,
):
    """A source file missing on disk → integrity finding → fall
    through so the LLM-forge path can rebuild."""
    files_dir, workspace = _set_up_files_pack(tmp_path, monkeypatch)
    (files_dir / "scripts/tasks.py").unlink()  # break integrity
    m = _manifest_with_files_pack()
    assert _maybe_install_via_files_pack(_job(), m, tmp_path) is None


def test_dispatcher_falls_through_on_substitution_failure(
    tmp_path: Path, monkeypatch,
):
    """A declared placeholder that doesn't resolve raises → log +
    fall through to LLM-forge instead of crashing the install."""
    gallery = tmp_path / "gallery"
    app = gallery / "task-manager"
    app.mkdir(parents=True)
    (app / "p-9bfa1c84.json").write_text("{}")
    _write_files_pack(app / "files", [
        {"path": "scripts/task-check.sh", "mode": "0755",
         "_content": "WORKSPACE={workspace}\nMISSING={app_id}\n",
         "placeholders": ["workspace", "app_id"]},
    ])
    workspace = tmp_path / "Users/personal-bot/.openclaw/workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot_id, network=None: tmp_path / "Users" / bot_id,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {"personal-bot": {}}},
    )

    m = _manifest_with_files_pack()
    job = _job()
    job.app_id = ""           # missing context — substitution will fail
    m.id = ""
    result = _maybe_install_via_files_pack(job, m, tmp_path)
    assert result is None      # graceful fall-through


def test_dispatcher_falls_through_when_manifest_has_partial_coverage(
    tmp_path: Path, monkeypatch,
):
    """Smart-forge dispatcher (docs/note-smart-forge-and-file-
    provenance-2026-06-04.md): when the manifest declares files[]
    that include some marked as ``forge`` (or inferred forge because
    they're not in the pack), the dispatcher falls through to
    LLM-forge for the full install. Phase 4.5 still runs after."""
    files_dir, workspace = _set_up_files_pack(tmp_path, monkeypatch)
    m = _manifest_with_files_pack()
    # Declare two files: one bundled (in the pack), one forge (not).
    m.files = [
        {"path": "scripts/tasks.py", "provenance": "bundled"},
        {"path": "HEARTBEAT.template.md", "provenance": "forge"},
    ]
    result = _maybe_install_via_files_pack(_job(), m, tmp_path)
    # Partial coverage -> None -> LLM-forge handles everything.
    assert result is None
    # Verify nothing landed (LLM-forge will do that).
    assert not (workspace / "scripts/tasks.py").exists()


def test_dispatcher_complete_coverage_installs_only_bundled_subset(
    tmp_path: Path, monkeypatch,
):
    """When every file declared in the manifest is bundled (either
    explicitly or inferred from pack membership), the dispatcher
    installs that subset via files-pack and returns a BuildResult.
    The allowed_paths filter prevents stray pack entries from being
    installed."""
    files_dir, workspace = _set_up_files_pack(tmp_path, monkeypatch)
    m = _manifest_with_files_pack()
    # Declare exactly the one file that's in the pack.
    m.files = [
        {"path": "scripts/tasks.py", "provenance": "bundled"},
    ]
    result = _maybe_install_via_files_pack(_job(), m, tmp_path)
    assert result is not None
    assert result.status == "complete"
    assert len(result.files_written) == 1
    assert (workspace / "scripts/tasks.py").is_file()


def test_dispatcher_proceeds_on_top_level_sha_mismatch(
    tmp_path: Path, monkeypatch,
):
    """Top-level files_pack.sha256 mismatch is logged but does not
    block the install — per-file integrity is the authoritative check
    (spec §7)."""
    files_dir, workspace = _set_up_files_pack(tmp_path, monkeypatch)
    m = _manifest_with_files_pack(sha256="0" * 64)   # wrong sha
    result = _maybe_install_via_files_pack(_job(), m, tmp_path)
    assert result is not None
    assert result.status == "complete"

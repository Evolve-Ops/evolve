"""Tests for ``applications.workspace_sync``.

Coverage:
  - Resolver priority: gallery files-pack > workspace_files_source > None.
  - Path-traversal guard on workspace_files_source.
  - Synthesised files-pack metadata for side-loaded dirs.
  - Drift detection: clean run is a no-op; modified source triggers re-copy.
  - FORCE mode: re-copies regardless of drift.
  - SKIP mode: returns immediately, no IO.
  - __pycache__ invalidation alongside .py rewrites.
  - Orphan detection: declared-but-source-removed files surface.
  - Missing-in-source detection: declared-but-source-missing files surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402
from evolve_admin.applications.workspace_sync import (  # noqa: E402
    SyncMode,
    WorkspaceFilesSourceError,
    WorkspaceSyncResult,
    resolve_workspace_files_source,
    sync_workspace_files,
    synthesise_files_pack_metadata,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A tmp tree that looks like a repo (has a 'gallery/' to satisfy the
    repo-root probe and a 'docs/myapp/' for side-loaded sources)."""
    (tmp_path / "gallery").mkdir()
    (tmp_path / "docs" / "myapp" / "scripts").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A tmp bot workspace dir; sync writes into here."""
    ws = tmp_path / "bot_workspace"
    ws.mkdir()
    return ws


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Resolver ────────────────────────────────────────────────────────────────


def test_resolver_returns_none_when_nothing_declared(fake_repo: Path) -> None:
    m = ApplicationManifest(id="x", name="X", bot_id="b")
    assert resolve_workspace_files_source(m, repo_root=fake_repo) is None


def test_resolver_picks_up_workspace_files_source(fake_repo: Path) -> None:
    m = ApplicationManifest(
        id="x", name="X", bot_id="b",
        workspace_files_source="docs/myapp/",
    )
    src = resolve_workspace_files_source(m, repo_root=fake_repo)
    assert src is not None
    assert src.synthesise is True
    assert src.origin == "manifest"
    assert src.files_pack_dir == (fake_repo / "docs" / "myapp").resolve()


def test_resolver_rejects_traversal_escape(fake_repo: Path, tmp_path: Path) -> None:
    """A workspace_files_source that resolves outside the repo root raises
    rather than silently reading from /etc/."""
    outside = tmp_path / "outside"
    outside.mkdir()
    m = ApplicationManifest(
        id="x", name="X", bot_id="b",
        workspace_files_source="../outside/",
    )
    with pytest.raises(WorkspaceFilesSourceError, match="outside the repo root"):
        resolve_workspace_files_source(m, repo_root=fake_repo)


def test_resolver_returns_none_when_declared_source_missing(fake_repo: Path) -> None:
    """A declared source that doesn't exist on disk should return None,
    not raise. The caller (sync) logs and continues."""
    m = ApplicationManifest(
        id="x", name="X", bot_id="b",
        workspace_files_source="docs/nope/",
    )
    assert resolve_workspace_files_source(m, repo_root=fake_repo) is None


# ── Synthesised metadata ────────────────────────────────────────────────────


def test_synthesise_hashes_only_declared_files(fake_repo: Path) -> None:
    """A stray file in the source dir that isn't in manifest.files[] must
    NOT land in the synthesised pack — sync only acts on declared files."""
    _write(fake_repo / "docs" / "myapp" / "scripts" / "real.py", "print('hi')\n")
    _write(fake_repo / "docs" / "myapp" / "scripts" / "stray.py", "print('nope')\n")
    m = ApplicationManifest(id="x", name="X", bot_id="b")
    m.files = [{"path": "scripts/real.py"}]

    pack = synthesise_files_pack_metadata(fake_repo / "docs" / "myapp", m)
    assert [f.path for f in pack.files] == ["scripts/real.py"]
    assert pack.files[0].sha256 == _sha("print('hi')\n")
    assert pack.files[0].mode == "0644"


def test_synthesise_infers_sh_as_0755(fake_repo: Path) -> None:
    _write(fake_repo / "docs" / "myapp" / "scripts" / "run.sh", "#!/bin/bash\necho hi\n")
    m = ApplicationManifest(id="x", name="X", bot_id="b")
    m.files = [{"path": "scripts/run.sh"}]

    pack = synthesise_files_pack_metadata(fake_repo / "docs" / "myapp", m)
    assert pack.files[0].mode == "0755"


def test_synthesise_skips_files_missing_from_source(fake_repo: Path) -> None:
    """Files declared but absent on the source side don't crash; sync's
    later pass reports them as missing_in_source."""
    _write(fake_repo / "docs" / "myapp" / "scripts" / "have.py", "x\n")
    m = ApplicationManifest(id="x", name="X", bot_id="b")
    m.files = [
        {"path": "scripts/have.py"},
        {"path": "scripts/missing.py"},
    ]
    pack = synthesise_files_pack_metadata(fake_repo / "docs" / "myapp", m)
    assert [f.path for f in pack.files] == ["scripts/have.py"]


# ── sync_workspace_files end-to-end ─────────────────────────────────────────


def _make_manifest(workspace_files_source: str = "docs/myapp/", files=None):
    m = ApplicationManifest(
        id="myapp", name="MyApp", bot_id="b",
        pkg_id="",  # no gallery match
        workspace_files_source=workspace_files_source,
    )
    m.files = files or [{"path": "scripts/foo.py"}]
    return m


def _patch_repo_root(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    monkeypatch.setattr(
        "evolve_admin.applications.workspace_sync._repo_root",
        lambda: fake_repo,
    )


def test_sync_skip_mode_is_immediate_noop(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repo_root(monkeypatch, fake_repo)
    m = _make_manifest()
    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
        mode=SyncMode.SKIP,
    )
    assert result.skipped is True
    assert result.skipped_reason == "sync disabled"


def test_sync_skipped_when_no_source_resolved(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pkg_id match, no workspace_files_source → skip with a reason."""
    _patch_repo_root(monkeypatch, fake_repo)
    m = _make_manifest(workspace_files_source="")
    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert result.skipped is True
    assert "no source" in result.skipped_reason


def test_sync_clean_run_is_noop(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source sha matches workspace sha → no copy, synced_count=0."""
    _patch_repo_root(monkeypatch, fake_repo)
    content = "print('clean')\n"
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", content)
    _write(workspace / "scripts" / "foo.py", content)
    m = _make_manifest()

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert result.skipped is False
    assert result.synced_count == 0
    assert result.drifted_paths == []
    assert (workspace / "scripts" / "foo.py").read_text() == content


def test_sync_drift_triggers_recopy(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Atlas case: source updated, workspace stale → sync re-copies
    just the drifted file, and the workspace content matches the source
    after."""
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", "NEW\n")
    _write(workspace / "scripts" / "foo.py", "OLD\n")
    _write(fake_repo / "docs" / "myapp" / "scripts" / "bar.py", "BAR\n")
    _write(workspace / "scripts" / "bar.py", "BAR\n")  # unchanged
    m = _make_manifest(files=[
        {"path": "scripts/foo.py"},
        {"path": "scripts/bar.py"},
    ])

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert result.synced_count == 1
    assert result.drifted_paths == ["scripts/foo.py"]
    assert (workspace / "scripts" / "foo.py").read_text() == "NEW\n"
    assert (workspace / "scripts" / "bar.py").read_text() == "BAR\n"


def test_sync_force_mode_recopies_everything(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", "X\n")
    _write(workspace / "scripts" / "foo.py", "X\n")  # already matches
    m = _make_manifest()

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
        mode=SyncMode.FORCE,
    )
    assert result.synced_count == 1


def test_sync_invalidates_pycache_for_py_drift(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", "NEW\n")
    _write(workspace / "scripts" / "foo.py", "OLD\n")
    cache_dir = workspace / "scripts" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "foo.cpython-313.pyc").write_bytes(b"stale-bytecode")
    m = _make_manifest()

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert not cache_dir.exists()
    assert str(cache_dir) in result.pycache_cleared


def test_sync_reports_missing_in_source(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manifest declares two files; only one exists on source side. The
    missing one surfaces in missing_in_source so an operator can
    distinguish manifest drift from workspace drift."""
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", "X\n")
    m = _make_manifest(files=[
        {"path": "scripts/foo.py"},
        {"path": "scripts/gone.py"},
    ])

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert "scripts/gone.py" in result.missing_in_source


def test_sync_reports_orphans(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file declared in manifest.files[] that exists in the workspace
    but not in the source is an orphan (logged, NOT deleted in v1)."""
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "kept.py", "K\n")
    _write(workspace / "scripts" / "kept.py", "K\n")
    _write(workspace / "scripts" / "orphan.py", "stale-content")  # still in ws
    m = _make_manifest(files=[
        {"path": "scripts/kept.py"},
        {"path": "scripts/orphan.py"},  # declared, but source side removed it
    ])

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    assert "scripts/orphan.py" in result.orphan_paths
    assert (workspace / "scripts" / "orphan.py").exists()  # not deleted in v1


def test_sync_stamp_round_trips(
    fake_repo: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """to_stamp() returns the same keys the admin UI / JSON output will
    read. Pin the shape so a refactor can't silently rename fields."""
    _patch_repo_root(monkeypatch, fake_repo)
    _write(fake_repo / "docs" / "myapp" / "scripts" / "foo.py", "X\n")
    _write(workspace / "scripts" / "foo.py", "X\n")
    m = _make_manifest()

    result = sync_workspace_files(
        m, fake_repo / "shared",
        bot_user="b", workspace_dir=workspace,
    )
    stamp = result.to_stamp()
    expected_keys = {
        "last_synced_at", "source", "origin", "synced_count",
        "drifted_paths", "orphan_paths", "missing_in_source", "errors",
    }
    assert expected_keys <= set(stamp.keys())


# -- AL-1.4b: the files-pack {{app_id}} placeholder keeps its legacy binding --


def test_install_context_app_id_placeholder_is_the_legacy_manifest_id() -> None:
    """AL-1.4b did not sweep the identity read that feeds ``resolve_install_
    context(app_id=...)``, and this pins why.

    ``pkg_id`` / ``app_id`` in the install context are the ``{{pkg_id}}`` /
    ``{{app_id}}`` PLACEHOLDER values substituted into files-pack file CONTENT
    written into a bot's workspace, so their values are already recorded on
    disk in that bot's tree. ``{{app_id}}`` has been bound to the manifest's
    legacy ``id`` since files-pack shipped; the canonical ``app_id`` of a
    stamped manifest is its ``pkg_id``. Swapping would rewrite the substituted
    text on the next drift re-copy and split one app's workspace files across
    two identities.
    """
    from evolve_admin.applications.app_identity import resolve_app_id
    from evolve_admin.applications.files_pack import resolve_install_context

    manifest = ApplicationManifest(
        id="task-manager", name="task-manager", bot_id="bot-a",
        pkg_id="p-cccccccc",
    )
    ctx = resolve_install_context(
        bot_id=manifest.bot_id, bot_user="bot-a", workspace="/w",
        pkg_id=manifest.pkg_id, app_id=manifest.id,
        installed_at="2026-01-01T00:00:00Z", shared_dir="/s",
    )
    assert ctx["app_id"] == "task-manager"
    assert ctx["pkg_id"] == "p-cccccccc"
    # The canonical resolver disagrees with the placeholder -- deliberately.
    assert resolve_app_id(
        {"id": manifest.id, "pkg_id": manifest.pkg_id}
    ) == "p-cccccccc" != ctx["app_id"]

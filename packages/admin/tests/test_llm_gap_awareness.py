"""F-P.11.b — LLM-side gap awareness tests.

Covers the smart-forge mixed-mode path:

  1. ``BuildRequest`` carries the ``paths_already_covered`` list and
     serializes it into the inbox JSON the bot's LLM reads.
  2. ``build_prompt`` mentions the field so the LLM knows what to do
     with it.
  3. ``_maybe_install_via_files_pack`` stashes a partial-install plan
     on ``job.context_snapshot`` when the manifest declares a partial
     files-pack (some files bundled, some forge).
  4. ``_install_partial_files_pack`` replays the F-P.2 install path
     against just the bundled subset.

Step 2 dispatcher integration is exercised by the existing
``test_files_pack_install.py`` regression. These tests target the
new building blocks directly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge  # noqa: E402
from evolve_admin.applications.forge_engine import (  # noqa: E402
    _install_partial_files_pack,
    _maybe_install_via_files_pack,
)
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


# ── BuildRequest contract ──────────────────────────────────────────────────


def test_build_request_serializes_paths_already_covered():
    r = bot_forge.BuildRequest(
        job_id="j-1",
        kind="build",
        pkg_id="p-1",
        pkg_version="1.0",
        app_id="app-1",
        app_name="App One",
        build_spec="...",
        paths_already_covered=["scripts/foo.py", "scripts/bar.sh"],
    )
    body = json.loads(r.to_json())
    assert body["paths_already_covered"] == ["scripts/foo.py", "scripts/bar.sh"]


def test_build_request_paths_already_covered_defaults_to_empty():
    """Backward-compat: existing call sites that don't set the field
    serialize with an empty list, not a missing key. The bot's LLM
    can always read the key without checking for absence."""
    r = bot_forge.BuildRequest(
        job_id="j-1", kind="build", pkg_id="p-1", pkg_version="1.0",
        app_id="app-1", app_name="App", build_spec="...",
    )
    body = json.loads(r.to_json())
    assert "paths_already_covered" in body
    assert body["paths_already_covered"] == []


# ── build_prompt contract ──────────────────────────────────────────────────


def test_build_prompt_mentions_paths_already_covered():
    """The bot's LLM reads the prompt + the inbox JSON. The prompt
    must tell it that ``paths_already_covered`` exists and what to
    do with it — otherwise the LLM ignores the field and over-builds."""
    prompt = bot_forge.build_prompt("j-1", "p-1")
    assert "paths_already_covered" in prompt
    assert "SKIP" in prompt.upper()


# ── Partial install — _install_partial_files_pack ──────────────────────────


@pytest.fixture
def partial_pack_on_disk(tmp_path: Path):
    """Lays out a files-pack with two files (so we can test allowlist
    filtering) and a target workspace."""
    pack = tmp_path / "files"
    (pack / "scripts").mkdir(parents=True)
    (pack / "scripts/foo.py").write_text("# foo\nprint('foo')\n")
    (pack / "scripts/bar.sh").write_text("#!/bin/bash\necho bar\n")
    os.chmod(pack / "scripts/foo.py", 0o644)
    os.chmod(pack / "scripts/bar.sh", 0o755)
    # Minimal valid metadata for load_files_pack_metadata.
    import hashlib
    foo_bytes = (pack / "scripts/foo.py").read_bytes()
    bar_bytes = (pack / "scripts/bar.sh").read_bytes()
    (pack / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-test", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [
            {"path": "scripts/foo.py", "mode": "0644",
             "sha256": hashlib.sha256(foo_bytes).hexdigest(),
             "size_bytes": len(foo_bytes), "placeholders": []},
            {"path": "scripts/bar.sh", "mode": "0755",
             "sha256": hashlib.sha256(bar_bytes).hexdigest(),
             "size_bytes": len(bar_bytes), "placeholders": []},
        ],
    }))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"pack_dir": pack, "workspace": workspace}


def _fake_job():
    """Minimal ForgeJob stand-in for _install_partial_files_pack —
    only job_id + context_snapshot are referenced."""
    from evolve_admin.applications.forge_jobs import ForgeJob
    return ForgeJob(
        job_id="j-partial", run_id="r-00000001", job_type="install",
        pkg_id="p-test", app_id="app-test", bot_id="team-bot-a",
        pkg_version_before=None, gallery_version=None,
    )


def test_partial_install_writes_bundled_subset_only(
    tmp_path: Path, partial_pack_on_disk,
):
    job = _fake_job()
    plan = {
        "files_pack_dir": str(partial_pack_on_disk["pack_dir"]),
        "workspace": str(partial_pack_on_disk["workspace"]),
        "context": {},
        "bundled_paths": ["scripts/foo.py"],   # foo bundled, bar is forge
        "forge_paths": ["scripts/bar.sh"],
    }
    written = _install_partial_files_pack(job, plan, tmp_path)

    assert any(e["path"] == "scripts/foo.py" for e in written)
    # bar should NOT have been installed by the partial helper.
    bar_paths = [e["path"] for e in written if e["path"] == "scripts/bar.sh"]
    assert bar_paths == []
    assert (partial_pack_on_disk["workspace"] / "scripts/foo.py").is_file()
    assert not (partial_pack_on_disk["workspace"] / "scripts/bar.sh").exists()


def test_partial_install_empty_bundled_paths_returns_empty(
    tmp_path: Path, partial_pack_on_disk,
):
    job = _fake_job()
    plan = {
        "files_pack_dir": str(partial_pack_on_disk["pack_dir"]),
        "workspace": str(partial_pack_on_disk["workspace"]),
        "context": {},
        "bundled_paths": [],
        "forge_paths": ["scripts/foo.py", "scripts/bar.sh"],
    }
    written = _install_partial_files_pack(job, plan, tmp_path)
    assert written == []
    # Nothing landed in the workspace.
    assert not (partial_pack_on_disk["workspace"] / "scripts/foo.py").exists()


def test_partial_install_bad_plan_shape_returns_empty(tmp_path: Path):
    """Plan missing required keys → log + return []. Never raises;
    the LLM-forge result has already been accepted upstream."""
    job = _fake_job()
    written = _install_partial_files_pack(job, {}, tmp_path)
    assert written == []


def test_partial_install_missing_files_pack_dir_returns_empty(
    tmp_path: Path,
):
    job = _fake_job()
    plan = {
        "files_pack_dir": str(tmp_path / "does-not-exist"),
        "workspace": str(tmp_path / "ws"),
        "context": {},
        "bundled_paths": ["scripts/foo.py"],
        "forge_paths": [],
    }
    (tmp_path / "ws").mkdir()
    written = _install_partial_files_pack(job, plan, tmp_path)
    # Should NOT raise — the dispatcher already accepted LLM output upstream.
    assert written == []


# ── Stash-on-partial: _maybe_install_via_files_pack ─────────────────────────


def test_dispatcher_stashes_partial_plan_on_context_snapshot(
    tmp_path: Path, monkeypatch,
):
    """Smart-forge dispatcher detects partial coverage and stashes the
    install plan in job.context_snapshot['files_pack_partial'] so the
    Step 2 dispatcher can finish the bundled install after LLM-forge."""
    from evolve_admin.applications.forge_jobs import ForgeJob

    # Set up the same fixture as the existing dispatcher tests use.
    gallery = tmp_path / "gallery"
    app = gallery / "task-manager"
    app.mkdir(parents=True)
    (app / "p-9bfa1c84.json").write_text("{}")
    pack_dir = app / "files"
    (pack_dir / "scripts").mkdir(parents=True)
    (pack_dir / "scripts/tasks.py").write_text("print('ok')\n")
    os.chmod(pack_dir / "scripts/tasks.py", 0o644)
    import hashlib
    pack_bytes = (pack_dir / "scripts/tasks.py").read_bytes()
    (pack_dir / "manifest.json").write_text(json.dumps({
        "format_version": "1.0",
        "snapshot_source": {
            "pkg_id": "p-9bfa1c84", "pkg_version": "1.0",
            "snapshot_at": "2026-06-04T00:00:00Z",
        },
        "files": [
            {"path": "scripts/tasks.py", "mode": "0644",
             "sha256": hashlib.sha256(pack_bytes).hexdigest(),
             "size_bytes": len(pack_bytes), "placeholders": []},
        ],
    }))
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

    job = ForgeJob(
        job_id="j-partial", run_id="r-00000001", job_type="install",
        pkg_id="p-9bfa1c84", app_id="task-manager",
        bot_id="personal-bot",
        pkg_version_before=None, gallery_version=None,
    )
    m = ApplicationManifest(
        id="task-manager", name="Task Manager", bot_id="personal-bot",
        pkg_id="p-9bfa1c84",
        files=[
            {"path": "scripts/tasks.py", "provenance": "bundled"},
            {"path": "HEARTBEAT.template.md", "provenance": "forge"},
        ],
        files_pack={
            "format_version": "1.0",
            "files_count": 1,
            "snapshot_source_pkg_version": "1.0",
            "sha256": "ignored-in-test",
        },
    )

    # _maybe_install_via_files_pack returns None for partial mode (so the
    # Step 2 dispatcher continues to LLM-forge); the plan lands on
    # context_snapshot for the caller to consume.
    assert _maybe_install_via_files_pack(job, m, tmp_path) is None
    plan = job.context_snapshot.get("files_pack_partial")
    assert plan is not None
    assert sorted(plan["bundled_paths"]) == ["scripts/tasks.py"]
    assert sorted(plan["forge_paths"]) == ["HEARTBEAT.template.md"]
    assert "files_pack_dir" in plan
    assert "workspace" in plan
    assert "context" in plan
    # Workspace MUST NOT have anything written yet — bundled install
    # happens AFTER LLM-forge to keep bundled-content authoritative.
    assert not (workspace / "scripts/tasks.py").exists()

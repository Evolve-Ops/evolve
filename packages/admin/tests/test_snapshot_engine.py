"""Tests for the snapshot engine (F-P.7.a).

The engine extracts the F-P.3 snapshot-files-pack CLI's core logic
into a callable function so the admin daemon endpoint can drive
the same workflow as the CLI without subprocess shenanigans.

The CLI's own tests (test_cli_snapshot_files_pack.py) cover the
Click wrapper + output formatting. These tests cover the engine
directly: envelope shape, error code stability, placeholder
substitution semantics including the F-P.3.x bare-token pass.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import snapshot_engine  # noqa: E402
from evolve_admin.applications.files_pack import (  # noqa: E402
    load_files_pack_metadata,
)


@pytest.fixture
def pod_layout(tmp_path: Path, monkeypatch):
    """Fake pod tree with one source bot and one installed app.

    Mirrors the CLI test fixture but exposes the engine inputs directly
    (network dict, bot_user, workspace path) instead of monkeypatching
    config.* like the CLI tests do.
    """
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    # Two source files.
    (workspace / "scripts/tasks.py").write_text(
        "# pure python — no bot-specific bits\n"
        "print('ok')\n"
    )
    os.chmod(workspace / "scripts/tasks.py", 0o644)
    (workspace / "scripts/task-check.sh").write_text(
        "#!/bin/bash\n"
        f"WORKSPACE=/Users/{bot_user}/.openclaw/workspace\n"
        f"LABEL=com.{bot_user}.task-check\n"
        f"_BOT_ID = \"{bot_user}\"\n"
    )
    os.chmod(workspace / "scripts/task-check.sh", 0o755)

    manifest_data = {
        "id": "task-manager",
        "name": "Task Manager",
        "bot_id": bot_user,
        "pkg_id": "p-9bfa1c84",
        "pkg_version": "2026.06.03-1.4",
        "files": [
            {"path": "scripts/tasks.py", "sha256": "irrelevant"},
            {"path": "scripts/task-check.sh", "sha256": "irrelevant"},
        ],
    }
    (workspace / "manifests" / "task-manager.json").write_text(
        json.dumps(manifest_data)
    )

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    return {
        "pod": pod,
        "workspace": workspace,
        "bot_user": bot_user,
        "network": {"bots": {bot_user: {"user": bot_user}}},
    }


# ── Input validation ────────────────────────────────────────────────────────


def test_unknown_bot_returns_envelope_with_unknown_bot_code(
    tmp_path: Path, pod_layout,
):
    res = snapshot_engine.snapshot_installed_app(
        bot_id="no-such-bot",
        pkg_id="p-9bfa1c84",
        out_dir=tmp_path / "out",
        network=pod_layout["network"],
    )
    assert res["ok"] is False
    assert res["error"].startswith("unknown_bot:")
    assert res["out_dir"] == ""


def test_missing_workspace_returns_workspace_missing_code(
    tmp_path: Path, pod_layout, monkeypatch,
):
    # Point bot_home at a path that doesn't have an .openclaw/workspace.
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: tmp_path / "no-such-pod" / bid,
    )
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=tmp_path / "out",
        network=pod_layout["network"],
    )
    assert res["ok"] is False
    assert res["error"].startswith("workspace_missing:")


def test_no_matching_manifest_returns_manifest_not_found(
    tmp_path: Path, pod_layout,
):
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-no-such-pkg",
        out_dir=tmp_path / "out",
        network=pod_layout["network"],
    )
    assert res["ok"] is False
    assert res["error"].startswith("manifest_not_found:")


def test_non_empty_out_without_force_returns_out_dir_not_empty(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("prior run")
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    assert res["ok"] is False
    assert res["error"].startswith("out_dir_not_empty:")
    # Stale file preserved (engine doesn't clobber without force).
    assert (out_dir / "stale.txt").exists()


def test_force_overwrites_existing_out_dir(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("prior run")
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        force=True,
        network=pod_layout["network"],
    )
    assert res["ok"] is True
    assert not (out_dir / "stale.txt").exists()
    assert (out_dir / "manifest.json").exists()


# ── Happy-path envelope shape ───────────────────────────────────────────────


def test_success_envelope_carries_expected_fields(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    assert res["ok"] is True
    assert res["error"] == ""
    assert res["out_dir"] == str(out_dir)
    assert res["files_count"] == 2
    assert res["format_version"] == "1.0"
    assert res["snapshot_source_pkg_version"] == "2026.06.03-1.4"
    assert len(res["sha256"]) == 64  # SHA-256 hex digest
    assert isinstance(res["per_file"], list)
    assert len(res["per_file"]) == 2
    # Each per_file entry has the documented shape.
    for entry in res["per_file"]:
        assert "path" in entry
        assert "mode" in entry
        assert "sha256" in entry
        assert "size_bytes" in entry
        assert "placeholders" in entry
        assert "bare_token_count" in entry


def test_internal_manifest_omits_bot_id_from_snapshot_source(
    tmp_path: Path, pod_layout,
):
    """The internal files/manifest.json's snapshot_source MUST NOT
    include bot_id — it would leak reserved-token source-bot ids
    into the gallery when the files-pack lands."""
    out_dir = tmp_path / "out"
    snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    meta = load_files_pack_metadata(out_dir)
    assert "bot_id" not in meta.snapshot_source
    assert meta.snapshot_source["pkg_id"] == "p-9bfa1c84"


# ── Placeholder detection — F-P.3 + F-P.3.x ─────────────────────────────────


def test_auto_detects_workspace_and_dotted_identifier(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    body = (out_dir / "scripts/task-check.sh").read_text()
    assert "{workspace}" in body
    assert "com.{bot_id}." in body


def test_bare_token_detector_substitutes_naked_bot_id(
    tmp_path: Path, pod_layout,
):
    """The fixture's task-check.sh has `_BOT_ID = "team-bot-a"` — the
    F-P.3.x bare-token pass should rewrite it to `{bot_id}` and add
    bot_id to the per-file placeholders[]."""
    out_dir = tmp_path / "out"
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    body = (out_dir / "scripts/task-check.sh").read_text()
    assert '_BOT_ID = "{bot_id}"' in body
    # Engine reports the bare-token count for operator review.
    sh_entry = next(
        e for e in res["per_file"] if e["path"] == "scripts/task-check.sh"
    )
    assert sh_entry["bare_token_count"] >= 1
    assert "bot_id" in sh_entry["placeholders"]


def test_no_auto_detect_disables_all_substitution(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        auto_detect=False,
        network=pod_layout["network"],
    )
    body = (out_dir / "scripts/task-check.sh").read_text()
    # All source-bot literals preserved verbatim.
    assert pod_layout["bot_user"] in body
    assert "{workspace}" not in body
    assert "{bot_id}" not in body
    # All per_file entries report no placeholders + zero bare-token counts.
    for entry in res["per_file"]:
        assert entry["placeholders"] == []
        assert entry["bare_token_count"] == 0


def test_short_bot_id_skips_bare_token_pass(tmp_path: Path, monkeypatch):
    """Min-length-3 guard: 2-char bot_id doesn't trigger the bare-token
    pass even when the literal appears in content."""
    pod = tmp_path / "Users"
    short = "xy"
    workspace = pod / short / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts/foo.py").write_text("AXIS = ['xy']\n")
    os.chmod(workspace / "scripts/foo.py", 0o644)
    (workspace / "manifests/x.json").write_text(json.dumps({
        "id": "x", "name": "X", "bot_id": short,
        "pkg_id": "p-xy", "pkg_version": "1.0",
        "files": [{"path": "scripts/foo.py", "sha256": "x"}],
    }))
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    out_dir = tmp_path / "out"
    res = snapshot_engine.snapshot_installed_app(
        bot_id=short, pkg_id="p-xy", out_dir=out_dir,
        network={"bots": {short: {"user": short}}},
    )
    assert res["ok"] is True
    body = (out_dir / "scripts/foo.py").read_text()
    # 'xy' preserved verbatim — below the min-length-3 guard.
    assert "'xy'" in body
    assert "{bot_id}" not in body


def test_missing_files_on_disk_reported_as_skipped(
    tmp_path: Path, pod_layout,
):
    """If the installed manifest references a file that's been deleted
    from disk, the engine should skip it and report in ``skipped[]``."""
    # Add a manifest entry for a file we never created.
    manifest_path = (
        pod_layout["workspace"] / "manifests" / "task-manager.json"
    )
    m = json.loads(manifest_path.read_text())
    m["files"].append({"path": "scripts/ghost.py", "sha256": "x"})
    manifest_path.write_text(json.dumps(m))

    out_dir = tmp_path / "out"
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=out_dir,
        network=pod_layout["network"],
    )
    assert res["ok"] is True
    # Engine recorded the missing file.
    skipped_paths = {s["path"] for s in res["skipped"]}
    assert "scripts/ghost.py" in skipped_paths
    # And the surviving files still landed.
    assert res["files_count"] == 2


def test_engine_loads_network_when_not_provided(
    tmp_path: Path, pod_layout, monkeypatch,
):
    """When ``network`` is omitted, the engine calls
    ``config.load_network`` itself. Verify the path works."""
    captured = {}

    def fake_load_network(path=None):
        captured["called"] = True
        return pod_layout["network"]

    monkeypatch.setattr(
        "evolve_admin.config.load_network", fake_load_network,
    )
    res = snapshot_engine.snapshot_installed_app(
        bot_id=pod_layout["bot_user"],
        pkg_id="p-9bfa1c84",
        out_dir=tmp_path / "out",
    )
    assert res["ok"] is True
    assert captured.get("called") is True

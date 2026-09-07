"""Tests for the ``evolve-admin snapshot-files-pack`` CLI subcommand.

Spec: internal/spec-files-pack-hybrid-2026-06-03.md §8.

Stubs the network + workspace + manifest layout so the CLI runs
without touching real bot accounts. Asserts:
  * argument validation (bot/pkg required, bot in network.json)
  * source-bot manifest lookup (by pkg_id in workspace/manifests/)
  * file copy with mode preservation + SHA computation
  * placeholder auto-detection rewrites source-bot tokens
  * --no-auto-detect leaves content untouched
  * --force overwrites existing output
  * output manifest.json shape conforms to F-P.1's loader
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import cli  # noqa: E402
from evolve_admin.applications.files_pack import (  # noqa: E402
    load_files_pack_metadata,
)


@pytest.fixture
def pod_layout(tmp_path: Path, monkeypatch):
    """Fake pod tree with one source bot and one installed app."""
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    # The bot's installed manifest references two files.
    (workspace / "scripts/tasks.py").write_text(
        "# pure python — no bot-specific bits\n"
        "print('ok')\n"
    )
    os.chmod(workspace / "scripts/tasks.py", 0o644)
    (workspace / "scripts/task-check.sh").write_text(
        "#!/bin/bash\n"
        f"WORKSPACE=/Users/{bot_user}/.openclaw/workspace\n"
        f"LABEL=com.{bot_user}.task-check\n"
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

    # Set up the builtin gallery so the CLI can resolve the default
    # --out path.
    gallery = tmp_path / "gallery"
    (gallery / "task-manager").mkdir(parents=True)
    (gallery / "task-manager" / "p-9bfa1c84.json").write_text(
        json.dumps({"pkg_id": "p-9bfa1c84"})
    )

    # Patch the CLI's network + bot_home + gallery resolution.
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {bot_user: {"user": bot_user}}},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    return {
        "pod": pod,
        "workspace": workspace,
        "gallery": gallery,
        "bot_user": bot_user,
    }


def _invoke(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


# ── Argument validation ─────────────────────────────────────────────────────


def test_snapshot_requires_bot_and_pkg(pod_layout):
    res = _invoke("snapshot-files-pack")
    assert res.exit_code != 0
    assert "--bot" in res.output or "--pkg" in res.output


def test_snapshot_rejects_unknown_bot(pod_layout):
    res = _invoke(
        "snapshot-files-pack", "--bot", "no-such", "--pkg", "p-9bfa1c84",
    )
    assert res.exit_code == 2
    assert "not in network.json" in res.output


def test_snapshot_rejects_pkg_not_in_workspace(tmp_path: Path, pod_layout):
    # Pass an explicit --out so the CLI's gallery-dir resolution
    # doesn't pre-empt the engine's manifest lookup with its own
    # "no gallery directory contains p-doesnotexist.json" error.
    res = _invoke(
        "snapshot-files-pack", "--bot", pod_layout["bot_user"],
        "--pkg", "p-doesnotexist",
        "--out", str(tmp_path / "out"),
    )
    assert res.exit_code == 2
    # Engine error code surfaces in CLI output (F-P.7.a refactor:
    # CLI prints the engine envelope's ``error`` field verbatim).
    assert "manifest_not_found" in res.output


# ── Happy path ──────────────────────────────────────────────────────────────


def test_snapshot_writes_files_with_modes_and_metadata(tmp_path: Path, pod_layout):
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 0, res.output

    # Both source files landed.
    assert (out_dir / "scripts/tasks.py").is_file()
    assert (out_dir / "scripts/task-check.sh").is_file()

    # Modes preserved.
    mode_644 = (out_dir / "scripts/tasks.py").stat().st_mode & 0o777
    mode_755 = (out_dir / "scripts/task-check.sh").stat().st_mode & 0o777
    assert mode_644 == 0o644
    assert mode_755 == 0o755

    # Output manifest.json round-trips through the F-P.1 loader.
    meta = load_files_pack_metadata(out_dir)
    assert meta is not None
    assert meta.format_version == "1.0"
    # snapshot_source intentionally omits bot_id (would leak reserved
    # tokens into the gallery; see F-P.7 PR notes for rationale).
    assert "bot_id" not in meta.snapshot_source
    assert meta.snapshot_source["pkg_id"] == "p-9bfa1c84"
    assert {f.path for f in meta.files} == {
        "scripts/tasks.py", "scripts/task-check.sh",
    }


def test_snapshot_auto_detects_workspace_placeholder(tmp_path: Path, pod_layout):
    """The task-check.sh contains `/Users/<bot>/.openclaw/workspace`
    and `com.<bot>.task-check` — both should get rewritten to
    placeholders with the names declared in the metadata."""
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 0, res.output

    # task-check.sh got rewritten.
    body = (out_dir / "scripts/task-check.sh").read_text()
    assert "{workspace}" in body
    assert "com.{bot_id}." in body
    # And the source-bot literal is gone.
    assert pod_layout["bot_user"] not in body

    # tasks.py has no source-bot tokens — should pass through verbatim
    # with empty placeholders[].
    meta = load_files_pack_metadata(out_dir)
    tasks_entry = next(f for f in meta.files if f.path == "scripts/tasks.py")
    sh_entry = next(f for f in meta.files if f.path == "scripts/task-check.sh")
    assert tasks_entry.placeholders == []
    # task-check declared at least one placeholder.
    assert set(sh_entry.placeholders) >= {"workspace", "bot_id"}


def test_snapshot_no_auto_detect_leaves_content_untouched(tmp_path: Path, pod_layout):
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
        "--no-auto-detect",
    )
    assert res.exit_code == 0, res.output

    # The source-bot literal still appears (operator will edit by hand).
    body = (out_dir / "scripts/task-check.sh").read_text()
    assert pod_layout["bot_user"] in body
    assert "{workspace}" not in body
    # And the per-file placeholders[] list is empty for everything.
    meta = load_files_pack_metadata(out_dir)
    for entry in meta.files:
        assert entry.placeholders == []


def test_snapshot_refuses_to_overwrite_non_empty_out_without_force(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("from a prior run")
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 2
    # Engine error code surfaces in CLI output.
    assert "out_dir_not_empty" in res.output
    # Old file preserved.
    assert (out_dir / "stale.txt").read_text() == "from a prior run"


def test_snapshot_force_overwrites_existing(tmp_path: Path, pod_layout):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("from a prior run")
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
        "--force",
    )
    assert res.exit_code == 0
    # Stale content swept.
    assert not (out_dir / "stale.txt").exists()
    # New content present.
    assert (out_dir / "manifest.json").is_file()


def test_snapshot_writes_to_default_gallery_path_when_no_out(
    tmp_path: Path, pod_layout,
):
    """Without --out, the CLI writes to <gallery>/<slug>/files/."""
    expected_dir = pod_layout["gallery"] / "task-manager" / "files"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
    )
    assert res.exit_code == 0, res.output
    assert (expected_dir / "manifest.json").is_file()


def test_snapshot_metadata_carries_pkg_version_from_source(
    tmp_path: Path, pod_layout,
):
    out_dir = tmp_path / "out"
    _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    meta = load_files_pack_metadata(out_dir)
    # The CLI should record which pkg_version this snapshot was taken
    # against so operators can detect drift.
    assert meta.snapshot_source.get("pkg_version") == "2026.06.03-1.4"


# ── Bare-token bot_id detector (F-P.3.x) ────────────────────────────────────
#
# F-P.5 exposed that the original three detector patterns (workspace path,
# bot_user root path, com.<bot_id>. dotted identifier) only caught bot_id in
# path/identifier contexts — bare-token occurrences like `_BOT_ID = "atlas"`
# and example CLI lines `--owner atlas` slipped through and had to be
# hand-edited. F-P.3.x adds a fourth pass: \b<bot_id>\b → {bot_id}.
#
# Risk: word-boundary regex catches partial-word matches but a bot_id that
# coincides with a real English word would false-positive. Mitigations:
#   1. Min-length guard (3 chars) skips ultra-short bot_ids
#   2. Substituted count surfaced per-file in CLI output for operator review
#   3. --no-auto-detect remains the escape hatch


@pytest.fixture
def pod_layout_with_bare_tokens(tmp_path: Path, monkeypatch):
    """Pod tree where files contain bare-token occurrences of bot_id.

    Mirrors the F-P.5 reality: scripts/tasks.py has `_BOT_ID = "team-bot-a"`
    and TASKS.md has `--owner team-bot-a` example lines, plus a
    partial-word `team-bot-a_helper` that MUST NOT be substituted.
    """
    pod = tmp_path / "Users"
    bot_user = "team-bot-a"
    workspace = pod / bot_user / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)

    # tasks.py: bare bot_id assignment + partial-word that must NOT match.
    (workspace / "scripts/tasks.py").write_text(
        '_BOT_ID = "team-bot-a"\n'
        "# helper class — name contains bot_id as substring but with\n"
        "# underscore suffix so word-boundary regex should reject it.\n"
        "class team_bot_a_helper:\n"
        "    pass\n"
    )
    os.chmod(workspace / "scripts/tasks.py", 0o644)

    # TASKS.md: two bare-token example lines + a path-shaped one the
    # original patterns also catch (so we verify the bare-token pass
    # runs AFTER path-shaped substitution and doesn't double-count).
    (workspace / "TASKS.md").write_text(
        "# Task Manager\n"
        "\n"
        "Example invocations:\n"
        "  tasks.py list --owner team-bot-a --status open\n"
        '  tasks.py update OP-0001 --owner "team-bot-a"\n'
        "\n"
        f"Workspace: /Users/{bot_user}/.openclaw/workspace\n"
    )
    os.chmod(workspace / "TASKS.md", 0o644)

    manifest_data = {
        "id": "task-manager",
        "name": "Task Manager",
        "bot_id": bot_user,
        "pkg_id": "p-9bfa1c84",
        "pkg_version": "2026.06.03-1.4",
        "files": [
            {"path": "scripts/tasks.py", "sha256": "irrelevant"},
            {"path": "TASKS.md", "sha256": "irrelevant"},
        ],
    }
    (workspace / "manifests" / "task-manager.json").write_text(
        json.dumps(manifest_data)
    )

    gallery = tmp_path / "gallery"
    (gallery / "task-manager").mkdir(parents=True)
    (gallery / "task-manager" / "p-9bfa1c84.json").write_text(
        json.dumps({"pkg_id": "p-9bfa1c84"})
    )

    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {bot_user: {"user": bot_user}}},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )
    return {
        "pod": pod,
        "workspace": workspace,
        "gallery": gallery,
        "bot_user": bot_user,
    }


def test_snapshot_substitutes_bare_bot_id_tokens(
    tmp_path: Path, pod_layout_with_bare_tokens,
):
    """The F-P.5 manual-fix gap: `_BOT_ID = "team-bot-a"` and
    `--owner team-bot-a` example lines must be auto-substituted to
    `{bot_id}` and bot_id added to the per-file placeholders[]."""
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout_with_bare_tokens["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 0, res.output

    tasks_body = (out_dir / "scripts/tasks.py").read_text()
    assert '_BOT_ID = "{bot_id}"' in tasks_body, tasks_body
    # Bare bot_id is gone; partial-word stays put.
    assert '"team-bot-a"' not in tasks_body
    # Word-boundary mitigation: partial-word `team_bot_a_helper` is
    # different (underscore-separated) but the original class name is
    # `team_bot_a_helper` which contains "team_bot_a" — not the same as
    # "team-bot-a" so regex won't match. Verify it survives verbatim.
    assert "class team_bot_a_helper" in tasks_body

    tasks_md_body = (out_dir / "TASKS.md").read_text()
    assert "--owner {bot_id}" in tasks_md_body
    assert '"{bot_id}"' in tasks_md_body
    # Bare-token pass ran AFTER path substitution; the workspace path
    # also got rewritten by the original pattern.
    assert "{workspace}" in tasks_md_body

    meta = load_files_pack_metadata(out_dir)
    tasks_entry = next(f for f in meta.files if f.path == "scripts/tasks.py")
    md_entry = next(f for f in meta.files if f.path == "TASKS.md")
    assert "bot_id" in tasks_entry.placeholders
    assert "bot_id" in md_entry.placeholders


def test_snapshot_surfaces_bare_token_count_in_summary(
    tmp_path: Path, pod_layout_with_bare_tokens,
):
    """Operator sees how many bare-token substitutions happened per
    file so they can spot false positives (e.g. bot_id matching a
    common English word in markdown prose)."""
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout_with_bare_tokens["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 0, res.output
    # tasks.py has exactly one bare-token occurrence.
    assert "1 bare bot_id token substituted" in res.output, res.output
    # TASKS.md has two bare-token occurrences.
    assert "2 bare bot_id tokens substituted" in res.output, res.output


def test_snapshot_skips_bare_token_pass_for_short_bot_id(
    tmp_path: Path, monkeypatch,
):
    """Ultra-short bot_ids (< 3 chars) are skipped to avoid false
    positives — no real bot_id is shorter than "evo", so this is a
    safety net rather than a real limit."""
    pod = tmp_path / "Users"
    short = "xy"  # 2 chars — below the threshold.
    workspace = pod / short / ".openclaw" / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "scripts").mkdir(parents=True)
    # A file where the short bot_id appears as a common English token
    # (in "x" "y" axis labels). With min-length guard, this is left
    # untouched.
    (workspace / "scripts/foo.py").write_text(
        "AXIS = ['xy']  # not a bot_id literal\n"
    )
    os.chmod(workspace / "scripts/foo.py", 0o644)
    manifest_data = {
        "id": "task-manager",
        "name": "Task Manager",
        "bot_id": short,
        "pkg_id": "p-9bfa1c84",
        "pkg_version": "2026.06.03-1.4",
        "files": [{"path": "scripts/foo.py", "sha256": "irrelevant"}],
    }
    (workspace / "manifests" / "task-manager.json").write_text(
        json.dumps(manifest_data)
    )
    gallery = tmp_path / "gallery"
    (gallery / "task-manager").mkdir(parents=True)
    (gallery / "task-manager" / "p-9bfa1c84.json").write_text(
        json.dumps({"pkg_id": "p-9bfa1c84"})
    )
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bid, network=None: pod / bid,
    )
    monkeypatch.setattr(
        "evolve_admin.config.load_network",
        lambda *a, **kw: {"bots": {short: {"user": short}}},
    )
    monkeypatch.setattr(
        "evolve_admin.applications.gallery._BUILTIN_GALLERY_DIR",
        gallery,
    )

    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", short,
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
    )
    assert res.exit_code == 0, res.output

    # 'xy' was NOT substituted — file unchanged.
    body = (out_dir / "scripts/foo.py").read_text()
    assert "'xy'" in body
    assert "{bot_id}" not in body
    meta = load_files_pack_metadata(out_dir)
    foo_entry = next(f for f in meta.files if f.path == "scripts/foo.py")
    assert "bot_id" not in foo_entry.placeholders


def test_snapshot_no_auto_detect_skips_bare_token_pass_too(
    tmp_path: Path, pod_layout_with_bare_tokens,
):
    """--no-auto-detect is the escape hatch for any operator who hits a
    false positive — verify it disables bare-token substitution along
    with the other three patterns."""
    out_dir = tmp_path / "out"
    res = _invoke(
        "snapshot-files-pack",
        "--bot", pod_layout_with_bare_tokens["bot_user"],
        "--pkg", "p-9bfa1c84",
        "--out", str(out_dir),
        "--no-auto-detect",
    )
    assert res.exit_code == 0, res.output
    # All bare tokens preserved verbatim.
    tasks_body = (out_dir / "scripts/tasks.py").read_text()
    assert '_BOT_ID = "team-bot-a"' in tasks_body
    assert "{bot_id}" not in tasks_body
    tasks_md_body = (out_dir / "TASKS.md").read_text()
    assert "--owner team-bot-a" in tasks_md_body
    assert "{bot_id}" not in tasks_md_body
    # Summary should not mention bare-token substitution.
    assert "bare bot_id token" not in res.output

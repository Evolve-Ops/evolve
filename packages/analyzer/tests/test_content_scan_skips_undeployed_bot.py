"""Regression: content_scan must not red-flag registered-but-undeployed bots.

`evolve-admin add-bot --no-deploy` adds a bot to network.json ``members``
("register intent, deploy when the host is ready") without running
``openclaw onboard`` or a deploy — the bot's macOS account may not even exist
yet, so it has no ``~/.openclaw/workspace`` directory at all. Before this
guard, content_scan walked every member and fired a red
``content_scan_file_disappeared`` for each of the eight required per-bot files
(AGENTS.md, SOUL.md, …) on such a bot — eight alerts of pure noise for a bot
that was deliberately not deployed.

Distinct from the workspace-*producing* gap closed by PR #2906
(``provision_bot(skip_deploy=True)`` seeds docs): you cannot seed docs for a
nonexistent account. The fix here is for *registration-only* bots — the scan
skips a bot whose workspace directory is cleanly absent (not-yet-scannable),
while still surfacing a genuinely-deleted file on a *deployed* bot.

Owner: META:reports (signal-producer quality).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from content_scan import catalog as _catalog
from content_scan import scanner as _scanner


def _seed_pod_files(shared_dir: Path) -> None:
    """Create the pod-wide scanned files (POD_CONDUCT.md, …) under shared_dir so
    the pod-wide leg of the scan is clean and doesn't contribute its own
    file_disappeared findings to the per-bot assertions below."""
    cat = _catalog.load(shared_dir)
    for fname in cat.scope.scanned_pod_files:
        (shared_dir / fname).write_text("clean content with no patterns\n")


def _seed_workspace_with_required_files(shared_dir: Path, workspace: Path) -> None:
    """Create a deployed-bot workspace holding every scanned per-bot file so
    the deployed bot scans clean (no file_disappeared findings of its own)."""
    workspace.mkdir(parents=True)
    cat = _catalog.load(shared_dir)
    for fname in cat.scope.scanned_files_per_bot:
        (workspace / fname).write_text("clean content with no patterns\n")


def test_run_skips_bot_without_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    # The catalog is written on first run(); write it now so we can read the
    # per-bot file list to seed the deployed bot's workspace.
    _catalog.write_default_if_missing(shared_dir)
    _seed_pod_files(shared_dir)

    deployed_home = tmp_path / "homes" / "deployed"
    _seed_workspace_with_required_files(
        shared_dir, deployed_home / ".openclaw" / "workspace"
    )

    # Registered-but-undeployed bot: its home (and thus workspace) never exists.
    undeployed_home = tmp_path / "homes" / "undeployed"
    assert not undeployed_home.exists()

    homes = {"deployed": deployed_home, "undeployed": undeployed_home}
    monkeypatch.setattr(
        _scanner, "_bot_home", lambda bot_id, config=None: homes[bot_id]
    )

    result = _scanner.run(
        shared_dir, ["deployed", "undeployed"], {}, emit_signals=False
    )

    # The undeployed bot is skipped, the deployed bot is scanned.
    assert result["bots_checked"] == 1
    assert result["bots_skipped"] == 1
    # Crucially: zero content_scan_file_disappeared signals overall — neither
    # the (clean, fully-seeded) deployed bot nor the skipped undeployed bot
    # produced one.
    assert not any(
        f["type"] == "content_scan_file_disappeared" for f in result["findings"]
    )
    # And nothing at all is attributed to the undeployed bot.
    assert all(
        f["details"].get("bot_id") != "undeployed" for f in result["findings"]
    )


def test_run_still_flags_deleted_file_on_deployed_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not mask a real disappearance: a *deployed* bot (workspace
    present) that is missing one required file still fires
    content_scan_file_disappeared for exactly that file."""
    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    _catalog.write_default_if_missing(shared_dir)
    _seed_pod_files(shared_dir)

    home = tmp_path / "homes" / "deployed"
    workspace = home / ".openclaw" / "workspace"
    _seed_workspace_with_required_files(shared_dir, workspace)
    # Tamper: delete one required file from an otherwise-deployed workspace.
    (workspace / "SOUL.md").unlink()

    monkeypatch.setattr(_scanner, "_bot_home", lambda bot_id, config=None: home)

    result = _scanner.run(shared_dir, ["deployed"], {}, emit_signals=False)

    assert result["bots_checked"] == 1
    assert result["bots_skipped"] == 0
    disappeared = [
        f for f in result["findings"]
        if f["type"] == "content_scan_file_disappeared"
    ]
    assert len(disappeared) == 1
    assert disappeared[0]["details"]["file"] == "SOUL.md"
    assert disappeared[0]["details"]["bot_id"] == "deployed"


def test_workspace_present_only_false_on_clean_absence(tmp_path: Path) -> None:
    """_workspace_present returns True for an existing dir and False ONLY when
    the directory is cleanly absent — the positive-confirmation contract that
    keeps a permission glitch from silently suppressing a deployed bot's scan."""
    existing = tmp_path / "ws"
    existing.mkdir()
    assert _scanner._workspace_present(existing) is True

    missing = tmp_path / "nope" / ".openclaw" / "workspace"
    assert _scanner._workspace_present(missing) is False

    # A path whose final component exists but is a regular file (not a dir) is
    # treated as "not a workspace" → False.
    a_file = tmp_path / "afile"
    a_file.write_text("x")
    assert _scanner._workspace_present(a_file) is False

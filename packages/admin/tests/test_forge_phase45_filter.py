"""Tests for the Phase-2 / Phase-4.5 ownership split in forge_engine.

Phase 2 (bot build dispatch) writes workspace-relative build outputs;
Phase 4.5 (``_materialize_scheduled_actions``) writes side-effect install
artifacts (HEARTBEAT.md sections, LaunchAgent plists, …) from
``manifest.scheduled_actions[]``. The two domains never overlap.

The bug these tests pin: an improvement run on an app whose mechanism
swapped between gallery versions (e.g. ``launchd`` → ``oc_heartbeat_instruction``
per schema v17) carries a stale ``installed_artifact`` on the on-disk
manifest. If the bot echoes that path back in ``files_written``, Phase 2's
``verify_files_on_disk`` would fail "missing on disk" — even though the
new mechanism doesn't produce the old artifact.

Reference: docs/spec-heartbeat-instruction-2026-06-03.md §4 (Phase 4.5
ownership), §5 (A1 verifier). Concrete failure that motivated the fix:
forge job j-effe972d on 2026-06-03 (task-manager v17 improvement on
team-bot-c-equivalent, pkg p-9bfa1c84).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge, forge_engine  # noqa: E402
from evolve_admin.applications.forge_engine import (  # noqa: E402
    _phase45_owned_paths,
    _split_phase45_entries,
)
from evolve_admin.applications.forge_jobs import ForgeJob  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    MECHANISM_LAUNCHD,
    MECHANISM_OC_HEARTBEAT_INSTRUCTION,
    MECHANISM_OC_SESSION_INSTRUCTION,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _manifest(*actions: dict) -> ApplicationManifest:
    m = ApplicationManifest(
        id="task-manager", name="Task Manager", bot_id="personal-bot",
    )
    m.scheduled_actions = list(actions)
    return m


def _job() -> ForgeJob:
    return ForgeJob(
        job_id="j-test",
        run_id="r-00000001",
        job_type="improvement",
        pkg_id="p-9bfa1c84",
        app_id="task-manager",
        bot_id="personal-bot",
        pkg_version_before="2026.06.01-1.0",
        gallery_version="2026.06.03-1.3",
    )


# ── _phase45_owned_paths ─────────────────────────────────────────────────────


def test_owned_paths_empty_manifest_returns_empty_set() -> None:
    assert _phase45_owned_paths(None, "personal-bot") == set()


def test_owned_paths_no_scheduled_actions_returns_empty_set() -> None:
    manifest = _manifest()
    assert _phase45_owned_paths(manifest, "personal-bot") == set()


def test_owned_paths_heartbeat_instruction_includes_file_and_anchor() -> None:
    """v17 mechanism: install.file + install.section_anchor produces both the
    bare file path AND the ``file#anchor`` artifact form."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "...",
        },
    }
    owned = _phase45_owned_paths(_manifest(action), "personal-bot")
    assert "HEARTBEAT.md" in owned
    assert "HEARTBEAT.md#Task Manager — Check" in owned


def test_owned_paths_session_instruction_targets_agents_md() -> None:
    action = {
        "id": "session-start",
        "mechanism": MECHANISM_OC_SESSION_INSTRUCTION,
        "install": {
            "file": "AGENTS.md",
            "section_anchor": "## Task Manager — Session Start",
            "body": "...",
        },
    }
    owned = _phase45_owned_paths(_manifest(action), "personal-bot")
    assert "AGENTS.md" in owned
    assert "AGENTS.md#Task Manager — Session Start" in owned


def test_owned_paths_launchd_includes_tilde_and_absolute_plist_paths() -> None:
    """Stale installed_artifact may use either ``~/Library/...`` or the
    absolute ``/Users/<bot>/Library/...`` form — Phase 2 filter must catch
    both so a swap-stale artifact doesn't trip verify_files_on_disk."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {
            "plist_label": "com.personal-bot.task-check",
            "plist_xml": "<?xml ?><plist><dict/></plist>",
        },
    }
    owned = _phase45_owned_paths(_manifest(action), "personal-bot")
    assert "~/Library/LaunchAgents/com.personal-bot.task-check.plist" in owned
    assert "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-check.plist" in owned


def test_owned_paths_includes_stale_installed_artifact() -> None:
    """Even when the recipe says HEARTBEAT.md (v17), the on-disk manifest's
    pre-swap installed_artifact (a launchd plist) is included in the owned
    set — the bug being fixed."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "...",
        },
        # Stale stamp from a pre-v17 launchd install that hasn't been cleared.
        "installed_artifact": "~/Library/LaunchAgents/com.personal-bot.task-check.plist",
    }
    owned = _phase45_owned_paths(_manifest(action), "personal-bot")
    # Both the new mechanism's target AND the stale plist are recognised.
    assert "HEARTBEAT.md" in owned
    assert "HEARTBEAT.md#Task Manager — Check" in owned
    assert "~/Library/LaunchAgents/com.personal-bot.task-check.plist" in owned


def test_owned_paths_handles_non_dict_action_entries() -> None:
    """Defensive: malformed scheduled_actions doesn't crash the filter."""
    manifest = _manifest()
    manifest.scheduled_actions = [
        "not a dict",
        {"id": "good", "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
         "install": {"file": "HEARTBEAT.md", "section_anchor": "## X", "body": "."}},
        None,
    ]
    owned = _phase45_owned_paths(manifest, "personal-bot")
    assert "HEARTBEAT.md" in owned
    assert "HEARTBEAT.md#X" in owned


# ── _split_phase45_entries ───────────────────────────────────────────────────


def test_split_empty_owned_set_returns_input_unchanged() -> None:
    entries = [{"path": "scripts/tasks.py", "sha256": "a" * 64}]
    bot_files, filtered = _split_phase45_entries(entries, set())
    assert bot_files == entries
    assert filtered == []


def test_split_partitions_by_path_membership() -> None:
    entries = [
        {"path": "scripts/tasks.py", "sha256": "a" * 64},
        {"path": "HEARTBEAT.md", "sha256": "b" * 64},
        {"path": "~/Library/LaunchAgents/com.x.y.plist", "sha256": "c" * 64},
        {"path": "TASKS.md", "sha256": "d" * 64},
    ]
    owned = {
        "HEARTBEAT.md",
        "~/Library/LaunchAgents/com.x.y.plist",
    }
    bot_files, filtered = _split_phase45_entries(entries, owned)
    assert [e["path"] for e in bot_files] == ["scripts/tasks.py", "TASKS.md"]
    assert sorted(filtered) == sorted([
        "HEARTBEAT.md",
        "~/Library/LaunchAgents/com.x.y.plist",
    ])


def test_split_drops_entries_with_no_path_field_into_bot_files() -> None:
    """An entry without a path is kept in bot_files so the downstream
    ``verify_files_on_disk`` produces its existing "no path" error rather
    than silently swallowing the malformed entry."""
    entries = [{"sha256": "a" * 64}]  # no path
    bot_files, filtered = _split_phase45_entries(entries, {"HEARTBEAT.md"})
    assert bot_files == entries
    assert filtered == []


# ── Integration: simulated improvement run with mechanism-swap stale data ────
#
# This reproduces the j-effe972d failure shape: pre-v17 install left a
# stale launchd plist artifact on the manifest, gallery republished as
# v17 oc_heartbeat_instruction, improvement bot dispatch returns files
# (including the stale plist path it echoed from the manifest), and the
# Phase-2 verification step must not fail on the stale entry.


def _write_workspace_file(workspace: Path, rel: str, content: str = "x\n") -> dict:
    """Create a file under the bot's workspace and return its files_written entry."""
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return {
        "path": rel,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "file_id": "f-" + ("0" * 8),
    }


def test_run_bot_dispatch_filters_stale_phase45_artifact(tmp_path, monkeypatch):
    """Regression: improvement run after a mechanism swap (launchd →
    oc_heartbeat_instruction) where the on-disk manifest's stale
    installed_artifact points at a no-longer-produced LaunchAgent plist.
    The bot echoes the stale path in files_written; Phase-2 verification
    must filter it out so the build proceeds, leaving Phase-4.5 + the
    post-apply A1 verifier to handle install artifacts.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    workspace = tmp_path / "bot-workspace"
    workspace.mkdir()

    # Three real bot-written files under the workspace (the bot's actual
    # output) + one stale Phase-4.5 plist path echoed from the manifest.
    bot_entry_1 = _write_workspace_file(workspace, "scripts/tasks.py")
    bot_entry_2 = _write_workspace_file(workspace, "scripts/task_updater.py")
    bot_entry_3 = _write_workspace_file(workspace, "TASKS.md")
    stale_plist_entry = {
        "path": "~/Library/LaunchAgents/com.personal-bot.task-check.plist",
        "sha256": "0" * 64,
        "file_id": "f-stale001",
    }

    # On-disk manifest carries the v17 mechanism on the action AND the stale
    # launchd installed_artifact from the pre-v17 install.
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
            "command": "python3 scripts/tasks.py check",
        },
        "installed_artifact": "~/Library/LaunchAgents/com.personal-bot.task-check.plist",
        "installed_by": "forge:j-earlier-job",
        "installed_at": "2026-06-01T18:37:00Z",
    }
    manifest = _manifest(action)
    manifest.pkg_id = "p-9bfa1c84"
    manifest.pkg_version = "2026.06.01-1.0"
    manifest.build_spec = "Build the task manager — see spec."

    fake_result = bot_forge.BuildResult(
        status="complete",
        files_written=[bot_entry_1, bot_entry_2, bot_entry_3, stale_plist_entry],
        test_run="python3 -m py_compile scripts/tasks.py",
        test_exit_code=0,
        test_output="",
        notes="",
        raw={},
        agent_exit_code=143,
    )

    monkeypatch.setattr(
        forge_engine, "load_manifest", lambda app_id, bot_id, shared_dir: manifest,
    )
    monkeypatch.setattr(
        forge_engine, "save_manifest", lambda m, sd: None,
    )
    monkeypatch.setattr(
        bot_forge, "bot_workspace", lambda bot_id: workspace,
    )
    monkeypatch.setattr(
        bot_forge, "dispatch_build", lambda bot_id, request, **kw: fake_result,
    )
    # Short-circuit the critique loop — irrelevant to this regression.
    monkeypatch.setattr(
        bot_forge, "dispatch_critique",
        lambda bot_id, req, **kw: bot_forge.CritiqueResult(
            status="complete", issues=[], notes="", raw={},
        ),
    )

    job = _job()

    # Phase-2 verification used to raise "Bot output verification failed:
    # missing on disk: ~/Library/LaunchAgents/..." — after the fix it
    # filters the stale entry and proceeds without raising.
    forge_engine._run_bot_dispatch(job, context={}, shared_dir=shared_dir,
                                    critique_rounds=1)

    # manifest.files should reflect only the real bot-written files —
    # the stale plist path must NOT leak in via build_manifest_file_records.
    recorded_paths = {
        (e.get("path") or "").lstrip("/")
        for e in (manifest.files or [])
    }
    assert "scripts/tasks.py" in recorded_paths
    assert "scripts/task_updater.py" in recorded_paths
    assert "TASKS.md" in recorded_paths
    assert not any("LaunchAgents" in p for p in recorded_paths)


def test_run_bot_dispatch_still_fails_on_real_missing_workspace_file(
    tmp_path, monkeypatch,
):
    """Negative case: a real workspace-relative file the bot CLAIMS to have
    written but didn't (legitimate bot error) still trips Phase-2
    verification. The filter mustn't paper over genuine missing-file bugs.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    workspace = tmp_path / "bot-workspace"
    workspace.mkdir()

    real = _write_workspace_file(workspace, "scripts/tasks.py")
    phantom = {
        "path": "scripts/never_written.py",
        "sha256": "0" * 64,
        "file_id": "f-phantom01",
    }

    manifest = _manifest({
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Check",
            "body": "...",
        },
    })
    manifest.pkg_id = "p-9bfa1c84"

    fake_result = bot_forge.BuildResult(
        status="complete",
        files_written=[real, phantom],
        test_run=None, test_exit_code=0, test_output="", notes="", raw={},
    )

    monkeypatch.setattr(forge_engine, "load_manifest",
                        lambda a, b, s: manifest)
    monkeypatch.setattr(forge_engine, "save_manifest", lambda m, s: None)
    monkeypatch.setattr(bot_forge, "bot_workspace", lambda b: workspace)
    monkeypatch.setattr(bot_forge, "dispatch_build",
                        lambda b, r, **kw: fake_result)

    with pytest.raises(RuntimeError, match="never_written.py"):
        forge_engine._run_bot_dispatch(_job(), context={}, shared_dir=shared_dir,
                                       critique_rounds=1)

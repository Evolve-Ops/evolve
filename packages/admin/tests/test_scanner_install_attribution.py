"""Tests for v16 scanner attribution of LaunchAgents + openclaw.json hooks.

PR 2 of spec-forge-side-effects-2026-06-02.md §6. The scanner enumerates
plist files under ``~/Library/LaunchAgents/`` and hook entries in
``openclaw.json``, then attributes each to a discovered app via Label
namespace + script-path matching. Attributed entries become
``scheduled_actions[]`` items on the app's manifest with the v16
``mechanism`` + ``install`` + ``installed_artifact`` fields populated.

Focus areas:

  * ``_attribute_launchd_to_app`` — Label namespace match (``com.bot.app.*``)
    and ProgramArguments script-path match (basename + full path).
  * ``_attribute_hook_to_app`` — command path matches app's evidence files.
  * ``_build_scheduled_action_from_launchd`` produces a v16 entry with
    ``mechanism=launchd``, ``install.plist_label``, ``installed_artifact``
    pointing to the plist path, and a sensible schedule string for both
    StartInterval and StartCalendarInterval.
  * ``_build_scheduled_action_from_hook`` produces ``mechanism=oc_heartbeat_hook``
    for the ``heartbeat`` event and ``oc_session_hook`` for others, with
    ``installed_artifact`` as a JSON-pointer-shaped fragment.
  * The two helpers are wired into ``generate_manifest_for_app`` so a
    populated ``inventory.launchd_entries`` produces matching scheduled_actions
    entries on the output manifest.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    MECHANISM_LAUNCHD,
    MECHANISM_OC_HEARTBEAT_HOOK,
    MECHANISM_OC_SESSION_HOOK,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_app(app_id: str, evidence: list[str] | None = None) -> _scanner.DetectedApplication:
    return _scanner.DetectedApplication(
        id=app_id,
        name=app_id.replace("-", " ").title(),
        confidence=0.9,
        evidence_files=evidence if evidence is not None else [f"scripts/{app_id}.py"],
        evidence_summary=f"Discovered {app_id}.",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
    )


# ── _attribute_launchd_to_app ────────────────────────────────────────────────


def test_attribute_launchd_matches_label_namespace_convention():
    """com.{bot}.{app}.* Label is the forge install convention (spec §6.1 step 1)."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entry = {
        "label": "com.personal-bot.task-manager.check",
        "program_args": ["/bin/bash", "/some/unrelated/path.sh"],  # path won't match
    }
    assert _scanner._attribute_launchd_to_app(entry, app, "personal-bot")


def test_attribute_launchd_matches_label_with_underscore_variant():
    """Forge may produce ``task_manager`` instead of ``task-manager`` in Labels."""
    app = _make_app("task-manager", evidence=[])
    entry = {
        "label": "com.personal-bot.task_manager.check",
        "program_args": [],
    }
    assert _scanner._attribute_launchd_to_app(entry, app, "personal-bot")


def test_attribute_launchd_matches_by_program_arg_full_path():
    """Hand-installed crons typically have no namespace; attribute by script path."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entry = {
        "label": "team-bot-c.task-check",  # no namespace
        "program_args": [
            "/usr/bin/python3",
            "/Users/team-bot-c/.openclaw/workspace/scripts/tasks.py",
            "check",
        ],
    }
    assert _scanner._attribute_launchd_to_app(entry, app, "team-bot-c")


def test_attribute_launchd_matches_by_program_arg_basename():
    """Even when the full path doesn't match (different bot home), the basename does."""
    app = _make_app("task-manager", evidence=["ops/tools/unified_task_system.py"])
    entry = {
        "label": "anything.opaque",
        "program_args": ["/bin/bash", "/Users/team-bot-a/somewhere/unified_task_system.py"],
    }
    assert _scanner._attribute_launchd_to_app(entry, app, "team-bot-a")


def test_attribute_launchd_rejects_unrelated_entry():
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    entry = {
        "label": "com.team-bot-d.heartbeat-monitor",
        "program_args": ["/usr/bin/python3", "/Users/team-bot-d/scripts/heartbeat.py"],
    }
    assert _scanner._attribute_launchd_to_app(entry, app, "team-bot-d") is False


def test_attribute_launchd_handles_missing_fields_gracefully():
    """Empty/missing label or program_args don't crash; just return False."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    assert _scanner._attribute_launchd_to_app({}, app, "personal-bot") is False
    assert _scanner._attribute_launchd_to_app({"label": ""}, app, "personal-bot") is False
    assert _scanner._attribute_launchd_to_app({"program_args": []}, app, "personal-bot") is False


# ── _attribute_instruction_to_app (v17) ────────────────────────────────────


def test_attribute_instruction_matches_by_pkg_id():
    """When the marker carries pkg_id, attribution is authoritative."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    section = {
        "file": "HEARTBEAT.md",
        "anchor": "Task Manager — Heartbeat Check",
        "pkg_id": "p-9bfa1c84",
        "body": "...",
        "command_hint": "python3 scripts/tasks.py check",
    }
    assert _scanner._attribute_instruction_to_app(
        section, app, app_pkg_id="p-9bfa1c84"
    ) is True


def test_attribute_instruction_falls_back_to_command_match():
    """No pkg_id (or pkg_id mismatch) — fall back to command-path match."""
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    section = {
        "file": "HEARTBEAT.md",
        "anchor": "Task Manager — Heartbeat Check",
        "pkg_id": "",          # operator-authored section
        "body": "...",
        "command_hint": "python3 scripts/tasks.py check",
    }
    assert _scanner._attribute_instruction_to_app(section, app) is True


def test_attribute_instruction_command_basename_match():
    """Hand-installed sections may carry a divergent path; basename match."""
    app = _make_app("task-manager", evidence=["scripts/unified_task_system.py"])
    section = {
        "file": "HEARTBEAT.md", "anchor": "X", "pkg_id": "",
        "body": "...",
        "command_hint": "/full/path/unified_task_system.py check",
    }
    assert _scanner._attribute_instruction_to_app(section, app) is True


def test_attribute_instruction_rejects_unrelated():
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    section = {
        "file": "HEARTBEAT.md", "anchor": "X", "pkg_id": "",
        "body": "...",
        "command_hint": "python3 scripts/morning_brief.py",
    }
    assert _scanner._attribute_instruction_to_app(section, app) is False


def test_attribute_instruction_handles_empty_section_gracefully():
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])
    assert _scanner._attribute_instruction_to_app({}, app) is False
    assert _scanner._attribute_instruction_to_app(
        {"file": "HEARTBEAT.md", "command_hint": ""}, app
    ) is False


# ── _extract_managed_sections (pure parser) ────────────────────────────────


def test_extract_managed_sections_pulls_pkg_id_from_marker():
    text = (
        "# Heartbeat instructions\n\n"
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed: pkg=p-9bfa1c84 job=j-XXXX -->\n\n"
        "Every heartbeat, run `python3 scripts/tasks.py check`.\n\n"
        "## Operator section\n\n"
        "No marker here — operator-authored.\n"
    )
    sections = _scanner._extract_managed_sections(text, "HEARTBEAT.md")
    assert len(sections) == 1
    assert sections[0]["anchor"] == "Task Manager — Heartbeat Check"
    assert sections[0]["pkg_id"] == "p-9bfa1c84"
    assert sections[0]["command_hint"] == "python3 scripts/tasks.py check"


def test_extract_managed_sections_handles_marker_without_pkg():
    text = (
        "## X\n<!-- evolve-managed -->\n\nbody with `cmd-here`.\n"
    )
    sections = _scanner._extract_managed_sections(text, "HEARTBEAT.md")
    assert len(sections) == 1
    assert sections[0]["pkg_id"] == ""
    assert sections[0]["command_hint"] == "cmd-here"


def test_extract_managed_sections_skips_h1_top_level_heading():
    """The file title (# Heartbeat instructions) is not a managed section."""
    text = (
        "# Heartbeat instructions\n"
        "<!-- evolve-managed -->\n\n"
        "Top body.\n"
    )
    assert _scanner._extract_managed_sections(text, "HEARTBEAT.md") == []


# ── _build_scheduled_action_from_launchd ─────────────────────────────────────


def test_launchd_builder_sets_mechanism_and_install_label():
    app = _make_app("task-manager")
    entry = {
        "label": "com.personal-bot.task-manager.check",
        "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        "program_args": ["/usr/bin/python3", "scripts/tasks.py", "check"],
        "start_interval": 14400,
        "start_calendar_interval": None,
    }
    sa = _scanner._build_scheduled_action_from_launchd(entry, app, "personal-bot")
    assert sa["mechanism"] == MECHANISM_LAUNCHD
    assert sa["trigger"]["kind"] == "launchd"
    assert sa["install"]["plist_label"] == "com.personal-bot.task-manager.check"
    # Command flattens program_args.
    assert "tasks.py" in sa["install"]["command"]
    # 14400s = "every 14400 seconds"
    assert "14400" in sa["trigger"]["schedule"]
    # installed_artifact points back to the plist on disk.
    assert sa["installed_artifact"].endswith("com.personal-bot.task-manager.check.plist")
    assert sa["installed_by"] == "scanner:backfill"


def test_launchd_builder_renders_calendar_interval():
    app = _make_app("task-manager")
    entry = {
        "label": "com.personal-bot.task-manager.morning",
        "plist_path": "/x.plist",
        "program_args": ["/bin/bash", "ea-morning.sh"],
        "start_interval": None,
        "start_calendar_interval": {"Hour": 7, "Minute": 0},
    }
    sa = _scanner._build_scheduled_action_from_launchd(entry, app, "personal-bot")
    assert "Hour=7" in sa["trigger"]["schedule"]
    assert "Minute=0" in sa["trigger"]["schedule"]


def test_launchd_builder_derives_id_from_label_tail():
    """A `com.{bot}.{app}.check` Label yields a stable, readable action_id."""
    app = _make_app("task-manager")
    entry = {
        "label": "com.personal-bot.task-manager.check",
        "plist_path": "/x.plist",
        "program_args": [],
    }
    sa = _scanner._build_scheduled_action_from_launchd(entry, app, "personal-bot")
    # action_id includes "check" (the label tail) — readable for operators.
    assert "check" in sa["id"]


def test_launchd_builder_handles_missing_label_gracefully():
    app = _make_app("task-manager")
    entry = {
        "label": "",
        "plist_path": "",
        "program_args": ["/usr/bin/python3", "/path/to/tasks.py", "check"],
    }
    sa = _scanner._build_scheduled_action_from_launchd(entry, app, "personal-bot")
    # action_id derived from the first program arg's basename ("tasks").
    assert "tasks" in sa["id"]
    assert sa["install"]["plist_label"] == ""


# ── _build_scheduled_action_from_instruction (v17) ─────────────────────────


def test_hook_builder_heartbeat_event_sets_oc_heartbeat_hook_mechanism():
    """v17 replacement: HEARTBEAT.md → oc_heartbeat_instruction."""
    from evolve_admin.applications.manifest import MECHANISM_OC_HEARTBEAT_INSTRUCTION
    app = _make_app("task-manager")
    section = {
        "file": "HEARTBEAT.md",
        "anchor": "Task Manager — Heartbeat Check",
        "pkg_id": "p-9bfa1c84",
        "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
        "command_hint": "python3 scripts/tasks.py check",
    }
    sa = _scanner._build_scheduled_action_from_instruction(section, app)
    assert sa["mechanism"] == MECHANISM_OC_HEARTBEAT_INSTRUCTION
    assert sa["trigger"]["kind"] == "heartbeat"
    assert sa["install"]["file"] == "HEARTBEAT.md"
    assert sa["install"]["section_anchor"] == "## Task Manager — Heartbeat Check"
    assert sa["install"]["command"] == "python3 scripts/tasks.py check"
    assert sa["installed_artifact"] == "HEARTBEAT.md#Task Manager — Heartbeat Check"
    assert sa["installed_by"] == "scanner:backfill"


def test_hook_builder_non_heartbeat_event_sets_oc_session_hook_mechanism():
    """v17 replacement: AGENTS.md → oc_session_instruction."""
    from evolve_admin.applications.manifest import MECHANISM_OC_SESSION_INSTRUCTION
    app = _make_app("task-manager")
    section = {
        "file": "AGENTS.md",
        "anchor": "Task Manager — Session Start",
        "pkg_id": "",
        "body": "On session start, list open tasks via `python3 scripts/tasks.py list --status open`.",
        "command_hint": "python3 scripts/tasks.py list --status open",
    }
    sa = _scanner._build_scheduled_action_from_instruction(section, app)
    assert sa["mechanism"] == MECHANISM_OC_SESSION_INSTRUCTION
    assert sa["trigger"]["kind"] == "session_start"
    assert sa["installed_artifact"] == "AGENTS.md#Task Manager — Session Start"


# ── End-to-end: generate_manifest_for_app picks up the v16 entries ───────────


def _stub_resolve_tier(*args, **kwargs):
    """Skip the actual LLM call — returns a minimal enriched-fields dict."""
    return ""


def test_generate_manifest_for_app_attributes_launchd_to_scheduled_actions():
    """The flagship end-to-end test: an inventory carrying a LaunchAgent
    matching an app's evidence files yields a manifest with that schedule
    on its scheduled_actions[]."""
    inv = _scanner.WorkspaceInventory(workspace=Path("/tmp/x"), bot_id="personal-bot")
    inv.launchd_entries = [{
        "label": "com.personal-bot.task-manager.check",
        "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        "program_args": ["/usr/bin/python3", "scripts/tasks.py", "check"],
        "start_interval": 14400,
        "start_calendar_interval": None,
        "run_at_load": False,
        "raw": {},
    }]
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])

    # The real generate_manifest_for_app calls an LLM to enrich identity/
    # success_criteria/etc. We stub the network-using helpers to skip that.
    with patch.object(_scanner, "_call_anthropic", return_value=""), \
         patch.object(_scanner, "_read_api_key", return_value=""):
        manifest = _scanner.generate_manifest_for_app(app, inv, "tier3")

    sa_list = manifest.get("scheduled_actions") or []
    launchd_actions = [sa for sa in sa_list if sa.get("mechanism") == MECHANISM_LAUNCHD]
    assert len(launchd_actions) == 1
    sa = launchd_actions[0]
    assert sa["install"]["plist_label"] == "com.personal-bot.task-manager.check"
    assert sa["installed_artifact"].endswith(".plist")


def test_generate_manifest_for_app_attributes_heartbeat_instruction():
    """v17: a HEARTBEAT.md managed section gets attributed to the app via
    pkg_id match or command-path fallback, then becomes a scheduled_actions
    entry on the manifest."""
    from evolve_admin.applications.manifest import MECHANISM_OC_HEARTBEAT_INSTRUCTION
    inv = _scanner.WorkspaceInventory(workspace=Path("/tmp/x"), bot_id="personal-bot")
    inv.heartbeat_md_sections = [{
        "file": "HEARTBEAT.md",
        "anchor": "Task Manager — Heartbeat Check",
        "pkg_id": "",   # no marker pkg → fall back to command match
        "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
        "command_hint": "python3 scripts/tasks.py check",
    }]
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])

    with patch.object(_scanner, "_call_anthropic", return_value=""), \
         patch.object(_scanner, "_read_api_key", return_value=""):
        manifest = _scanner.generate_manifest_for_app(app, inv, "tier3")

    sa_list = manifest.get("scheduled_actions") or []
    hook_actions = [sa for sa in sa_list if sa.get("mechanism") == MECHANISM_OC_HEARTBEAT_INSTRUCTION]
    assert len(hook_actions) == 1
    sa = hook_actions[0]
    assert sa["install"]["file"] == "HEARTBEAT.md"
    assert sa["installed_artifact"] == "HEARTBEAT.md#Task Manager — Heartbeat Check"


def test_generate_manifest_skips_install_entries_that_dont_match():
    """A LaunchAgent for a different app does NOT appear in this app's manifest."""
    inv = _scanner.WorkspaceInventory(workspace=Path("/tmp/x"), bot_id="personal-bot")
    inv.launchd_entries = [{
        "label": "com.personal-bot.heartbeat-monitor",
        "plist_path": "/x.plist",
        "program_args": ["/usr/bin/python3", "/some/path/heartbeat.py"],
        "start_interval": 60,
        "start_calendar_interval": None,
    }]
    app = _make_app("task-manager", evidence=["scripts/tasks.py"])

    with patch.object(_scanner, "_call_anthropic", return_value=""), \
         patch.object(_scanner, "_read_api_key", return_value=""):
        manifest = _scanner.generate_manifest_for_app(app, inv, "tier3")

    # No launchd entries on the manifest — the inventory entry was about
    # heartbeat-monitor, not task-manager.
    sa_list = manifest.get("scheduled_actions") or []
    launchd_actions = [sa for sa in sa_list if sa.get("mechanism") == MECHANISM_LAUNCHD]
    assert launchd_actions == []

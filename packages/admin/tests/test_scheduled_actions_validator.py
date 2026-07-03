"""Tests for ``applications.scheduled_actions_validator``.

Two layers:

- Unit tests on ``detect_unstructured_schedule`` and
  ``validate_scheduled_actions`` covering the four interesting cases:
  no markers / markers + installable action / markers + non-installable
  action / markers + no actions.

- Integration test that loads every gallery package on disk and asserts
  none of them fail validation. This is the regression guard for the
  2026-06-04 Atlas incident: any future gallery package that describes a
  daemon in prose without a structured ``scheduled_actions[]`` entry
  will fail this test before it can ship.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scheduled_actions_validator import (  # noqa: E402
    detect_unstructured_schedule,
    validate_scheduled_actions,
)


# ── detect_unstructured_schedule ─────────────────────────────────────────────


def test_detect_returns_empty_when_no_markers() -> None:
    spec = (
        "A tag-based task manager. CLI commands for adding, listing, and "
        "completing tasks. Stores tasks in workspace/tasks.json."
    )
    assert detect_unstructured_schedule(spec) == []


def test_detect_returns_empty_on_non_string_input() -> None:
    assert detect_unstructured_schedule(None) == []
    assert detect_unstructured_schedule(123) == []
    assert detect_unstructured_schedule({"build_spec": "..."}) == []


def test_detect_matches_launch_daemon_path() -> None:
    spec = "Install at /Library/LaunchDaemons/com.bot.app.plist"
    found = detect_unstructured_schedule(spec)
    assert "/Library/LaunchDaemons/" in found
    # Standalone ``LaunchDaemon`` does NOT match here because the only
    # occurrence is part of ``LaunchDaemons`` (the path form, plural).
    # That's correct — the path pattern alone is enough signal.
    assert "LaunchDaemon" not in found


def test_detect_matches_launch_agent_path() -> None:
    spec = "Write to /Users/atlas/Library/LaunchAgents/com.atlas.foo.plist"
    found = detect_unstructured_schedule(spec)
    assert "/Library/LaunchAgents/" in found
    # Same shape: path form is plural, standalone ``LaunchAgent`` shouldn't match.
    assert "LaunchAgent" not in found


def test_detect_matches_standalone_launch_daemon_word() -> None:
    # When the build_spec uses ``LaunchDaemon`` (singular) as a narrative
    # term — separate from the path — it must match. This is the Atlas
    # case: "the LaunchDaemon is the only scheduler."
    spec = "The LaunchDaemon is the only scheduler. The LaunchAgent runs at login."
    found = detect_unstructured_schedule(spec)
    assert "LaunchDaemon" in found
    assert "LaunchAgent" in found


def test_detect_matches_cron_driven_framing() -> None:
    spec = "A cron-driven daemon that runs once per day."
    found = detect_unstructured_schedule(spec)
    assert any("cron-driven" in m or "cron driven" in m for m in found)


def test_detect_ignores_passing_mention_of_cron() -> None:
    # "cron job" alone (without "trigger" / "-driven") is too weak — the
    # word "cron" appears in normal prose without implying installation.
    spec = "This is much faster than a cron would be. We don't use crontab here."
    found = detect_unstructured_schedule(spec)
    # ``crontab`` alone (without -e / -l) and bare ``cron`` should NOT trip.
    assert "/Library/LaunchDaemons/" not in found
    assert "/Library/LaunchAgents/" not in found
    # "cron job" is a documented marker — confirm we do NOT match the bare
    # word "cron" or "crontab" without the editing verbs.
    assert all("cron-driven" not in m and "cron trigger" not in m for m in found)


def test_detect_matches_launchctl_install_verbs() -> None:
    assert detect_unstructured_schedule(
        "sudo launchctl bootstrap system /Library/LaunchDaemons/x.plist"
    )
    assert detect_unstructured_schedule("launchctl load -w /path/to/x.plist")
    # ``launchctl print`` (read-only inspection) does NOT match.
    found = detect_unstructured_schedule("Run launchctl print system to inspect.")
    assert not any("launchctl" in m for m in found)


def test_detect_matches_plist_schedule_keys() -> None:
    spec = "<key>StartCalendarInterval</key><dict>...</dict>"
    found = detect_unstructured_schedule(spec)
    assert "StartCalendarInterval" in found


def test_detect_deduplicates_markers() -> None:
    # Same marker mentioned three times → returned once.
    spec = "LaunchDaemon at /Library/LaunchDaemons/. The LaunchDaemon installs."
    found = detect_unstructured_schedule(spec)
    assert found.count("LaunchDaemon") == 1
    assert found.count("/Library/LaunchDaemons/") == 1


def test_detect_matches_explicit_file_block() -> None:
    spec = "## FILE: /Library/LaunchDaemons/com.bot.foo.plist\n<plist>...</plist>"
    found = detect_unstructured_schedule(spec)
    assert any("FILE:" in m for m in found)


# ── validate_scheduled_actions ───────────────────────────────────────────────


def test_validate_passes_when_no_markers() -> None:
    result = validate_scheduled_actions({"build_spec": "Pure CLI app. No daemon.", "scheduled_actions": []})
    assert result["ok"] is True
    assert result["markers"] == []
    assert result["severity"] == "info"


def test_validate_passes_when_markers_plus_installable_action() -> None:
    result = validate_scheduled_actions({
        "build_spec": "A cron-driven daemon at /Library/LaunchDaemons/com.bot.foo.plist",
        "scheduled_actions": [
            {"id": "daily-7am", "mechanism": "launchd", "install": {"plist_label": "x", "plist_xml": "<plist/>"}},
        ],
    })
    assert result["ok"] is True
    assert result["actions_installable"] == 1
    assert result["severity"] == "info"
    # Markers ARE listed even on the happy path so the operator can confirm
    # the validator saw what it expected.
    assert result["markers"]


def test_validate_passes_with_heartbeat_instruction_mechanism() -> None:
    # Heartbeat-instruction is forge-installable even though it's not a
    # LaunchDaemon — covers the Task Manager case.
    result = validate_scheduled_actions({
        "build_spec": "Heartbeat-driven. References LaunchDaemon as comparison.",
        "scheduled_actions": [
            {
                "id": "task-check",
                "mechanism": "oc_heartbeat_instruction",
                "install": {
                    "file": "HEARTBEAT.md",
                    "section_anchor": "## Task Check",
                    "body": "Run the task check and surface TASK_DUE lines.",
                },
            },
        ],
    })
    assert result["ok"] is True


def test_validate_fails_when_markers_and_no_actions() -> None:
    result = validate_scheduled_actions({
        "build_spec": "Install at /Library/LaunchDaemons/com.bot.foo.plist (LaunchDaemon, cron-driven)",
        "scheduled_actions": [],
    })
    assert result["ok"] is False
    assert result["severity"] == "build_blocker"
    assert result["actions_installable"] == 0
    assert "no forge-installable mechanism" in result["message"]
    assert "/Library/LaunchDaemons/" in result["message"]


def test_validate_fails_when_markers_and_only_unknown_mechanism() -> None:
    # scheduled_actions[] is non-empty but every entry has mechanism that
    # the forge installer can't act on. Functionally equivalent to empty.
    result = validate_scheduled_actions({
        "build_spec": "LaunchDaemon install at /Library/LaunchDaemons/",
        "scheduled_actions": [
            {"id": "x", "mechanism": "unknown"},
            {"id": "y", "mechanism": ""},
            {"id": "z", "mechanism": "external"},
        ],
    })
    assert result["ok"] is False
    assert result["actions_total"] == 3
    assert result["actions_installable"] == 0
    assert "non-installable mechanism" in result["message"]


def test_validate_accepts_manifest_object_duck_type() -> None:
    """validate_scheduled_actions reads .build_spec / .scheduled_actions
    via getattr when the input isn't a dict."""

    class _Stub:
        build_spec = "Install LaunchDaemon at /Library/LaunchDaemons/x.plist"
        scheduled_actions = [{
            "id": "x",
            "mechanism": "launchd",
            "install": {
                "plist_label": "ai.evolve.bot.x",
                "command": "python3 scripts/x.py",
                "schedule": {"every_minutes": 30},
            },
        }]

    result = validate_scheduled_actions(_Stub())
    assert result["ok"] is True


def test_validate_tolerates_missing_fields() -> None:
    # Empty dict, no fields at all → no markers → ok.
    result = validate_scheduled_actions({})
    assert result["ok"] is True
    assert result["markers"] == []


def test_validate_truncates_marker_list_in_message() -> None:
    spec = (
        "/Library/LaunchDaemons/ LaunchDaemon LaunchAgent "
        "/Library/LaunchAgents/ launchctl bootstrap "
        "StartCalendarInterval cron-driven cron trigger"
    )
    result = validate_scheduled_actions({"build_spec": spec, "scheduled_actions": []})
    assert result["ok"] is False
    # Message shows at most 4 markers + "+N more".
    assert "+" in result["message"] and "more" in result["message"]


# ── Per-entry install wiring (manifest-v7 Slice 1, spec §3.2) ────────────────


def test_wiring_fails_launchd_entry_without_recipe() -> None:
    # No install{} at all — materialize would fail with "plist_label is
    # required" and the app would ship with no cron, silently.
    result = validate_scheduled_actions({
        "build_spec": "Pure CLI app. No daemon.",
        "scheduled_actions": [{"id": "x", "mechanism": "launchd"}],
    })
    assert result["ok"] is False
    assert result["severity"] == "build_blocker"
    assert any("plist_label" in e for e in result["errors"])


def test_wiring_fails_launchd_command_without_schedule() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "launchd",
            "install": {"plist_label": "ai.evolve.bot.x", "command": "python3 s.py"},
        }],
    })
    assert result["ok"] is False
    assert any("install.schedule" in e for e in result["errors"])


def test_wiring_passes_launchd_legacy_plist_xml_shape() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "launchd",
            "install": {"plist_label": "ai.evolve.bot.x", "plist_xml": "<plist/>"},
        }],
    })
    assert result["ok"] is True


def test_wiring_fails_instruction_entry_missing_fields() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "oc_session_instruction",
            "install": {"file": "AGENTS.md"},
        }],
    })
    assert result["ok"] is False
    assert any("section_anchor" in e and "body" in e for e in result["errors"])


def test_wiring_fails_python_signal_entry_without_schedule() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "launchd_python_signal",
            "install": {
                "label": "ai.evolve.bot.x",
                "command": "python3 s.py",
                "signal_patterns": ["TASK_DUE"],
            },
        }],
    })
    assert result["ok"] is False
    assert any("schedule" in e for e in result["errors"])


def test_wiring_passes_python_signal_every_shorthand() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "launchd_python_signal",
            "install": {
                "label": "ai.evolve.bot.x",
                "command": "python3 s.py",
                "signal_patterns": ["TASK_DUE"],
                "every": "30m",
            },
        }],
    })
    assert result["ok"] is True


def test_wiring_fails_crontab_mechanism() -> None:
    # install_crontab_entry is unimplemented (sudoers gap) — every fresh
    # crontab entry fails at materialize.
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "crontab",
            "install": {"schedule": "0 7 * * *", "command": "python3 s.py"},
        }],
    })
    assert result["ok"] is False
    assert any("crontab" in e for e in result["errors"])


def test_wiring_fails_deprecated_hook_mechanism() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{"id": "x", "mechanism": "oc_heartbeat_hook"}],
    })
    assert result["ok"] is False
    assert any("deprecated" in e for e in result["errors"])


def test_wiring_fails_non_dict_entry() -> None:
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": ["run the thing every morning"],
    })
    assert result["ok"] is False
    assert any("not an object" in e for e in result["errors"])


def test_wiring_skips_non_install_mechanisms() -> None:
    # Pre-attribution / operator-managed entries carry no install contract;
    # forge skips them, so the wiring gate must not block on them.
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [
            {"id": "x", "mechanism": "unknown"},
            {"id": "y", "mechanism": ""},
            {"id": "z", "mechanism": "external"},
        ],
    })
    assert result["ok"] is True
    assert result["errors"] == []


def test_wiring_skips_already_installed_entry() -> None:
    # An entry with installed_artifact was materialized once (forge) or
    # hand-installed and scanner-attributed; blocking it would prevent
    # rebuilds of working apps.
    result = validate_scheduled_actions({
        "build_spec": "",
        "scheduled_actions": [{
            "id": "x",
            "mechanism": "launchd",
            "installed_artifact": "/Users/{bot}/Library/LaunchAgents/x.plist",
            "installed_by": "scanner:backfill",
        }],
    })
    assert result["ok"] is True
    assert result["errors"] == []


# ── Integration: every gallery package must validate clean ───────────────────


def _gallery_root() -> Path:
    # Walk up to the repo root (where the gallery/ directory lives).
    # We require ``gallery/index.json`` because the unrelated
    # ``packages/gallery/`` directory holds a different artifact (catalog.json)
    # and would otherwise be matched first by an is_dir() check.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "gallery"
        if candidate.is_dir() and (candidate / "index.json").is_file():
            return candidate
    raise RuntimeError("Could not locate gallery/index.json in repo tree")


def _gallery_packages() -> list[Path]:
    root = _gallery_root()
    pkgs: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        for f in child.glob("p-*.json"):
            pkgs.append(f)
    return sorted(pkgs)


@pytest.mark.parametrize("pkg_path", _gallery_packages(), ids=lambda p: p.parent.name)
def test_every_gallery_package_passes_scheduled_actions_validator(pkg_path: Path) -> None:
    """Regression guard: any new gallery package that describes a daemon
    in build_spec prose without a structured scheduled_actions[] entry
    will fail here before it can ship.

    This was added as a response to the Atlas Daily Digest incident on
    2026-06-04, where 8 of 12 then-current gallery packages exhibited
    the same bug. After this PR's migrations, all of them validate clean.
    """
    pkg = json.loads(pkg_path.read_text())
    result = validate_scheduled_actions(pkg)
    assert result["ok"], (
        f"Gallery package {pkg_path.parent.name} ({pkg.get('pkg_id', '?')}) "
        f"fails the scheduled_actions validator: {result['message']}"
    )


@pytest.mark.parametrize("pkg_path", _gallery_packages(), ids=lambda p: p.parent.name)
def test_every_gallery_launchd_label_lives_under_ai_evolve_namespace(pkg_path: Path) -> None:
    """Sudoers grants for the evolve user scope all per-bot LaunchDaemon
    ops (cp / chown / chmod / launchctl bootstrap / bootout / rm) to the
    ``ai.evolve.*.plist`` namespace. A manifest that ships a launchd
    action with a different label (the post-2026-06-04 first migration
    used ``com.${bot_id}.<app>``) installs cleanly on dev but blocks at
    ``sudo /bin/cp`` on a real pod with "a terminal is required to read
    the password".

    This test enforces the convention at gallery shipping time so the
    failure surfaces in CI rather than at operator install time. If a
    new mechanism legitimately needs a different namespace, extend the
    sudoers grants in setup_wizard._render_evolve_sudoers in the same
    PR — don't loosen this test.
    """
    pkg = json.loads(pkg_path.read_text())
    for action in pkg.get("scheduled_actions") or []:
        if action.get("mechanism") != "launchd":
            # oc_heartbeat_instruction, launchd_python_signal, etc. use
            # different install surfaces (HEARTBEAT.md sections, system
            # daemon paths managed by install_python_signal_action with
            # its own naming). Skip — this guard is specifically for the
            # raw ``launchd`` mechanism that writes to /Library/LaunchDaemons/.
            continue
        label = (action.get("install") or {}).get("plist_label", "")
        assert label.startswith("ai.evolve."), (
            f"{pkg_path.parent.name}/{action.get('id')}: plist_label "
            f"{label!r} does not start with 'ai.evolve.' — won't match "
            f"the evolve user's NOPASSWD sudoers grants. Rename to "
            f"ai.evolve.${{bot_id}}.<app> shape."
        )

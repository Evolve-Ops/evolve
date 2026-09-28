"""Unit tests for the v16 install-site verifier assertions (A1, A2, A5, A6).

PR 3 of spec-forge-side-effects-2026-06-02.md §7. Each assertion is a pure
function over ``(manifest, ctx)`` — these tests pin its behaviour against
synthetic manifests + ctx dicts. No filesystem hooks, no Signal store, no
LLM, no runner integration.

A3 (inputs exist) and A4 (evidence anchor) already exist as
``check_scheduled_action_inputs`` / ``check_scheduled_action_anchors``;
they're exercised by ``test_app_audit_structural.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    Finding,
    check_orphan_install_artifacts,
    check_scheduled_action_command_resolvable,
    check_scheduled_action_install_present,
    check_scheduled_action_output_channel,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _workspace(tmp_path: Path) -> Path:
    """Empty workspace dir; tests populate selectively."""
    (tmp_path / "scripts").mkdir()
    return tmp_path


def _action(
    mechanism: str,
    *,
    action_id: str = "task-check",
    artifact: str = "",
    install: dict | None = None,
    outputs: list | None = None,
) -> dict:
    return {
        "id": action_id,
        "mechanism": mechanism,
        "installed_artifact": artifact,
        "install": install or {},
        "outputs": outputs or [],
        "trigger": {"kind": mechanism},
    }


# ── A1: check_scheduled_action_install_present ──────────────────────────────


def test_a1_oc_heartbeat_instruction_resolves_when_section_present(tmp_path: Path) -> None:
    """v17: oc_heartbeat_instruction + matching HEARTBEAT.md section → no finding."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed: pkg=p-9bfa1c84 -->\n\n"
        "Every heartbeat, run `python3 scripts/tasks.py check`.\n"
    )
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            artifact="HEARTBEAT.md#Task Manager — Heartbeat Check",
            install={
                "file": "HEARTBEAT.md",
                "section_anchor": "## Task Manager — Heartbeat Check",
                "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
                "command": "python3 scripts/tasks.py check",
            },
        )],
    }
    ctx = {"workspace": ws}
    assert check_scheduled_action_install_present(manifest, ctx) == []


def test_a1_oc_heartbeat_instruction_missing_when_file_absent(tmp_path: Path) -> None:
    """v17: file doesn't exist in workspace → major install_missing."""
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            artifact="HEARTBEAT.md#Task Manager — Heartbeat Check",
            install={
                "file": "HEARTBEAT.md",
                "section_anchor": "## Task Manager — Heartbeat Check",
                "body": "...", "command": "python3 scripts/tasks.py check",
            },
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}  # no HEARTBEAT.md
    findings = check_scheduled_action_install_present(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "scheduled_action_install_missing"
    assert findings[0].severity == SEVERITY_MAJOR
    assert "HEARTBEAT.md" in findings[0].evidence["diagnostic"]


def test_a1_oc_heartbeat_instruction_missing_when_section_lacks_marker(tmp_path: Path) -> None:
    """v17: section exists without evolve-managed marker → operator content;
    A1 surfaces install_missing so the operator knows forge didn't install."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Task Manager — Heartbeat Check\n\n"
        "Operator-authored body, no marker.\n"
    )
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            artifact="HEARTBEAT.md#Task Manager — Heartbeat Check",
            install={
                "file": "HEARTBEAT.md",
                "section_anchor": "## Task Manager — Heartbeat Check",
                "body": "...", "command": "python3 scripts/tasks.py check",
            },
        )],
    }
    ctx = {"workspace": ws}
    findings = check_scheduled_action_install_present(manifest, ctx)
    assert len(findings) == 1
    assert "evolve-managed" in findings[0].evidence["diagnostic"]


def test_a1_deprecated_oc_heartbeat_hook_reports_migration(tmp_path: Path) -> None:
    """v17 (deprecated): oc_heartbeat_hook mechanism surfaces remediation."""
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_hook",
            artifact="openclaw.json#hooks.heartbeat",
            install={"hook_event": "heartbeat"},
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}
    findings = check_scheduled_action_install_present(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert "migrate_manifest" in findings[0].summary


def test_a1_oc_hook_missing_when_command_does_not_match(tmp_path: Path) -> None:
    """v17: section body doesn't reference install.command → install_missing."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed -->\n\n"
        "Different body that doesn't mention the command.\n"
    )
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            artifact="HEARTBEAT.md#Task Manager — Heartbeat Check",
            install={
                "file": "HEARTBEAT.md",
                "section_anchor": "## Task Manager — Heartbeat Check",
                "body": "...", "command": "python3 scripts/tasks.py check",
            },
        )],
    }
    ctx = {"workspace": ws}
    findings = check_scheduled_action_install_present(manifest, ctx)
    assert len(findings) == 1
    assert "no longer references install.command" in findings[0].evidence["diagnostic"]


def test_a1_launchd_resolves_when_plist_enumerated(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [_action(
            "launchd",
            artifact="/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
            install={"plist_label": "com.personal-bot.task-manager.check"},
        )],
    }
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.personal-bot.task-manager.check",
            "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        }],
    }
    assert check_scheduled_action_install_present(manifest, ctx) == []


def test_a1_launchd_resolves_when_label_loaded_but_plist_unread(tmp_path: Path) -> None:
    """Even when the scanner couldn't read the plist, a loaded label is sufficient."""
    manifest = {
        "scheduled_actions": [_action(
            "launchd",
            install={"plist_label": "com.personal-bot.task-manager.check"},
        )],
    }
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [],
        "launchctl_labels": ["com.personal-bot.task-manager.check"],
    }
    assert check_scheduled_action_install_present(manifest, ctx) == []


def test_a1_launchd_missing_when_neither_enumerated_nor_loaded(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [_action(
            "launchd",
            install={"plist_label": "com.personal-bot.task-manager.check"},
        )],
    }
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [],
        "launchctl_labels": [],
    }
    findings = check_scheduled_action_install_present(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].evidence["label"] == "com.personal-bot.task-manager.check"


def test_a1_crontab_resolves_when_command_in_crontab(tmp_path: Path) -> None:
    manifest = {
        "scheduled_actions": [_action(
            "crontab",
            install={
                "label": "task-check",
                "command": "python3 scripts/tasks.py check",
            },
        )],
    }
    ctx = {
        "workspace": _workspace(tmp_path),
        "crontab_lines": ["0 */4 * * * python3 scripts/tasks.py check"],
    }
    assert check_scheduled_action_install_present(manifest, ctx) == []


def test_a1_external_mechanism_is_skipped(tmp_path: Path) -> None:
    """External mechanism = nothing to verify locally → no finding."""
    manifest = {
        "scheduled_actions": [_action("external")],
    }
    assert check_scheduled_action_install_present(manifest, {"workspace": _workspace(tmp_path)}) == []


def test_a1_unknown_mechanism_is_skipped(tmp_path: Path) -> None:
    """Unknown = pre-attribution; no finding (scanner will fill in)."""
    manifest = {
        "scheduled_actions": [_action("unknown")],
    }
    assert check_scheduled_action_install_present(manifest, {"workspace": _workspace(tmp_path)}) == []


def test_a1_unrecognized_mechanism_is_minor(tmp_path: Path) -> None:
    """A typo / bogus mechanism value (`oc_hartbeat_hook`) surfaces as minor."""
    manifest = {
        "scheduled_actions": [_action("oc_hartbeat_hook")],  # typo
    }
    findings = check_scheduled_action_install_present(manifest, {"workspace": _workspace(tmp_path)})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MINOR


# ── A2: check_scheduled_action_command_resolvable ───────────────────────────


def test_a2_command_with_existing_script_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / "scripts/tasks.py").write_text("print('ok')")
    manifest = {
        "scheduled_actions": [_action(
            "oc_heartbeat_hook",
            install={"command": "python3 scripts/tasks.py check"},
        )],
    }
    assert check_scheduled_action_command_resolvable(manifest, {"workspace": ws}) == []


def test_a2_command_with_missing_script_fires_major(tmp_path: Path) -> None:
    """The script the install.command invokes doesn't exist on disk."""
    manifest = {
        "scheduled_actions": [_action(
            "oc_heartbeat_hook",
            install={"command": "python3 scripts/missing.py check"},
        )],
    }
    findings = check_scheduled_action_command_resolvable(manifest, {"workspace": _workspace(tmp_path)})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert "missing.py" in findings[0].evidence["command"]


def test_a2_empty_command_is_skipped(tmp_path: Path) -> None:
    """A1 owns the 'should there be a command' question; A2 only checks
    that whatever IS declared resolves."""
    manifest = {
        "scheduled_actions": [_action("oc_heartbeat_hook", install={})],
    }
    assert check_scheduled_action_command_resolvable(manifest, {"workspace": _workspace(tmp_path)}) == []


# ── A5: check_scheduled_action_output_channel ───────────────────────────────


def _instruction_install_cfg() -> dict:
    return {
        "file": "HEARTBEAT.md",
        "section_anchor": "## Task Manager — Heartbeat Check",
        "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
        "command": "python3 scripts/tasks.py check",
    }


def test_a5_session_message_output_with_registered_section_passes(tmp_path: Path) -> None:
    """v17: section IS registered for the declared file → output channel valid."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed -->\n\n"
        "Every heartbeat, run `python3 scripts/tasks.py check`.\n"
    )
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            install=_instruction_install_cfg(),
            outputs=[{"kind": "session_message", "channel": "primary"}],
        )],
    }
    ctx = {"workspace": ws}
    assert check_scheduled_action_output_channel(manifest, ctx) == []


def test_a5_session_message_with_no_section_registered_fires(tmp_path: Path) -> None:
    """v17: action promises session_message output but no managed section
    is present → the output goes nowhere the bot can see."""
    manifest = {
        "evidence_files": ["scripts/tasks.py"],
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            install=_instruction_install_cfg(),
            outputs=[{"kind": "session_message"}],
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}   # no HEARTBEAT.md
    findings = check_scheduled_action_output_channel(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "scheduled_action_output_channel_invalid"
    assert findings[0].severity == SEVERITY_MAJOR


def test_a5_no_session_output_is_skipped(tmp_path: Path) -> None:
    """No session_message output declared → A5 has nothing to verify."""
    manifest = {
        "scheduled_actions": [_action(
            "oc_heartbeat_instruction",
            install=_instruction_install_cfg(),
            outputs=[{"kind": "log_file", "channel": "/tmp/x.log"}],
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}
    assert check_scheduled_action_output_channel(manifest, ctx) == []


def test_a5_launchd_mechanism_is_skipped(tmp_path: Path) -> None:
    """A launchd action with a session_message output is its own design
    problem caught by a different assertion; A5 only fires on
    instruction-based mechanisms where the channel matters structurally."""
    manifest = {
        "scheduled_actions": [_action(
            "launchd",
            outputs=[{"kind": "session_message"}],
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}
    assert check_scheduled_action_output_channel(manifest, ctx) == []


def test_a5_deprecated_hook_mechanism_skipped_to_avoid_double_report(tmp_path: Path) -> None:
    """A1 already flags deprecated hook mechanisms; A5 shouldn't double-fire."""
    manifest = {
        "scheduled_actions": [_action(
            "oc_heartbeat_hook",
            install={"hook_event": "heartbeat"},
            outputs=[{"kind": "session_message"}],
        )],
    }
    ctx = {"workspace": _workspace(tmp_path)}
    assert check_scheduled_action_output_channel(manifest, ctx) == []


# ── A6: check_orphan_install_artifacts ──────────────────────────────────────


def test_a6_skips_when_runner_did_not_compute_union(tmp_path: Path) -> None:
    """Without ``all_pod_installed_artifacts`` in ctx, A6 can't decide what's
    orphan and no-ops cleanly. Important for single-manifest tests that don't
    have cross-app vision."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.personal-bot.task-manager.check",
            "plist_path": "/Users/personal-bot/Library/LaunchAgents/x.plist",
        }],
        # all_pod_installed_artifacts deliberately absent
    }
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_finds_orphan_launchd_when_no_manifest_claims(tmp_path: Path) -> None:
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.personal-bot.old-app.check",
            "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.old-app.check.plist",
        }],
        "all_pod_installed_artifacts": set(),  # nothing claimed
    }
    findings = check_orphan_install_artifacts(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "scheduled_action_orphan_install"
    assert findings[0].severity == SEVERITY_MINOR
    assert "com.personal-bot.old-app.check" in findings[0].evidence["label"]


def test_a6_recognizes_claimed_by_path(tmp_path: Path) -> None:
    """Manifest claims by full plist path → not an orphan."""
    plist = "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist"
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.personal-bot.task-manager.check",
            "plist_path": plist,
        }],
        "all_pod_installed_artifacts": {plist},
    }
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_recognizes_claimed_by_label(tmp_path: Path) -> None:
    """Some manifests carry just the label (no full path) — A6 must still
    treat that as claimed."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.personal-bot.task-manager.check",
            "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        }],
        "all_pod_installed_artifacts": {"com.personal-bot.task-manager.check"},
    }
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_finds_orphan_managed_section_in_heartbeat_md(tmp_path: Path) -> None:
    """v17: an evolve-managed section in HEARTBEAT.md that no manifest
    claims → orphan finding."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Old App — Heartbeat Check\n"
        "<!-- evolve-managed: pkg=p-deleted-app -->\n\n"
        "Old body.\n"
    )
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": ws,
        "all_pod_installed_artifacts": set(),  # nothing claimed
    }
    findings = check_orphan_install_artifacts(manifest, ctx)
    orphans = [f for f in findings
               if f.evidence.get("file") == "HEARTBEAT.md"]
    assert len(orphans) == 1
    assert orphans[0].evidence["anchor"] == "Old App — Heartbeat Check"


def test_a6_ignores_unmanaged_sections_in_heartbeat_md(tmp_path: Path) -> None:
    """v17: a section WITHOUT the evolve-managed marker is operator content
    — A6 doesn't flag it as orphan."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat instructions\n\n"
        "## Operator Note\n\n"
        "I wrote this myself, no marker.\n"
    )
    manifest = {"bot_id": "personal-bot"}
    ctx = {"workspace": ws, "all_pod_installed_artifacts": set()}
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_recognizes_claimed_managed_section(tmp_path: Path) -> None:
    """When a manifest claims the section via artifact, A6 doesn't flag it."""
    ws = _workspace(tmp_path)
    (ws / "HEARTBEAT.md").write_text(
        "## Task Manager — Heartbeat Check\n"
        "<!-- evolve-managed -->\n\n"
        "body\n"
    )
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": ws,
        "all_pod_installed_artifacts": {
            "HEARTBEAT.md#Task Manager — Heartbeat Check",
        },
    }
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_ignores_launchd_entries_not_matching_bot(tmp_path: Path) -> None:
    """A scanner enumeration pulls in EVERY plist under the bot user's
    ~/Library/LaunchAgents, including third-party ones the bot user happens to
    have installed. A6 only complains about ones in this bot's Evolve namespace
    (ai.evolve.{bot}.* / com.{bot}.*) — a system/Apple plist that merely lives
    under the bot home is not an Evolve app install."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.apple.something.daemon",  # not in the bot's namespace
            "plist_path": "/Users/personal-bot/Library/LaunchAgents/x.plist",
        }],
        "all_pod_installed_artifacts": set(),
    }
    # Pre-fix this fired a false-positive orphan because the plist path lived
    # under the bot home; the namespace gate now excludes it.
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_excludes_third_party_dropbox_plist(tmp_path: Path) -> None:
    """Regression for the 2026-06-12 review: a Dropbox LaunchAgent in the bot
    user's ~/Library/LaunchAgents must NOT be flagged as an unclaimed app
    daemon. This false-positive class was 62% of one test pod's firing
    structural signals."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "com.dropbox.dropboxmacupdate.xpcservice",
            "plist_path": (
                "/Users/personal-bot/Library/LaunchAgents/"
                "com.dropbox.dropboxmacupdate.xpcservice.plist"
            ),
        }],
        "all_pod_installed_artifacts": set(),
    }
    assert check_orphan_install_artifacts(manifest, ctx) == []


def test_a6_attributes_ai_evolve_namespace_label(tmp_path: Path) -> None:
    """The canonical post-2026-06-04 namespace (ai.evolve.{bot}.{app}) IS an
    Evolve install, so an unclaimed one is still surfaced as an orphan."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "ai.evolve.personal-bot.old-app",
            "plist_path": (
                "/Users/personal-bot/Library/LaunchAgents/ai.evolve.personal-bot.old-app.plist"
            ),
        }],
        "all_pod_installed_artifacts": set(),
    }
    findings = check_orphan_install_artifacts(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "scheduled_action_orphan_install"
    assert findings[0].evidence["label"] == "ai.evolve.personal-bot.old-app"


def test_a6_orphan_signature_coalesces_across_apps(tmp_path: Path) -> None:
    """Bug-2 (2026-06-12): the runner invokes A6 once per manifest with the
    pod-wide claimed union, so a single orphan plist is re-discovered by every
    app's audit. The SAME plist must produce the SAME Signal signature
    regardless of which app_id the runner passes — otherwise N apps fan one
    orphan out into N Signals (18 and 13 across two bots in the review)."""
    manifest = {"bot_id": "personal-bot"}
    ctx = {
        "workspace": _workspace(tmp_path),
        "bot_launchd_entries": [{
            "label": "ai.evolve.personal-bot.orphaned",
            "plist_path": (
                "/Users/personal-bot/Library/LaunchAgents/ai.evolve.personal-bot.orphaned.plist"
            ),
        }],
        "all_pod_installed_artifacts": set(),
    }
    findings = check_orphan_install_artifacts(manifest, ctx)
    assert len(findings) == 1
    orphan = findings[0]

    # The runner computes f.signature(bot_id, app_id) once per manifest, so the
    # same orphan is signed with each app's id in turn — they must collapse.
    sig_app_a = orphan.signature("personal-bot", "app-alpha")
    sig_app_b = orphan.signature("personal-bot", "app-bravo")
    assert sig_app_a == sig_app_b
    # The signature carries no app_id, so the dedup is real (not coincidental).
    assert "app-alpha" not in sig_app_a
    assert "app-bravo" not in sig_app_a

    # Two genuinely-different orphan plists stay distinct (keyed on plist_path).
    other = Finding(
        assertion_id="scheduled_action_orphan_install",
        severity=SEVERITY_MINOR, summary="x",
        evidence={
            "label": "ai.evolve.personal-bot.other",
            "plist_path": (
                "/Users/personal-bot/Library/LaunchAgents/ai.evolve.personal-bot.other.plist"
            ),
            "bot_id": "personal-bot",
        },
    )
    assert other.signature("personal-bot", "app-alpha") != sig_app_a

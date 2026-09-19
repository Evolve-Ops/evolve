"""Tests for forge Phase 4.5 — _materialize_scheduled_actions().

PR 4 of spec-forge-side-effects-2026-06-02.md §5. Phase 4.5 walks
``manifest.scheduled_actions[]`` after operator approval and calls the
install helpers for each entry whose mechanism is forge-installable
(oc_*_hook, launchd). Successful installs stamp ``installed_at``,
``installed_by``, and ``installed_artifact`` on the action in-place;
failures and skips return a status entry without mutating the action.

Mocks the install helpers (which have their own tests in
``test_install_helpers.py``) so these tests focus on the dispatch +
stamping logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.forge_engine import (  # noqa: E402
    _materialize_scheduled_actions,
    _normalise_schedule,
    _parse_every_duration,
    _record_scheduled_action_outcomes,
    _scheduled_action_failure_signature,
)
from evolve_admin.applications.forge_jobs import ForgeJob  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    MECHANISM_LAUNCHD,
    MECHANISM_LAUNCHD_PYTHON_SIGNAL,
    MECHANISM_OC_HEARTBEAT_HOOK,
    MECHANISM_OC_HEARTBEAT_INSTRUCTION,
    MECHANISM_OC_SESSION_INSTRUCTION,
    MECHANISM_UNKNOWN,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _job(job_id: str = "j-pr4-test") -> ForgeJob:
    return ForgeJob(
        job_id=job_id,
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-test",
        app_id="task-manager",
        bot_id="personal-bot",
        pkg_version_before=None,
        gallery_version="2026.06.02-1.0",
    )


def _manifest_with_actions(*actions: dict) -> ApplicationManifest:
    m = ApplicationManifest(
        id="task-manager", name="Task Manager", bot_id="personal-bot",
    )
    m.scheduled_actions = list(actions)
    return m


# ── Instruction installation path (v17, spec-heartbeat-instruction §4) ─────


def test_phase_4_5_dispatches_heartbeat_instruction_install() -> None:
    """A scheduled_action with mechanism=oc_heartbeat_instruction routes to
    install_heartbeat_instruction with the v17 install fields, and stamps
    the action on success."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Task Manager — Heartbeat Check",
            "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
            "command": "python3 scripts/tasks.py check",
        },
    }
    manifest = _manifest_with_actions(action)
    # Manifest needs a pkg_id so the marker carries attribution.
    manifest.pkg_id = "p-9bfa1c84"
    job = _job(job_id="j-pr10-test")

    fake_hb = {
        "ok": True,
        "artifact": "HEARTBEAT.md#Task Manager — Heartbeat Check",
        "error": "",
        "already_present": False,
        "created_file": False,
    }
    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               return_value=fake_hb) as mock_hb:
        summary = _materialize_scheduled_actions(job, manifest)

    # Helper called with v17 args, including pkg/job for marker attribution.
    mock_hb.assert_called_once_with(
        "personal-bot", "HEARTBEAT.md",
        "## Task Manager — Heartbeat Check",
        "Every heartbeat, run `python3 scripts/tasks.py check`.",
        pkg_id="p-9bfa1c84", job_id="j-pr10-test",
    )
    # Summary records success.
    assert summary[0]["status"] == "ok"
    assert summary[0]["artifact"] == "HEARTBEAT.md#Task Manager — Heartbeat Check"
    # Action stamped in-place.
    assert action["installed_by"] == "forge:j-pr10-test"
    assert action["installed_artifact"] == "HEARTBEAT.md#Task Manager — Heartbeat Check"
    assert action["installed_at"]   # non-empty ISO timestamp


def test_phase_4_5_dispatches_session_instruction_install() -> None:
    """oc_session_instruction uses the same dispatch path as
    oc_heartbeat_instruction; the file just differs (typically AGENTS.md)."""
    action = {
        "id": "session-summary",
        "mechanism": MECHANISM_OC_SESSION_INSTRUCTION,
        "install": {
            "file": "AGENTS.md",
            "section_anchor": "## Task Manager — Session Start",
            "body": "On session start, list open tasks.",
            "command": "python3 scripts/tasks.py list --status open",
        },
    }
    manifest = _manifest_with_actions(action)
    job = _job()

    fake_hb = {"ok": True, "artifact": "AGENTS.md#Task Manager — Session Start",
               "error": "", "already_present": False, "created_file": False}
    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               return_value=fake_hb) as mock_hb:
        _materialize_scheduled_actions(job, manifest)

    args, kwargs = mock_hb.call_args
    assert args[1] == "AGENTS.md"
    assert "Session Start" in args[2]


def test_phase_4_5_instruction_missing_required_fields_fails() -> None:
    """install.file / section_anchor / body are required — missing any one
    of them surfaces in the failure error so the operator knows what to fix."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {"file": "HEARTBEAT.md"},   # missing section_anchor, body
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"
    assert "section_anchor" in summary[0]["error"]
    assert "body" in summary[0]["error"]


# ── Deprecated mechanism — should surface clear remediation ─────────────────


def test_phase_4_5_deprecated_oc_heartbeat_hook_fails_with_remediation() -> None:
    """A manifest still carrying mechanism=oc_heartbeat_hook (because the
    operator hand-edited the file or migration was skipped) surfaces a
    clear remediation: run migrate_manifest first."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_HOOK,
        "install": {"hook_event": "heartbeat", "command": "python3 scripts/tasks.py check"},
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"
    assert "deprecated" in summary[0]["error"].lower()
    assert "migrate_manifest" in summary[0]["error"]
    assert "spec-heartbeat-instruction" in summary[0]["error"]
    # No stamp on the action — forge didn't install anything.
    assert "installed_at" not in action
    assert "installed_artifact" not in action


def test_phase_4_5_dispatches_launchd_install() -> None:
    """Legacy plist_xml shape — still supported for back-compat."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {
            "plist_label": "com.personal-bot.task-manager.check",
            "plist_xml": "<?xml version='1.0'?><plist><dict/></plist>",
        },
    }
    manifest = _manifest_with_actions(action)
    job = _job()

    fake_la = {
        "ok": True,
        "artifact": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-manager.check.plist",
        "error": "",
        "loaded": True,
    }
    with patch("evolve_admin.applications.install_helpers.install_launch_agent",
               return_value=fake_la) as mock_la:
        summary = _materialize_scheduled_actions(job, manifest)

    mock_la.assert_called_once()
    assert summary[0]["status"] == "ok"
    assert action["installed_artifact"].endswith(".plist")


def test_phase_4_5_dispatches_launchd_install_command_shape() -> None:
    """Canonical command-shape (post-2026-06-04 migration) — install.command
    + install.schedule routes to install_launchd_command_action, which is
    what every migrated gallery manifest will use."""
    action = {
        "id": "atlas-digest",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {
            "plist_label": "com.${bot_id}.atlas-digest",
            "command":     "/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
            "cwd":         "${workspace}",
            "schedule":    {"cron": {"Hour": 7, "Minute": 0}},
        },
    }
    manifest = _manifest_with_actions(action)
    manifest.bot_id = "atlas"
    job = _job()
    job.bot_id = "atlas"

    fake_la = {
        "ok": True,
        "artifact": "/Users/atlas/Library/LaunchAgents/com.atlas.atlas-digest.plist",
        "error": "",
        "loaded": True,
    }
    with patch(
        "evolve_admin.applications.install_helpers.install_launchd_command_action",
        return_value=fake_la,
    ) as mock_cmd, patch(
        "evolve_admin.applications.install_helpers.install_launch_agent",
    ) as mock_la_raw:
        summary = _materialize_scheduled_actions(job, manifest)

    # The command-shape helper is called; the legacy raw-plist helper is NOT.
    mock_cmd.assert_called_once()
    mock_la_raw.assert_not_called()

    # Check the dispatcher passed the install fields through unmodified —
    # substitution happens INSIDE install_launchd_command_action, not at
    # the dispatch boundary.
    call_kwargs = mock_cmd.call_args
    assert call_kwargs.args[0] == "atlas"   # bot_id
    assert call_kwargs.args[1] == "atlas-digest"  # action_id
    assert call_kwargs.args[2] == "com.${bot_id}.atlas-digest"
    assert call_kwargs.args[3] == "/bin/bash ${workspace}/scripts/atlas-digest-cron.sh"
    assert call_kwargs.args[4] == {"cron": {"Hour": 7, "Minute": 0}}
    assert call_kwargs.kwargs["cwd"] == "${workspace}"

    assert summary[0]["status"] == "ok"
    assert action["installed_artifact"].endswith(".plist")


def test_phase_4_5_launchd_command_without_schedule_fails() -> None:
    """install.command without install.schedule is a manifest authoring
    error — fail fast with a clear message."""
    action = {
        "id": "atlas-digest",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {
            "plist_label": "com.atlas.x",
            "command":     "/bin/bash run.sh",
            # No schedule!
        },
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"
    assert "schedule" in summary[0]["error"]


def test_phase_4_5_launchd_command_takes_precedence_over_plist_xml() -> None:
    """If a manifest supplies BOTH command and plist_xml (migration in
    progress, or both fields kept defensively), command wins. This is the
    canonical path post-2026-06-04; raw plist_xml is legacy fallback only."""
    action = {
        "id": "x",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {
            "plist_label": "com.atlas.x",
            "command":     "/bin/bash /tmp/x.sh",
            "schedule":    {"every_minutes": 5},
            "plist_xml":   "<?xml version='1.0'?><plist><dict/></plist>",
        },
    }
    manifest = _manifest_with_actions(action)
    fake_la = {"ok": True, "artifact": "/tmp/x.plist", "error": "", "loaded": True}
    with patch(
        "evolve_admin.applications.install_helpers.install_launchd_command_action",
        return_value=fake_la,
    ) as mock_cmd, patch(
        "evolve_admin.applications.install_helpers.install_launch_agent",
    ) as mock_la_raw:
        summary = _materialize_scheduled_actions(_job(), manifest)
    mock_cmd.assert_called_once()
    mock_la_raw.assert_not_called()
    assert summary[0]["status"] == "ok"


def test_phase_4_5_launchd_with_neither_shape_fails_loudly() -> None:
    """plist_label only, no command, no plist_xml → must fail with a
    message that names BOTH available shapes."""
    action = {
        "id": "x",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {"plist_label": "com.atlas.x"},
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"
    err = summary[0]["error"]
    assert "install.command" in err
    assert "install.plist_xml" in err


# ── Skip + failure paths ────────────────────────────────────────────────────


def test_phase_4_5_skips_unknown_mechanism() -> None:
    """mechanism=unknown means the scanner hasn't attributed yet — forge
    install can't do anything."""
    action = {"id": "discovered", "mechanism": MECHANISM_UNKNOWN, "install": {}}
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "skipped"
    # No stamp applied.
    assert "installed_by" not in action


def test_phase_4_5_skips_external_mechanism() -> None:
    action = {"id": "github-action", "mechanism": "external", "install": {}}
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "skipped"


def _instruction_install() -> dict:
    """Reusable install recipe for the v17 instruction mechanism."""
    return {
        "file": "HEARTBEAT.md",
        "section_anchor": "## Task Manager — Heartbeat Check",
        "body": "Every heartbeat, run `python3 scripts/tasks.py check`.",
        "command": "python3 scripts/tasks.py check",
    }


def test_phase_4_5_skips_already_installed_by_forge() -> None:
    """Idempotency: an action with installed_by=forge:* and a populated
    artifact is treated as already installed. Subsequent forge runs don't
    re-install it (and don't re-stamp installed_at — that would mask drift
    in the audit timeline)."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": _instruction_install(),
        "installed_artifact": "HEARTBEAT.md#Task Manager — Heartbeat Check",
        "installed_by": "forge:j-earlier-job",
        "installed_at": "2026-06-01T00:00:00Z",
    }
    manifest = _manifest_with_actions(action)

    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction") as mock_hb:
        summary = _materialize_scheduled_actions(_job(), manifest)

    assert summary[0]["status"] == "skipped"
    assert "prior forge run" in summary[0]["reason"]
    mock_hb.assert_not_called()
    # Stamps preserved (not overwritten).
    assert action["installed_by"] == "forge:j-earlier-job"
    assert action["installed_at"] == "2026-06-01T00:00:00Z"


def test_phase_4_5_does_not_skip_scanner_backfill() -> None:
    """An action whose installed_by starts with ``scanner:`` is NOT treated
    as already installed by forge — forge should install it for real this
    time and overwrite the backfill stamp."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": _instruction_install(),
        "installed_artifact": "HEARTBEAT.md#Task Manager — Heartbeat Check",
        "installed_by": "scanner:backfill",
    }
    manifest = _manifest_with_actions(action)

    fake_hb = {
        "ok": True,
        "artifact": "HEARTBEAT.md#Task Manager — Heartbeat Check",
        "error": "",
    }
    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               return_value=fake_hb):
        summary = _materialize_scheduled_actions(_job(), manifest)

    assert summary[0]["status"] == "ok"
    assert action["installed_by"].startswith("forge:")


def test_phase_4_5_records_failed_install_without_stamping() -> None:
    """A helper failure surfaces in the summary but does NOT stamp the
    action — leaving the install metadata absent is correct (forge didn't
    actually install)."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": _instruction_install(),
    }
    manifest = _manifest_with_actions(action)

    fake_hb = {"ok": False, "artifact": "", "error": "could not write HEARTBEAT.md"}
    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               return_value=fake_hb):
        summary = _materialize_scheduled_actions(_job(), manifest)

    assert summary[0]["status"] == "failed"
    assert "could not write" in summary[0]["error"]
    assert "installed_by" not in action
    assert "installed_artifact" not in action


def test_phase_4_5_validates_install_config_for_hook() -> None:
    """install.file/section_anchor/body are required for instruction
    mechanism. Missing any is a failure, not a skip."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {"file": "HEARTBEAT.md"},   # missing section_anchor, body
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"
    assert "section_anchor" in summary[0]["error"]
    assert "body" in summary[0]["error"]


def test_phase_4_5_validates_install_config_for_launchd() -> None:
    """launchd mechanism requires plist_label AND plist_xml."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD,
        "install": {"plist_label": "com.x.y"},  # no plist_xml
    }
    manifest = _manifest_with_actions(action)
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary[0]["status"] == "failed"


def test_phase_4_5_continues_on_individual_failure() -> None:
    """Failure on one action does NOT stop processing of the rest — Phase 4.5
    is per-action best-effort by design."""
    install_good = _instruction_install()
    install_bad = dict(install_good, section_anchor="## Task Manager — Archive",
                       body="Every heartbeat, run archive.",
                       command="python3 scripts/tasks.py archive")
    a1 = {"id": "good", "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
          "install": install_good}
    a2 = {"id": "bad",  "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
          "install": install_bad}
    manifest = _manifest_with_actions(a1, a2)

    def fake_hb(bot_id, file, section_anchor, body, **kwargs):
        # First call succeeds; second fails (matches the "archive" body).
        if "archive" in body:
            return {"ok": False, "artifact": "", "error": "validation failed"}
        return {"ok": True, "artifact": "HEARTBEAT.md#" + section_anchor.lstrip("# "),
                "error": ""}

    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               side_effect=fake_hb):
        summary = _materialize_scheduled_actions(_job(), manifest)

    assert len(summary) == 2
    assert summary[0]["status"] == "ok"
    assert summary[1]["status"] == "failed"


def test_phase_4_5_helper_exception_becomes_failed_entry() -> None:
    """If install_helpers raises (e.g. import failure, unexpected ValueError),
    Phase 4.5 catches it as a failed entry rather than aborting the apply
    phase."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": _instruction_install(),
    }
    manifest = _manifest_with_actions(action)

    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               side_effect=RuntimeError("workspace write failed")):
        summary = _materialize_scheduled_actions(_job(), manifest)

    assert summary[0]["status"] == "failed"
    assert "RuntimeError" in summary[0]["error"]


def test_phase_4_5_empty_scheduled_actions_returns_empty_summary() -> None:
    """No scheduled_actions → no work → empty summary, no errors."""
    manifest = _manifest_with_actions()
    summary = _materialize_scheduled_actions(_job(), manifest)
    assert summary == []


def test_phase_4_5_handles_non_dict_action_entries_gracefully() -> None:
    """Defensive: malformed scheduled_actions data doesn't crash."""
    manifest = _manifest_with_actions(
        "not a dict",
        {"id": "good", "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
         "install": _instruction_install()},
    )
    fake_hb = {"ok": True, "artifact": "HEARTBEAT.md#Task Manager — Heartbeat Check",
               "error": ""}
    with patch("evolve_admin.applications.install_helpers.install_heartbeat_instruction",
               return_value=fake_hb):
        summary = _materialize_scheduled_actions(_job(), manifest)
    # Only the real entry produced output; the string was silently skipped.
    assert len(summary) == 1
    assert summary[0]["action_id"] == "good"


# ── launchd_python_signal dispatch (v18) ────────────────────────────────────


def _python_signal_install(**overrides) -> dict:
    base = {
        "label": "com.personal-bot.task-check",
        "command": "python3 scripts/tasks.py check",
        "every": "30m",
        "signal_patterns": ["TASK_DUE:", "FOLLOWUP_NEEDED:"],
        "signal_type": "task_pending",
        "signal_severity": "info",
    }
    base.update(overrides)
    return base


def test_phase_4_5_dispatches_launchd_python_signal_install() -> None:
    """Happy path: mechanism=launchd_python_signal routes to
    install_python_signal_action and stamps on success."""
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": _python_signal_install(),
    }
    manifest = _manifest_with_actions(action)
    manifest.pkg_id = "p-c20a5564"
    job = _job(job_id="j-ta2-test")

    fake_ps = {
        "ok": True,
        "artifact": (
            "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-check.plist"
            "+/Users/personal-bot/.openclaw/workspace/evolve/scheduled/task-check.py"
        ),
        "error": "",
        "wrapper_path": "/Users/personal-bot/.openclaw/workspace/evolve/scheduled/task-check.py",
        "plist_path": "/Users/personal-bot/Library/LaunchAgents/com.personal-bot.task-check.plist",
        "loaded": True,
    }
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
        return_value=fake_ps,
    ) as mock_ps:
        summary = _materialize_scheduled_actions(job, manifest)

    # Helper received the right positional + keyword args.
    mock_ps.assert_called_once()
    args, kwargs = mock_ps.call_args
    bot_id, action_id, label, command, schedule, patterns = args
    assert bot_id == "personal-bot"
    assert action_id == "task-check"
    assert label == "com.personal-bot.task-check"
    assert command == "python3 scripts/tasks.py check"
    assert schedule == {"every_minutes": 30}   # parsed from "30m"
    assert patterns == ["TASK_DUE:", "FOLLOWUP_NEEDED:"]
    assert kwargs["signal_type"] == "task_pending"
    assert kwargs["pkg_id"] == "p-c20a5564"
    assert kwargs["job_id"] == "j-ta2-test"

    # Summary + action stamping.
    assert summary[0]["status"] == "ok"
    assert "task-check.plist" in summary[0]["artifact"]
    assert action["installed_by"] == "forge:j-ta2-test"
    assert action["installed_artifact"] == fake_ps["artifact"]
    assert action["installed_at"]


def test_phase_4_5_python_signal_supports_every_minutes_long_form() -> None:
    """Both ``every: "30m"`` and ``every_minutes: 30`` are accepted."""
    install = _python_signal_install()
    del install["every"]
    install["every_minutes"] = 45
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": install,
    }
    manifest = _manifest_with_actions(action)

    fake_ps = {"ok": True, "artifact": "/plist+/wrap", "error": ""}
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
        return_value=fake_ps,
    ) as mock_ps:
        _materialize_scheduled_actions(_job(), manifest)
    args, _kwargs = mock_ps.call_args
    schedule = args[4]
    assert schedule == {"every_minutes": 45}


def test_phase_4_5_python_signal_supports_cron_schedule() -> None:
    """cron schedule passes through to the helper unmodified."""
    install = _python_signal_install()
    del install["every"]
    install["schedule"] = {"cron": {"Hour": 0, "Minute": 30}}
    action = {
        "id": "daily-prune",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": install,
    }
    manifest = _manifest_with_actions(action)

    fake_ps = {"ok": True, "artifact": "/plist+/wrap", "error": ""}
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
        return_value=fake_ps,
    ) as mock_ps:
        _materialize_scheduled_actions(_job(), manifest)
    args, _kwargs = mock_ps.call_args
    schedule = args[4]
    assert schedule == {"cron": {"Hour": 0, "Minute": 30}}


def test_phase_4_5_python_signal_string_pattern_becomes_list() -> None:
    """Operator-friendly fallback: a single signal_patterns string is
    wrapped into a one-element list before dispatch."""
    install = _python_signal_install(signal_patterns="TASK_DUE:")
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": install,
    }
    manifest = _manifest_with_actions(action)
    fake_ps = {"ok": True, "artifact": "/plist+/wrap", "error": ""}
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
        return_value=fake_ps,
    ) as mock_ps:
        _materialize_scheduled_actions(_job(), manifest)
    args, _kwargs = mock_ps.call_args
    assert args[5] == ["TASK_DUE:"]


def test_phase_4_5_python_signal_missing_required_fields_fails() -> None:
    """Each of label, command, signal_patterns is required. Missing
    fields produce a clear failure entry without dispatching."""
    for missing in ("label", "command", "signal_patterns"):
        install = _python_signal_install()
        install[missing] = "" if missing != "signal_patterns" else []
        action = {
            "id": "task-check",
            "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
            "install": install,
        }
        manifest = _manifest_with_actions(action)
        with patch(
            "evolve_admin.applications.install_helpers.install_python_signal_action",
        ) as mock_ps:
            summary = _materialize_scheduled_actions(_job(), manifest)
        mock_ps.assert_not_called()
        assert summary[0]["status"] == "failed", f"missing {missing}"
        assert missing in summary[0]["error"]


def test_phase_4_5_python_signal_bad_schedule_surfaces_parser_error() -> None:
    """A bogus duration string ('30 minutes' / '2 days') is rejected at
    dispatch with the parser's error message — helper never runs."""
    install = _python_signal_install(every="thirty minutes")
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": install,
    }
    manifest = _manifest_with_actions(action)
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
    ) as mock_ps:
        summary = _materialize_scheduled_actions(_job(), manifest)
    mock_ps.assert_not_called()
    assert summary[0]["status"] == "failed"
    assert "duration" in summary[0]["error"] or "every" in summary[0]["error"]


def test_phase_4_5_python_signal_missing_any_schedule_fails() -> None:
    """An install block without every/every_minutes/schedule is rejected."""
    install = _python_signal_install()
    del install["every"]
    action = {
        "id": "task-check",
        "mechanism": MECHANISM_LAUNCHD_PYTHON_SIGNAL,
        "install": install,
    }
    manifest = _manifest_with_actions(action)
    with patch(
        "evolve_admin.applications.install_helpers.install_python_signal_action",
    ) as mock_ps:
        summary = _materialize_scheduled_actions(_job(), manifest)
    mock_ps.assert_not_called()
    assert summary[0]["status"] == "failed"
    assert "schedule" in summary[0]["error"]


# ── Failure visibility: stamps + job flag + Signals (audit slate S2) ────────
#
# _record_scheduled_action_outcomes is the layer that makes a Phase 4.5
# partial failure observable: install_error/install_failed_at on the manifest
# action entry, job.completed_with_errors, and a per-action Signal (producer
# forge_engine) keyed on (bot, app, action) so re-installs dedup and a later
# success resolves. Uses the REAL signals store on a tmp shared_dir — the
# store is plain temp-file+rename, no privileges needed.


def _sum_ok(action_id: str) -> dict:
    return {"action_id": action_id, "mechanism": "launchd",
            "status": "ok", "artifact": "/tmp/x.plist"}


def _sum_failed(action_id: str, error: str = "bootstrap refused: exit 5") -> dict:
    return {"action_id": action_id, "mechanism": "launchd",
            "status": "failed", "error": error}


def _firing(shared_dir):
    from signals import store as sig_store
    return list(sig_store.iter_signals(shared_dir, subdirs=("firing",)))


def test_record_outcomes_all_success_stays_quiet(tmp_path) -> None:
    """All actions installed → no flag, no stamps, no Signal (the unchanged
    happy path — this pins that S2 adds zero noise to healthy installs)."""
    action = {"id": "a1", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(action)
    job = _job()

    _record_scheduled_action_outcomes(job, manifest, [_sum_ok("a1")], tmp_path)

    assert job.completed_with_errors is False
    assert "install_error" not in action
    assert "install_failed_at" not in action
    assert _firing(tmp_path) == []


def test_record_outcomes_failure_flags_job_stamps_manifest_fires_signal(tmp_path) -> None:
    """One action fails → job.completed_with_errors True, the manifest entry
    carries install_error/install_failed_at (the enumerable declared-but-not-
    live marker), and a warn Signal fires with the per-action detail."""
    a1 = {"id": "good", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    a2 = {"id": "bad", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(a1, a2)
    job = _job()

    summary = [_sum_ok("good"), _sum_failed("bad")]
    _record_scheduled_action_outcomes(job, manifest, summary, tmp_path)

    # Job outcome visible.
    assert job.completed_with_errors is True
    # Manifest stamps: only the failed entry.
    assert "install_error" not in a1
    assert a2["install_error"] == "bootstrap refused: exit 5"
    assert a2["install_failed_at"]
    # Signal observed with the enumerable per-action detail.
    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.producer == "forge_engine"
    assert sig.type == "forge_scheduled_action_failed"
    assert sig.severity == "warn"
    assert sig.bot_id == "personal-bot"
    assert sig.signature == _scheduled_action_failure_signature(
        "personal-bot", "task-manager", "bad")
    assert sig.details["action_id"] == "bad"
    assert sig.details["mechanism"] == "launchd"
    assert sig.details["error"] == "bootstrap refused: exit 5"
    assert sig.details["declared_actions"] == 2
    assert sig.details["installed_actions"] == 1


def test_record_outcomes_repeat_failure_dedups_not_spams(tmp_path) -> None:
    """A re-install that fails the same action again bumps the existing
    Signal (signature keyed on bot/app/action, NOT job) — no sibling spam."""
    action = {"id": "bad", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(action)

    _record_scheduled_action_outcomes(
        _job("j-first"), manifest, [_sum_failed("bad")], tmp_path)
    _record_scheduled_action_outcomes(
        _job("j-second"), manifest, [_sum_failed("bad")], tmp_path)

    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    assert sigs[0].observation_count == 2


def test_record_outcomes_reinstall_success_resolves_signal(tmp_path) -> None:
    """A later successful install of the same action clears the manifest
    stamps AND resolves the prior Signal — targeted by signature, so other
    apps' still-live failures are untouched (no producer-wide sweep)."""
    action = {"id": "flaky", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(action)

    _record_scheduled_action_outcomes(
        _job("j-first"), manifest, [_sum_failed("flaky")], tmp_path)
    assert action["install_error"]
    assert len(_firing(tmp_path)) == 1

    # A DIFFERENT app's failure signal must survive the resolve below.
    other = _manifest_with_actions(
        {"id": "other-act", "mechanism": MECHANISM_LAUNCHD, "install": {}})
    other.id = "other-app"
    other_job = _job("j-other")
    other_job.app_id = "other-app"
    _record_scheduled_action_outcomes(
        other_job, other, [_sum_failed("other-act")], tmp_path)
    assert len(_firing(tmp_path)) == 2

    _record_scheduled_action_outcomes(
        _job("j-retry"), manifest, [_sum_ok("flaky")], tmp_path)

    assert "install_error" not in action
    assert "install_failed_at" not in action
    remaining = _firing(tmp_path)
    assert len(remaining) == 1
    assert remaining[0].details["app_id"] == "other-app"


def test_record_outcomes_clears_flag_on_same_job_rerun(tmp_path) -> None:
    """completed_with_errors is an unconditional (n_failed > 0) assignment,
    not a one-way latch: a second pass over the SAME job object that now
    succeeds resets the flag (defends the apply-actions / same-job path from
    badging a clean run 'complete (errors)')."""
    action = {"id": "a1", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(action)
    job = _job()

    _record_scheduled_action_outcomes(job, manifest, [_sum_failed("a1")], tmp_path)
    assert job.completed_with_errors is True

    _record_scheduled_action_outcomes(job, manifest, [_sum_ok("a1")], tmp_path)
    assert job.completed_with_errors is False


def test_record_outcomes_signal_store_failure_is_nonfatal(tmp_path) -> None:
    """Signal plumbing must never fail the apply: if observe() raises, the
    job flag and manifest stamps still land (the manifest save that follows
    in _apply_forge_output proceeds untouched)."""
    action = {"id": "bad", "mechanism": MECHANISM_LAUNCHD, "install": {}}
    manifest = _manifest_with_actions(action)
    job = _job()

    with patch("signals.store.observe",
               side_effect=RuntimeError("store exploded")):
        _record_scheduled_action_outcomes(
            job, manifest, [_sum_failed("bad")], tmp_path)

    assert job.completed_with_errors is True
    assert action["install_error"]


# ── Duration parser ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,minutes", [
    ("30m", 30),
    ("2h", 120),
    ("1d", 1440),
    ("7d", 7 * 1440),
    ("  15m  ", 15),
    ("60s", 1),
    ("M", None),         # missing digits
    ("30", None),        # missing unit
    ("0m", None),        # sub-minute
    ("30 minutes", None),
    ("h", None),
    ("", None),
    (None, None),
])
def test_parse_every_duration(text, minutes) -> None:
    assert _parse_every_duration(text) == minutes


# ── Schedule normaliser priority ────────────────────────────────────────────


def test_normalise_schedule_prefers_every_over_every_minutes() -> None:
    """When both ``every`` and ``every_minutes`` are set, ``every`` wins
    (operator-facing form takes precedence over the legacy form)."""
    out, err = _normalise_schedule({"every": "1h", "every_minutes": 999})
    assert err is None
    assert out == {"every_minutes": 60}


def test_normalise_schedule_falls_back_to_schedule_dict() -> None:
    """When neither every/every_minutes is set, a raw schedule dict
    passes through (back-compat with whatever the helper already
    accepts)."""
    out, err = _normalise_schedule({"schedule": {"cron": {"Minute": 0}}})
    assert err is None
    assert out == {"cron": {"Minute": 0}}


def test_normalise_schedule_rejects_raw_schedule_with_no_known_keys() -> None:
    """Helper-shape passthrough requires every_minutes or cron — anything
    else is an error to catch typos."""
    out, err = _normalise_schedule({"schedule": {"frequency": "30m"}})
    assert out is None
    assert "every_minutes" in err and "cron" in err


def test_normalise_schedule_no_schedule_at_all_is_error() -> None:
    out, err = _normalise_schedule({"command": "python3 x.py"})
    assert out is None
    assert "schedule" in err

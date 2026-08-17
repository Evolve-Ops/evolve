"""tests/test_install_python_signal.py — launchd_python_signal install helper.

Spec: docs/spec-launchd-python-signal-2026-06-03.md.

Covers ``install_python_signal_action`` and its companion plist
renderer ``_build_plist_xml``. The wrapper-script template
``_WRAPPER_TEMPLATE`` is exercised by formatting it with concrete
values and asserting the resulting Python parses + carries the
expected runtime behavior.

The Signal-store side of the wrapper (``signals.store.observe``) is
exercised indirectly: we generate a wrapper, ``compile()`` it to
catch syntax errors, then ``exec()`` it under a stubbed ``signals``
module to verify the wrapper's signal-pattern matching + selective
write behavior.

Scope is the helper layer: HTTP routing (admin-daemon endpoint) +
Phase 4.5 dispatcher are covered in follow-on PRs (T-A.2, T-A.4).
"""

from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.install_helpers import (  # noqa: E402
    _WRAPPER_TEMPLATE,
    _build_plist_xml,
    install_python_signal_action,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_bot_layout(tmp_path: Path, monkeypatch):
    """A fake bot workspace under tmp_path. Patches ``get_bot_user`` and
    ``load_network`` so the helper resolves into the temp tree, and
    stubs ``install_launchd_system_daemon`` so we don't actually call launchctl.

    Returns a dict with ``workspace``, ``scheduled_dir``, and
    ``la_calls`` (a list capturing every call into the launch-agent
    helper)."""
    bot_user = "team-bot-a"
    workspace = tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.get_bot_user",
        lambda bot_id, network=None: bot_user,
    )
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.load_network",
        lambda *_a, **_kw: {"bots": {"team-bot-a": {}}},
    )

    # Re-target the workspace path constant used inside the helper.
    # The helper builds workspace from /Users/{bot_user}/..., so we
    # monkey-patch Path to redirect that subtree.
    real_path = Path

    class _PathRouter:
        @staticmethod
        def __call__(s):
            if isinstance(s, str) and s.startswith(f"/Users/{bot_user}/"):
                return real_path(str(tmp_path) + s)
            return real_path(s)

    # The helper uses ``Path(f"/Users/{bot_user}/.openclaw/workspace")``.
    # Rather than rewrite Path globally, intercept at the helper's
    # module namespace.
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.Path",
        lambda s: real_path(
            str(tmp_path) + s
            if isinstance(s, str) and s.startswith("/Users/")
            else s
        ),
    )

    la_calls: list[dict] = []

    def fake_launch_agent(bot_id, label, plist_xml, *, network=None, bootstrap=True):
        la_calls.append({
            "bot_id": bot_id, "label": label,
            "plist_xml": plist_xml, "bootstrap": bootstrap,
        })
        return {
            "ok": True,
            "artifact": f"/Users/{bot_user}/Library/LaunchAgents/{label}.plist",
            "error": "",
            "loaded": bootstrap,
        }

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.install_launchd_system_daemon",
        fake_launch_agent,
    )

    return {
        "workspace": workspace,
        "scheduled_dir": workspace / "evolve" / "scheduled",
        "la_calls": la_calls,
        "bot_user": bot_user,
    }


# ── _build_plist_xml ─────────────────────────────────────────────────────────


def test_plist_every_minutes_emits_start_interval():
    xml = _build_plist_xml(
        label="com.team-bot-a.task-check",
        wrapper_path="/foo/wrap.py",
        schedule={"every_minutes": 30},
        log_path="/tmp/log",
        err_log_path="/tmp/err",
    )
    assert "<key>StartInterval</key>" in xml
    assert "<integer>1800</integer>" in xml   # 30 * 60
    assert "<key>StartCalendarInterval</key>" not in xml
    assert "com.team-bot-a.task-check" in xml
    # Venv python, not /usr/bin/env python3 — the wrapper imports `signals`
    # from the evolve-analyzer package, which only the venv has installed.
    assert "<string>/Users/Shared/evolve-venv/bin/python3</string>" in xml
    assert "<string>/foo/wrap.py</string>" in xml


def test_plist_cron_emits_start_calendar_interval():
    xml = _build_plist_xml(
        label="com.team-bot-a.daily-prune",
        wrapper_path="/foo/wrap.py",
        schedule={"cron": {"Hour": 0, "Minute": 30}},
        log_path="/tmp/log",
        err_log_path="/tmp/err",
    )
    assert "<key>StartCalendarInterval</key>" in xml
    assert "<key>Hour</key>" in xml
    assert "<integer>0</integer>" in xml
    assert "<key>Minute</key>" in xml
    assert "<integer>30</integer>" in xml
    assert "<key>StartInterval</key>" not in xml


def test_plist_rejects_zero_every_minutes():
    with pytest.raises(ValueError, match=">= 1"):
        _build_plist_xml(
            label="x", wrapper_path="/x", schedule={"every_minutes": 0},
            log_path="/tmp/o", err_log_path="/tmp/e",
        )


def test_plist_rejects_no_schedule():
    with pytest.raises(ValueError, match="either every_minutes or cron"):
        _build_plist_xml(
            label="x", wrapper_path="/x", schedule={},
            log_path="/tmp/o", err_log_path="/tmp/e",
        )


def test_plist_rejects_empty_cron_dict():
    with pytest.raises(ValueError, match="non-empty"):
        _build_plist_xml(
            label="x", wrapper_path="/x", schedule={"cron": {}},
            log_path="/tmp/o", err_log_path="/tmp/e",
        )


# ── install_python_signal_action — input validation ─────────────────────────


def test_install_requires_bot_id_action_id_label_command(fake_bot_layout):
    for missing in ("bot_id", "action_id", "label", "command"):
        kwargs = dict(
            bot_id="team-bot-a", action_id="task-check",
            label="com.team-bot-a.task-check",
            command="python3 scripts/tasks.py check",
            schedule={"every_minutes": 30},
            signal_patterns=["TASK_DUE:"],
            bootstrap=False,
        )
        kwargs[missing] = ""
        result = install_python_signal_action(**kwargs)
        assert result["ok"] is False, f"missing {missing}: {result}"
        assert "requires" in result["error"]


def test_install_requires_nonempty_signal_patterns(fake_bot_layout):
    result = install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule={"every_minutes": 30},
        signal_patterns=[],
        bootstrap=False,
    )
    assert result["ok"] is False
    assert "signal_patterns" in result["error"]


def test_install_requires_schedule_dict(fake_bot_layout):
    result = install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule="every_30m",  # type: ignore[arg-type]
        signal_patterns=["TASK_DUE:"],
        bootstrap=False,
    )
    assert result["ok"] is False
    assert "schedule" in result["error"]


def test_install_rejects_label_with_slash_or_plist_suffix(fake_bot_layout):
    for bad in ("com/team-bot-a/task-check", "com.team-bot-a.task.plist"):
        result = install_python_signal_action(
            bot_id="team-bot-a", action_id="task-check",
            label=bad,
            command="python3 scripts/tasks.py check",
            schedule={"every_minutes": 30},
            signal_patterns=["TASK_DUE:"],
            bootstrap=False,
        )
        assert result["ok"] is False, f"expected reject for {bad!r}"
        assert "label" in result["error"]


# ── install_python_signal_action — happy path ───────────────────────────────


def test_install_happy_path_writes_wrapper_and_plist(fake_bot_layout):
    result = install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule={"every_minutes": 30},
        signal_patterns=["TASK_DUE:", "FOLLOWUP_NEEDED:"],
        signal_type="task_pending",
        pkg_id="p-c20a5564", job_id="j-deadbeef",
        bootstrap=False,
    )
    assert result["ok"] is True, result
    assert result["wrapper_path"].endswith("evolve/scheduled/task-check.py")
    assert result["plist_path"].endswith(
        "Library/LaunchAgents/com.team-bot-a.task-check.plist"
    )
    # Combined artifact for downstream consumers.
    assert "+" in result["artifact"]

    # Wrapper landed on disk and is executable.
    wp = Path(result["wrapper_path"])
    assert wp.is_file()
    mode = wp.stat().st_mode & 0o777
    assert mode & 0o111, f"wrapper not executable: 0o{mode:o}"

    # Wrapper content carries the install-time config.
    body = wp.read_text()
    assert "python3 scripts/tasks.py check" in body
    assert "TASK_DUE:" in body
    assert "FOLLOWUP_NEEDED:" in body
    assert "p-c20a5564" in body
    assert "j-deadbeef" in body
    assert "task_pending" in body

    # Plist was passed to install_launchd_system_daemon with the right label
    # and shape.
    assert len(fake_bot_layout["la_calls"]) == 1
    la = fake_bot_layout["la_calls"][0]
    assert la["label"] == "com.team-bot-a.task-check"
    assert la["bootstrap"] is False  # we asked to skip
    assert "StartInterval" in la["plist_xml"]


def test_install_defaults_cwd_to_workspace(fake_bot_layout):
    """Blank ``cwd`` should default to the bot's workspace root, so a
    relative ``command`` resolves correctly."""
    install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule={"every_minutes": 30},
        signal_patterns=["TASK_DUE:"],
        bootstrap=False,
    )
    wp = fake_bot_layout["scheduled_dir"] / "task-check.py"
    body = wp.read_text()
    # The workspace path appears as the CWD constant in the wrapper.
    assert "/.openclaw/workspace" in body


def test_install_carries_explicit_cwd_through_to_wrapper(fake_bot_layout):
    install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule={"every_minutes": 30},
        signal_patterns=["TASK_DUE:"],
        cwd="/Users/team-bot-a/.openclaw/workspace/scripts",
        bootstrap=False,
    )
    wp = fake_bot_layout["scheduled_dir"] / "task-check.py"
    body = wp.read_text()
    assert "/scripts" in body


def test_install_is_idempotent(fake_bot_layout):
    """Re-installing the same action overwrites cleanly."""
    for _ in range(3):
        result = install_python_signal_action(
            bot_id="team-bot-a", action_id="task-check",
            label="com.team-bot-a.task-check",
            command="python3 scripts/tasks.py check",
            schedule={"every_minutes": 30},
            signal_patterns=["TASK_DUE:"],
            bootstrap=False,
        )
        assert result["ok"] is True
    # Wrapper still exists and parses.
    wp = fake_bot_layout["scheduled_dir"] / "task-check.py"
    assert wp.is_file()
    compile(wp.read_text(), str(wp), "exec")


def test_install_plist_write_failure_surfaces_with_wrapper_intact(fake_bot_layout, monkeypatch):
    """If install_launchd_system_daemon fails after the wrapper landed, the
    error surfaces but the wrapper is preserved so the operator can
    re-try just the plist step."""

    def fake_la_fail(*args, **kwargs):
        return {"ok": False, "artifact": "", "error": "plist permission denied"}

    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.install_launchd_system_daemon",
        fake_la_fail,
    )
    result = install_python_signal_action(
        bot_id="team-bot-a", action_id="task-check",
        label="com.team-bot-a.task-check",
        command="python3 scripts/tasks.py check",
        schedule={"every_minutes": 30},
        signal_patterns=["TASK_DUE:"],
        bootstrap=False,
    )
    assert result["ok"] is False
    assert "plist permission denied" in result["error"]
    # Wrapper preserved.
    wp = fake_bot_layout["scheduled_dir"] / "task-check.py"
    assert wp.is_file()
    # And surfaced in the error envelope for the operator.
    assert result.get("wrapper_path", "").endswith("task-check.py")


# ── Wrapper template — syntax + behaviour ───────────────────────────────────


def _render_wrapper(*, command, patterns):
    """Render the wrapper template with minimal stub values."""
    import shlex
    return _WRAPPER_TEMPLATE.format(
        label="com.team-bot-a.task-check",
        pkg_id="p-c20a5564", job_id="j-deadbeef",
        command_repr=repr(command),
        command_argv_repr=repr(shlex.split(command)),
        cwd_repr=repr("/Users/team-bot-a/.openclaw/workspace"),
        patterns_repr=repr(patterns),
        signal_type_repr=repr("task_pending"),
        signal_severity_repr=repr("info"),
        bot_id_repr=repr("team-bot-a"),
        app_id_repr=repr("app_task_manager"),
        shared_dir_repr=repr("/Users/Shared/evolve"),
        label_repr=repr("com.team-bot-a.task-check"),
        analyzer_pkg_repr=repr("/Users/Shared/evolve-repo/packages/analyzer"),
    )


def test_wrapper_template_parses_as_valid_python():
    body = _render_wrapper(
        command="python3 scripts/tasks.py check",
        patterns=["TASK_DUE:", "FOLLOWUP_NEEDED:"],
    )
    # syntax error here = bug in the template
    compile(body, "<wrapper>", "exec")


def test_wrapper_no_pattern_match_silent_exit_no_signal_write(tmp_path):
    """The cost-critical path: when stdout has no signal lines, the
    wrapper exits 0 and never touches the Signal store."""
    body = _render_wrapper(
        command="echo 'nothing to see here'",  # not a real shell command — we patch _run_command
        patterns=["TASK_DUE:"],
    )
    ns: dict = {}
    exec(compile(body, "<wrapper>", "exec"), ns)

    # Replace _run_command to return clean stdout.
    ns["_run_command"] = lambda: ("just a normal line\nanother normal line\n", 0)
    # Stub signal_store to fail loudly if called — should NOT be called.
    write_counter = mock.Mock()
    ns["_write_signal"] = write_counter

    rc = ns["main"]()
    assert rc == 0
    write_counter.assert_not_called()


def test_wrapper_pattern_match_triggers_signal_write(tmp_path):
    """When stdout has a signal-pattern line, the wrapper calls the
    Signal-store write path."""
    body = _render_wrapper(
        command="python3 scripts/tasks.py check",
        patterns=["TASK_DUE:", "FOLLOWUP_NEEDED:"],
    )
    ns: dict = {}
    exec(compile(body, "<wrapper>", "exec"), ns)

    ns["_run_command"] = lambda: (
        "checked 3 tasks\n"
        "TASK_DUE: OP-0007 — Q3 planning doc\n"
        "FOLLOWUP_NEEDED: OP-0009 — vendor reply\n"
        "FOLLOWUP_NEEDED: OP-0011 — site visit\n",
        0,
    )
    captured = mock.Mock(return_value=0)
    ns["_write_signal"] = captured

    rc = ns["main"]()
    assert rc == 0
    # Exactly one Signal write per invocation, regardless of how many
    # matched lines (matches are summarised in the Signal's details).
    captured.assert_called_once()
    matched = captured.call_args[0][0]
    assert any("TASK_DUE: OP-0007" in m for m in matched)
    assert any("FOLLOWUP_NEEDED: OP-0009" in m for m in matched)
    # Non-matching line excluded.
    assert not any("checked 3 tasks" in m for m in matched)


def test_wrapper_handles_command_timeout_silently(tmp_path):
    """A subprocess.TimeoutExpired (modelled by _run_command returning
    (None, None)) must not crash the wrapper or write a Signal."""
    body = _render_wrapper(
        command="sleep 9999",
        patterns=["TASK_DUE:"],
    )
    ns: dict = {}
    exec(compile(body, "<wrapper>", "exec"), ns)

    ns["_run_command"] = lambda: (None, None)
    write_counter = mock.Mock()
    ns["_write_signal"] = write_counter

    rc = ns["main"]()
    assert rc == 0
    write_counter.assert_not_called()


def test_wrapper_signal_write_failure_returns_nonzero(tmp_path):
    """When the Signal-store write fails, ``main`` returns the
    non-zero exit code so launchd can record the failure."""
    body = _render_wrapper(
        command="python3 scripts/tasks.py check",
        patterns=["TASK_DUE:"],
    )
    ns: dict = {}
    exec(compile(body, "<wrapper>", "exec"), ns)

    ns["_run_command"] = lambda: ("TASK_DUE: OP-0007\n", 0)
    ns["_write_signal"] = mock.Mock(return_value=1)

    rc = ns["main"]()
    assert rc == 1

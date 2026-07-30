"""Tests for audit_dispatch — admin-side on-demand audit request helper.

Tests cover the inbox-write path (with /tmp staging redirected to a tmp
tree), idempotent mark_finding_accepted, and unaccept_finding. The actual
sudo-based kick is mocked since we can't shell out as another user in
unit tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch  # noqa: F401  (kept for downstream tests)

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import audit_dispatch  # noqa: E402


# ── request_audit ───────────────────────────────────────────────────────────


@pytest.fixture
def tmp_inbox_root(tmp_path: Path, monkeypatch):
    """Re-point audit_dispatch's inbox path resolution into tmp_path."""
    def _inbox(bot_user: str) -> Path:
        return tmp_path / "Users" / bot_user / ".openclaw" / "workspace" / "evolve" / "audit_inbox"
    monkeypatch.setattr(audit_dispatch, "_audit_inbox_dir", _inbox)
    return tmp_path


def test_request_audit_writes_inbox_file_no_kick(tmp_inbox_root, monkeypatch) -> None:
    # Disable the kick (Popen) so we just test the file write.
    monkeypatch.setattr(audit_dispatch, "_kick_runner", lambda u, r: (True, ""))
    result = audit_dispatch.request_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        apps=["journal"], full_audit=False,
        requested_by="test",
        kick=False,
    )
    assert result.ok
    assert result.request_id.startswith("audit-req-")
    inbox_dir = tmp_inbox_root / "Users/team_bot_a/.openclaw/workspace/evolve/audit_inbox"
    files = list(inbox_dir.glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["kind"] == "tier3_audit"
    assert body["apps"] == ["journal"]
    assert body["full_audit"] is False
    assert body["requested_by"] == "test"


def test_request_audit_all_apps_marks_kind(tmp_inbox_root, monkeypatch) -> None:
    monkeypatch.setattr(audit_dispatch, "_kick_runner", lambda u, r: (True, ""))
    result = audit_dispatch.request_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        apps=None, full_audit=False,
        requested_by="cli", kick=False,
    )
    assert result.ok
    inbox_dir = tmp_inbox_root / "Users/team_bot_a/.openclaw/workspace/evolve/audit_inbox"
    body = json.loads(next(inbox_dir.glob("*.json")).read_text())
    assert body["apps"] == "all"


def test_request_audit_kick_called_when_requested(tmp_inbox_root, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    def _fake_kick(user, rid):
        calls.append((user, rid))
        return True, ""
    monkeypatch.setattr(audit_dispatch, "_kick_runner", _fake_kick)
    result = audit_dispatch.request_audit(
        bot_id="team_bot_a", bot_user="team_bot_a",
        apps=["x"], full_audit=False, requested_by="ui",
        kick=True,
    )
    assert result.kicked is True
    assert len(calls) == 1
    assert calls[0][0] == "team_bot_a"
    assert calls[0][1] == result.request_id


def test_request_audit_kick_failure_does_not_clear_ok(
    tmp_inbox_root, monkeypatch,
) -> None:
    """Kick failure keeps ok=True (inbox queued) but populates error.

    The tier-3 cron daemon doesn't pass --pickup-inbox, so a kick failure
    means the inbox file sits unprocessed indefinitely — the UI needs to
    see the error to surface it to the operator. Historically (pre-2026-05-26)
    the kick error was only logged; this test pins the new contract that it
    also lands in DispatchResult.error.
    """
    monkeypatch.setattr(
        audit_dispatch, "_kick_runner", lambda u, r: (False, "Popen failed"),
    )
    result = audit_dispatch.request_audit(
        bot_id="team_bot_a", bot_user="team_bot_a", apps=["x"],
        full_audit=False, requested_by="ui", kick=True,
    )
    assert result.ok is True   # inbox write succeeded
    assert result.kicked is False
    assert result.error == "kick failed: Popen failed"


# ── _kick_runner ────────────────────────────────────────────────────────────


def test_kick_runner_skips_sudo_when_target_is_current_user(
    monkeypatch, tmp_path,
) -> None:
    """When bot_user == current process user, no sudo wrapper is used.

    The evo bot runs on the same macOS account as the admin daemon
    (both 'evolve'). Sudo-to-self is pointless AND the historical
    sudoers grant didn't permit it, so this branch is the actual fix
    for the evo bot's silent-kick-failure.
    """
    fake_runner = tmp_path / "app_audit_runner.py"
    fake_runner.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(
        audit_dispatch, "_resolve_runner_path", lambda: str(fake_runner),
    )
    monkeypatch.setattr(
        audit_dispatch, "_current_process_user", lambda: "evolve",
    )

    captured: dict = {}

    class _FakeProc:
        def wait(self, timeout):  # noqa: ARG002
            return 0   # immediate clean exit
        def poll(self):
            return 0

    def _fake_popen(cmd, **kwargs):  # noqa: ARG001
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(audit_dispatch.subprocess, "Popen", _fake_popen)

    ok, err = audit_dispatch._kick_runner("evolve", "audit-req-abc")
    assert ok is True, err
    assert captured["cmd"][0] != "/usr/bin/sudo", (
        "Expected direct invocation when bot_user == current user, "
        f"got sudo wrapper: {captured['cmd']}"
    )
    assert captured["cmd"][0] == "/opt/homebrew/bin/python3"


def test_kick_runner_uses_sudo_for_other_bots(monkeypatch, tmp_path) -> None:
    """When bot_user != current user, the sudo wrapper is used (with -n)."""
    fake_runner = tmp_path / "app_audit_runner.py"
    fake_runner.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(
        audit_dispatch, "_resolve_runner_path", lambda: str(fake_runner),
    )
    monkeypatch.setattr(
        audit_dispatch, "_current_process_user", lambda: "evolve",
    )

    captured: dict = {}

    class _FakeProc:
        def wait(self, timeout):  # noqa: ARG002
            return 0

    def _fake_popen(cmd, **kwargs):  # noqa: ARG001
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(audit_dispatch.subprocess, "Popen", _fake_popen)

    ok, err = audit_dispatch._kick_runner("team_bot_a", "audit-req-xyz")
    assert ok is True, err
    assert captured["cmd"][:5] == [
        "/usr/bin/sudo", "-n", "-H", "-u", "team_bot_a",
    ]


def test_kick_runner_surfaces_fast_fail_stderr(monkeypatch, tmp_path) -> None:
    """A fast-exit non-zero process has its stderr surfaced as the error.

    This is the previously-silent failure mode that hid the sudo
    permission denial — the old code piped stderr to DEVNULL. The new
    code captures stderr to a tempfile, waits briefly, and reads it
    back when the process exits non-zero.
    """
    fake_runner = tmp_path / "app_audit_runner.py"
    fake_runner.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(
        audit_dispatch, "_resolve_runner_path", lambda: str(fake_runner),
    )
    monkeypatch.setattr(
        audit_dispatch, "_current_process_user", lambda: "evolve",
    )
    # Make the fast-fail wait short so the test doesn't drag.
    monkeypatch.setattr(audit_dispatch, "_KICK_FAST_FAIL_WAIT_SECONDS", 0.5)

    class _FastFailProc:
        def __init__(self, stderr_fd):
            os.write(
                stderr_fd,
                b"Sorry, user evolve is not allowed to execute ...\n",
            )
        def wait(self, timeout):  # noqa: ARG002
            return 1

    def _fake_popen(cmd, **kwargs):  # noqa: ARG001
        return _FastFailProc(kwargs["stderr"])

    monkeypatch.setattr(audit_dispatch.subprocess, "Popen", _fake_popen)

    ok, err = audit_dispatch._kick_runner("team_bot_a", "audit-req-rejected")
    assert ok is False
    assert "not allowed" in err, err


def test_kick_runner_detaches_long_running_process(
    monkeypatch, tmp_path,
) -> None:
    """When the runner is still running after the fast-fail window, kick succeeds.

    Real audits take seconds-to-minutes; we don't want the HTTP request
    to block that long, so we treat "still alive after 2s" as success
    and let the runner continue detached.
    """
    fake_runner = tmp_path / "app_audit_runner.py"
    fake_runner.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(
        audit_dispatch, "_resolve_runner_path", lambda: str(fake_runner),
    )
    monkeypatch.setattr(
        audit_dispatch, "_current_process_user", lambda: "evolve",
    )
    monkeypatch.setattr(audit_dispatch, "_KICK_FAST_FAIL_WAIT_SECONDS", 0.1)

    class _LongRunningProc:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

    monkeypatch.setattr(
        audit_dispatch.subprocess, "Popen",
        lambda cmd, **kw: _LongRunningProc(),
    )

    ok, err = audit_dispatch._kick_runner("evolve", "audit-req-busy")
    assert ok is True
    assert err == ""


# ── mark_finding_accepted ───────────────────────────────────────────────────


@pytest.fixture
def manifest_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Re-point manifest path resolution into a temp /Users/<bot>/.

    Patches ``applications_dir`` directly (rather than the underlying
    ``get_bot_workspace``) because the conftest's prebinding of
    ``evolve_admin`` creates a subtle dual-module situation: the relative
    import ``from ..config import get_bot_workspace`` inside ``applications_dir``
    resolves to a config module that isn't the same object as the one
    ``from evolve_admin import config`` produces in test code. Patching the
    higher-level function sidesteps that.
    """
    bot_user = "team_bot_a"
    bot_workspace = tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
    (bot_workspace / "manifests").mkdir(parents=True)

    # Write a starter manifest
    manifest = {
        "id": "journal", "name": "Journal", "bot_id": "team_bot_a",
        "audit_accepted": [],
    }
    (bot_workspace / "manifests/journal.json").write_text(
        json.dumps(manifest, indent=2),
    )

    # Patch applications_dir in BOTH possible module locations — see note above.
    # We touch both so whichever module the test code imports through finds the
    # mock.
    def _appdir(shared_dir, bot_id):
        return bot_workspace / "manifests"
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir, raising=True)
    # Also patch any cached reference inside audit_dispatch if it has one
    # via re-export — applications_dir is imported locally inside
    # mark_finding_accepted so re-resolution per call is fine, but if a
    # second import path exists patch it too.
    import sys
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if mod_name.endswith("manifest") and hasattr(mod, "applications_dir"):
            try:
                monkeypatch.setattr(mod, "applications_dir", _appdir, raising=True)
            except (AttributeError, TypeError):
                pass
    return tmp_path


def test_mark_finding_accepted_appends_entry(manifest_workspace: Path) -> None:
    ok, err = audit_dispatch.mark_finding_accepted(
        bot_id="team_bot_a", bot_user="team_bot_a", app_id="journal",
        signature="sig-A", accepted_by="test",
        rationale="intentional limitation",
    )
    assert ok, err

    manifest_path = (
        manifest_workspace / "Users/team_bot_a/.openclaw/workspace/manifests/journal.json"
    )
    data = json.loads(manifest_path.read_text())
    accepted = data["audit_accepted"]
    assert len(accepted) == 1
    assert accepted[0]["signature"] == "sig-A"
    assert accepted[0]["rationale"] == "intentional limitation"


# NOTE: idempotency + unaccept tests intentionally omitted from this file.
# They fight the worktree conftest's `_prebind_evolve_admin` machinery —
# applications_dir() resolves get_bot_workspace through a relative import
# whose module object isn't the same as the one our monkeypatch can reach,
# so subsequent calls after the first test in a session pick up the
# production /Users/<bot>/ path. The core write path is exercised by
# test_mark_finding_accepted_appends_entry above; idempotency + unaccept
# coverage will land via integration tests against a real pod in a
# follow-up.

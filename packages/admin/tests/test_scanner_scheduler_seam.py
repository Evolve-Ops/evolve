"""Bite 5b — scanner._snapshot_launchctl_labels goes through the platform-
portable Scheduler seam, NOT a module-global LaunchdScheduler() handle.

The snapshot feeds launchd/systemd labels into the per-bot app-scan LLM
context (useful when a behavior references a specific scheduled job). It is
read-only and must self-empty to ``[]`` on any failure — never crash.

* macOS: the probe must run AS THE BOT USER (it enumerates the bot's own
  launchd domain), which the adapter's sudo prefix can't express. We
  guarded-derive an unsudo'd launchd adapter and inject a runner that wraps
  the adapter-built argv in ``sudo -u <bot_user>`` (cwd /Users/Shared, 5s
  timeout) — byte-identical to the pre-seam handle.
* systemd: the bot's gateway is a system unit the daemon already sees, so
  ``get_scheduler().list()`` answers truthfully (the real label set) with no
  run-as-bot indirection.

Convention (mirrors tests/test_mcp_service_scheduler_seam.py): inject the
adapter via ``set_scheduler()`` and reset on teardown (MANDATORY — a leaked
fake singleton poisons later tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.applications import scanner as _scanner  # noqa: E402
from evolve_admin.runtime import (  # noqa: E402
    LaunchdScheduler,
    SystemdScheduler,
    set_scheduler,
)
from platform_profile import LINUX, set_profile  # noqa: E402

BOT_ID = "team_bot_a"
BOT_USER = "team_bot_a"


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)
    set_profile(None)


@pytest.fixture(autouse=True)
def _stub_bot_user(monkeypatch):
    """Resolve the bot user without touching a real network.json."""
    monkeypatch.setattr(_scanner, "load_network", lambda: {})
    monkeypatch.setattr(_scanner, "get_bot_user", lambda bot_id, net: BOT_USER)


def test_snapshot_macos_wraps_list_argv_in_sudo_u_bot(monkeypatch):
    """On macOS the adapter-built ``/bin/launchctl list`` argv is wrapped in
    ``sudo -u <bot_user>`` by the injected runner — the load-bearing run-as-bot
    posture, byte-identical to the pre-seam handle. The parsed label set flows
    through to the snapshot."""
    captured: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        captured.append(list(argv))

        class _R:
            returncode = 0
            stdout = "PID\tStatus\tLabel\n7\t0\tcom.team_bot_a.app\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(_scanner.subprocess, "run", _fake_run)
    set_scheduler(LaunchdScheduler())  # default sudo adapter; the runner override

    labels = _scanner._snapshot_launchctl_labels(BOT_ID)

    assert labels == ["com.team_bot_a.app"]
    # The bot-scoped wrapper prefixes the adapter argv with `sudo -u <bot>`,
    # and the adapter itself is no-sudo (no leading `sudo` from the adapter).
    assert len(captured) == 1
    argv = captured[0]
    assert argv[:3] == ["sudo", "-u", BOT_USER]
    assert "/bin/launchctl" in argv
    assert "list" in argv


def test_snapshot_on_systemd_uses_seam_list_no_run_as_bot(tmp_path):
    """On a Linux pod the snapshot uses the injected SystemdScheduler's list()
    directly — the real system-unit label set, NEVER a ``sudo -u <bot>``
    launchctl wrap. The bot gateway is a system unit the daemon already sees."""
    calls: list[list[str]] = []

    def _reply(argv):
        calls.append(list(argv))
        # list-units → one loaded gateway service; list-timers → empty.
        if "list-units" in argv:
            return 0, "ai.openclaw.team_bot_a-gateway.service loaded active running\n", ""
        return 0, "", ""

    set_scheduler(
        SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=_reply)
    )
    set_profile(LINUX)

    labels = _scanner._snapshot_launchctl_labels(BOT_ID)

    assert labels == ["ai.openclaw.team_bot_a-gateway"]
    # Only systemctl was invoked — never launchctl, never sudo -u.
    assert calls
    assert all("launchctl" not in " ".join(c) for c in calls)
    assert all(c[:2] != ["sudo", "-u"] for c in calls)


def test_snapshot_empty_on_systemd_list_failure(tmp_path):
    """A systemd list() failure self-empties to ``[]`` — read-only, never a
    crash that aborts the scan."""

    def _boom(argv):
        return 1, "", "systemctl: command not found"

    set_scheduler(
        SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=_boom)
    )
    set_profile(LINUX)

    assert _scanner._snapshot_launchctl_labels(BOT_ID) == []

"""Bite 5b — cron_exit_monitor's status probe goes through the platform-portable
Scheduler seam, NOT a module-global LaunchdScheduler() handle.

The audit-scheduler daemon calls ``_status_probe`` every cycle, so on a Linux
pod it must resolve the injected SystemdScheduler and answer truthfully rather
than crash on a missing launchctl. ``status()`` is a portable Protocol verb.

``sudo_non_interactive=True`` (``sudo -n``) is LOAD-BEARING: the probe runs in
a TTY-less daemon context, where a plain ``sudo`` would BLOCK on a password
prompt instead of failing fast and classifiably (the ``status_error``
tri-state). ``get_scheduler()``'s default LaunchdScheduler is plain ``sudo``,
so a clean swap would silently flip the posture — hence the guarded-derive. On
macOS the argv stays byte-identical (``sudo -n /bin/launchctl list <label>``);
on systemd the injected adapter answers directly.

Convention (mirrors tests/test_mcp_service_scheduler_seam.py): inject
``LaunchdScheduler(runner=<recording fake>)`` via ``set_scheduler()`` — the
real adapter builds the argv, the fake records it, no process is spawned.
``set_scheduler(None)`` / ``set_profile(None)`` on teardown is MANDATORY: a
leaked fake singleton poisons every later test that calls ``get_scheduler()``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin import cron_exit_monitor  # noqa: E402
from evolve_admin.runtime import (  # noqa: E402
    LaunchdScheduler,
    SystemdScheduler,
    set_scheduler,
)
from platform_profile import LINUX, set_profile  # noqa: E402

LABEL = "ai.evolve.evolve.oc-log-rotate"


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)
    set_profile(None)


class _RecordingRunner:
    """Seam-shaped runner: records argv, replies from a per-argv script."""

    def __init__(self, reply=None):
        self.calls: list[list[str]] = []
        self._reply = reply or (lambda argv: (0, "", ""))

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self._reply(argv)


def test_status_probe_macos_uses_sudo_n_list_argv():
    """On macOS the probe reaches the adapter as ``sudo -n /bin/launchctl list
    <label>`` — the load-bearing daemon-context fail-fast posture, byte-identical
    to the pre-seam ``LaunchdScheduler(sudo_non_interactive=True, timeout=5.0)``
    handle. A managed running PID flows through to the status dict."""
    runner = _RecordingRunner(
        reply=lambda argv: (0, '"PID" = 4242;\n"LastExitStatus" = 0;', "")
    )
    set_scheduler(LaunchdScheduler(runner=runner))

    st = cron_exit_monitor._status_probe(LABEL)

    assert runner.calls == [["sudo", "-n", "/bin/launchctl", "list", LABEL]]
    assert st["running"] is True
    assert st["pid"] == 4242
    assert st["status_error"] is None


def test_status_probe_sudo_gap_reports_cannot_escalate():
    """A non-interactive sudo gap is classified as ``cannot_escalate`` (the
    tri-state), NOT a false "not loaded" — the probe never hangs."""
    runner = _RecordingRunner(
        reply=lambda argv: (1, "", "sudo: a password is required")
    )
    set_scheduler(LaunchdScheduler(runner=runner))

    st = cron_exit_monitor._status_probe(LABEL)

    assert st["status_error"] == "cannot_escalate"
    assert st["managed"] is False  # "unknown", not "absent"


def test_status_probe_on_systemd_routes_through_injected_adapter(tmp_path):
    """On a Linux pod the probe uses the injected SystemdScheduler's status()
    directly — truthful (a real systemctl probe), never a launchctl crash and
    never the sudo -n launchd argv. A loaded+running unit reports as such."""
    calls: list[list[str]] = []

    def _reply(argv):
        calls.append(list(argv))
        # systemctl show → loaded + running with a PID.
        return (
            0,
            "LoadState=loaded\nActiveState=active\nMainPID=7\n"
            "ExecMainStatus=0\n",
            "",
        )

    set_scheduler(
        SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=_reply)
    )
    set_profile(LINUX)

    st = cron_exit_monitor._status_probe(LABEL)

    # Only systemctl was invoked — never launchctl.
    assert all("launchctl" not in " ".join(c) for c in calls)
    assert any(c and c[0].endswith("systemctl") for c in calls)
    assert st["managed"] is True
    assert st["running"] is True
    assert st["pid"] == 7


def test_status_probe_not_cached_picks_up_late_injection():
    """The probe must NOT cache a launchd adapter across calls — a fake injected
    AFTER the first probe must still intercept the second (a cached module
    global would defeat the platform gate's set_scheduler injection)."""
    first = _RecordingRunner(
        reply=lambda argv: (0, '"PID" = 1;', "")
    )
    set_scheduler(LaunchdScheduler(runner=first))
    cron_exit_monitor._status_probe(LABEL)

    second = _RecordingRunner(
        reply=lambda argv: (0, '"PID" = 2;', "")
    )
    set_scheduler(LaunchdScheduler(runner=second))
    st = cron_exit_monitor._status_probe(LABEL)

    assert st["pid"] == 2
    assert second.calls == [["sudo", "-n", "/bin/launchctl", "list", LABEL]]

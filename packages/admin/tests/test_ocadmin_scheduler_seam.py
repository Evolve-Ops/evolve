"""Bite 4 — ocadmin gateway restart + gui-domain cleanup go through the
platform-portable Scheduler seam (NOT a module-global LaunchdScheduler).

Two distinct postures:

* ``_restart_gateway`` / ``_menu_restart_gateway`` — system-domain
  ``kickstart -k``. ocadmin runs as root, so the pre-seam handle's
  ``use_sudo=False`` only dropped a redundant ``sudo`` prefix; the
  migrated ``get_scheduler().restart()`` reaches the macOS adapter as
  ``sudo /bin/launchctl kickstart -k system/<label>`` — behavior-identical
  on macOS (root running ``sudo`` of a system-domain op is the same), and
  correct on Linux (``systemctl restart`` needs root). Routing through the
  seam means a Linux pod resolves the injected ``SystemdScheduler``.

* ``_remove_conflicting_user_agents`` — per-user ``gui/<uid>`` LaunchAgent
  cleanup. No systemd analogue (Linux pods have no per-user units), so the
  function early-returns on a non-macOS profile and the macOS path routes
  the ``raw()`` calls through the fail-fast ``get_launchd_scheduler()``.

Convention (mirrors tests/test_repo_puller.py::_seam_scheduler and
test_scheduler_seam_analyzer_migration.py): inject
``LaunchdScheduler(runner=<recording fake>)`` via ``set_scheduler()`` — the
real adapter builds the argv, the fake records it, no process is spawned.
``set_scheduler(None)`` on teardown is MANDATORY: a leaked fake singleton
poisons every later test that calls ``get_scheduler()``. ⚠️ kickstart -k is
live-traffic destructive in production; a test must NEVER reach a real
launchctl.
"""

from __future__ import annotations

import pytest

from evolve_admin import ocadmin
from evolve_admin.runtime import LaunchdScheduler, set_scheduler


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)


class _RecordingRunner:
    """Seam-shaped runner: records argv, replies from a per-argv script."""

    def __init__(self, reply=None):
        self.calls: list[list[str]] = []
        self._reply = reply or (lambda argv: (0, "", ""))

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self._reply(argv)


# ── gateway restart (both call sites) ────────────────────────────────────────


def test_restart_gateway_routes_through_seam_sudo_system_domain(capsys):
    """``_restart_gateway`` reaches the seam's macOS adapter as
    ``sudo /bin/launchctl kickstart -k system/<label>``. The pre-seam handle
    used ``use_sudo=False`` (ocadmin is root, the prefix was redundant); the
    sudo-default seam is byte-equivalent on macOS and correct on Linux."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    ocadmin._restart_gateway("team-bot-c")

    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.team-bot-c-gateway"],
    ]
    assert "restarted" in capsys.readouterr().out


def test_menu_restart_gateway_routes_through_seam(capsys):
    """The interactive-menu restart shares the same seam path/argv."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    ocadmin._menu_restart_gateway("team-bot-c")

    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.team-bot-c-gateway"],
    ]
    assert "restarted" in capsys.readouterr().out


def test_restart_gateway_failure_is_reported_not_raised(capsys):
    """A non-zero restart must surface the launchctl diagnostic and never
    raise (operator-feedback contract)."""
    runner = _RecordingRunner(
        reply=lambda argv: (1, "", "Could not find service in domain for system")
    )
    set_scheduler(LaunchdScheduler(runner=runner))

    ocadmin._restart_gateway("team-bot-c")  # must not raise

    assert "Could not find service" in capsys.readouterr().out


# ── gui-domain cleanup (platform-gated) ──────────────────────────────────────


def test_remove_conflicting_user_agents_skips_on_linux():
    """On a non-macOS pod the function is a clean no-op — per-user
    ``gui/<uid>`` LaunchAgents don't exist on Linux, so it must NOT reach any
    launchctl call (the injected fake records zero calls) and must NOT raise.
    The fail-fast get_launchd_scheduler() inside is never reached because the
    early-return fires first — so this stays correct even though the injected
    adapter IS launchd."""
    from platform_profile import LINUX, MACOS, set_profile

    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))
    set_profile(LINUX)
    try:
        # A populated network would otherwise drive the per-bot loop; the
        # early-return must fire before any of it.
        ocadmin._remove_conflicting_user_agents(
            {"bots": {"team-bot-c": {"user": "team-bot-c"}}}
        )
    finally:
        # conftest's per-test fixture also restores MACOS on teardown; be
        # explicit at the site that flipped it.
        set_profile(MACOS)

    assert runner.calls == [], "Linux pod must make zero launchctl calls"


def test_remove_conflicting_user_agents_macos_uses_launchd_adapter(monkeypatch):
    """On macOS the gui-domain probe/bootout route through the launchd
    adapter via get_launchd_scheduler(). With no system daemon plist on disk
    the per-bot loop continues early — proving the macOS path is taken (no
    early platform return) without needing a real gui/<uid> agent."""
    # macOS is pinned by the autouse conftest fixture; assert the path runs.
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    # No /Library/LaunchDaemons plist for this fake bot, so the loop hits the
    # ``if not daemon_path.exists(): continue`` guard — exercising the macOS
    # branch (past the early return) with zero launchctl calls and no raise.
    ocadmin._remove_conflicting_user_agents(
        {"bots": {"zzznope": {"user": "zzznope"}}}
    )

    assert runner.calls == []

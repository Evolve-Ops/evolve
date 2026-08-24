"""Bite 6 — mcp_service (the Claude-Desktop MCP-bridge daemon manager) goes
through the platform-portable Scheduler seam, NOT two module-global
LaunchdScheduler() handles.

The bridge is KEPT on a Linux pod (it binds 0.0.0.0:5051 for remote Claude
Desktop), so this is a real seam migration. Three distinct postures:

* Protocol verbs (``restart`` / ``kill`` / ``status``) — guarded-derive off
  ``get_scheduler()``. ``sudo_non_interactive=True`` (``sudo -n``) is
  LOAD-BEARING (TTY-less daemon / web-API fail-fast) and ``get_scheduler()``'s
  default LaunchdScheduler is plain ``sudo``, so a clean swap would silently
  flip the posture — hence the guarded-derive. On macOS the argv stays
  byte-identical (``sudo -n /bin/launchctl …``); on systemd the injected
  adapter answers directly. A 15s bound is passed per-call.

* ``install`` / ``uninstall`` / ``start`` — platform-split: Linux uses the
  portable ``install`` / ``remove`` / ``restart`` verbs (the bridge unit under
  the systemd daemon dir); macOS keeps its staged-plist
  bootout/bootstrap/kickstart ``raw()`` escape hatches, routed through the
  fail-fast ``get_launchd_scheduler`` accessor with the ``sudo -n`` posture
  preserved.

* legacy ``~/Library/LaunchAgents`` gui-domain sweep
  (``_bootout_legacy_agents``) — platform-gated OUT on Linux (a fresh Linux
  pod has no legacy LaunchAgents); the macOS ``raw()`` bootout routes through
  the fail-fast accessor with the no-sudo posture preserved.

Convention (mirrors tests/test_ocadmin_scheduler_seam.py): inject
``LaunchdScheduler(runner=<recording fake>)`` via ``set_scheduler()`` — the
real adapter builds the argv, the fake records it, no process is spawned.
``set_scheduler(None)`` on teardown is MANDATORY: a leaked fake singleton
poisons every later test that calls ``get_scheduler()``. ⚠️ bootout / kickstart
-k are live-traffic destructive in production; a test must NEVER reach a real
launchctl.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolve_admin import mcp_service
from evolve_admin.runtime import LaunchdScheduler, SystemdScheduler, set_scheduler

LABEL = mcp_service.LABEL


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


# ── Protocol verbs: restart / kill (macOS sudo -n posture) ────────────────────


def test_restart_routes_through_seam_with_sudo_n_posture(monkeypatch):
    """``restart`` reaches the macOS adapter as ``sudo -n /bin/launchctl
    kickstart -k system/<label>`` — the load-bearing daemon-context fail-fast
    posture, byte-identical to the pre-seam ``sudo_non_interactive=True``
    handle. The SIGHUP pre-step is skipped (no running pid)."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))
    # restart() guards on status()["installed"]; force "installed, no pid" so
    # it skips the SIGHUP os.kill and reaches the kickstart verb.
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": True, "running": False, "managed": True,
                 "pid": None, "last_exit": None},
    )

    ok, _out = mcp_service.restart()

    assert ok
    assert runner.calls == [
        ["sudo", "-n", "/bin/launchctl", "kickstart", "-k", f"system/{LABEL}"],
    ]


def test_stop_routes_kill_through_seam_with_sudo_n_posture():
    """``stop`` reaches the macOS adapter as ``sudo -n /bin/launchctl kill
    SIGTERM system/<label>`` (the kill verb keeps the service registered)."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    ok, _out = mcp_service.stop()

    assert ok
    assert runner.calls == [
        ["sudo", "-n", "/bin/launchctl", "kill", "SIGTERM", f"system/{LABEL}"],
    ]


def test_restart_not_installed_short_circuits(monkeypatch):
    """``restart`` on an uninstalled bridge returns the not-installed message
    and makes ZERO launchctl calls (the portable ``installed`` guard)."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": False, "running": False, "managed": False,
                 "pid": None, "last_exit": None},
    )

    ok, msg = mcp_service.restart()

    assert not ok
    assert "not installed" in msg
    assert runner.calls == []


# ── Protocol verb: status (no-sudo probe + sudo-n fallback) ───────────────────


def test_status_uses_nosudo_probe_then_sudo_fallback(monkeypatch, tmp_path):
    """``status`` first probes unsudo'd (``/bin/launchctl list <label>``); when
    that reports not-managed it falls back to the ``sudo -n`` probe. Both go
    through the seam. The macOS plist short-circuit is satisfied by a present
    service-definition path (a real file under tmp) — patched at the
    ``_service_def_path`` helper so the internal resolution sees it."""
    plist = tmp_path / f"{LABEL}.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(mcp_service, "_service_def_path", lambda: plist)

    # First (no-sudo) list says not-found (rc=113); fallback (sudo -n) reports
    # a running PID.
    def _reply(argv):
        if "sudo" in argv:
            return 0, '"PID" = 4242;\n"LastExitStatus" = 0;', ""
        return 113, "", "Could not find service"

    runner = _RecordingRunner(reply=_reply)
    set_scheduler(LaunchdScheduler(runner=runner))

    st = mcp_service.status()

    # The fallback's running PID flows through to the result.
    assert st["running"] is True
    assert st["pid"] == 4242
    # First call is the no-sudo probe; second is the sudo -n fallback.
    assert runner.calls[0] == ["/bin/launchctl", "list", LABEL]
    assert runner.calls[1] == ["sudo", "-n", "/bin/launchctl", "list", LABEL]


def test_status_no_plist_short_circuits_on_macos(monkeypatch, tmp_path):
    """On macOS, an absent plist short-circuits status() to zero launchctl
    calls (the historical fast path)."""
    # Point the service-def resolver at a file that does NOT exist.
    monkeypatch.setattr(
        mcp_service, "_service_def_path", lambda: tmp_path / f"{LABEL}.plist")
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    st = mcp_service.status()

    assert st == {"installed": False, "running": False, "managed": False,
                  "pid": None, "last_exit": None}
    assert runner.calls == []


# ── legacy gui-domain sweep (platform-gated) ──────────────────────────────────


def test_bootout_legacy_agents_skips_on_linux(monkeypatch):
    """On a non-macOS pod the legacy LaunchAgent sweep is a clean no-op — a
    fresh Linux pod has no ``~/Library/LaunchAgents``. It must make ZERO
    launchctl calls (the fail-fast get_launchd_scheduler inside is never
    reached because the early-return fires first) and must NOT raise — even
    though the injected adapter IS launchd."""
    from platform_profile import LINUX, MACOS, set_profile

    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))
    set_profile(LINUX)
    try:
        notes = mcp_service._bootout_legacy_agents()
    finally:
        set_profile(MACOS)

    assert notes == []
    assert runner.calls == [], "Linux pod must make zero launchctl calls"


def test_bootout_legacy_agents_macos_uses_nosudo_launchd_adapter(monkeypatch):
    """On macOS, when invoked as root under sudo, the gui-domain bootout routes
    through the launchd adapter via get_launchd_scheduler() with the no-sudo
    posture: ``/bin/launchctl bootout gui/<uid>/<legacy-label>`` (no sudo
    prefix — the caller is already root in the gui domain). No legacy plist
    files exist, so the file-sweep loop adds nothing."""
    # Simulate root-under-sudo so the gui-domain probe branch runs.
    monkeypatch.setattr(mcp_service.os, "getuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "pod-admin-user")

    def _reply(argv):
        if argv[:2] == ["/usr/bin/id", "-u"]:
            return 0, "501\n", ""
        return 0, "", ""

    # subprocess.run handles the `id -u` lookup; route it to the same script.
    class _Res:
        def __init__(self, rc, out, err):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def _fake_run(argv, **kw):
        rc, out, err = _reply(argv)
        return _Res(rc, out, err)

    monkeypatch.setattr(mcp_service.subprocess, "run", _fake_run)
    # No legacy plist files on disk.
    monkeypatch.setattr(mcp_service, "_legacy_agent_plist_paths", lambda: [])

    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    notes = mcp_service._bootout_legacy_agents()

    # The bootout argv is no-sudo (caller already root in the gui domain).
    assert ["/bin/launchctl", "bootout",
            f"gui/501/{mcp_service._LEGACY_AGENT_LABEL}"] in runner.calls
    # rc=0 from the fake ⇒ a success note recorded.
    assert any("bootout gui/501" in n for n in notes)


# ── install / uninstall / start: Linux routes through Protocol verbs ──────────


def test_install_on_linux_routes_through_systemd_install(monkeypatch, tmp_path):
    """On a Linux pod, install() routes through the portable ``install`` verb
    (SystemdScheduler), NOT a launchctl bootout/bootstrap. The recording
    SystemdScheduler shows ``systemctl`` argv only — never ``launchctl``."""
    from platform_profile import LINUX, MACOS, set_profile

    calls: list[list[str]] = []
    set_scheduler(
        SystemdScheduler(
            unit_dir=tmp_path, use_sudo=False,
            runner=lambda argv: (calls.append(list(argv)), (0, "", ""))[1],
        )
    )
    set_profile(LINUX)
    # status() is consulted in the post-install poll; report installed+running
    # so the loop returns promptly.
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": True, "running": True, "managed": True,
                 "pid": 7, "last_exit": None},
    )
    # LOG_DIR.mkdir + CLAUDE.md write are best-effort; redirect them away.
    monkeypatch.setattr(mcp_service, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(mcp_service, "_write_claude_md_from_network", lambda *a: None)
    try:
        ok, _msg = mcp_service.install(network=tmp_path / "network.json")
    finally:
        set_profile(MACOS)

    assert ok
    # Units were written under the systemd unit dir, and only systemctl was
    # invoked — no launchctl anywhere.
    assert (tmp_path / f"{LABEL}.service").exists()
    assert all("launchctl" not in " ".join(c) for c in calls)
    assert any(c and c[0].endswith("systemctl") for c in calls)


def test_uninstall_on_linux_routes_through_systemd_remove(monkeypatch, tmp_path):
    """On a Linux pod, uninstall() routes through the portable ``remove`` verb
    (``systemctl disable --now`` + unit rm + daemon-reload), never a launchctl
    bootout."""
    from platform_profile import LINUX, MACOS, set_profile

    # Seed an on-disk service unit so remove() has something to delete.
    (tmp_path / f"{LABEL}.service").write_text("[Service]\n")

    calls: list[list[str]] = []
    set_scheduler(
        SystemdScheduler(
            unit_dir=tmp_path, use_sudo=False,
            runner=lambda argv: (calls.append(list(argv)), (0, "", ""))[1],
        )
    )
    set_profile(LINUX)
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": True, "running": True, "managed": True,
                 "pid": 7, "last_exit": None},
    )
    try:
        ok, _msg = mcp_service.uninstall()
    finally:
        set_profile(MACOS)

    assert ok
    assert not (tmp_path / f"{LABEL}.service").exists()
    assert all("launchctl" not in " ".join(c) for c in calls)


def test_start_on_linux_restarts_only_when_not_running(monkeypatch, tmp_path):
    """On a Linux pod, start() restarts a stopped unit but leaves a running
    one untouched (start() is non-destructive — only restart() bounces)."""
    from platform_profile import LINUX, MACOS, set_profile

    calls: list[list[str]] = []
    set_scheduler(
        SystemdScheduler(
            unit_dir=tmp_path, use_sudo=False,
            runner=lambda argv: (calls.append(list(argv)), (0, "active", ""))[1],
        )
    )
    set_profile(LINUX)
    monkeypatch.setattr(mcp_service, "_log_dir", lambda: tmp_path / "logs")
    # Already running ⇒ start() must NOT call restart.
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": True, "running": True, "managed": True,
                 "pid": 9, "last_exit": None},
    )
    try:
        ok, msg = mcp_service.start()
    finally:
        set_profile(MACOS)

    assert ok and msg == "Started"
    assert calls == [], "a running bridge must not be bounced by start()"


# ── install / uninstall: macOS keeps the sudo -n raw posture ──────────────────


def test_install_on_macos_uses_sudo_n_raw_bootstrap(monkeypatch, tmp_path):
    """On macOS, install() keeps the staged-plist flow: the bootout +
    bootstrap go through the seam as ``sudo -n /bin/launchctl …`` (the
    load-bearing daemon-context posture, byte-identical to pre-seam)."""
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    # Stub the macOS staged-plist write + the legacy sweep + the post-install
    # status poll so we isolate the raw bootout/bootstrap argv.
    monkeypatch.setattr(mcp_service, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(mcp_service, "_bootout_legacy_agents", lambda: [])
    monkeypatch.setattr(mcp_service, "_write_system_plist", lambda c: (True, "written"))
    monkeypatch.setattr(mcp_service, "_write_claude_md_from_network", lambda *a: None)
    monkeypatch.setattr(
        mcp_service, "status",
        lambda: {"installed": True, "running": True, "managed": True,
                 "pid": 11, "last_exit": None},
    )

    ok, _msg = mcp_service.install(network=tmp_path / "network.json")

    assert ok
    plist = str(mcp_service.PLIST_PATH)
    assert ["sudo", "-n", "/bin/launchctl", "bootout", f"system/{LABEL}"] in runner.calls
    assert ["sudo", "-n", "/bin/launchctl", "bootstrap", "system", plist] in runner.calls


# ── platform-resolved paths: no hardcoded macOS daemon/log literals ───────────
#
# The bridge used to hardcode ``/Library/LaunchDaemons/<label>.plist`` and
# ``/Users/evolve/.evolve/logs`` — both macOS-only. The first is now derived
# from ``platform_profile.daemon_dir`` (the macOS install/uninstall/start/status
# branches consult it; on Linux it's display-only — the seam owns the unit
# path), the second from ``user_home("evolve")`` so the systemd unit's
# ``StandardOutput=`` target lands in evolve's REAL home on each OS. macOS
# byte-identity is the hard invariant (the plist golden in
# test_plist_jobspec_parity covers the rendered output).


def _force_pwd_fallback(monkeypatch):
    """Make ``user_home`` take its profile-keyed construction fallback by
    forcing ``pwd.getpwnam`` to miss — isolates the path resolution from
    whatever accounts happen to exist on the test box (a real pod has an
    ``evolve`` user; CI does not)."""
    import evolve_config

    def _miss(user):
        raise KeyError(user)

    monkeypatch.setattr(evolve_config.pwd, "getpwnam", _miss)


def test_service_def_path_per_platform(monkeypatch):
    """``_service_def_path`` is daemon_dir-derived, NOT a hardcoded literal:
    the macOS plist under ``/Library/LaunchDaemons`` (byte-identical to the old
    constant) and the systemd unit under ``/etc/systemd/system`` on Linux."""
    from platform_profile import LINUX, MACOS, set_profile

    set_profile(MACOS)
    assert mcp_service._service_def_path() == Path(
        f"/Library/LaunchDaemons/{LABEL}.plist")
    # __getattr__ re-export resolves to the same value.
    assert str(mcp_service.PLIST_PATH) == f"/Library/LaunchDaemons/{LABEL}.plist"

    try:
        set_profile(LINUX)
        assert mcp_service._service_def_path() == Path(
            f"/etc/systemd/system/{LABEL}.service")
        assert str(mcp_service.PLIST_PATH) == f"/etc/systemd/system/{LABEL}.service"
    finally:
        set_profile(MACOS)


def test_log_path_follows_evolve_home_per_platform(monkeypatch):
    """The bridge log dir resolves to evolve's home — ``/Users/evolve`` on
    macOS (byte-identical to the retired literal), ``/home/evolve`` on Linux —
    NEVER the hardcoded ``/Users/evolve`` on a Linux pod."""
    from platform_profile import LINUX, MACOS, set_profile

    _force_pwd_fallback(monkeypatch)

    set_profile(MACOS)
    assert mcp_service._log_dir() == Path("/Users/evolve/.evolve/logs")
    assert mcp_service._log_path() == Path("/Users/evolve/.evolve/logs/mcp-bridge.log")

    try:
        set_profile(LINUX)
        assert mcp_service._log_dir() == Path("/home/evolve/.evolve/logs")
        assert mcp_service._log_path() == Path(
            "/home/evolve/.evolve/logs/mcp-bridge.log")
    finally:
        set_profile(MACOS)


def test_log_dir_follows_real_evolve_account_home(monkeypatch):
    """When the ``evolve`` account exists, the log dir follows its ACTUAL
    home (pwd-first) rather than any constructed path — so a non-default
    home is honored, and the value is never a frozen literal."""
    import types

    import evolve_config

    monkeypatch.setattr(
        evolve_config.pwd, "getpwnam",
        lambda u: types.SimpleNamespace(pw_dir="/srv/evolve-acct"),
    )
    assert mcp_service._log_dir() == Path("/srv/evolve-acct/.evolve/logs")


def test_bridge_jobspec_log_path_is_linux_home_on_linux(monkeypatch):
    """End-to-end: the JobSpec the Scheduler seam renders into a systemd unit
    carries the Linux ``/home/evolve`` log path in ``stdout_path`` /
    ``stderr_path`` (the unit's ``StandardOutput=`` target) — proving the
    install artifact is Linux-correct, not the old macOS literal."""
    from platform_profile import LINUX, MACOS, set_profile

    _force_pwd_fallback(monkeypatch)
    try:
        set_profile(LINUX)
        spec = mcp_service._bridge_job_spec()
        assert spec.stdout_path == "/home/evolve/.evolve/logs/mcp-bridge.log"
        assert spec.stderr_path == "/home/evolve/.evolve/logs/mcp-bridge.log"
    finally:
        set_profile(MACOS)

    # macOS byte-identity: the same JobSpec field on the macOS profile is the
    # retired literal's value.
    spec_macos = mcp_service._bridge_job_spec()
    assert spec_macos.stdout_path == "/Users/evolve/.evolve/logs/mcp-bridge.log"

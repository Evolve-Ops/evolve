"""META:platform 8.3 — admin-server self-service is systemd-aware on Linux.

Repro the live evolve-vps-pod false-negative and lock the fix: on a Linux
VPS the admin-ui runs as the systemd unit ``ai.evolve.evolve.admin-ui``
(loaded/active/running), but pre-fix ``service.status()`` returned
``running: false`` because it gated the system-daemon branch on a
``/Library/LaunchDaemons`` plist path that never exists on Linux, then fell
through to a macOS-only user-LaunchAgent path. The fix routes
status/restart/install/uninstall through the platform seam
(``get_profile().name == "linux"`` + ``get_scheduler()`` → SystemdScheduler).

Every test pins a profile explicitly and injects a scheduler; the
``_seam_scheduler`` fixture MANDATORILY resets the ``_scheduler`` global on
teardown (conftest's ``_restore_module_state`` does NOT) so a fake never
leaks process-wide. PLACEHOLDER unit/label names per docs/PLACEHOLDER_NAMING.md
where a generic stand-in is needed; SYSTEM_LABEL is the real (platform)
admin-ui label, which is correct on both OSes by design.
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

from evolve_admin import service as svc  # noqa: E402
from evolve_admin.runtime import (  # noqa: E402
    FakeScheduler,
    JobSpec,
    SystemdScheduler,
    set_scheduler,
)
from platform_profile import LINUX, MACOS, set_profile  # noqa: E402
from runtime import scheduler as scheduler_mod  # noqa: E402


# ── mandatory teardown: never leak an injected scheduler ─────────────────────
#
# The exemplar from the bite-2 web seam tests. conftest's
# _restore_module_state does NOT reset the runtime `_scheduler` singleton, so
# without this the fake bleeds into every later test process-wide and reddens
# other shards. set_profile is re-pinned to MACOS on teardown by conftest's
# autouse fixture; we only own the scheduler reset.


@pytest.fixture(autouse=True)
def _seam_scheduler(monkeypatch):
    """Forbid a real systemctl/launchctl subprocess; reset the seam after."""

    def _boom(*a, **kw):  # pragma: no cover — guard
        raise AssertionError(
            f"scheduler reached a REAL subprocess — inject a runner. argv={a!r}"
        )

    monkeypatch.setattr(scheduler_mod, "_subprocess_runner", _boom)
    set_scheduler(None)
    yield
    set_scheduler(None)


class _SystemctlRunner:
    """Fake systemctl runner — records argv, replies per-verb.

    ``show_props`` is the body returned for ``systemctl show`` (the live
    ActiveState/SubState/MainPID/ExecMainStatus shape). ``restart_rc`` drives
    the ``systemctl restart`` outcome.
    """

    def __init__(self, *, show_props: str = "", restart_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._show_props = show_props
        self._restart_rc = restart_rc

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append([str(a) for a in argv])
        args = [a for a in argv if a != "sudo" and a != "-n"]
        verb = ""
        for a in args:
            base = a.rsplit("/", 1)[-1]
            if base in ("show", "restart", "is-active", "list-units"):
                verb = base
                break
        if verb == "show":
            return (0, self._show_props, "")
        if verb == "restart":
            return (self._restart_rc, "", "" if self._restart_rc == 0 else "Job failed")
        return (0, "", "")

    def did(self, verb: str) -> bool:
        return any(
            any(a == verb or str(a).endswith(f"/{verb}") for a in c)
            for c in self.calls
        )


# The live ground-truth from the evolve-vps-pod: ActiveState=active,
# SubState=running, MainPID=1380281. systemctl show prints LoadState=loaded.
_LIVE_SHOW = (
    "LoadState=loaded\n"
    "ActiveState=active\n"
    "MainPID=1380281\n"
    "ExecMainStatus=0\n"
)


def _pin_linux_systemd(runner: _SystemctlRunner, unit_dir: Path) -> None:
    """Pin LINUX + a SystemdScheduler whose status()['installed'] reads a
    real unit file under ``unit_dir`` (use_sudo=False, fake runner)."""
    set_profile(LINUX)
    set_scheduler(
        SystemdScheduler(unit_dir=unit_dir, use_sudo=False, runner=runner)
    )


def _write_unit(unit_dir: Path) -> None:
    """Drop a <SYSTEM_LABEL>.service file so status()['installed'] is True —
    mirrors the unit deploy.py installs at provisioning."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{svc.SYSTEM_LABEL}.service").write_text(
        "[Unit]\nDescription=admin-ui\n[Service]\nExecStart=/x\n"
    )


# ── status() on Linux — the live repro, now correct ──────────────────────────


def test_status_linux_running_reports_systemd_truth(tmp_path):
    """The exact live false-negative: status() must now return
    running:true / backend:systemd / managed:true / pid set."""
    unit_dir = tmp_path / "systemd"
    _write_unit(unit_dir)
    runner = _SystemctlRunner(show_props=_LIVE_SHOW)
    _pin_linux_systemd(runner, unit_dir)

    st = svc.status()

    assert st["backend"] == "systemd"
    assert st["running"] is True
    assert st["installed"] is True
    assert st["managed"] is True
    assert st["pid"] == 1380281
    assert st["last_exit"] == "0"
    # The probe must have gone through systemctl (the seam), never launchctl.
    assert runner.did("show")


def test_status_linux_ignores_stale_user_launchagent(tmp_path, monkeypatch):
    """A stale ``com.evolve.admin.plist`` (PLIST_PATH) on a Linux box must
    NOT make status() report installed via the launchd path — the systemd
    query is authoritative. We point PLIST_PATH at an existing file and
    confirm status() still reflects the (absent) systemd unit, not the plist."""
    stale = tmp_path / "com.evolve.admin.plist"
    stale.write_text("<plist/>")
    monkeypatch.setattr(svc, "PLIST_PATH", stale)

    unit_dir = tmp_path / "systemd"  # no unit file written → not installed
    runner = _SystemctlRunner(show_props="")  # show returns no LoadState=loaded
    _pin_linux_systemd(runner, unit_dir)

    st = svc.status()

    assert st["backend"] == "systemd"
    assert st["installed"] is False  # NOT True-from-stale-plist
    assert st["running"] is False
    # managed stays True: systemd owns the unit regardless of running state.
    assert st["managed"] is True


def test_status_linux_uses_fakescheduler_shape(tmp_path):
    """status() reads installed/running/pid/last_exit straight off the
    scheduler's status() dict — a FakeScheduler reporting systemd proves the
    shape contract without a real systemctl."""
    set_profile(LINUX)
    fake = FakeScheduler()
    fake.seed_job(
        JobSpec(label=svc.SYSTEM_LABEL, program_args=["/x"], keep_alive=True),
        running=True, last_exit="0",
    )
    set_scheduler(fake)

    st = svc.status()
    assert st["backend"] == "systemd"
    assert st["running"] is True
    assert st["managed"] is True
    assert st["pid"] is not None


# ── restart() on Linux — seam, not execv ─────────────────────────────────────


def test_restart_linux_uses_systemctl_not_execv(tmp_path, monkeypatch):
    """restart() on Linux must call get_scheduler().restart(SYSTEM_LABEL)
    (systemd) and must NOT take the os.execv self-re-exec path."""
    unit_dir = tmp_path / "systemd"
    _write_unit(unit_dir)
    runner = _SystemctlRunner(show_props=_LIVE_SHOW, restart_rc=0)
    _pin_linux_systemd(runner, unit_dir)

    # Trip-wire: a thread spawn here would mean the execv fallback ran.
    import threading

    started: list[bool] = []
    real_thread = threading.Thread

    def _spy_thread(*a, **kw):
        started.append(True)
        return real_thread(*a, **kw)

    monkeypatch.setattr(threading, "Thread", _spy_thread)

    ok, msg = svc.restart()

    assert ok is True
    assert runner.did("restart")
    assert started == [], "restart() must not reach the os.execv thread on Linux"
    assert "systemctl" in msg.lower() or "systemd" in msg.lower()


def test_restart_linux_falls_through_to_execv_on_systemd_failure(tmp_path, monkeypatch):
    """If systemd restart genuinely fails, restart() falls through to the
    execv re-exec (so a restart still works) — and never routes through the
    macOS launchd label even if a stale PLIST_PATH exists."""
    stale = tmp_path / "com.evolve.admin.plist"
    stale.write_text("<plist/>")
    monkeypatch.setattr(svc, "PLIST_PATH", stale)

    unit_dir = tmp_path / "systemd"
    _write_unit(unit_dir)
    runner = _SystemctlRunner(show_props=_LIVE_SHOW, restart_rc=1)  # restart FAILS
    _pin_linux_systemd(runner, unit_dir)

    import threading

    started: list[bool] = []
    real_thread = threading.Thread

    class _NoRunThread:
        def __init__(self, *a, **kw):
            started.append(True)

        def start(self):  # don't actually re-exec the test process
            pass

    monkeypatch.setattr(threading, "Thread", _NoRunThread)

    ok, msg = svc.restart()

    assert ok is True
    assert started == [True], "execv fallback must run when systemd restart fails"
    # Must NOT have issued a launchctl kickstart against the wrong label.
    assert not runner.did("kickstart")


# ── install()/uninstall() on Linux — no-op-with-message, no plist write ──────


def test_install_linux_is_noop_with_message(tmp_path, monkeypatch):
    """install() on Linux must NOT write a launchd plist (the stale-plist
    leak) — it returns (True, message pointing at the systemd unit)."""
    plist = tmp_path / "com.evolve.admin.plist"
    monkeypatch.setattr(svc, "PLIST_PATH", plist)
    set_profile(LINUX)
    set_scheduler(FakeScheduler())

    ok, msg = svc.install()

    assert ok is True
    assert svc.SYSTEM_LABEL in msg
    assert "restart" in msg.lower()
    assert not plist.exists(), "install() must NOT write a plist on Linux"


def test_uninstall_linux_is_noop_with_message(tmp_path, monkeypatch):
    """uninstall() on Linux must not systemctl-disable / delete the unit and
    must not touch a plist — no-op-with-message."""
    plist = tmp_path / "com.evolve.admin.plist"
    plist.write_text("<plist/>")  # even if a stale one exists
    monkeypatch.setattr(svc, "PLIST_PATH", plist)
    set_profile(LINUX)
    set_scheduler(FakeScheduler())

    ok, msg = svc.uninstall()

    assert ok is True
    assert svc.SYSTEM_LABEL in msg
    assert plist.exists(), "uninstall() must NOT delete the plist on Linux"

    ok_q, msg_q = svc.uninstall(quiet=True)
    assert ok_q is True
    assert msg_q == "not installed"


# ── macOS byte-identity — only a `backend` key is ADDED ──────────────────────


def test_status_macos_adds_only_backend_key(tmp_path, monkeypatch):
    """On macOS every existing key keeps its value and the only addition is
    backend:'launchd'. We force the not-installed early-return (both plist
    paths absent) and assert the full dict."""
    set_profile(MACOS)
    monkeypatch.setattr(svc, "PLIST_PATH", tmp_path / "nope.plist")
    monkeypatch.setattr(svc, "SYSTEM_PLIST_PATH", tmp_path / "nope-system.plist")
    # No scheduler call happens on the not-installed path, but pin a fake so
    # the seam never reaches a real launchctl if behavior ever changes.
    set_scheduler(FakeScheduler())

    st = svc.status()

    assert st == {
        "installed": False,
        "running": False,
        "managed": False,
        "pid": None,
        "last_exit": None,
        "backend": "launchd",
    }


def test_status_macos_running_keeps_existing_values(tmp_path, monkeypatch):
    """macOS running path: existing installed/running/managed/pid/last_exit
    keep their pre-fix meaning; backend:'launchd' is the only new key."""
    set_profile(MACOS)
    plist = tmp_path / "com.evolve.admin.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(svc, "PLIST_PATH", plist)
    monkeypatch.setattr(svc, "SYSTEM_PLIST_PATH", tmp_path / "nope-system.plist")

    fake = FakeScheduler()
    fake.seed_job(
        JobSpec(label=svc.LABEL, program_args=["/x"], keep_alive=True),
        running=True, last_exit="0",
    )
    set_scheduler(fake)

    st = svc.status()

    assert st["backend"] == "launchd"
    assert st["installed"] is True
    assert st["running"] is True
    assert st["managed"] is True
    assert st["pid"] is not None
    assert st["last_exit"] == "0"


def test_install_macos_still_attempts_launchd(tmp_path, monkeypatch):
    """macOS install() must keep its launchd behavior — proven by it writing
    the plist (the very thing the Linux branch suppresses)."""
    set_profile(MACOS)
    plist = tmp_path / "agents" / "com.evolve.admin.plist"
    monkeypatch.setattr(svc, "PLIST_PATH", plist)
    monkeypatch.setattr(svc, "SYSTEM_PLIST_PATH", tmp_path / "nope-system.plist")
    monkeypatch.setattr(svc, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(svc, "LOG_PATH", tmp_path / "logs" / "admin-server.log")
    set_scheduler(FakeScheduler())

    # _launchctl no-ops under a FakeScheduler (not a LaunchdScheduler), and
    # status() under the fake reports not-running, so install() returns the
    # "process did not start" branch — but the plist MUST have been written.
    svc.install()

    assert plist.exists(), "macOS install() must still write the launchd plist"

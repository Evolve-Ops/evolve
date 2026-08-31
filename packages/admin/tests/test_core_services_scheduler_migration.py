"""Phase 4.3 C Track S Step S2b — core services on the Scheduler seam.

Pins the migration semantics for the admin core-services cluster
(service.py, health.py, recovery.py, cli.py) that test_scheduler_seam.py
does not cover:

1. service.py — the daemon-managing-itself special case. Its launchctl
   traffic must flow through a gui/<uid>-domain adapter that shares the
   ACTIVE seam's runner (so one ``set_scheduler`` injection intercepts
   everything), unsudo'd, with the CLI surface's messages unchanged.
2. health.py — ``_launchd_loaded`` keeps its tri-state (loaded /
   not-loaded / can't-determine) and the fix actions keep their message
   contracts.
3. recovery.py — pause/resume/kickstart keep ``sudo -n`` (daemon
   context: fail fast, never block on a password prompt) and the
   rc 36/37 goal-state-reached semantics.
4. cli.py — uninstall maps onto ``Scheduler.remove()``; migrate-jobs
   keeps its per-step output while flowing through ``raw()``.
5. The argv gate: none of the four modules may build launchctl argv for
   subprocess again (source-level regression guard).

⚠️ ``kickstart -k`` / ``bootout`` are live-traffic destructive on a pod
host. Injection is mandatory: the autouse fixture below makes any real
subprocess spawn from the scheduler module an immediate test failure.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import health, recovery, service  # noqa: E402
from evolve_admin.runtime import LaunchdScheduler, set_scheduler  # noqa: E402
from evolve_admin.runtime import scheduler as scheduler_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_subprocess_from_scheduler(monkeypatch):
    """Any real subprocess spawn from the scheduler module fails the test."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            "LaunchdScheduler attempted a REAL subprocess in a unit test — "
            f"inject a runner. argv={a!r}"
        )

    monkeypatch.setattr(scheduler_mod.subprocess, "run", _boom)
    yield
    set_scheduler(None)


class RecordingRunner:
    """Records argv lists; per-token (rc, stdout, stderr) overrides."""

    def __init__(self, responses: dict | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for token, resp in self.responses.items():
            if any(a == token or str(a).endswith(f"/{token}") for a in argv):
                return resp
        return (0, "", "")


class FakeProtocolScheduler:
    """Pure-Protocol fake — has the verbs, none of launchd's plumbing."""

    def __init__(self, status_result: dict | None = None):
        self.status_result = status_result or {
            "installed": True, "managed": True, "running": True,
            "pid": 777, "last_exit": "0",
        }
        self.calls: list[tuple] = []

    def install(self, spec):  # pragma: no cover — not exercised here
        self.calls.append(("install", spec.label))

    def remove(self, label):
        self.calls.append(("remove", label))
        return True, f"Removed: {label}"

    def restart(self, label):
        self.calls.append(("restart", label))
        return True, ""

    def list(self, *, prefix=None):  # pragma: no cover
        return []

    def status(self, label):
        self.calls.append(("status", label))
        return dict(self.status_result)

    def running(self, label):  # pragma: no cover
        return bool(self.status_result["running"])

    def kill(self, label):  # pragma: no cover
        return True, ""


def _inject(runner) -> LaunchdScheduler:
    s = LaunchdScheduler(runner=runner)  # pod defaults: system domain, sudo
    set_scheduler(s)
    return s


_LIST_RUNNING = (0, '{\n\t"PID" = 4242;\n\t"LastExitStatus" = 0;\n};\n', "")


# ── service.py — self-management through the seam ────────────────────────────


@pytest.fixture
def svc_paths(tmp_path, monkeypatch):
    """Point service.py's module paths at tmp so nothing real is touched."""
    plist = tmp_path / "LaunchAgents" / "com.evolve.admin.plist"
    sys_plist = tmp_path / "LaunchDaemons" / "ai.evolve.evolve.admin-ui.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(service, "PLIST_PATH", plist)
    monkeypatch.setattr(service, "SYSTEM_PLIST_PATH", sys_plist)
    monkeypatch.setattr(service, "LOG_DIR", log_dir)
    monkeypatch.setattr(service, "LOG_PATH", log_dir / "admin-server.log")
    return plist, sys_plist


def test_service_launchctl_is_unsudoed_user_domain(svc_paths):
    """service.py manages a USER LaunchAgent — its launchctl must never be
    sudo-prefixed even though the injected seam default is the sudo'd
    system-domain adapter."""
    r = RecordingRunner()
    _inject(r)
    rc, out = service._launchctl("bootout", f"gui/{service._uid()}", "x.plist")
    assert rc == 0
    assert r.calls == [["/bin/launchctl", "bootout", f"gui/{service._uid()}", "x.plist"]]


def test_service_install_reregisters_via_seam(svc_paths, monkeypatch):
    plist, _sys = svc_paths
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    r = RecordingRunner({"list": _LIST_RUNNING})
    _inject(r)

    ok, msg = service.install(port=5050)
    assert ok, msg
    assert "running (PID 4242)" in msg
    assert plist.exists(), "plist must be written before registration"
    uid = service._uid()
    verbs = [c[1] for c in r.calls]
    assert verbs[:3] == ["bootout", "unload", "bootstrap"], (
        "install must always re-register: path-form bootout + legacy unload "
        "+ bootstrap — Scheduler.install()'s byte-identical skip would lose "
        "the always-reload contract"
    )
    assert r.calls[2] == ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)]


def test_service_uninstall_message_and_argv(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner()
    _inject(r)
    ok, msg = service.uninstall()
    assert ok and msg == "Service uninstalled successfully."
    assert not plist.exists()
    assert r.calls == [
        ["/bin/launchctl", "bootout", f"gui/{service._uid()}", str(plist)],
    ]


def test_service_start_stop_messages(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner()
    _inject(r)
    assert service.start() == (True, "Started")
    assert service.stop() == (True, "Stopped")
    assert r.calls == [
        ["/bin/launchctl", "start", service.LABEL],
        ["/bin/launchctl", "stop", service.LABEL],
    ]


def test_service_restart_system_daemon_sudo_fallback(svc_paths):
    """Self-restart of the system daemon: unsudo'd kickstart first (root
    case), then the sudo'd Protocol restart() as the retry."""
    _plist, sys_plist = svc_paths
    sys_plist.parent.mkdir(parents=True, exist_ok=True)
    sys_plist.write_text("x")

    label = service.SYSTEM_LABEL

    class TwoStepRunner(RecordingRunner):
        def __call__(self, argv):
            self.calls.append(list(argv))
            if argv[0] == "sudo":
                return (0, "", "")     # sudo'd retry succeeds
            return (1, "", "not privileged")  # unsudo'd attempt fails

    r = TwoStepRunner()
    _inject(r)
    ok, msg = service.restart()
    assert ok and msg == "Restarted via sudo launchctl (system daemon)"
    assert r.calls == [
        ["/bin/launchctl", "kickstart", "-k", f"system/{label}"],
        ["sudo", "/bin/launchctl", "kickstart", "-k", f"system/{label}"],
    ]


def test_service_restart_gui_label_via_protocol_verb(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner()
    _inject(r)
    ok, msg = service.restart()
    assert ok and msg == "Restarted via launchctl"
    assert r.calls == [
        ["/bin/launchctl", "kickstart", "-k",
         f"gui/{service._uid()}/{service.LABEL}"],
    ]


def test_service_status_parses_pid_via_seam(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner({"list": _LIST_RUNNING})
    _inject(r)
    s = service.status()
    assert s["installed"] and s["managed"] and s["running"]
    assert s["pid"] == 4242
    assert s["last_exit"] == "0"
    assert ["/bin/launchctl", "list", service.LABEL] in r.calls


def test_service_status_unregistered(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner({"list": (113, "", "Could not find service")})
    _inject(r)
    s = service.status()
    assert s["installed"] is True
    assert s["managed"] is False and s["running"] is False


def test_service_with_protocol_fake_uses_verbs_and_noops_raw(svc_paths):
    """A pure-Protocol scheduler: raw self-management no-ops; status() and
    restart() route through the Protocol verbs."""
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    fake = FakeProtocolScheduler()
    set_scheduler(fake)

    assert service._launchctl("bootout", "gui/1", "x") == (0, "")
    s = service.status()
    assert s["running"] is True and s["pid"] == 777
    ok, _ = service.restart()
    assert ok
    assert ("restart", service.LABEL) in fake.calls


# ── health.py — tri-state probe + fix actions ────────────────────────────────


def test_launchd_loaded_tristate_true_false_none():
    label = "ai.evolve.evolve.heal"

    r = RecordingRunner({"list": (0, "{...}", "")})
    _inject(r)
    assert health._launchd_loaded(label) is True
    assert r.calls == [["sudo", "/bin/launchctl", "list", label]], (
        "probe argv must stay sudo'd /bin/launchctl list <label>"
    )

    _inject(RecordingRunner({"list": (113, "", "")}))
    assert health._launchd_loaded(label) is False

    _inject(RecordingRunner({"list": (1, "", "Could not find service x")}))
    assert health._launchd_loaded(label) is False

    _inject(RecordingRunner({"list": (1, "", "sudo: a password is required")}))
    assert health._launchd_loaded(label) is None, (
        "sudo-couldn't-escalate must stay distinguishable from not-loaded "
        "(tooling failure vs finding)"
    )


def test_launchd_loaded_protocol_fallback_uses_status():
    fake = FakeProtocolScheduler({"installed": True, "managed": False,
                                  "running": False, "pid": None,
                                  "last_exit": None})
    set_scheduler(fake)
    assert health._launchd_loaded("any.label") is False
    assert ("status", "any.label") in fake.calls


def test_execute_fix_kickstart_message_contract():
    label = "ai.evolve.evolve.repo-puller"
    r = RecordingRunner()
    _inject(r)
    ok, msg = health._execute_fix({"action": "launchctl_kickstart", "label": label})
    assert ok and msg == f"kickstart {label}"
    assert r.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k", f"system/{label}"],
    ]

    _inject(RecordingRunner({"kickstart": (1, "", "boom")}))
    ok, msg = health._execute_fix({"action": "launchctl_kickstart", "label": label})
    assert not ok and msg == "boom"

    ok, msg = health._execute_fix(
        {"action": "launchctl_kickstart", "label": label}, dry_run=True)
    assert ok and msg == f"[dry-run] would kickstart {label}"


def test_execute_fix_bootstrap_message_contract(tmp_path, monkeypatch):
    label = "ai.evolve.evolve.heal"
    monkeypatch.setattr(health, "LAUNCHD_DIR", tmp_path)

    # Plist missing → actionable error, no launchctl issued.
    r = RecordingRunner()
    _inject(r)
    ok, msg = health._execute_fix({"action": "launchctl_bootstrap", "label": label})
    assert not ok and "Plist not found" in msg
    assert r.calls == []

    plist = tmp_path / f"{label}.plist"
    plist.write_text("x")
    ok, msg = health._execute_fix({"action": "launchctl_bootstrap", "label": label})
    assert ok and msg == f"bootstrap {label}"
    assert r.calls == [
        ["sudo", "/bin/launchctl", "bootstrap", "system", str(plist)],
    ]


# ── recovery.py — sudo -n + rc 36/37 semantics ───────────────────────────────


def test_recovery_bootout_keeps_sudo_dash_n():
    """``sudo -n`` is load-bearing: pause-all runs in daemon context with
    no TTY and must fail fast, never block on a password prompt."""
    r = RecordingRunner()
    _inject(r)
    res = recovery._bootout_gateway("admin_bot", dry_run=False)
    assert res.ok is True and res.rc == 0
    assert r.calls == [
        ["sudo", "-n", "/bin/launchctl", "bootout",
         f"system/{recovery._gateway_label('admin_bot')}"],
    ]


def test_recovery_bootout_rc36_is_already_stopped():
    r = RecordingRunner(
        {"bootout": (36, "", "Could not find specified service")})
    _inject(r)
    res = recovery._bootout_gateway("admin_bot", dry_run=False)
    assert res.ok is True and res.skipped is True and res.rc == 36
    assert res.skip_reason == "already stopped"


def test_recovery_bootstrap_rc37_is_already_loaded(tmp_path, monkeypatch):
    plist = tmp_path / "ai.openclaw.admin_bot-gateway.plist"
    plist.write_text("x")
    monkeypatch.setattr(recovery, "_gateway_plist_path", lambda _b: plist)
    r = RecordingRunner({"bootstrap": (37, "", "Service is already loaded")})
    _inject(r)
    res = recovery._bootstrap_gateway("admin_bot", dry_run=False)
    assert res.ok is True and res.skipped is True and res.rc == 37
    assert r.calls == [
        ["sudo", "-n", "/bin/launchctl", "bootstrap", "system", str(plist)],
    ]


def test_recovery_bootstrap_missing_plist_skips_without_launchctl(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recovery, "_gateway_plist_path", lambda _b: tmp_path / "missing.plist")
    r = RecordingRunner()
    _inject(r)
    res = recovery._bootstrap_gateway("admin_bot", dry_run=False)
    assert res.ok is True and res.skipped is True
    assert "plist not found" in res.skip_reason
    assert r.calls == []


def test_recovery_kickstart_message_contract():
    label = recovery._gateway_label("admin_bot")
    r = RecordingRunner()
    _inject(r)
    assert recovery._restart_bot_gateway("admin_bot") == (True, "kickstart ok")
    assert r.calls == [
        ["sudo", "-n", "/bin/launchctl", "kickstart", "-k", f"system/{label}"],
    ]

    _inject(RecordingRunner({"kickstart": (3, "", "no such service")}))
    ok, msg = recovery._restart_bot_gateway("admin_bot")
    assert not ok and msg == "kickstart rc=3: no such service"


def test_recovery_protocol_fake_noops_raw_paths():
    """Non-launchd Scheduler injected → the raw launchctl paths no-op with
    success (deploy._scheduler_launchctl convention)."""
    set_scheduler(FakeProtocolScheduler())
    res = recovery._bootout_gateway("admin_bot", dry_run=False)
    assert res.ok is True and res.rc == 0
    assert recovery._restart_bot_gateway("admin_bot") == (True, "kickstart ok")


def test_recovery_dry_run_issues_no_launchctl():
    r = RecordingRunner()
    _inject(r)
    res = recovery._bootout_gateway("admin_bot", dry_run=True)
    assert res.ok is True and res.stdout == "(dry-run)"
    assert r.calls == []


# ── cli.py — uninstall maps onto remove(); migrate-jobs flows raw ────────────


def test_cli_uninstall_removes_launchd_jobs_via_seam(tmp_path, monkeypatch):
    """`evolve-admin uninstall` job removal = Scheduler.remove() exactly:
    bootout (rc ignored) + sudo rm of the plist, all through the seam."""
    import json

    from click.testing import CliRunner

    from evolve_admin import cli, deploy

    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    plist = daemons / "ai.openclaw.evolve.heal.plist"
    plist.write_text("x")

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "networkId": "t", "bots": {}, "sharedDir": str(tmp_path / "shared"),
    }))

    real_path = Path

    class _PathShim:
        """Redirect the hardcoded /Library/LaunchDaemons glob to tmp."""

        def __new__(cls, *a):
            p = real_path(*a)
            if str(p) == "/Library/LaunchDaemons":
                return daemons
            return p

    monkeypatch.setattr(cli, "Path", _PathShim)

    sudo_calls: list[list[str]] = []

    def fake_run_sudo(cmd, result, check=True):
        sudo_calls.append(list(cmd))
        return None

    monkeypatch.setattr(deploy, "_run_sudo", fake_run_sudo)

    r = RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=r, plist_dir=daemons))

    res = CliRunner().invoke(
        cli.main, ["--network", str(network_path), "uninstall", "--keep-data"],
        input="y\n",
    )
    assert res.exit_code == 0, res.output
    assert "Removed launchd: ai.openclaw.evolve.heal" in res.output
    assert ["sudo", "/bin/launchctl", "bootout",
            "system/ai.openclaw.evolve.heal"] in r.calls
    assert ["sudo", "/bin/rm", str(plist)] in r.calls
    assert not any(
        any("launchctl" in str(tok) for tok in c) for c in sudo_calls
    ), "_run_sudo must no longer carry launchctl argv"


def test_cli_migrate_jobs_bootstraps_via_seam(tmp_path, monkeypatch):
    import subprocess as _sp

    from click.testing import CliRunner

    from evolve_admin import cli

    src = tmp_path / "plists"
    src.mkdir()
    (src / "ai.openclaw.evolve.heal.plist").write_text("x")
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    monkeypatch.setattr(cli, "_PLISTS_DIR", src)
    monkeypatch.setattr(cli, "_LAUNCHD_DIR", daemons)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    run_calls: list[list[str]] = []

    def fake_run(cmd, *, check=True):
        run_calls.append(list(cmd))
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run", fake_run)

    r = RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=r))

    res = CliRunner().invoke(cli.main, ["migrate-jobs"])
    assert res.exit_code == 0, res.output
    dest = daemons / "ai.openclaw.evolve.heal.plist"
    assert ["sudo", "/bin/launchctl", "bootout",
            "system/ai.openclaw.evolve.heal"] in r.calls
    assert ["sudo", "/bin/launchctl", "bootstrap", "system", str(dest)] in r.calls
    assert "bootstrap ai.openclaw.evolve.heal" in res.output
    assert not any("launchctl" in str(tok) for c in run_calls for tok in c), (
        "_run must no longer carry launchctl argv"
    )


def test_cli_migrate_jobs_tolerates_already_loaded(tmp_path, monkeypatch):
    import subprocess as _sp

    from click.testing import CliRunner

    from evolve_admin import cli

    src = tmp_path / "plists"
    src.mkdir()
    (src / "ai.openclaw.evolve.heal.plist").write_text("x")
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    monkeypatch.setattr(cli, "_PLISTS_DIR", src)
    monkeypatch.setattr(cli, "_LAUNCHD_DIR", daemons)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        cli, "_run",
        lambda cmd, *, check=True: _sp.CompletedProcess(cmd, 0, "", ""))

    r = RecordingRunner({
        "bootstrap": (5, "", "Bootstrap failed: 119: Service is already loaded"),
    })
    set_scheduler(LaunchdScheduler(runner=r))

    res = CliRunner().invoke(cli.main, ["migrate-jobs"])
    assert res.exit_code == 0, res.output
    assert "bootstrap ai.openclaw.evolve.heal (already loaded)" in res.output


# ── service CLI surface — `evolve-admin service …` stays byte-identical ──────


def _invoke_service(*args: str):
    from click.testing import CliRunner

    from evolve_admin import cli

    return CliRunner().invoke(cli.main, ["service", *args])


def test_cli_service_install_output(svc_paths, monkeypatch):
    """`evolve-admin service install` — same operator-facing lines, and the
    registration argv still reaches launchd (unsudo'd, gui domain) through
    the seam."""
    plist, _sys = svc_paths
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)
    r = RecordingRunner({"list": _LIST_RUNNING})
    _inject(r)

    res = _invoke_service("install", "--port", "5051")
    assert res.exit_code == 0, res.output
    assert "Installing evolve-admin service" in res.output
    assert "✓ Service installed and running (PID 4242)" in res.output
    assert ["/bin/launchctl", "bootstrap", f"gui/{service._uid()}",
            str(plist)] in r.calls


def test_cli_service_uninstall_output(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner()
    _inject(r)

    res = _invoke_service("uninstall")
    assert res.exit_code == 0, res.output
    assert "✓ Service uninstalled successfully." in res.output
    assert not plist.exists()


def test_cli_service_uninstall_not_installed_exits_1(svc_paths):
    r = RecordingRunner()
    _inject(r)
    res = _invoke_service("uninstall")
    assert res.exit_code == 1
    assert "✗ Service not installed" in res.output
    assert r.calls == [], "no launchctl when there is nothing to remove"


def test_cli_service_restart_output(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner()
    _inject(r)

    res = _invoke_service("restart")
    assert res.exit_code == 0, res.output
    assert "✓ Restarted" in res.output
    assert r.calls == [
        ["/bin/launchctl", "kickstart", "-k",
         f"gui/{service._uid()}/{service.LABEL}"],
    ]


def test_cli_service_status_output(svc_paths):
    plist, _sys = svc_paths
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("x")
    r = RecordingRunner({"list": _LIST_RUNNING})
    _inject(r)

    res = _invoke_service("status")
    assert res.exit_code == 0, res.output
    assert "Installed" in res.output
    assert "PID 4242" in res.output


# ── argv gate — none of the four modules builds launchctl argv again ─────────


def test_no_launchctl_subprocess_argv_in_core_services():
    """Source-level regression guard for the S2b gate: a string literal
    'launchctl' (or '/bin/launchctl') may appear in comments, docstrings
    and operator-facing messages, but never as a quoted argv token in a
    list (subprocess or runner argv)."""
    pkg = _ADMIN_DIR / "evolve_admin"
    pattern = re.compile(r'[\[,]\s*["\'](?:/bin/)?launchctl["\']')
    offenders: list[str] = []
    for name in ("service.py", "health.py", "recovery.py", "cli.py"):
        text = (pkg / name).read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{name}:{i}: {line.strip()}")
    assert not offenders, (
        "launchctl argv literals crept back into the core-services cluster "
        "(go through the Scheduler seam instead):\n" + "\n".join(offenders)
    )

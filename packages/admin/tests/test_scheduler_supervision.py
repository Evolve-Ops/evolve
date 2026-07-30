"""tests/test_scheduler_supervision.py — the Scheduler.supervision() seam.

`supervision(label)` is the crash-loop telemetry read the gateway-supervision
net consumes. It normalizes each platform's restart accounting to one shape
``{n_restarts, sub_state, active_state, pid, last_exit, env, status_error}`` so
the consumer carries no launchd/systemd branch. These pins lock the systemd
parse (NRestarts + SubState + Environment), the launchd best-effort shape
(no counter, env from the plist), and the FakeScheduler seed surface — all with
ZERO real subprocesses.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime.scheduler import (  # noqa: E402
    FakeScheduler,
    JobSpec,
    LaunchdScheduler,
    SystemdScheduler,
)

GW = "ai.openclaw.evo-gateway"


# ── systemd ───────────────────────────────────────────────────────────────────


def _systemd(runner) -> SystemdScheduler:
    return SystemdScheduler(runner=runner, use_sudo=False)


def test_systemd_supervision_parses_autorestart_and_counter():
    show = (
        "LoadState=loaded\n"
        "ActiveState=activating\n"
        "SubState=auto-restart\n"
        "NRestarts=2266\n"
        "MainPID=0\n"
        "ExecMainStatus=1\n"
        "Environment=OPENCLAW_GATEWAY_PORT=19030 NODE_ENV=production\n"
    )
    s = _systemd(lambda argv: (0, show, ""))
    out = s.supervision(GW)
    assert out["sub_state"] == "auto-restart"
    assert out["active_state"] == "activating"
    assert out["n_restarts"] == 2266
    assert out["pid"] is None  # MainPID=0 → not running
    assert out["last_exit"] == "1"
    assert out["env"]["OPENCLAW_GATEWAY_PORT"] == "19030"
    assert out["env"]["NODE_ENV"] == "production"
    assert out["status_error"] is None


def test_systemd_supervision_running_unit_reports_pid():
    show = (
        "LoadState=loaded\nActiveState=active\nSubState=running\n"
        "NRestarts=0\nMainPID=4242\nExecMainStatus=0\nEnvironment=\n"
    )
    out = _systemd(lambda argv: (0, show, "")).supervision(GW)
    assert out["pid"] == 4242
    assert out["n_restarts"] == 0
    assert out["sub_state"] == "running"
    assert out["env"] == {}


def test_systemd_supervision_not_found_is_not_an_error():
    out = _systemd(lambda argv: (4, "", "Unit not found")).supervision(GW)
    assert out["status_error"] is None
    assert out["n_restarts"] is None


def test_systemd_supervision_escalation_failure_is_tooling_not_finding():
    out = _systemd(
        lambda argv: (1, "", "sudo: a password is required")
    ).supervision(GW)
    assert out["status_error"] == "cannot_escalate"


def test_systemd_supervision_unit_set_on_show_argv():
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return 0, "LoadState=loaded\nNRestarts=0\nMainPID=1\n", ""

    _systemd(runner).supervision(GW)
    # The show targets the .service unit and asks for the crash-loop properties.
    assert f"{GW}.service" in seen["argv"]
    props = next(a for a in seen["argv"] if "NRestarts" in a)
    assert "SubState" in props and "Environment" in props


# ── launchd ───────────────────────────────────────────────────────────────────


def test_launchd_supervision_no_counter_but_reads_plist_env(tmp_path):
    # A real plist on disk carrying the gateway port.
    plist = tmp_path / f"{GW}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": GW,
        "EnvironmentVariables": {"OPENCLAW_GATEWAY_PORT": "19030"},
    }))
    # launchctl list output for a managed, running job.
    list_out = '{\n\t"PID" = 9001;\n\t"LastExitStatus" = 0;\n};\n'
    s = LaunchdScheduler(
        runner=lambda argv: (0, list_out, ""),
        plist_dir=tmp_path, use_sudo=False,
    )
    out = s.supervision(GW)
    assert out["n_restarts"] is None      # launchd has no restart counter
    assert out["sub_state"] is None
    assert out["active_state"] is None
    assert out["pid"] == 9001
    assert out["env"]["OPENCLAW_GATEWAY_PORT"] == "19030"
    assert out["status_error"] is None


def test_launchd_supervision_missing_plist_env_is_empty(tmp_path):
    # No plist on disk (e.g. a loaded orphan whose plist was deleted).
    list_out = '{\n\t"PID" = 9001;\n};\n'
    s = LaunchdScheduler(
        runner=lambda argv: (0, list_out, ""),
        plist_dir=tmp_path, use_sudo=False,
    )
    out = s.supervision(GW)
    assert out["env"] == {}
    assert out["pid"] == 9001


def test_launchd_supervision_propagates_probe_error(tmp_path):
    s = LaunchdScheduler(
        runner=lambda argv: (1, "", "sudo: a password is required"),
        plist_dir=tmp_path, use_sudo=False,
    )
    assert s.supervision(GW)["status_error"] == "cannot_escalate"


# ── fake ──────────────────────────────────────────────────────────────────────


def test_fake_supervision_seeds_fields_and_tracks_live_pid():
    f = FakeScheduler()
    spec = JobSpec(label=GW, program_args=["/bin/echo"], keep_alive=True)
    f.seed_job(spec)
    f.seed_supervision(GW, n_restarts=5, sub_state="auto-restart",
                       env={"OPENCLAW_GATEWAY_PORT": "19030"})
    out = f.supervision(GW)
    assert out["n_restarts"] == 5
    assert out["sub_state"] == "auto-restart"
    assert out["env"]["OPENCLAW_GATEWAY_PORT"] == "19030"
    assert out["pid"] is not None  # live from the running fake job

    # kill + respawn → the pid the consumer sees changes (churn signal).
    first = out["pid"]
    f.kill(GW)
    assert f.supervision(GW)["pid"] != first


def test_fake_supervision_defaults_when_unseeded():
    f = FakeScheduler()
    f.seed_job(JobSpec(label=GW, program_args=["/bin/echo"], keep_alive=True))
    out = f.supervision(GW)
    assert out["n_restarts"] is None
    assert out["sub_state"] is None
    assert out["env"] == {}

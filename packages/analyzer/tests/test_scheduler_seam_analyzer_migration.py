"""4.3c S2b — analyzer launchctl call sites go through the Scheduler seam.

Every launchctl invocation in the migrated analyzer files
(heal.restart_gateway, the arbiter appliers' gateway kickstarts,
permissions.writer, spend_caps._pause_bot_crons,
tile_metrics._check_infra_daemons, oc_cli.oc_gateway_restart,
app_audit_runner._launchctl_labels) must route through
``runtime.scheduler`` — never a hand-built ``subprocess.run`` argv.

Test convention (mirrors packages/admin/tests/test_deploy_scheduler_migration.py):
inject ``LaunchdScheduler(runner=<recording fake>)`` via ``set_scheduler()``
— the real adapter builds the argv, the fake runner records it, and no
process is ever spawned. ⚠️ kickstart -k / unload are live-traffic
destructive in production; a test must NEVER reach a real launchctl.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from platform_profile import LINUX, MACOS, set_profile
from runtime.scheduler import LaunchdScheduler, set_scheduler


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


# ── heal.restart_gateway ─────────────────────────────────────────────────────


def test_heal_restart_gateway_routes_through_scheduler(monkeypatch):
    import heal

    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))
    # Pass the system-plist gate without touching /Library, and skip the
    # 3s settle sleep.
    monkeypatch.setattr(heal, "Path", lambda p: SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(heal.time, "sleep", lambda s: None)

    assert heal.restart_gateway("zzzbot") is True
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.zzzbot-gateway"],
    ]


def test_heal_restart_gateway_seam_failure_falls_back_no_launchctl(monkeypatch):
    """restart() failing must fall through to the openclaw-CLI fallback
    (unchanged contract) — and that fallback must not be a launchctl call."""
    import heal

    runner = _RecordingRunner(reply=lambda argv: (1, "", "boom"))
    set_scheduler(LaunchdScheduler(runner=runner))
    monkeypatch.setattr(heal, "Path", lambda p: SimpleNamespace(exists=lambda: True))

    fallback_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        fallback_calls.append(list(argv))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(heal.subprocess, "run", fake_run)

    assert heal.restart_gateway("zzzbot") is False
    assert len(runner.calls) == 1  # one seam restart attempt
    assert fallback_calls, "openclaw-CLI fallback should still be attempted"
    for argv in fallback_calls:
        assert "launchctl" not in " ".join(argv)


# ── arbiter appliers — gateway kickstart after config write ─────────────────


@pytest.mark.parametrize(
    "applier_module",
    ["arbiter.appliers.permissions", "arbiter.appliers.agent_defaults"],
)
def test_applier_materialize_and_write_kickstarts_via_seam(
    applier_module, monkeypatch, tmp_path
):
    """Production path of _materialize_and_write: write succeeds → gateway
    restart goes through the seam, and a failed restart does not change the
    (ok, message) result shape the autonomous-apply loop depends on."""
    import importlib

    import permissions.writer as writer_mod
    import evolve_admin.deploy as deploy_mod
    import evolve_admin.openclaw_materializer as mat_mod

    mod = importlib.import_module(applier_module)

    monkeypatch.setattr(mod, "_bot_home", lambda b: tmp_path)
    monkeypatch.setattr(writer_mod, "read_openclaw_json", lambda p: {"agents": {}})
    monkeypatch.setattr(
        mat_mod, "materialize_openclaw_policy_slice", lambda cfg, bot, shared: None
    )
    monkeypatch.setattr(
        deploy_mod, "safe_write_bot_config", lambda *a, **k: (True, "")
    )

    # restart fails (service not loaded) — must NOT surface as a write failure.
    runner = _RecordingRunner(reply=lambda argv: (3, "", "not loaded"))
    set_scheduler(LaunchdScheduler(runner=runner))

    ok, msg = mod._materialize_and_write(
        "zzzbot", tmp_path, kickstart=True, reason="test"
    )
    assert ok is True
    assert "zzzbot" in msg
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.zzzbot-gateway"],
    ]

    # kickstart=False must not touch the scheduler at all.
    runner.calls.clear()
    ok, _ = mod._materialize_and_write(
        "zzzbot", tmp_path, kickstart=False, reason="test"
    )
    assert ok is True
    assert runner.calls == []


# ── permissions.writer ───────────────────────────────────────────────────────


def test_write_openclaw_fields_kickstarts_via_seam(monkeypatch):
    import permissions.writer as writer_mod

    monkeypatch.setattr(writer_mod, "read_openclaw_json", lambda p: {"a": 1})

    staged: list[list[str]] = []

    def fake_run(argv, **kwargs):
        staged.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # The /tmp-staging cp/chown/chmod ritual stays on subprocess — fake it.
    monkeypatch.setattr(writer_mod.subprocess, "run", fake_run)

    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    ok, msg = writer_mod.write_openclaw_fields("zzzbot", {"a": 2})
    assert ok is True
    # Gateway restart went through the seam…
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.zzzbot-gateway"],
    ]
    # …and the remaining direct subprocess calls contain no launchctl.
    assert staged, "cp/chown/chmod staging should have run"
    for argv in staged:
        assert "launchctl" not in " ".join(argv)


# ── spend_caps._pause_bot_crons ──────────────────────────────────────────────


_LIST_OUT = (
    "PID\tStatus\tLabel\n"
    "123\t0\tai.openclaw.zzzbot-gateway\n"
    "-\t0\tcom.acme.zzzbot.daily\n"
    "-\t0\tcom.other.job\n"
)


def test_pause_bot_crons_lists_and_unloads_via_seam():
    import spend_caps

    def reply(argv):
        if argv[-1] == "list":
            return 0, _LIST_OUT, ""
        return 0, "", ""

    runner = _RecordingRunner(reply=reply)
    set_scheduler(LaunchdScheduler(runner=runner))

    msg = spend_caps._pause_bot_crons("zzzbot")
    assert msg == "Paused 1 cron jobs"
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "list"],
        ["sudo", "/bin/launchctl", "unload",
         "/Library/LaunchDaemons/com.acme.zzzbot.daily.plist"],
    ]


def test_pause_bot_crons_reports_failed_unloads():
    import spend_caps

    def reply(argv):
        if argv[-1] == "list":
            return 0, _LIST_OUT, ""
        return 1, "", "Load failed"

    set_scheduler(LaunchdScheduler(runner=_RecordingRunner(reply=reply)))
    assert spend_caps._pause_bot_crons("zzzbot") == "Paused 0 cron jobs (1 failed)"


# ── tile_metrics._check_infra_daemons ────────────────────────────────────────


def test_check_infra_daemons_tri_state_via_seam(monkeypatch):
    """macOS: rc=113 → missing; sudo-gap → quiet (no false critical); rc=0 → loaded."""
    import tile_metrics

    set_profile(MACOS)

    def reply(argv):
        label = argv[-1]
        if label.endswith(".heal"):
            return 113, "", "Could not find service"
        if label.endswith(".verify"):
            return 1, "", "sudo: a password is required"
        return 0, '{\n\t"PID" = 42;\n}', ""

    monkeypatch.setattr(
        tile_metrics, "_infra_probe_scheduler",
        LaunchdScheduler(runner=_RecordingRunner(reply=reply), timeout=5.0),
    )
    assert tile_metrics._check_infra_daemons() == ["heal"]


def test_check_infra_daemons_gates_out_on_linux(monkeypatch):
    """Linux: the launchd-only probe gates OUT — returns [] (no chip), and
    NEVER touches the probe scheduler (so no launchctl is reached)."""
    import tile_metrics

    set_profile(LINUX)

    # A recording runner that fails the test if it is ever invoked — the
    # Linux gate must short-circuit before any raw() probe.
    def boom(argv):
        raise AssertionError(f"probe must not run on Linux; got argv={argv}")

    monkeypatch.setattr(
        tile_metrics, "_infra_probe_scheduler",
        LaunchdScheduler(runner=_RecordingRunner(reply=boom), timeout=5.0),
    )
    assert tile_metrics._check_infra_daemons() == []


# ── oc_cli.oc_gateway_restart ────────────────────────────────────────────────


def _write_network(tmp_path, bot_id, user):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"bots": {bot_id: {"user": user}}}))
    return str(net)


def test_oc_gateway_restart_kickstart_via_seam(tmp_path):
    import oc_cli

    net = _write_network(tmp_path, "zzztestbot", "zzztestuser")
    runner = _RecordingRunner()
    set_scheduler(LaunchdScheduler(runner=runner))

    res = oc_cli.oc_gateway_restart("zzztestbot", network_path=net)
    assert res["ok"] is True
    assert res["service"] == "ai.openclaw.zzztestbot-gateway"
    assert res["domain"] == "system"
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k",
         "system/ai.openclaw.zzztestbot-gateway"],
    ]


def test_oc_gateway_restart_bootstrap_then_kickstart_via_seam(
    tmp_path, monkeypatch
):
    import oc_cli

    net = _write_network(tmp_path, "zzztestbot", "zzztestuser")
    plist = str(tmp_path / "ai.openclaw.zzztestbot-gateway.plist")
    monkeypatch.setattr(
        oc_cli, "_discover_gateway_domain", lambda svc, user, uid: ("system", plist)
    )

    state = {"kicks": 0}

    def reply(argv):
        if argv[2] == "kickstart":
            state["kicks"] += 1
            if state["kicks"] == 1:
                return 1, "", "Could not find service"
            return 0, "", ""
        if argv[2] == "bootstrap":
            return 0, "", ""
        raise AssertionError(f"unexpected argv: {argv}")

    runner = _RecordingRunner(reply=reply)
    set_scheduler(LaunchdScheduler(runner=runner))

    res = oc_cli.oc_gateway_restart("zzztestbot", network_path=net)
    assert res["ok"] is True
    assert res["method"] == "bootstrap+kickstart"
    assert res["plist"] == plist
    svc = "system/ai.openclaw.zzztestbot-gateway"
    assert runner.calls == [
        ["sudo", "/bin/launchctl", "kickstart", "-k", svc],
        ["sudo", "/bin/launchctl", "bootstrap", "system", plist],
        ["sudo", "/bin/launchctl", "kickstart", "-k", svc],
    ]


def test_oc_gateway_restart_non_launchd_adapter_fails_closed(
    tmp_path, monkeypatch
):
    """A non-launchd Scheduler can't honor the raw() probe loop —
    oc_gateway_restart must keep its never-raise dict contract."""
    import oc_cli

    net = _write_network(tmp_path, "zzztestbot", "zzztestuser")
    # Keep the openclaw-CLI fallback from spawning anything real.
    monkeypatch.setattr(oc_cli, "_find_openclaw", lambda: None)

    class NotLaunchd:
        pass

    set_scheduler(NotLaunchd())
    res = oc_cli.oc_gateway_restart("zzztestbot", network_path=net)
    assert res["ok"] is False
    assert "scheduler unavailable" in res["error"]


# ── app_audit_runner._launchctl_labels ───────────────────────────────────────


def test_launchctl_labels_macos_uses_no_sudo_list_argv():
    """Bite 5b: macOS guarded-derives the unsudo'd launchd adapter and reaches
    it as ``/bin/launchctl list`` (NO sudo prefix — the audit runner executes
    as the bot user, who holds no launchctl sudo grants). Byte-identical to the
    pre-seam ``LaunchdScheduler(use_sudo=False, timeout=5.0)`` argv."""
    import app_audit_runner
    from runtime.scheduler import LaunchdScheduler, set_scheduler

    runner = _RecordingRunner(
        reply=lambda argv: (0, "PID\tStatus\tLabel\n7\t0\tcom.example.a\n", "")
    )
    set_scheduler(LaunchdScheduler(runner=runner))

    assert app_audit_runner._launchctl_labels() == ["com.example.a"]
    assert runner.calls == [["/bin/launchctl", "list"]]


def test_launchctl_labels_on_systemd_uses_seam_list(tmp_path):
    """Bite 5b: on a Linux pod the snapshot uses the injected SystemdScheduler's
    list() directly — the real systemd label set instead of ``[]``, and NEVER a
    launchctl call."""
    import app_audit_runner
    from runtime.scheduler import SystemdScheduler, set_scheduler

    calls: list[list[str]] = []

    def _reply(argv):
        calls.append(list(argv))
        if "list-units" in argv:
            return 0, "ai.openclaw.zzzbot-gateway.service loaded active running\n", ""
        return 0, "", ""

    set_scheduler(
        SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=_reply)
    )

    assert app_audit_runner._launchctl_labels() == ["ai.openclaw.zzzbot-gateway"]
    assert all("launchctl" not in " ".join(c) for c in calls)


def test_launchctl_labels_empty_on_failure():
    """Any list() failure self-empties to ``[]`` — the cron_labels_loaded
    assertion treats empty as 'nothing to compare against', never a crash."""
    import app_audit_runner
    from runtime.scheduler import LaunchdScheduler, set_scheduler

    runner = _RecordingRunner(reply=lambda argv: (1, "", "boom"))
    set_scheduler(LaunchdScheduler(runner=runner))
    assert app_audit_runner._launchctl_labels() == []

"""Golden + verb tests — JobSpec → systemd rendering (Linux port L1 / W3A).

The systemd sibling of ``test_plist_jobspec_parity.py``. Three layers:

1. **Unit-file golden tests**: every JobSpec-built job *shape* in the
   codebase (gateway keep-alive daemon, interval job, interval+jitter,
   calendar dict, calendar list, WatchPaths) renders through
   ``render_systemd_units`` and is compared against a frozen fixture under
   ``fixtures/systemd_golden/`` by **parsed-ini equality** (sections →
   key/value pairs), not byte-diff — comments and blank lines are free,
   key drift is not. Unlike the plist goldens (captured from pre-refactor
   emitters), these pin the FIRST blessed rendering: the v1 Linux contract
   from docs/design-linux-port-2026-06-10.md §3.

2. **Renderer unit behavior**: systemd Exec/Environment escaping
   (``%`` specifiers, ``$`` expansion, quoting), OnCalendar translation,
   keep_alive→Restart mapping, and the fail-loudly contract for anything
   JobSpec can express but systemd rendering can't.

3. **``SystemdScheduler`` verbs** via an injected runner — ZERO real
   systemctl spawns (module-level booby-trap, same as
   ``test_scheduler_swappability.py``), including the byte-identical
   install skip, stale-sibling reinstall, and the atomic service+timer
   pair handling.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from pathlib import Path

import pytest

from evolve_admin.runtime import (
    FakeScheduler,
    JobSpec,
    SystemdScheduler,
    render_systemd_units,
    set_scheduler,
)
from evolve_admin.runtime.scheduler import label_from_unit_name, systemd_unit_name

FIXTURES = Path(__file__).parent / "fixtures" / "systemd_golden"

# Linux-flavored pins (the /var/lib/evolve layout from design §6) — fixture
# values only; the renderer treats paths as opaque strings.
PIN_PY = "/var/lib/evolve/venv/bin/python3"
PIN_ANALYZER = "/var/lib/evolve/repo/packages/analyzer"


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO subprocess spawns, from ANY module — a real ``systemctl`` (or
    sudo cp) here would mutate the host. Same trap as the swappability
    proof: ``run``/``call``/``check_output`` all construct ``Popen``."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"a REAL subprocess spawn was attempted in a systemd adapter test. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)  # never leak an injected adapter into other tests


# ── the golden job shapes ─────────────────────────────────────────────────────
# One JobSpec per job *shape* the launchd emitters build today, with
# placeholder identities (scrub guard) and Linux-flavored paths.

GOLDEN_CASES: "dict[str, JobSpec]" = {
    # install_bot_gateway_plist shape: KeepAlive daemon, throttle, umask.
    "gateway": JobSpec(
        label="ai.openclaw.team_bot_b-gateway",
        program_args=[
            "/usr/bin/node", "/usr/lib/node_modules/openclaw/dist/index.js",
            "gateway", "--port", "18790",
        ],
        user="personal_bot_user",
        group_name="staff",
        keep_alive=True,
        run_at_load=True,
        throttle_interval=5,
        umask=63,
        comment="OpenClaw Gateway for team_bot_b (headless, survives logout)",
        stdout_path="/home/personal_bot_user/.openclaw/logs/gateway.log",
        stderr_path="/home/personal_bot_user/.openclaw/logs/gateway.err.log",
        env={
            "HOME": "/home/personal_bot_user",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "OPENCLAW_GATEWAY_PORT": "18790",
            "OPENCLAW_LAUNCHD_LABEL": "ai.openclaw.team_bot_b-gateway",
        },
    ),
    # deploy._plist_content interval shape with jitter + a spaced argument.
    "interval_jitter": JobSpec(
        label="ai.evolve.audit.team_bot_b",
        program_args=[PIN_PY, f"{PIN_ANALYZER}/audit.py", "--mode", "full sweep"],
        user="team_bot_b",
        start_interval=1800,
        jitter_seconds=300,
        stdout_path="/home/team_bot_b/.openclaw/logs/evolve-audit.log",
        stderr_path="/home/team_bot_b/.openclaw/logs/evolve-audit.err.log",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": PIN_ANALYZER,
            "EVOLVE_NETWORK": "/var/lib/evolve/network.json",
        },
    ),
    # interval + RunAtLoad shape (deploy_interval_args_runatload).
    "interval_runatload": JobSpec(
        label="ai.evolve.scan-status.team_bot_a",
        program_args=[
            PIN_PY, f"{PIN_ANALYZER}/scan_status.py",
            "--shared-dir", "/var/lib/evolve",
        ],
        user="team_bot_a",
        start_interval=3600,
        run_at_load=True,
        stdout_path="/home/team_bot_a/.openclaw/logs/evolve-scan-status.log",
        stderr_path="/home/team_bot_a/.openclaw/logs/evolve-scan-status.err.log",
    ),
    # single calendar dict with Weekday (deploy_weekday).
    "calendar_weekday": JobSpec(
        label="ai.evolve.evolve.weekly-report",
        program_args=[PIN_PY, f"{PIN_ANALYZER}/report.py"],
        user="evolve",
        start_calendar={"Weekday": 1, "Hour": 3, "Minute": 30},
        stdout_path="/home/evolve/.openclaw/logs/evolve-weekly-report.log",
        stderr_path="/home/evolve/.openclaw/logs/evolve-weekly-report.err.log",
    ),
    # calendar list-of-dicts (deploy_times).
    "calendar_times": JobSpec(
        label="ai.evolve.evolve.pod-report",
        program_args=[PIN_PY, f"{PIN_ANALYZER}/pod_report.py"],
        user="evolve",
        start_calendar=[{"Hour": 8, "Minute": 0}, {"Hour": 20, "Minute": 30}],
        stdout_path="/home/evolve/.openclaw/logs/evolve-pod-report.log",
        stderr_path="/home/evolve/.openclaw/logs/evolve-pod-report.err.log",
    ),
    # WatchPaths shape (better-engine: interval + watch + small jitter).
    "watch_paths": JobSpec(
        label="ai.openclaw.evolve.better",
        program_args=[PIN_PY, f"{PIN_ANALYZER}/better_engine_refresh.py"],
        user="evolve",
        jitter_seconds=5,
        env={"HOME": "/home/evolve", "EVOLVE_SHARED": "/var/lib/evolve"},
        start_interval=900,
        watch_paths=["/var/lib/evolve/better-engine/.refresh-urgent"],
        run_at_load=True,
        stdout_path="/var/lib/evolve/logs/better_engine.log",
        stderr_path="/var/lib/evolve/logs/better_engine.err",
    ),
}


def _parse_units(text: str) -> "dict[str, list[tuple[str, str]]]":
    """Minimal systemd-unit ini parser: ``{section: [(key, value), ...]}``.

    Order within a section is preserved (systemd honors repetition order
    for keys like Environment/OnCalendar); comments and blank lines are
    ignored — that's the "parsed equality, not byte equality" contract.
    configparser is unusable here: it forbids the duplicate keys systemd
    requires.
    """
    sections: "dict[str, list[tuple[str, str]]]" = {}
    current: "str | None" = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        assert current is not None, f"key/value before any [Section]: {line!r}"
        key, sep, value = line.partition("=")
        assert sep, f"non-assignment line in unit file: {line!r}"
        sections[current].append((key.strip(), value.strip()))
    return sections


@pytest.mark.parametrize("name", list(GOLDEN_CASES), ids=list(GOLDEN_CASES))
def test_rendering_matches_golden_fixture(name):
    spec = GOLDEN_CASES[name]
    rendered = render_systemd_units(spec)

    golden_files = sorted(p.name for p in FIXTURES.iterdir() if p.name.startswith(spec.label + "."))
    assert sorted(rendered) == golden_files, (
        f"{name}: rendered unit set {sorted(rendered)} != golden set {golden_files} "
        "(a gained/lost .timer/.path is a behavior change — re-bless deliberately)"
    )
    for unit_name, content in rendered.items():
        new = _parse_units(content)
        old = _parse_units((FIXTURES / unit_name).read_text())
        assert new == old, (
            f"{name}/{unit_name}: parsed unit diverged from golden.\n"
            f"new: {new}\nold: {old}"
        )


def test_golden_fixture_dir_has_no_strays():
    """Every fixture belongs to a case above — a stray file is a stale
    blessing that would silently stop being checked."""
    expected: "set[str]" = set()
    for spec in GOLDEN_CASES.values():
        expected.update(render_systemd_units(spec))
    on_disk = {p.name for p in FIXTURES.iterdir() if not p.name.startswith(".")}
    assert on_disk == expected


# ── golden semantics worth asserting by name (not just fixture equality) ────


def test_gateway_keepalive_maps_to_restart_always():
    svc = _parse_units(render_systemd_units(GOLDEN_CASES["gateway"])[
        "ai.openclaw.team_bot_b-gateway.service"])
    service = dict(svc["Service"])
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "5"          # throttle_interval wins over the 10s default
    assert service["UMask"] == "0077"            # decimal 63 → octal string
    assert ("WantedBy", "multi-user.target") in svc["Install"]
    # launchd KeepAlive retries forever — the start-rate limiter is disabled.
    assert ("StartLimitIntervalSec", "0") in svc["Unit"]


def test_interval_timer_oneshot_semantics():
    units = render_systemd_units(GOLDEN_CASES["interval_jitter"])
    tmr = _parse_units(units["ai.evolve.audit.team_bot_b.timer"])["Timer"]
    assert ("OnBootSec", "1800s") in tmr         # no run_at_load → first fire N after boot
    assert ("OnUnitActiveSec", "1800s") in tmr
    assert ("RandomizedDelaySec", "300s") in tmr  # jitter is native, no bash-sleep wrapper
    svc = _parse_units(units["ai.evolve.audit.team_bot_b.service"])
    exec_line = dict(svc["Service"])["ExecStart"]
    assert "sleep" not in exec_line and "RANDOM" not in exec_line
    assert exec_line.endswith('--mode "full sweep"')
    assert "Install" not in svc, "timer-activated services are never enabled directly"


def test_interval_run_at_load_folds_into_onbootsec_zero():
    units = render_systemd_units(GOLDEN_CASES["interval_runatload"])
    tmr = _parse_units(units["ai.evolve.scan-status.team_bot_a.timer"])["Timer"]
    assert ("OnBootSec", "0s") in tmr
    assert ("OnUnitActiveSec", "3600s") in tmr


def test_calendar_translations():
    units = render_systemd_units(GOLDEN_CASES["calendar_weekday"])
    tmr = _parse_units(units["ai.evolve.evolve.weekly-report.timer"])["Timer"]
    assert ("OnCalendar", "Mon *-*-* 03:30:00") in tmr

    units = render_systemd_units(GOLDEN_CASES["calendar_times"])
    tmr = _parse_units(units["ai.evolve.evolve.pod-report.timer"])["Timer"]
    cals = [v for k, v in tmr if k == "OnCalendar"]
    assert cals == ["*-*-* 08:00:00", "*-*-* 20:30:00"]


def test_watch_paths_renders_a_path_unit():
    units = render_systemd_units(GOLDEN_CASES["watch_paths"])
    assert set(units) == {
        "ai.openclaw.evolve.better.service",
        "ai.openclaw.evolve.better.timer",
        "ai.openclaw.evolve.better.path",
    }
    pth = _parse_units(units["ai.openclaw.evolve.better.path"])
    assert ("PathModified", "/var/lib/evolve/better-engine/.refresh-urgent") in pth["Path"]
    assert ("Unit", "ai.openclaw.evolve.better.service") in pth["Path"]
    # path-triggered urgent runs bypass the timer's RandomizedDelaySec —
    # they stay near-immediate, which is what the small jitter preserved
    # on launchd.
    tmr = _parse_units(units["ai.openclaw.evolve.better.timer"])["Timer"]
    assert ("RandomizedDelaySec", "5s") in tmr


# ── renderer unit behavior: escaping + fail-loudly contracts ─────────────────


def _exec_line(*program_args: str, **kw) -> str:
    units = render_systemd_units(JobSpec(label="ai.evolve.t", program_args=list(program_args), **kw))
    return dict(_parse_units(units["ai.evolve.t.service"])["Service"])["ExecStart"]


def test_exec_quoting_spaces_and_shell_text():
    line = _exec_line("/bin/bash", "-lc", "echo 'a & b' > /tmp/out")
    assert line == '/bin/bash -lc "echo \'a & b\' > /tmp/out"'


def test_exec_escapes_percent_dollar_backslash_and_quotes():
    line = _exec_line("/bin/echo", "100%", "$HOME", 'say "hi"', "back\\slash")
    # % → %% (specifier), $ → $$ (expansion), quotes/backslashes C-escaped.
    assert line == '/bin/echo 100%% "$$HOME" "say \\"hi\\"" "back\\\\slash"'


def test_env_assignment_quoting():
    units = render_systemd_units(JobSpec(
        label="ai.evolve.t", program_args=["/bin/true"],
        env={"NODE_ENV": "prod", "Q": 'a"<>&', "PCT": "50%"},
    ))
    svc = _parse_units(units["ai.evolve.t.service"])["Service"]
    envs = [v for k, v in svc if k == "Environment"]
    assert envs == ['"NODE_ENV=prod"', '"Q=a\\"<>&"', '"PCT=50%%"']


def test_env_key_must_be_a_valid_name():
    with pytest.raises(ValueError, match="environment name"):
        render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"], env={"BAD KEY": "x"},
        ))


def test_newline_injection_is_rejected_everywhere():
    """ini has no escaping for newlines — a \\n in any value would inject
    keys (e.g. User=root) into the unit. Must raise, never render."""
    evil = "x\n[Service]\nUser=root"
    for kw in (
        {"program_args": ["/bin/echo", evil]},
        {"program_args": ["/bin/true"], "comment": evil},
        {"program_args": ["/bin/true"], "env": {"K": evil}},
        {"program_args": ["/bin/true"], "stdout_path": evil},
        {"program_args": ["/bin/true"], "working_dir": evil},
        {"program_args": ["/bin/true"], "watch_paths": [evil]},
    ):
        with pytest.raises(ValueError, match="newline"):
            render_systemd_units(JobSpec(label="ai.evolve.t", **kw))


def test_label_must_be_unit_name_safe():
    for bad in ("", "has space", "slash/label", "new\nline"):
        with pytest.raises(ValueError):
            render_systemd_units(JobSpec(label=bad, program_args=["/bin/true"]))


def test_exec_path_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        render_systemd_units(JobSpec(label="ai.evolve.t", program_args=["echo", "hi"]))


def test_program_args_validation_matches_launchd_renderer():
    with pytest.raises(ValueError, match="non-empty"):
        render_systemd_units(JobSpec(label="ai.evolve.t", program_args=[]))
    with pytest.raises(ValueError, match="must be strings"):
        render_systemd_units(JobSpec(label="ai.evolve.t", program_args=["/bin/echo", 5050]))


def test_keep_alive_successful_exit_maps_to_on_failure():
    units = render_systemd_units(JobSpec(
        label="ai.evolve.t", program_args=["/bin/true"],
        keep_alive={"SuccessfulExit": False},
    ))
    svc = dict(_parse_units(units["ai.evolve.t.service"])["Service"])
    assert svc["Restart"] == "on-failure"
    assert svc["RestartSec"] == "10"  # launchd's default throttle


def test_unrenderable_keep_alive_shape_fails_loudly():
    with pytest.raises(ValueError, match="keep_alive shape"):
        render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"],
            keep_alive={"PathState": {"/tmp/x": True}},
        ))


def test_jitter_without_a_timer_fails_loudly():
    with pytest.raises(ValueError, match="RandomizedDelaySec"):
        render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"],
            keep_alive=True, jitter_seconds=30,
        ))


def test_unknown_calendar_keys_fail_loudly():
    with pytest.raises(ValueError, match="OnCalendar"):
        render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"],
            start_calendar={"Hour": 3, "Era": 1},
        ))


def test_calendar_weekday_names_and_wildcards():
    def cal(rule):
        units = render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"], start_calendar=rule,
        ))
        tmr = _parse_units(units["ai.evolve.t.timer"])["Timer"]
        return [v for k, v in tmr if k == "OnCalendar"][0]

    assert cal({"Minute": 15}) == "*-*-* *:15:00"               # hourly
    assert cal({"Hour": 2, "Minute": 5}) == "*-*-* 02:05:00"     # daily
    assert cal({"Weekday": 0, "Hour": 1, "Minute": 0}) == "Sun *-*-* 01:00:00"
    assert cal({"Weekday": 7, "Hour": 1, "Minute": 0}) == "Sun *-*-* 01:00:00"  # launchd: 7 is Sunday too
    assert cal({"Weekday": 5, "Hour": 17, "Minute": 0}) == "Fri *-*-* 17:00:00"
    assert cal({"Month": 1, "Day": 2, "Hour": 3, "Minute": 4}) == "*-01-02 03:04:00"


def test_extra_renders_nothing_and_logs_loudly(caplog):
    with caplog.at_level(logging.ERROR, logger="runtime.scheduler"):
        units = render_systemd_units(JobSpec(
            label="ai.evolve.t", program_args=["/bin/true"],
            extra={"LimitLoadToSessionType": "Aqua"},
        ))
    assert any("renders NOTHING" in r.getMessage() for r in caplog.records)
    assert "LimitLoadToSessionType" not in units["ai.evolve.t.service"]


def test_percent_is_specifier_escaped_in_expanded_settings():
    """systemd expands % specifiers in Description / WorkingDirectory /
    StandardOutput / PathModified — a literal % must render as %%."""
    units = render_systemd_units(JobSpec(
        label="ai.evolve.t", program_args=["/bin/true"],
        comment="50% faster",
        working_dir="/tmp/100%dir",
        stdout_path="/tmp/out-50%.log",
        watch_paths=["/tmp/flag-1%"],
    ))
    svc = _parse_units(units["ai.evolve.t.service"])
    assert ("Description", "50%% faster") in svc["Unit"]
    assert ("WorkingDirectory", "/tmp/100%%dir") in svc["Service"]
    assert ("StandardOutput", "append:/tmp/out-50%%.log") in svc["Service"]
    pth = _parse_units(units["ai.evolve.t.path"])
    assert ("PathModified", "/tmp/flag-1%%") in pth["Path"]


def test_watch_only_run_at_load_job_keeps_its_boot_start(tmp_path):
    """watch_paths + run_at_load with NO timer: launchd starts the job at
    load — the service must carry [Install] and be activated alongside
    the .path unit (nothing about run_at_load may drop silently)."""
    spec = JobSpec(
        label="ai.evolve.watcher", program_args=["/bin/true"],
        run_at_load=True, watch_paths=["/tmp/flag"],
    )
    units = render_systemd_units(spec)
    svc = _parse_units(units["ai.evolve.watcher.service"])
    assert ("WantedBy", "multi-user.target") in svc["Install"]

    runner = _Runner()
    assert _scheduler(tmp_path, runner).install(spec).ok
    assert runner.systemctl_calls() == [
        ["daemon-reload"],
        ["enable", "ai.evolve.watcher.path", "ai.evolve.watcher.service"],
        ["restart", "ai.evolve.watcher.path", "ai.evolve.watcher.service"],
    ]


def test_label_unit_name_mapping_round_trips():
    label = "ai.evolve.evolve.admin-ui"
    for kind in ("service", "timer", "path"):
        assert label_from_unit_name(systemd_unit_name(label, kind)) == label
    assert label_from_unit_name("ai.evolve.x.socket") is None
    assert label_from_unit_name("dbus.service") == "dbus"


# ── SystemdScheduler verbs (injected runner; nothing real is spawned) ────────


class _Runner:
    """Records every argv; answers via ``responder(argv) -> (rc, out, err)``
    (default success). Captures unit content staged through ``sudo cp`` the
    way the gateway plist parity test does."""

    def __init__(self, responder=None):
        self.calls: "list[list[str]]" = []
        self.staged: "dict[str, str]" = {}
        self._responder = responder

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:2] == ["sudo", "/bin/cp"] and len(argv) == 4:
            self.staged[argv[3]] = Path(argv[2]).read_text()
        if self._responder is not None:
            answer = self._responder(argv)
            if answer is not None:
                return answer
        return (0, "", "")

    def systemctl_calls(self) -> "list[list[str]]":
        out = []
        for argv in self.calls:
            bare = [a for a in argv if a not in ("sudo", "-n")]
            if bare and bare[0] == "/usr/bin/systemctl":
                out.append(bare[1:])
        return out


INTERVAL_SPEC = GOLDEN_CASES["interval_jitter"]
DAEMON_SPEC = GOLDEN_CASES["gateway"]


def _scheduler(tmp_path, runner=None, **kw):
    kw.setdefault("use_sudo", False)
    return SystemdScheduler(unit_dir=tmp_path, runner=runner or _Runner(), **kw)


def test_install_writes_unit_pair_and_activates_the_timer(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    res = sched.install(INTERVAL_SPEC)
    assert res.ok and not res.skipped
    assert res.message == f"Installed: {INTERVAL_SPEC.label}"

    rendered = render_systemd_units(INTERVAL_SPEC)
    for name, content in rendered.items():
        assert (tmp_path / name).read_text() == content
    assert runner.systemctl_calls() == [
        ["daemon-reload"],
        ["enable", f"{INTERVAL_SPEC.label}.timer"],
        ["restart", f"{INTERVAL_SPEC.label}.timer"],
    ]


def test_install_daemon_activates_the_service(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(DAEMON_SPEC).ok
    assert runner.systemctl_calls() == [
        ["daemon-reload"],
        ["enable", f"{DAEMON_SPEC.label}.service"],
        ["restart", f"{DAEMON_SPEC.label}.service"],
    ]


def test_install_skip_is_byte_identical_and_spawns_nothing(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok
    n_calls = len(runner.calls)

    res = sched.install(INTERVAL_SPEC)
    assert res.ok and res.skipped
    assert res.message == f"Up-to-date: {INTERVAL_SPEC.label} (skipped reinstall)"
    assert len(runner.calls) == n_calls, "the skip path must not touch systemctl"


def test_changed_spec_reinstalls(tmp_path):
    sched = _scheduler(tmp_path)
    assert sched.install(INTERVAL_SPEC).ok
    changed = dataclasses.replace(INTERVAL_SPEC, start_interval=300)
    res = sched.install(changed)
    assert res.ok and not res.skipped
    assert "OnUnitActiveSec=300s" in (tmp_path / f"{INTERVAL_SPEC.label}.timer").read_text()


def test_reinstall_without_schedule_removes_the_stale_timer(tmp_path):
    """A job that loses its timer (interval → keep-alive daemon) must not
    skip, and the leftover .timer is disabled + deleted — the pair is
    atomic in both directions."""
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok
    assert (tmp_path / f"{INTERVAL_SPEC.label}.timer").exists()

    daemon = dataclasses.replace(
        INTERVAL_SPEC, start_interval=None, jitter_seconds=0, keep_alive=True,
    )
    res = sched.install(daemon)
    assert res.ok and not res.skipped
    assert not (tmp_path / f"{INTERVAL_SPEC.label}.timer").exists()
    assert ["disable", "--now", f"{INTERVAL_SPEC.label}.timer"] in runner.systemctl_calls()


def test_install_with_sudo_stages_chowns_and_chmods(tmp_path):
    runner = _Runner()
    sched = SystemdScheduler(unit_dir=tmp_path, use_sudo=True, runner=runner)
    spec = GOLDEN_CASES["calendar_weekday"]
    assert sched.install(spec).ok

    rendered = render_systemd_units(spec)
    for name, content in rendered.items():
        dst = str(tmp_path / name)
        assert runner.staged[dst] == content, "staged content must be the rendered unit"
        assert ["sudo", "/usr/bin/chown", "root:root", dst] in runner.calls
        assert ["sudo", "/bin/chmod", "644", dst] in runner.calls
    # systemctl runs under sudo too
    assert ["sudo", "/usr/bin/systemctl", "daemon-reload"] in runner.calls


def test_install_unregistered_oneshot_only_reloads(tmp_path):
    """No keep_alive, no run_at_load, no schedule, no watch — registered
    (startable by hand) but never enabled or started, like a launchd
    bootstrap of a RunAtLoad=false job."""
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(JobSpec(label="ai.evolve.manual", program_args=["/bin/true"])).ok
    assert runner.systemctl_calls() == [["daemon-reload"]]


def test_install_surfaces_systemctl_failures(tmp_path):
    def fail_enable(argv):
        if "enable" in argv:
            return (1, "", "Failed to enable unit")
        return None

    sched = _scheduler(tmp_path, _Runner(fail_enable))
    res = sched.install(INTERVAL_SPEC)
    assert not res.ok
    assert "enable failed" in res.message


def test_remove_handles_the_pair_atomically(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok

    ok, msg = sched.remove(INTERVAL_SPEC.label)
    assert ok and msg == f"Removed: {INTERVAL_SPEC.label}"
    assert not any(tmp_path.iterdir()), "both unit files must be gone"
    assert ["disable", "--now",
            f"{INTERVAL_SPEC.label}.service", f"{INTERVAL_SPEC.label}.timer",
            ] in runner.systemctl_calls()
    assert runner.systemctl_calls()[-1] == ["daemon-reload"]


def test_remove_is_idempotent(tmp_path):
    runner = _Runner()
    ok, msg = _scheduler(tmp_path, runner).remove("ai.evolve.never-installed")
    assert ok
    assert "no unit files on disk" in msg
    assert runner.calls == [], "nothing to disable, nothing spawned"


def test_restart_targets_the_service(tmp_path):
    runner = _Runner()
    ok, _ = _scheduler(tmp_path, runner).restart("ai.evolve.evolve.admin-ui")
    assert ok
    assert runner.systemctl_calls() == [["restart", "ai.evolve.evolve.admin-ui.service"]]


# ── disable / enable (persistent pause / resume) ──────────────────────────────


def test_disable_stops_the_whole_on_disk_set_but_keeps_files(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok
    runner.calls.clear()

    ok, msg = sched.disable(INTERVAL_SPEC.label)
    assert ok and msg == f"Disabled: {INTERVAL_SPEC.label}"
    # disable --now on every unit on disk (.service + .timer), no daemon-reload,
    # and — unlike remove — the unit files stay.
    assert runner.systemctl_calls() == [
        ["disable", "--now",
         f"{INTERVAL_SPEC.label}.service", f"{INTERVAL_SPEC.label}.timer"],
    ]
    assert (tmp_path / f"{INTERVAL_SPEC.label}.timer").exists()


def test_enable_rearms_the_timer(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok
    runner.calls.clear()

    ok, msg = sched.enable(INTERVAL_SPEC.label)
    assert ok and msg == f"Enabled: {INTERVAL_SPEC.label}"
    # Activation unit is the timer (present); enable + restart re-arms it.
    # No daemon-reload — disable/enable never change unit content.
    assert runner.systemctl_calls() == [
        ["enable", f"{INTERVAL_SPEC.label}.timer"],
        ["restart", f"{INTERVAL_SPEC.label}.timer"],
    ]


def test_enable_daemon_targets_the_service(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(DAEMON_SPEC).ok
    runner.calls.clear()

    ok, _ = sched.enable(DAEMON_SPEC.label)
    assert ok
    assert runner.systemctl_calls() == [
        ["enable", f"{DAEMON_SPEC.label}.service"],
        ["restart", f"{DAEMON_SPEC.label}.service"],
    ]


def test_disable_enable_are_idempotent_when_no_units_on_disk(tmp_path):
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    ok, msg = sched.disable("ai.evolve.never-installed")
    assert ok and "no unit files on disk" in msg
    ok, msg = sched.enable("ai.evolve.never-installed")
    assert ok and "no unit files on disk" in msg
    assert runner.calls == [], "nothing on disk → nothing spawned"


def test_disable_surfaces_systemctl_failure(tmp_path):
    runner = _Runner(lambda argv: (1, "", "boom") if "disable" in argv else None)
    sched = _scheduler(tmp_path, runner)
    assert sched.install(INTERVAL_SPEC).ok
    ok, msg = sched.disable(INTERVAL_SPEC.label)
    assert not ok
    assert "disable --now failed" in msg


def test_kill_passes_the_signal(tmp_path):
    runner = _Runner()
    ok, _ = _scheduler(tmp_path, runner).kill("ai.evolve.x", signal="SIGKILL")
    assert ok
    assert runner.systemctl_calls() == [["kill", "-s", "SIGKILL", "ai.evolve.x.service"]]


def test_running_parses_is_active(tmp_path):
    sched = _scheduler(tmp_path, _Runner(lambda argv: (0, "active\n", "")))
    assert sched.running("ai.evolve.x") is True
    sched = _scheduler(tmp_path, _Runner(lambda argv: (3, "inactive\n", "")))
    assert sched.running("ai.evolve.x") is False


def test_status_parses_systemctl_show(tmp_path):
    show = "LoadState=loaded\nActiveState=active\nMainPID=4242\nExecMainStatus=0\n"
    sched = _scheduler(tmp_path, _Runner(lambda argv: (0, show, "") if "show" in argv else None))
    (tmp_path / "ai.evolve.x.service").write_text("x")
    assert sched.status("ai.evolve.x") == {
        "installed": True, "managed": True, "running": True,
        "pid": 4242, "last_exit": "0", "status_error": None,
    }


def test_status_loaded_but_idle(tmp_path):
    show = "LoadState=loaded\nActiveState=inactive\nMainPID=0\nExecMainStatus=78\n"
    sched = _scheduler(tmp_path, _Runner(lambda argv: (0, show, "") if "show" in argv else None))
    st = sched.status("ai.evolve.x")
    assert st["managed"] is True and st["running"] is False
    assert st["pid"] is None and st["last_exit"] == "78"


@pytest.mark.parametrize("answer", [
    (4, "", "Unit ai.evolve.ghost.service could not be found."),  # systemd ≥ 246
    (0, "LoadState=not-found\nActiveState=inactive\nMainPID=0\n", ""),  # older systemd
], ids=["rc4", "rc0-not-found"])
def test_status_unmanaged_matches_the_adapter_shape_contract(tmp_path, answer):
    """Dict-equality against FakeScheduler for the unmanaged case — the
    same cross-adapter pin test_scheduler_swappability.py holds between
    Fake and Launchd."""
    sched = _scheduler(tmp_path, _Runner(lambda argv: answer))
    assert sched.status("ai.evolve.ghost") == FakeScheduler().status("ai.evolve.ghost")


def test_list_dedupes_units_and_timers_to_labels(tmp_path):
    list_units = (
        "ai.evolve.evolve.admin-ui.service loaded active running Evolve Admin\n"
        "ai.evolve.audit.team_bot_b.service loaded inactive dead audit\n"
        "ai.evolve.audit.team_bot_b.timer loaded active waiting Timer\n"
        "ai.openclaw.evolve.better.path loaded active waiting Path watch\n"
        "● ai.evolve.evolve.heal.service loaded failed failed heal\n"
        "ssh.service loaded active running OpenBSD Secure Shell server\n"
    )
    list_timers = (
        "Mon 2026-06-15 03:30:00 UTC 3h left - - ai.evolve.audit.team_bot_b.timer ai.evolve.audit.team_bot_b.service\n"
    )

    def responder(argv):
        if "list-units" in argv:
            return (0, list_units, "")
        if "list-timers" in argv:
            return (0, list_timers, "")
        return None

    runner = _Runner(responder)
    sched = _scheduler(tmp_path, runner)
    assert sched.list(prefix="ai.evolve.") == [
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.audit.team_bot_b",
        "ai.evolve.evolve.heal",
    ]
    # the pattern rides into systemctl so a busy host isn't fully listed
    assert ["list-units", "--all", "--plain", "--no-legend", "ai.evolve.*"] \
        in runner.systemctl_calls()
    # without a prefix, everything systemd knows comes back (launchctl list parity)
    assert "ssh" in sched.list()


def test_list_returns_empty_on_failure(tmp_path):
    sched = _scheduler(tmp_path, _Runner(lambda argv: (1, "", "boom")))
    assert sched.list(prefix="ai.evolve.") == []


def test_install_skip_then_explicit_restart_via_real_call_path(tmp_path):
    """The deploy-side restart-happens guarantee
    (deploy._install_job_ensuring_restart) closes over this adapter with
    no call-site change — the W3A swappability proof, now against the
    real second platform."""
    from evolve_admin import deploy

    runner = _Runner()
    set_scheduler(_scheduler(tmp_path, runner))

    ok, msg = deploy._install_job_ensuring_restart(INTERVAL_SPEC)
    assert ok, msg
    ok, msg = deploy._install_job_ensuring_restart(INTERVAL_SPEC)
    assert ok, msg
    assert "restarted" in msg, "skip path must issue the explicit restart"
    assert runner.systemctl_calls()[-1] == \
        ["restart", f"{INTERVAL_SPEC.label}.service"]


# ── per-call timeout override (bite 1) ───────────────────────────────────────
#
# Mirrors the launchd shape in test_scheduler_seam.py: the override only takes
# effect on the DEFAULT subprocess runner (an injected 1-arg fake owns its own
# timeout). These assert the value reaching ``scheduler_mod.subprocess.run``
# while NO custom runner is injected.

from evolve_admin.runtime import scheduler as scheduler_mod  # noqa: E402


class _SystemctlTimeoutCapture:
    """Records (verb, timeout) per systemctl subcommand / binary, replies per
    verb so the verb logic can complete. Patched as the module's
    ``subprocess.run`` — the per-call override path."""

    def __init__(self, replies=None) -> None:
        self.seen: "list[tuple[str, float | None]]" = []
        self.replies = replies or {}

    @staticmethod
    def _key(argv) -> str:
        args = [a for a in argv if a not in ("sudo", "-n")]
        if args and args[0].endswith("systemctl"):
            return args[1] if len(args) > 1 else "systemctl"
        return args[0].rsplit("/", 1)[-1] if args else ""

    def __call__(self, argv, **kw):
        key = self._key([str(a) for a in argv])
        self.seen.append((key, kw.get("timeout")))
        rc, out, err = self.replies.get(key, (0, "", ""))

        class _R:
            returncode = rc
            stdout = out
            stderr = err

        return _R()

    def timeout_for(self, key: str) -> "float | None":
        for k, t in self.seen:
            if k == key:
                return t
        raise AssertionError(f"verb {key!r} never reached the runner; saw {self.seen!r}")


@pytest.mark.parametrize(
    "call, key",
    [
        (lambda s: s.restart("ai.evolve.x", timeout=15.0), "restart"),
        (lambda s: s.status("ai.evolve.x", timeout=15.0), "show"),
        (lambda s: s.list(timeout=15.0), "list-units"),
    ],
)
def test_systemd_per_call_timeout_override_reaches_default_runner(monkeypatch, tmp_path, call, key):
    cap = _SystemctlTimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = SystemdScheduler(unit_dir=tmp_path, use_sudo=False, timeout=30.0)  # no runner
    call(s)
    assert cap.timeout_for(key) == 15.0


def test_systemd_per_call_timeout_on_install_and_remove(monkeypatch, tmp_path):
    cap = _SystemctlTimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = SystemdScheduler(unit_dir=tmp_path, use_sudo=False, timeout=30.0)
    s.install(DAEMON_SPEC, timeout=15.0)
    assert {t for _k, t in cap.seen} == {15.0}, cap.seen
    assert cap.timeout_for("daemon-reload") == 15.0

    (tmp_path / f"{DAEMON_SPEC.label}.service").write_text("x")
    cap.seen.clear()
    s.remove(DAEMON_SPEC.label, timeout=15.0)
    assert cap.timeout_for("disable") == 15.0


def test_systemd_no_override_falls_back_to_constructor_timeout(monkeypatch, tmp_path):
    cap = _SystemctlTimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = SystemdScheduler(unit_dir=tmp_path, use_sudo=False, timeout=7.0)
    s.restart("ai.evolve.x")  # no timeout=
    assert cap.timeout_for("restart") == 7.0


def test_systemd_per_call_timeout_ignored_by_injected_fake_runner(tmp_path):
    """Backward-compat: a 1-arg ``_Runner`` still works through every verb
    when a per-call timeout override is passed (the override is NOT forwarded
    to the injected runner — no TypeError, byte-identical argv)."""
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    assert sched.install(DAEMON_SPEC, timeout=15.0).ok
    assert sched.restart(DAEMON_SPEC.label, timeout=15.0)[0]
    sched.status(DAEMON_SPEC.label, timeout=15.0)
    sched.list(timeout=15.0)
    assert sched.remove(DAEMON_SPEC.label, timeout=15.0)[0]
    # argv identical to the no-override path (restart still targets the service)
    assert ["restart", f"{DAEMON_SPEC.label}.service"] in runner.systemctl_calls()
    assert all(isinstance(c, list) for c in runner.calls)


def test_install_creates_stdout_log_dirs_through_the_runner(tmp_path):
    """fix 2 / W10-E: systemd (unlike launchd) does NOT create the directory
    behind StandardOutput/StandardError. The seam must mkdir+chown each log
    parent — to the unit's ``User=`` — BEFORE the unit starts, or it 209/STDOUT
    crash-loops (the live admin-ui hit exactly this: 39 restarts because
    /home/evolve/.openclaw/logs did not exist). use_sudo=False routes the calls
    through the injected runner, so we assert the argv without touching the real
    filesystem behind the spec's absolute log paths."""
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    res = sched.install(DAEMON_SPEC)
    assert res.ok and not res.skipped

    log_dir = str(Path(DAEMON_SPEC.stdout_path).parent)
    mkdir = ["/bin/mkdir", "-p", log_dir]
    # -h / --no-dereference: the chown must never follow a symlink swapped
    # in at the leaf — kernel-enforced even when the lstat audit raced.
    chown = ["/usr/bin/chown", "-h", DAEMON_SPEC.user, log_dir]
    assert mkdir in runner.calls, f"log dir mkdir not emitted; calls={runner.calls}"
    assert chown in runner.calls, f"log dir chown not emitted; calls={runner.calls}"

    # …and BEFORE the unit is activated (daemon-reload), so the dir already
    # exists when systemd opens the log file for the started unit.
    reload_idx = next(i for i, c in enumerate(runner.calls)
                      if c and c[-1] == "daemon-reload")
    assert runner.calls.index(mkdir) < reload_idx
    assert runner.calls.index(chown) < reload_idx


def test_install_skip_does_not_recreate_log_dirs(tmp_path):
    """The byte-identical skip path must stay side-effect-free: an up-to-date
    unit set already has its log dirs, so a skipped reinstall emits no mkdir."""
    runner = _Runner()
    sched = _scheduler(tmp_path, runner)
    rendered = render_systemd_units(DAEMON_SPEC)
    for name, content in rendered.items():
        (tmp_path / name).write_text(content)
    res = sched.install(DAEMON_SPEC)
    assert res.ok and res.skipped
    assert not any("/bin/mkdir" in c for c in runner.calls), runner.calls


# ── log-dir symlink hardening (privileged-fs TOCTOU family) ──────────────────
#
# The log parent lives under a bot-writable prefix; before this layer a bot
# could plant ``~/.openclaw/logs -> /etc/x`` and the next install would
# ``sudo mkdir -p`` + ``sudo chown <bot>`` the TARGET. Each fixture builds the
# actual attack on a real tmp_path filesystem — the audit is Python-side
# lstat, so it runs for real even though mkdir/chown go through the fake
# runner. The install must refuse LOUDLY (res.ok False, message names the
# symlink) and emit NO privileged fs op for the poisoned path.


def _spec_logging_under(logs_dir: Path) -> JobSpec:
    return dataclasses.replace(
        DAEMON_SPEC,
        stdout_path=str(logs_dir / "gateway.log"),
        stderr_path=str(logs_dir / "gateway.err.log"),
    )


def _fs_ops(runner: _Runner) -> "list[list[str]]":
    return [c for c in runner.calls
            if c and c[0] in ("/bin/mkdir", "/usr/bin/chown")]


def test_install_refuses_symlink_planted_at_log_dir(tmp_path):
    """The headline attack: the leaf itself is a bot-planted symlink."""
    oc = tmp_path / "home" / "bot" / ".openclaw"
    oc.mkdir(parents=True)
    os.symlink("/etc", oc / "logs")

    runner = _Runner()
    res = _scheduler(tmp_path / "units", runner).install(_spec_logging_under(oc / "logs"))
    assert not res.ok
    assert "symlink" in res.message and str(oc / "logs") in res.message
    # refused BEFORE any privileged fs op — and before any unit was written
    assert _fs_ops(runner) == [], runner.calls
    assert not (tmp_path / "units").exists()


def test_install_refuses_symlinked_intermediate_component(tmp_path):
    """`rm -rf`-style intermediate redirect: ``.openclaw`` itself is the link,
    so the leaf path string resolves inside the target."""
    home = tmp_path / "home" / "bot"
    home.mkdir(parents=True)
    os.symlink("/etc", home / ".openclaw")

    runner = _Runner()
    logs = home / ".openclaw" / "logs"
    res = _scheduler(tmp_path / "units", runner).install(_spec_logging_under(logs))
    assert not res.ok
    assert "symlink" in res.message and str(home / ".openclaw") in res.message
    assert _fs_ops(runner) == [], runner.calls


def test_install_refuses_log_dir_swapped_in_behind_the_mkdir(tmp_path):
    """The race arm: path is clean at the pre-audit, the bot swaps a symlink
    in while (as-if) the mkdir runs — the post-mkdir re-audit must catch it
    and the chown must never be emitted."""
    oc = tmp_path / "home" / "bot" / ".openclaw"
    oc.mkdir(parents=True)
    logs = oc / "logs"

    def racing_bot(argv):
        if argv[0] == "/bin/mkdir":
            os.symlink("/etc", logs)  # the swap, timed with the mkdir
        return None

    runner = _Runner(responder=racing_bot)
    res = _scheduler(tmp_path / "units", runner).install(_spec_logging_under(logs))
    assert not res.ok and "symlink" in res.message
    assert not any(c[0] == "/usr/bin/chown" for c in _fs_ops(runner)), runner.calls


def test_root_owned_intermediate_symlink_is_not_a_refusal(tmp_path, monkeypatch):
    """Ubuntu /usr-merge shape: a root-owned intermediate symlink is part of
    the OS layout, not plantable by a bot — install must proceed (with -h on
    the chown). Simulated via lstat, since only root can mint uid-0 links."""
    import stat as stat_mod
    import types

    home = tmp_path / "home" / "bot"
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    logs = oc / "logs"
    logs.mkdir()

    real_lstat = os.lstat

    def lstat_with_root_link(path, *a, **kw):
        if str(path) == str(oc):
            return types.SimpleNamespace(st_mode=stat_mod.S_IFLNK | 0o777, st_uid=0)
        return real_lstat(path, *a, **kw)

    # scheduler.py does a plain ``import os`` — patching the module object
    # here patches the same object it resolves at call time.
    monkeypatch.setattr(os, "lstat", lstat_with_root_link)

    runner = _Runner()
    res = _scheduler(tmp_path / "units", runner).install(_spec_logging_under(logs))
    assert res.ok, res.message
    assert ["/usr/bin/chown", "-h", DAEMON_SPEC.user, str(logs)] in runner.calls


@pytest.mark.skipif(os.geteuid() == 0, reason="mode bits don't bind root")
def test_install_refuses_unauditable_log_dir(tmp_path):
    """EACCES on the lstat walk ⇒ the path cannot be audited ⇒ no privileged
    op — refusing loudly beats chowning blind (unreadable-fail-safe)."""
    locked = tmp_path / "home" / "bot"
    locked.mkdir(parents=True)
    logs = locked / "logs"
    locked.chmod(0o000)
    try:
        runner = _Runner()
        res = _scheduler(tmp_path / "units", runner).install(_spec_logging_under(logs))
        assert not res.ok and "cannot lstat" in res.message
        assert _fs_ops(runner) == [], runner.calls
    finally:
        locked.chmod(0o755)

"""Phase 4.3 C Track S Step S1 — the Scheduler seam.

Every test injects a :class:`FakeRunner` — NO test in this file may ever
spawn a real ``launchctl``. ``kickstart -k`` and ``bootout`` are
live-traffic destructive (they restart/stop running bot gateways on a
pod host); ``_no_subprocess`` enforces the discipline at the module
level by making any subprocess spawn from the scheduler module an
immediate test failure.

Design: docs/design-phase4.3c-scheduler-isolation-seams-2026-06-10.md.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from evolve_admin.runtime import (
    InstallResult,
    JobSpec,
    LaunchdScheduler,
    Scheduler,
    get_scheduler,
    render_launchd_plist,
    set_scheduler,
)
from evolve_admin.runtime import scheduler as scheduler_mod


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    """Any real subprocess spawn from the scheduler module fails the test."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            "LaunchdScheduler attempted a REAL subprocess in a unit test — "
            f"inject a FakeRunner. argv={a!r}"
        )

    monkeypatch.setattr(scheduler_mod.subprocess, "run", _boom)


class FakeRunner:
    """Records every argv; replies per-launchctl-verb.

    ``responses`` maps a launchctl verb (``"bootstrap"``, ``"list"``, …) or
    a non-launchctl binary basename (``"cp"``, ``"rm"``, …) to an
    ``(rc, stdout, stderr)`` tuple. Unknown → success with empty output.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    @staticmethod
    def _key(argv: list[str]) -> str:
        args = list(argv)
        if args and args[0] == "sudo":
            args = args[1:]
        if not args:
            return ""
        if args[0].endswith("launchctl"):
            return args[1] if len(args) > 1 else "launchctl"
        return args[0].rsplit("/", 1)[-1]

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.responses.get(self._key(argv), (0, "", ""))

    def verbs(self) -> list[str]:
        return [self._key(c) for c in self.calls]


def _sched(tmp_path: Path, runner: FakeRunner, **kw) -> LaunchdScheduler:
    kw.setdefault("plist_dir", tmp_path)
    return LaunchdScheduler(runner=runner, **kw)


SPEC = JobSpec(
    label="ai.evolve.evolve.unit-test-job",
    program_args=["/bin/echo", "hello"],
    user="evolve",
    start_interval=900,
    run_at_load=True,
)


# ── Protocol conformance ──────────────────────────────────────────────────────


def test_launchd_scheduler_satisfies_protocol(tmp_path):
    assert isinstance(_sched(tmp_path, FakeRunner()), Scheduler)


# ── install ───────────────────────────────────────────────────────────────────


def test_install_non_sudo_writes_plist_and_bootstraps(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=False)
    res = s.install(SPEC)
    assert res.ok, res.message
    assert not res.skipped, "fresh install must not report the idempotent skip"

    dst = tmp_path / f"{SPEC.label}.plist"
    assert dst.exists()
    assert plistlib.loads(dst.read_bytes()) == plistlib.loads(
        render_launchd_plist(SPEC).encode()
    )
    # bootout (re-register), settle-poll (print), then bootstrap — non-sudo.
    assert r.verbs() == ["bootout", "print", "bootstrap"]
    assert r.calls[0] == ["/bin/launchctl", "bootout", f"system/{SPEC.label}"]
    assert r.calls[-1] == ["/bin/launchctl", "bootstrap", "system", str(dst)]


def test_install_is_idempotent_byte_identical_short_circuit(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=False)
    assert s.install(SPEC)[0]
    n_calls = len(r.calls)

    res = s.install(SPEC)  # identical spec → must not bounce the daemon
    assert res.ok
    assert res.skipped, "byte-identical re-install must set InstallResult.skipped"
    assert "Up-to-date" in res.message
    assert len(r.calls) == n_calls, "re-install of identical plist must not bootout/bootstrap"


def test_install_falls_through_when_existing_plist_unreadable(tmp_path, monkeypatch):
    """Existing plist on disk but unreadable (PermissionError) → must NOT take
    the byte-identical skip; fall through to a full (re)install. The privileged
    write below overwrites regardless. (Inherited from the retired
    deploy._write_plist's fallback contract — its install() home is here now.)"""
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=False)
    dst = tmp_path / f"{SPEC.label}.plist"
    dst.write_text("<plist>stale-and-unreadable</plist>")

    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if str(self) == str(dst):
            raise PermissionError("simulated")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    res = s.install(SPEC)
    assert res.ok, res.message
    assert not res.skipped, "unreadable existing plist must NOT short-circuit as skip"
    assert "bootstrap" in r.verbs(), "must fall through to a full (re)register"


def test_install_sudo_runs_full_pod_ritual(tmp_path):
    """sudo mode = the deploy._write_plist ritual: /tmp staging + sudo /bin/cp
    + chown root:wheel + chmod 644 + bootout + settle + bootstrap, full
    binary paths throughout (macOS-paths table in CLAUDE.md)."""
    staged: dict[str, str] = {}

    class CaptureRunner(FakeRunner):
        def __call__(self, argv):
            if self._key(argv) == "cp":
                staged["content"] = Path(argv[2]).read_text()  # before unlink
            return super().__call__(argv)

    r = CaptureRunner()
    s = _sched(tmp_path, r, use_sudo=True)
    assert s.install(SPEC).ok
    assert r.verbs() == ["cp", "chown", "chmod", "bootout", "print", "bootstrap"]

    dst = str(tmp_path / f"{SPEC.label}.plist")
    assert r.calls[0][:2] == ["sudo", "/bin/cp"] and r.calls[0][3] == dst
    assert r.calls[1] == ["sudo", "/usr/sbin/chown", "root:wheel", dst]
    assert r.calls[2] == ["sudo", "/bin/chmod", "644", dst]
    assert r.calls[3] == ["sudo", "/bin/launchctl", "bootout", f"system/{SPEC.label}"]
    assert r.calls[5] == ["sudo", "/bin/launchctl", "bootstrap", "system", dst]
    # The staged file carried the rendered spec.
    assert plistlib.loads(staged["content"].encode())["Label"] == SPEC.label


def test_install_reports_bootstrap_failure(tmp_path):
    r = FakeRunner({"bootstrap": (5, "", "Bootstrap failed: 5: Input/output error")})
    s = _sched(tmp_path, r, use_sudo=False)
    res = s.install(SPEC)
    assert not res.ok
    assert "Input/output error" in res.message


def test_install_sudo_cp_failure_aborts_before_launchctl(tmp_path):
    r = FakeRunner({"cp": (1, "", "cp: permission denied")})
    s = _sched(tmp_path, r, use_sudo=True)
    res = s.install(SPEC)
    assert not res.ok
    assert "permission denied" in res.message
    assert "bootout" not in r.verbs() and "bootstrap" not in r.verbs(), (
        "a failed plist write must not re-register the old job"
    )


def test_install_waits_for_unload_to_settle(tmp_path):
    """While ``launchctl print`` still reports the service (non-empty stdout),
    install must keep polling before bootstrap — bootstrapping mid-unload
    fails with 'Service is being unloaded' (deploy.py's hard-won lesson)."""
    state = {"polls": 0}

    class SlowUnload(FakeRunner):
        def __call__(self, argv):
            result = super().__call__(argv)  # records the call
            if self._key(argv) == "print":
                state["polls"] += 1
                if state["polls"] < 3:  # still tearing down for 2 polls
                    return (0, "service = { active }", "")
            return result

    s = _sched(tmp_path, SlowUnload(), use_sudo=False, unload_timeout=5.0)
    assert s.install(SPEC).ok
    assert state["polls"] == 3


# ── remove ────────────────────────────────────────────────────────────────────


def test_remove_non_sudo_boots_out_and_deletes(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=False)
    assert s.install(SPEC)[0]
    ok, msg = s.remove(SPEC.label)
    assert ok, msg
    assert not (tmp_path / f"{SPEC.label}.plist").exists()
    # non-sudo deletes directly (no runner call for rm) — bootout is last.
    assert r.verbs()[-1] == "bootout"


def test_remove_sudo_uses_sudo_rm(tmp_path):
    (tmp_path / f"{SPEC.label}.plist").write_text("x")
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=True)
    ok, _ = s.remove(SPEC.label)
    assert ok
    assert r.calls[0] == ["sudo", "/bin/launchctl", "bootout", f"system/{SPEC.label}"]
    assert r.calls[1] == ["sudo", "/bin/rm", str(tmp_path / f"{SPEC.label}.plist")]


def test_remove_is_idempotent_when_never_installed(tmp_path):
    r = FakeRunner({"bootout": (3, "", "Boot-out failed: 3: No such process")})
    s = _sched(tmp_path, r, use_sudo=False)
    ok, msg = s.remove("ai.evolve.evolve.never-existed")
    assert ok, msg


# ── restart ───────────────────────────────────────────────────────────────────


def test_restart_issues_kickstart_k(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r)
    ok, _ = s.restart("ai.evolve.evolve.admin-ui")
    assert ok
    assert r.calls == [[
        "sudo", "/bin/launchctl", "kickstart", "-k",
        "system/ai.evolve.evolve.admin-ui",
    ]]


def test_restart_failure_maps_to_false(tmp_path):
    r = FakeRunner({"kickstart": (113, "", "Could not find service")})
    ok, msg = _sched(tmp_path, r).restart("ai.evolve.evolve.gone")
    assert not ok
    assert "Could not find service" in msg


# ── disable / enable (persistent pause / resume) ──────────────────────────────


def test_disable_persists_then_boots_out(tmp_path):
    """disable = launchctl disable (reboot-surviving) THEN bootout (stop now);
    a bare bootout alone would let the plist auto-load at the next boot."""
    r = FakeRunner()
    s = _sched(tmp_path, r)
    ok, msg = s.disable("ai.evolve.evolve.app-x")
    assert ok, msg
    assert r.verbs() == ["disable", "bootout"]
    assert r.calls[0] == [
        "sudo", "/bin/launchctl", "disable", "system/ai.evolve.evolve.app-x",
    ]
    assert r.calls[1] == [
        "sudo", "/bin/launchctl", "bootout", "system/ai.evolve.evolve.app-x",
    ]


def test_disable_failure_short_circuits_before_bootout(tmp_path):
    r = FakeRunner({"disable": (1, "", "Could not disable service")})
    ok, msg = _sched(tmp_path, r).disable("ai.evolve.evolve.app-x")
    assert not ok
    assert "disable failed" in msg
    # A failed disable must NOT bootout — stopping the job without the
    # reboot-surviving guarantee is worse than doing nothing.
    assert r.verbs() == ["disable"]


def test_disable_bootout_not_loaded_still_succeeds(tmp_path):
    # bootout of a not-currently-loaded job fails; disable() ignores that rc
    # (the persistent disable already landed).
    r = FakeRunner({"bootout": (3, "", "Boot-out failed: 3: No such process")})
    ok, msg = _sched(tmp_path, r).disable("ai.evolve.evolve.app-x")
    assert ok, msg


def test_enable_clears_override_then_reloads(tmp_path):
    """enable = launchctl enable (clears override DB) THEN a clean reload
    (bootout-settle + bootstrap from the on-disk plist)."""
    plist = tmp_path / "ai.evolve.evolve.app-x.plist"
    plist.write_text("x")
    r = FakeRunner()
    s = _sched(tmp_path, r)
    ok, msg = s.enable("ai.evolve.evolve.app-x")
    assert ok, msg
    assert r.verbs() == ["enable", "bootout", "print", "bootstrap"]
    assert r.calls[0] == [
        "sudo", "/bin/launchctl", "enable", "system/ai.evolve.evolve.app-x",
    ]
    assert r.calls[-1] == [
        "sudo", "/bin/launchctl", "bootstrap", "system", str(plist),
    ]


def test_enable_no_plist_on_disk_is_noop_success(tmp_path):
    # Override cleared, but nothing to bootstrap — idempotent no-op success.
    r = FakeRunner()
    ok, msg = _sched(tmp_path, r).enable("ai.evolve.evolve.ghost")
    assert ok
    assert r.verbs() == ["enable"]
    assert "no plist" in msg


def test_enable_failure_maps_to_false(tmp_path):
    r = FakeRunner({"enable": (1, "", "Could not enable service")})
    ok, msg = _sched(tmp_path, r).enable("ai.evolve.evolve.app-x")
    assert not ok
    assert "enable failed" in msg


def test_enable_bootstrap_failure_maps_to_false(tmp_path):
    (tmp_path / "ai.evolve.evolve.app-x.plist").write_text("x")
    r = FakeRunner({"bootstrap": (5, "", "Bootstrap failed")})
    ok, msg = _sched(tmp_path, r).enable("ai.evolve.evolve.app-x")
    assert not ok
    assert "bootstrap failed" in msg


# ── list ──────────────────────────────────────────────────────────────────────

_LAUNCHCTL_LIST = (
    "PID\tStatus\tLabel\n"
    "4242\t0\tai.evolve.evolve.admin-ui\n"
    "-\t0\tai.evolve.evolve.heal\n"
    "123\t0\tcom.apple.mdworker\n"
    "-\t78\tai.openclaw.evolve.better\n"
)


def test_list_parses_labels_and_filters_by_prefix(tmp_path):
    r = FakeRunner({"list": (0, _LAUNCHCTL_LIST, "")})
    s = _sched(tmp_path, r)
    assert s.list() == [
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.heal",
        "com.apple.mdworker",
        "ai.openclaw.evolve.better",
    ]
    assert s.list(prefix="ai.evolve.") == [
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.heal",
    ]


def test_list_failure_returns_empty(tmp_path):
    r = FakeRunner({"list": (1, "", "Could not connect")})
    assert _sched(tmp_path, r).list() == []


# ── status / running ──────────────────────────────────────────────────────────

_LAUNCHCTL_LIST_LABEL = (
    "{\n"
    '\t"LimitLoadToSessionType" = "System";\n'
    '\t"Label" = "ai.evolve.evolve.admin-ui";\n'
    '\t"LastExitStatus" = 0;\n'
    '\t"PID" = 4242;\n'
    "};\n"
)


def test_status_running_job(tmp_path):
    (tmp_path / "ai.evolve.evolve.admin-ui.plist").write_text("x")
    r = FakeRunner({"list": (0, _LAUNCHCTL_LIST_LABEL, "")})
    s = _sched(tmp_path, r)
    st = s.status("ai.evolve.evolve.admin-ui")
    assert st == {
        "installed": True,
        "managed": True,
        "running": True,
        "pid": 4242,
        "last_exit": "0",
        "status_error": None,
    }
    assert s.running("ai.evolve.evolve.admin-ui") is True


def test_status_registered_but_not_running(tmp_path):
    out = '{\n\t"Label" = "ai.evolve.evolve.heal";\n\t"LastExitStatus" = 256;\n};\n'
    r = FakeRunner({"list": (0, out, "")})
    st = _sched(tmp_path, r).status("ai.evolve.evolve.heal")
    assert st["managed"] is True
    assert st["running"] is False
    assert st["pid"] is None
    assert st["last_exit"] == "256"
    assert st["installed"] is False  # no plist staged in tmp_path


def test_status_unmanaged_label(tmp_path):
    r = FakeRunner({"list": (113, "", "Could not find service")})
    st = _sched(tmp_path, r).status("ai.evolve.evolve.ghost")
    assert st == {
        "installed": False,
        "managed": False,
        "running": False,
        "pid": None,
        "last_exit": None,
        "status_error": None,  # not-found is authoritative, not a probe failure
    }


def test_status_classifies_sudo_escalation_failure(tmp_path):
    """The PR #1579 tri-state through the verb surface: sudo-denied is a
    tooling failure (status_error="cannot_escalate"), NOT "not loaded"."""
    r = FakeRunner({"list": (1, "", "sudo: a password is required")})
    st = _sched(tmp_path, r).status("ai.evolve.evolve.heal")
    assert st["managed"] is False
    assert st["status_error"] == "cannot_escalate"


def test_status_classifies_other_probe_failure_as_error(tmp_path):
    r = FakeRunner({"list": (1, "", "launchctl: weird transient failure")})
    st = _sched(tmp_path, r).status("ai.evolve.evolve.heal")
    assert st["managed"] is False
    assert st["status_error"] == "error"


# ── kill ──────────────────────────────────────────────────────────────────────


def test_kill_sends_sigterm_by_default(tmp_path):
    r = FakeRunner()
    ok, _ = _sched(tmp_path, r).kill("ai.evolve.evolve.heal")
    assert ok
    assert r.calls == [[
        "sudo", "/bin/launchctl", "kill", "SIGTERM", "system/ai.evolve.evolve.heal",
    ]]


# ── custom domain (LaunchAgent shape) ─────────────────────────────────────────


def test_gui_domain_instance_targets_gui_domain(tmp_path):
    r = FakeRunner()
    s = LaunchdScheduler(
        domain="gui/501", plist_dir=tmp_path, use_sudo=False, runner=r,
    )
    s.restart("com.evolve.tunnel")
    assert r.calls == [["/bin/launchctl", "kickstart", "-k", "gui/501/com.evolve.tunnel"]]


# ── get/set factory ───────────────────────────────────────────────────────────


def test_get_scheduler_defaults_to_launchd_and_set_injects():
    set_scheduler(None)
    try:
        default = get_scheduler()
        assert isinstance(default, LaunchdScheduler)
        assert get_scheduler() is default  # process-wide singleton

        class StubScheduler:
            def install(self, spec): return InstallResult(True, "")
            def remove(self, label): return (True, "")
            def disable(self, label): return (True, "")
            def enable(self, label): return (True, "")
            def restart(self, label): return (True, "")
            def list(self, *, prefix=None): return []
            def status(self, label): return {}
            def running(self, label): return False
            def supervision(self, label): return {}
            def kill(self, label): return (True, "")
            def artifact_path(self, label): return f"/x/{label}.plist"

        stub = StubScheduler()
        assert isinstance(stub, Scheduler)  # structural conformance
        set_scheduler(stub)
        assert get_scheduler() is stub
    finally:
        set_scheduler(None)  # never leak into other tests


def test_default_scheduler_targets_pod_standard():
    set_scheduler(None)
    try:
        s = get_scheduler()
        assert s.domain == "system"
        assert s.plist_dir == Path("/Library/LaunchDaemons")
        assert s.use_sudo is True
    finally:
        set_scheduler(None)


# ── S2 affordances: raw(), sudo -n, runner timeout ───────────────────────────


def test_raw_passes_args_verbatim_and_returns_triple(tmp_path):
    r = FakeRunner({"print": (3, "out-text", "err-text")})
    s = _sched(tmp_path, r)
    rc, out, err = s.raw("print", "user/501")
    assert (rc, out, err) == (3, "out-text", "err-text")
    assert r.calls == [["sudo", "/bin/launchctl", "print", "user/501"]]


def test_sudo_non_interactive_prefixes_sudo_dash_n(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r, sudo_non_interactive=True)
    s.raw("list", "com.evolve.x")
    assert r.calls == [["sudo", "-n", "/bin/launchctl", "list", "com.evolve.x"]]


def test_sudo_non_interactive_ignored_without_sudo(tmp_path):
    r = FakeRunner()
    s = _sched(tmp_path, r, use_sudo=False, sudo_non_interactive=True)
    s.raw("list")
    assert r.calls == [["/bin/launchctl", "list"]]


def test_timeout_kwarg_reaches_the_default_runner(monkeypatch):
    """LaunchdScheduler(timeout=…) threads through to subprocess.run; an
    exception there is reported as (1, "", str(e)), never raised."""
    import subprocess

    seen: dict = {}

    def _record(argv, **kw):  # never spawns — records and fails like a wedge
        seen["argv"] = list(argv)
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))

    monkeypatch.setattr(scheduler_mod.subprocess, "run", _record)
    s = LaunchdScheduler(timeout=5.0)
    rc, out, err = s.raw("list", "com.evolve.x")
    assert seen["timeout"] == 5.0
    assert seen["argv"] == ["sudo", "/bin/launchctl", "list", "com.evolve.x"]
    assert rc == 1 and out == ""
    assert "timed out" in err


# ── per-call timeout override (bite 1) ───────────────────────────────────────
#
# The override only takes effect on the DEFAULT subprocess runner — that is
# where a per-call timeout is meaningful (an injected fake runner owns its own
# timeout posture and stays 1-arg, see the backward-compat regression below).
# So these tests assert the value that reaches ``subprocess.run`` while NO
# custom runner is injected, mirroring ``test_timeout_kwarg_reaches_the_default_runner``.


class _TimeoutCapture:
    """Records the timeout subprocess.run is invoked with, per launchctl/binary
    verb, then short-circuits with a per-verb canned reply so the verb logic
    can complete. Patched in as ``scheduler_mod.subprocess.run`` — the
    default-runner path is the one the per-call override flows through."""

    def __init__(self, replies: dict | None = None) -> None:
        self.seen: list[tuple[str, float | None]] = []
        self.replies = replies or {}

    @staticmethod
    def _key(argv: list[str]) -> str:
        args = [a for a in argv if a not in ("sudo", "-n")]
        if args and args[0].endswith("launchctl"):
            return args[1] if len(args) > 1 else "launchctl"
        return args[0].rsplit("/", 1)[-1] if args else ""

    def __call__(self, argv, **kw):
        key = self._key(list(argv))
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
    "verb, call, key",
    [
        ("restart", lambda s: s.restart("ai.evolve.evolve.admin-ui", timeout=15.0), "kickstart"),
        ("status", lambda s: s.status("ai.evolve.evolve.admin-ui", timeout=15.0), "list"),
        ("list", lambda s: s.list(timeout=15.0), "list"),
    ],
)
def test_per_call_timeout_override_reaches_default_runner(monkeypatch, tmp_path, verb, call, key):
    cap = _TimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = LaunchdScheduler(plist_dir=tmp_path, timeout=30.0)  # NO injected runner
    call(s)
    assert cap.timeout_for(key) == 15.0, (
        f"{verb}(timeout=15.0) must thread 15.0 into the default runner"
    )


def test_per_call_timeout_override_on_install_and_remove(monkeypatch, tmp_path):
    cap = _TimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, timeout=30.0)
    s.install(SPEC, timeout=15.0)
    # every subprocess the install spawned carried the override
    assert {t for k, t in cap.seen} == {15.0}, cap.seen
    assert cap.timeout_for("bootstrap") == 15.0

    (tmp_path / f"{SPEC.label}.plist").write_text("x")
    cap.seen.clear()
    s.remove(SPEC.label, timeout=15.0)
    assert cap.timeout_for("bootout") == 15.0


def test_no_override_falls_back_to_constructor_timeout(monkeypatch, tmp_path):
    """restart(label) with no override uses the constructor's timeout — the
    pre-enrichment behavior is byte-identical."""
    cap = _TimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    s = LaunchdScheduler(plist_dir=tmp_path, timeout=7.0)
    s.restart("ai.evolve.evolve.admin-ui")  # no timeout=
    assert cap.timeout_for("kickstart") == 7.0


def test_default_timeout_is_30s_when_constructor_unset(monkeypatch, tmp_path):
    cap = _TimeoutCapture()
    monkeypatch.setattr(scheduler_mod.subprocess, "run", cap)
    LaunchdScheduler(plist_dir=tmp_path).restart("ai.evolve.evolve.admin-ui")
    assert cap.timeout_for("kickstart") == 30.0


def test_per_call_timeout_is_ignored_by_injected_fake_runner(tmp_path):
    """Backward-compat guarantee: an existing 1-arg FakeRunner still works
    through EVERY verb when a per-call timeout override is passed. The fake
    is a ``Callable[[list[str]], …]`` and owns its own timeout posture — the
    override must NOT be threaded into it (no TypeError, byte-identical argv)."""
    (tmp_path / f"{SPEC.label}.plist").write_text("x")
    r = FakeRunner({"list": (0, _LAUNCHCTL_LIST, "")})
    s = _sched(tmp_path, r, use_sudo=False)

    # install / remove / restart / status / list — all accept timeout=, all
    # still drive the 1-arg fake (proves the override is not forwarded to it).
    assert s.install(SPEC, timeout=15.0).ok
    assert s.restart(SPEC.label, timeout=15.0)[0]
    st = s.status(SPEC.label, timeout=15.0)  # fake replies rc=0 → managed
    assert st["managed"] is True
    assert "ai.evolve.evolve.admin-ui" in s.list(timeout=15.0)
    assert s.remove(SPEC.label, timeout=15.0)[0]

    # The fake was called 1-arg throughout (every recorded call is a plain
    # argv list — no timeout tuple/kwarg leaked in).
    assert all(isinstance(c, list) for c in r.calls)
    # argv is byte-identical to the no-override path: restart still kickstarts.
    assert ["/bin/launchctl", "kickstart", "-k", f"system/{SPEC.label}"] in r.calls

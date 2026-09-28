"""4.3c S3 — the Scheduler swappability proof.

Three layers:

1. **The proof flow**: a JobSpec-driven job is installed / restarted /
   inspected / removed through REAL call paths
   (``deploy._install_job_ensuring_restart`` for the write side,
   ``health._launchd_loaded`` for the read side) entirely against
   ``FakeScheduler`` via ``set_scheduler()``. The module-level
   ``_no_subprocess_anywhere`` guard booby-traps every subprocess entry
   point, so a passing run IS the zero-launchctl-spawned evidence.

2. **``FakeScheduler`` semantics** — install/skip/restart/kill/remove
   mirror ``LaunchdScheduler`` wherever a call site can observe them,
   including the exact ``status()`` dict shape.

3. **``SystemdScheduler``** — the second platform, REAL since W3A (the
   Linux port's L1 step, internal/design-linux-port-2026-06-10.md §3): held
   here to isinstance + per-verb signature parity with
   ``LaunchdScheduler`` so a swap can never break a call site. Its
   rendering goldens and verb behavior live in
   ``test_systemd_jobspec_golden.py``.

Neither portable adapter has ``raw()`` — deliberately. ``raw()`` runs
verbatim launchctl subcommands no other platform can honor; the call
sites that still need it are pinned as a shrink-only census in
``test_launchctl_seam_gates.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import subprocess

import pytest

from evolve_admin.runtime import (
    FakeScheduler,
    InstallResult,
    JobSpec,
    LaunchdScheduler,
    Scheduler,
    SystemdScheduler,
    get_launchd_scheduler,
    get_scheduler,
    set_scheduler,
)


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO subprocess spawns, from ANY module. ``subprocess.run`` /
    ``call`` / ``check_output`` all construct ``subprocess.Popen``, so
    trapping the class (plus the ``os`` spawn primitives) catches every
    spawn path a call site could take — launchctl included."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            "a REAL subprocess spawn was attempted in the swappability proof — "
            f"the Scheduler seam leaked. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


@pytest.fixture(autouse=True)
def _reset_scheduler():
    yield
    set_scheduler(None)  # never leak an injected adapter into other tests


SPEC = JobSpec(
    label="ai.evolve.evolve.s3-proof-job",
    program_args=["/bin/echo", "proof"],
    user="evolve",
    run_at_load=True,
    start_interval=900,
)

# A timer oneshot (SPEC above) is deliberately NOT bounced on the
# byte-identical skip path — "restarting" one runs the job. The
# restart-happens guarantee is a daemon contract, so the proof flow needs a
# daemon-shaped spec to exercise the restart verb through a real call path.
DAEMON_SPEC = dataclasses.replace(
    SPEC, label="ai.evolve.evolve.s3-proof-daemon", keep_alive=True,
)

PROTOCOL_VERBS = (
    "install", "remove", "disable", "enable", "restart", "list",
    "status", "running", "supervision", "kill",
)


# ── 1. the proof flow — real call paths, fake adapter, zero spawns ───────────


def test_jobspec_lifecycle_runs_end_to_end_on_fake_scheduler():
    from evolve_admin import deploy, health

    fake = FakeScheduler()
    set_scheduler(fake)

    # Install through the real deploy path (the gateway/infra-job ritual).
    ok, msg = deploy._install_job_ensuring_restart(DAEMON_SPEC)
    assert ok, msg
    st = fake.status(DAEMON_SPEC.label)
    assert st["managed"] and st["running"] and isinstance(st["pid"], int)
    first_pid = st["pid"]

    # Identical re-deploy: the skip path plus the restart-happens guarantee.
    # DAEMON_SPEC carries keep_alive=True, so the bounce is owed here.
    ok, msg = deploy._install_job_ensuring_restart(DAEMON_SPEC)
    assert ok, msg
    assert "restarted" in msg, "skip path must issue the explicit restart"
    assert fake.status(DAEMON_SPEC.label)["pid"] != first_pid, (
        "the bounce must be real"
    )

    # Read side through the real health path — Protocol surface, no raw().
    assert health._launchd_loaded(DAEMON_SPEC.label) is True
    assert fake.list(prefix="ai.evolve.") == [DAEMON_SPEC.label]

    # Remove, then the read path reports gone.
    ok, _ = get_scheduler().remove(DAEMON_SPEC.label)
    assert ok
    assert health._launchd_loaded(DAEMON_SPEC.label) is False
    assert fake.list() == []

    assert [c[0] for c in fake.calls] == ["install", "install", "restart", "remove"]


def test_timer_oneshot_skip_path_does_not_bounce_on_the_fake_adapter():
    """The other half of the discriminator, proved on the THIRD adapter.

    ``restart()`` on a timer/calendar oneshot runs the job rather than
    restarting it, so the byte-identical skip path must leave it alone. The
    launchd and systemd adapters are pinned in
    ``test_deploy_scheduler_migration.py``; FakeScheduler is proved here so
    every adapter a call site can be handed agrees.
    """
    from evolve_admin import deploy

    fake = FakeScheduler()
    set_scheduler(fake)

    assert deploy._install_job_ensuring_restart(SPEC)[0]
    pid = fake.status(SPEC.label)["pid"]

    ok, msg = deploy._install_job_ensuring_restart(SPEC)
    assert ok, msg
    assert "no bounce" in msg
    assert [c[0] for c in fake.calls] == ["install", "install"], (
        f"a timer oneshot was force-RUN on the skip path; calls={fake.calls!r}"
    )
    assert fake.status(SPEC.label)["pid"] == pid


def test_changed_spec_redeploys_without_the_skip_path():
    from evolve_admin import deploy

    fake = FakeScheduler()
    set_scheduler(fake)
    assert deploy._install_job_ensuring_restart(SPEC)[0]

    changed = dataclasses.replace(SPEC, start_interval=300)
    ok, msg = deploy._install_job_ensuring_restart(changed)
    assert ok, msg
    assert "restarted" not in msg, "a changed spec reinstalls; no skip+restart"
    assert fake.jobs[SPEC.label].start_interval == 300


# ── 2. FakeScheduler semantics ────────────────────────────────────────────────


def test_fake_scheduler_satisfies_protocol():
    assert isinstance(FakeScheduler(), Scheduler)


def test_fake_install_is_idempotent_on_identical_spec():
    fake = FakeScheduler()
    res = fake.install(SPEC)
    assert res.ok and not res.skipped
    pid = fake.status(SPEC.label)["pid"]

    res = fake.install(SPEC)
    assert res.ok and res.skipped
    assert "Up-to-date" in res.message
    assert fake.status(SPEC.label)["pid"] == pid, "the skip must not bounce the job"


def test_fake_install_validates_like_the_real_adapter():
    with pytest.raises(ValueError):
        FakeScheduler().install(JobSpec(label="ai.evolve.bad", program_args=[]))


def test_fake_install_start_on_load_mirrors_launchd():
    fake = FakeScheduler()
    idle = JobSpec(label="ai.evolve.evolve.idle", program_args=["/bin/echo"],
                   start_interval=900)
    assert fake.install(idle).ok
    assert fake.running(idle.label) is False, "no RunAtLoad/KeepAlive → not running"

    keeper = JobSpec(label="ai.evolve.evolve.keeper", program_args=["/bin/echo"],
                     keep_alive=True)
    assert fake.install(keeper).ok
    assert fake.running(keeper.label) is True


def test_fake_restart_unknown_label_fails_like_kickstart():
    ok, msg = FakeScheduler().restart("ai.evolve.evolve.ghost")
    assert not ok
    assert "Could not find service" in msg


def test_fake_kill_respects_keep_alive_respawn():
    fake = FakeScheduler()
    keeper = JobSpec(label="ai.evolve.evolve.keeper", program_args=["/bin/echo"],
                     keep_alive=True)
    plain = JobSpec(label="ai.evolve.evolve.plain", program_args=["/bin/echo"],
                    run_at_load=True)
    fake.install(keeper)
    fake.install(plain)
    pid = fake.status(keeper.label)["pid"]

    ok, _ = fake.kill(keeper.label)
    assert ok
    assert fake.running(keeper.label) is True, "KeepAlive jobs respawn"
    assert fake.status(keeper.label)["pid"] != pid

    ok, _ = fake.kill(plain.label)
    assert ok
    assert fake.running(plain.label) is False


def test_fake_disable_stops_and_enable_restarts():
    fake = FakeScheduler()
    keeper = JobSpec(label="ai.evolve.evolve.keeper", program_args=["/bin/echo"],
                     keep_alive=True)
    fake.install(keeper)
    assert fake.running(keeper.label) is True

    ok, _ = fake.disable(keeper.label)
    assert ok
    assert fake.running(keeper.label) is False, "disable stops the job now"
    assert keeper.label in fake.disabled

    ok, _ = fake.enable(keeper.label)
    assert ok
    assert fake.running(keeper.label) is True, "enable reloads a start-on-load job"
    assert keeper.label not in fake.disabled


def test_fake_enable_of_timer_job_stays_stopped():
    # A pure interval job doesn't start on load — enable clears the override
    # but the job only fires on schedule (pid stays None), mirroring launchd.
    fake = FakeScheduler()
    idle = JobSpec(label="ai.evolve.evolve.idle", program_args=["/bin/echo"],
                   start_interval=900)
    fake.install(idle)
    fake.disable(idle.label)
    ok, _ = fake.enable(idle.label)
    assert ok
    assert fake.running(idle.label) is False


def test_fake_disable_enable_idempotent_on_unknown_label():
    fake = FakeScheduler()
    ok, msg = fake.disable("ai.evolve.evolve.ghost")
    assert ok and "no plist" in msg
    ok, msg = fake.enable("ai.evolve.evolve.ghost")
    assert ok and "no plist" in msg


def test_fake_remove_is_idempotent():
    fake = FakeScheduler()
    ok, msg = fake.remove("ai.evolve.evolve.never-installed")
    assert ok
    assert "no plist on disk" in msg


def test_fake_status_shape_matches_launchd_adapter_exactly(tmp_path):
    """Dict-equality against the real adapter for the unmanaged case — the
    shape contract {installed, managed, running, pid, last_exit} can't drift."""
    launchd = LaunchdScheduler(
        runner=lambda argv: (113, "", "Could not find service"),
        plist_dir=tmp_path, use_sudo=False,
    )
    assert FakeScheduler().status("ai.evolve.ghost") == launchd.status("ai.evolve.ghost")


def test_fake_seed_job_arranges_state_without_recording_calls():
    fake = FakeScheduler()
    fake.seed_job(SPEC, running=False, last_exit="78")
    assert fake.calls == []
    st = fake.status(SPEC.label)
    assert st["managed"] is True and st["running"] is False and st["last_exit"] == "78"


# ── 3. SystemdScheduler — the second platform ─────────────────────────────────


def test_systemd_scheduler_satisfies_protocol():
    assert isinstance(SystemdScheduler(), Scheduler)


@pytest.mark.parametrize("verb", PROTOCOL_VERBS)
def test_adapter_signatures_match_launchd(verb):
    """Per-verb parameter parity (names, kinds, has-default) across all
    three adapters — a drifted signature would isinstance-pass but break
    call sites on swap."""

    def shape(cls):
        return [
            (p.name, p.kind, p.default is inspect.Parameter.empty)
            for p in inspect.signature(getattr(cls, verb)).parameters.values()
        ]

    assert shape(FakeScheduler) == shape(LaunchdScheduler)
    assert shape(SystemdScheduler) == shape(LaunchdScheduler)


def test_portable_adapters_have_no_raw_escape_hatch():
    """raw() is launchd-verbatim by design: only the launchd adapter has it,
    and the typed accessor refuses to hand it out under any other adapter."""
    assert hasattr(LaunchdScheduler, "raw")
    assert not hasattr(FakeScheduler, "raw")
    assert not hasattr(SystemdScheduler, "raw")

    for adapter in (FakeScheduler(), SystemdScheduler()):
        set_scheduler(adapter)
        with pytest.raises(RuntimeError, match="LaunchdScheduler"):
            get_launchd_scheduler()
    set_scheduler(None)


def test_install_result_field_contract_for_future_adapters():
    """Adapters report the idempotent skip as a FIELD — W3A must keep this
    (callers like _install_job_ensuring_restart key on .skipped, never on
    message substrings)."""
    assert InstallResult._fields == ("ok", "message", "skipped")

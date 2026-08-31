"""Phase 4.3 C Track S Step S2a — deploy.py on the Scheduler seam.

Pins the migration semantics that are NOT covered by the seam's own tests
(test_scheduler_seam.py):

1. The restart-happens guarantee for gateway installs. The legacy install
   ritual ALWAYS ran bootout+bootstrap, so every deploy bounced the
   gateway — config changes need a restarted gateway to take effect.
   ``Scheduler.install()`` skips the bounce when the on-disk plist is
   byte-identical, so ``_install_job_ensuring_restart`` must compensate
   with an explicit ``restart()`` on the skip path, and fall back to a
   full remove+install when the restart fails (plist on disk but service
   booted out — kickstart can't recover that state).
2. ``remove_orphaned_plists`` issues bootout+rm through the seam.
3. The argv gate: no test here (and no production path) may ever spawn a
   real ``launchctl`` — every test injects a runner / fake scheduler.

⚠️ ``kickstart -k`` and ``bootout`` are live-traffic destructive on a pod
host. Injection is mandatory, never optional.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.runtime import JobSpec, LaunchdScheduler, set_scheduler  # noqa: E402
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
    """Records argv lists; per-verb (rc, stdout, stderr) overrides."""

    def __init__(self, responses: dict | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for verb, resp in self.responses.items():
            # Match launchctl subverbs ("bootstrap") and binary basenames
            # ("rm" in ["sudo", "/bin/rm", ...]).
            if any(a == verb or str(a).endswith(f"/{verb}") for a in argv):
                return resp
        return (0, "", "")

    def verbs(self) -> list[str]:
        out = []
        for c in self.calls:
            for v in ("bootout", "bootstrap", "kickstart", "print", "cp",
                      "rm", "chown", "chmod"):
                if v in c or any(str(a).endswith(f"/{v}") for a in c):
                    out.append(v)
                    break
        return out


SPEC = JobSpec(
    label="ai.openclaw.testbot-gateway",
    program_args=["/bin/echo", "gateway"],
    user="testbot",
    keep_alive=True,
    run_at_load=True,
)


def _inject(runner, tmp_path) -> LaunchdScheduler:
    s = LaunchdScheduler(runner=runner, plist_dir=tmp_path, use_sudo=False)
    set_scheduler(s)
    return s


# ── _install_job_ensuring_restart: the restart-happens guarantee ─────────────


def test_changed_config_path_restarts_via_bootstrap(tmp_path):
    """Changed (or fresh) plist → install() runs bootout+bootstrap, which IS
    the restart. No extra kickstart may be issued (it would double-bounce)."""
    r = RecordingRunner()
    _inject(r, tmp_path)

    ok, msg = deploy._install_job_ensuring_restart(SPEC)
    assert ok, msg
    verbs = r.verbs()
    assert "bootout" in verbs and "bootstrap" in verbs
    assert "kickstart" not in verbs, (
        "fresh/changed install already bounces via bootout+bootstrap — an "
        "extra kickstart double-restarts the gateway"
    )


def test_identical_config_path_still_restarts(tmp_path):
    """Byte-identical plist → install() skips the bounce; the wrapper must
    issue an explicit kickstart so today's restart-happens guarantee holds
    (a redeploy with unchanged plist still reloaded openclaw.json)."""
    r = RecordingRunner()
    _inject(r, tmp_path)
    assert deploy._install_job_ensuring_restart(SPEC)[0]
    r.calls.clear()

    ok, msg = deploy._install_job_ensuring_restart(SPEC)
    assert ok, msg
    verbs = r.verbs()
    assert "kickstart" in verbs, (
        f"identical-plist install must still restart the daemon; verbs={verbs}"
    )
    assert "bootstrap" not in verbs, (
        "identical plist must not re-register (that was the skip's point)"
    )
    assert "restarted" in msg


def test_skip_with_dead_registration_falls_back_to_full_reinstall(tmp_path):
    """Plist on disk + byte-identical, but the service is NOT registered
    (operator bootout, failed earlier bootstrap). kickstart can't start an
    unregistered service — the wrapper must fall back to remove+install so
    the legacy always-bounce recovery behavior is preserved."""
    r = RecordingRunner()
    _inject(r, tmp_path)
    assert deploy._install_job_ensuring_restart(SPEC)[0]
    r.calls.clear()

    r.responses["kickstart"] = (113, "", "Could not find service")
    ok, msg = deploy._install_job_ensuring_restart(SPEC)
    assert ok, msg
    verbs = r.verbs()
    assert "kickstart" in verbs
    assert "bootstrap" in verbs, (
        f"failed kickstart after skip must trigger a full re-register; verbs={verbs}"
    )


def test_install_bot_gateway_plist_restarts_on_unchanged_redeploy(tmp_path, monkeypatch):
    """End-to-end through the public entrypoint: second redeploy with an
    unchanged gateway plist must still bounce the gateway."""
    r = RecordingRunner()
    _inject(r, tmp_path)
    monkeypatch.setattr(
        deploy.subprocess, "run",
        lambda *a, **kw: __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(deploy.shutil, "which", lambda *a, **kw: "/opt/homebrew/bin/node")
    monkeypatch.setattr(deploy, "_wait_for_gateway_port", lambda *a, **kw: True)

    ok, detail = deploy.install_bot_gateway_plist("testbot", 18799, user="testbot")
    assert ok, detail
    r.calls.clear()

    ok, detail = deploy.install_bot_gateway_plist("testbot", 18799, user="testbot")
    assert ok, detail
    verbs = r.verbs()
    assert "kickstart" in verbs or "bootstrap" in verbs, (
        f"unchanged redeploy must still restart the gateway; verbs={verbs}"
    )


# ── remove_orphaned_plists through the seam ───────────────────────────────────


def test_remove_orphaned_plists_uses_seam_remove(tmp_path):
    r = RecordingRunner()
    _inject(r, tmp_path)
    orphan = tmp_path / "ai.evolve.evolve.stale-job.plist"
    orphan.write_text("<plist/>")

    removed, failures = deploy.remove_orphaned_plists([orphan])
    assert removed == ["ai.evolve.evolve.stale-job"]
    assert failures == []
    verbs = r.verbs()
    assert "bootout" in verbs
    assert not orphan.exists(), "non-sudo remove() should delete the plist"


def test_remove_orphaned_plists_reports_failure_reason(tmp_path):
    # use_sudo=True so the rm goes through the (failing) runner.
    r = RecordingRunner({"rm": (1, "", "rm: it is stuck")})
    set_scheduler(LaunchdScheduler(runner=r, plist_dir=tmp_path, use_sudo=True))
    orphan = tmp_path / "ai.evolve.evolve.stuck-job.plist"
    orphan.write_text("<plist/>")

    removed, failures = deploy.remove_orphaned_plists([orphan])
    assert removed == []
    assert len(failures) == 1
    assert "ai.evolve.evolve.stuck-job" in failures[0]
    assert "stuck" in failures[0]


def test_remove_orphaned_plists_dry_run_issues_no_verbs(tmp_path):
    r = RecordingRunner()
    _inject(r, tmp_path)
    orphan = tmp_path / "ai.evolve.evolve.dry-job.plist"
    orphan.write_text("<plist/>")

    removed, failures = deploy.remove_orphaned_plists([orphan], dry_run=True)
    assert removed == ["ai.evolve.evolve.dry-job"]
    assert failures == []
    assert r.calls == []
    assert orphan.exists()


# ── the bounce is for DAEMONS only (timer oneshots must not be force-run) ────
#
# `Scheduler.restart()` is `kickstart -k` / `systemctl restart <label>.service`.
# On a timer/calendar-activated oneshot that does not restart anything — it RUNS
# THE JOB NOW. Units are byte-identical on nearly every deploy and the puller
# deploys every 15 min, so the skip path was force-running every monitor on the
# pod several times a day (~30 `systemctl restart …service` calls in one measured
# 2026-08-19 VPS deploy; model_liveness_monitor burns a real ~$0.89 agent turn
# set per run and tripped darwin's $5/day breaker).
#
# These tests pin BOTH halves of the discriminator, on BOTH scheduler impls,
# because the two pods run different ones (macOS mini + Linux VPS) and a fix in
# one adapter only would let them drift.


CALENDAR_MONITOR_SPEC = JobSpec(
    label="ai.openclaw.evolve.model_liveness_monitor",
    program_args=["/bin/echo", "probe"],
    user="evolve",
    # keep_alive left at its False default — a oneshot, like every monitor.
    run_at_load=False,
    start_calendar={"Hour": 4, "Minute": 40},
)

INTERVAL_MONITOR_SPEC = JobSpec(
    label="ai.openclaw.evolve.evo_path_probe_monitor",
    program_args=["/bin/echo", "probe"],
    user="evolve",
    # run_at_load=True is DELIBERATE here: most of the fleet's monitors set it,
    # and it must not readmit them to the bounce. It means "also run once when
    # loaded" — on a byte-identical redeploy nothing loads, so nothing is owed.
    run_at_load=True,
    start_interval=1800,
)


def _inject_systemd(runner, tmp_path) -> scheduler_mod.SystemdScheduler:
    s = scheduler_mod.SystemdScheduler(runner=runner, unit_dir=tmp_path, use_sudo=False)
    set_scheduler(s)
    return s


def _restart_calls(runner) -> list[list[str]]:
    """Every recorded argv that would restart/kickstart a job — the calls that
    force-RUN a timer oneshot. Asserted on the recorded argv, not on a mocked
    seam, so a refactor that renames the wrapper cannot make this pass vacantly.
    """
    return [
        c for c in runner.calls
        if "kickstart" in c or ("restart" in c and "--now" not in c)
    ]


@pytest.mark.parametrize("inject", [_inject, _inject_systemd], ids=["launchd", "systemd"])
def test_timer_oneshot_skip_path_issues_no_restart(inject, tmp_path):
    """Byte-identical timer/calendar oneshot → the wrapper must NOT bounce it.

    A bounce here is a forced job RUN: real tokens for model_liveness, and a
    sweep_resolve firing mid-deploy for every other monitor.
    """
    for spec in (CALENDAR_MONITOR_SPEC, INTERVAL_MONITOR_SPEC):
        r = RecordingRunner()
        inject(r, tmp_path / spec.label)

        ok, _msg = deploy._install_job_ensuring_restart(spec)  # fresh install
        assert ok
        r.calls.clear()

        ok, msg = deploy._install_job_ensuring_restart(spec)  # byte-identical
        assert ok, msg
        assert _restart_calls(r) == [], (
            f"{spec.label}: byte-identical timer oneshot was force-RUN; "
            f"calls={r.calls!r}"
        )
        assert "no bounce" in msg


@pytest.mark.parametrize("inject", [_inject, _inject_systemd], ids=["launchd", "systemd"])
def test_daemon_skip_path_still_bounces(inject, tmp_path):
    """#3362 regression guard — the half that must NOT change.

    A `deploy <bot>` bounces the gateway ONLY as a side effect of this wrapper.
    On a plugin-only change the unit is byte-identical, so the skip-path
    restart IS the bounce; when it no-ops the old gateway keeps the port, the
    port-bind wait is satisfied by it, and the deploy reports green while
    serving the OLD plugin.
    """
    r = RecordingRunner()
    inject(r, tmp_path)

    assert deploy._install_job_ensuring_restart(SPEC)[0]
    r.calls.clear()

    ok, msg = deploy._install_job_ensuring_restart(SPEC)
    assert ok, msg
    assert _restart_calls(r), (
        f"gateway (keep_alive=True) must still bounce on the byte-identical "
        f"skip path — #3362; calls={r.calls!r}"
    )
    assert "restarted" in msg


def test_is_timer_activated_oneshot_shapes():
    """The discriminator itself, spelled out per shape."""
    from evolve_admin.runtime import is_timer_activated_oneshot as pred

    assert pred(CALENDAR_MONITOR_SPEC)
    assert pred(INTERVAL_MONITOR_SPEC)

    # Supervised daemons — never.
    assert not pred(SPEC)                                            # keep_alive=True
    assert not pred(JobSpec(label="d", program_args=["/bin/echo"],
                            keep_alive={"SuccessfulExit": False},
                            start_interval=60))                      # launchd dict form
    # Path-activated (WatchPaths daemons) — never, even with a schedule.
    assert not pred(JobSpec(label="w", program_args=["/bin/echo"],
                            watch_paths=["/tmp/x"], start_interval=60))
    # No schedule at all (run-once-at-load) — never.
    assert not pred(JobSpec(label="o", program_args=["/bin/echo"], run_at_load=True))


def test_real_monitor_specs_are_recognized_as_timer_oneshots():
    """Bind the predicate to the PRODUCTION spec builder, not to hand-rolled
    fixtures — `_job_spec_for` is what every `_install_launchd` monitor goes
    through, so a change there that re-daemonizes monitors fails here."""
    from evolve_admin.runtime import is_timer_activated_oneshot as pred

    for schedule in (
        {"Hour": 4, "Minute": 40},              # daily (model_liveness)
        {"interval": 1800},                      # every 30 min (evo_path_probe)
        {"Minute": 5},                           # hourly (roster drift)
        {"Weekday": 0, "Hour": 3, "Minute": 45},  # weekly (vocabulary_expander)
        {"times": [{"Hour": 9, "Minute": 0}, {"Hour": 21, "Minute": 0}]},  # multi
    ):
        for run_at_load in (True, False):
            spec = deploy._job_spec_for(
                "ai.openclaw.evolve.some_monitor", "evolve",
                Path("/dev/null"), schedule, None, run_at_load, 0,
            )
            assert pred(spec), f"{schedule} run_at_load={run_at_load} not detected"

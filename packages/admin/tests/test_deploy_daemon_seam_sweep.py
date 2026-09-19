"""W10-C — every deploy.py daemon installer goes through the Scheduler seam.

Before this sweep, only ``_install_launchd_apply`` and
``_install_launchd_cost_converter`` passed ``via_seam=True``; every other
``_install_launchd_*`` wrote a launchd plist straight to
``/Library/LaunchDaemons`` (absent on Linux) and the four custom-shaped
daemons (admin-ui, mcp-bridge, signal-subscriber, better-engine) went
through the retired ``_write_plist``. None of them installed on a Linux
pod. The sweep flipped ``_install_launchd``'s default to ``via_seam=True``
and routed the four custom daemons through ``_install_spec_via_seam``.

These tests pin the contract that matters for the Linux port:

  * the generic installer chokepoint (``_install_launchd``, 60+ callers)
    and each custom daemon materialize as a **systemd unit on Linux** and a
    **launchd plist on macOS** — through ``get_scheduler()``, never a direct
    adapter construction or a hardcoded ``/Library/LaunchDaemons`` write;
  * the custom KeepAlive daemons preserve the old ``_write_plist``
    no-bounce-on-byte-identical contract (they must NOT restart the admin
    server / bridge on every puller-triggered redeploy).

⚠️ ``systemctl`` / ``launchctl`` are never spawned for real — every test
injects a scheduler with a recording runner and ``use_sudo=False`` so unit
files land in a tmp dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.runtime import (  # noqa: E402
    LaunchdScheduler,
    SystemdScheduler,
    set_scheduler,
)
from runtime import scheduler as scheduler_mod  # noqa: E402  (the seam's real home)


class _Runner:
    """Records argv lists; replies success to every launchctl/systemctl call.

    ``print`` returns empty (the unload-settle poll treats that as "gone").
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append([str(a) for a in argv])
        return (0, "", "")

    def verbs(self) -> list[str]:
        seen: list[str] = []
        for c in self.calls:
            for v in ("bootout", "bootstrap", "kickstart", "enable",
                      "restart", "disable", "daemon-reload", "reset-failed"):
                if any(a == v or str(a).endswith(f"/{v}") for a in c):
                    seen.append(v)
        return seen


@pytest.fixture(autouse=True)
def _forbid_real_scheduler_subprocess(monkeypatch):
    """A real subprocess from the scheduler module fails the test loudly."""

    def _boom(*a, **kw):  # pragma: no cover — guard
        raise AssertionError(
            f"scheduler reached a REAL subprocess — inject a runner. argv={a!r}"
        )

    monkeypatch.setattr(scheduler_mod, "_subprocess_runner", _boom)
    yield
    set_scheduler(None)


@pytest.fixture(autouse=True)
def _venv_python_exists(monkeypatch):
    """The generic installer preflight skips when VENV_PYTHON is absent; on a
    test box evolve-venv isn't installed, so point it at an extant binary."""
    monkeypatch.setattr(deploy, "VENV_PYTHON", "/bin/bash")


def _new_result():
    return deploy.DeployResult(bot_id="evolve", success=True)


# ── the generic chokepoint: _install_launchd (60+ callers) ───────────────────


def test_generic_installer_writes_systemd_unit_on_linux(tmp_path):
    """Default-path ``_install_launchd`` routes through the seam: on Linux the
    seam is the SystemdScheduler, so a ``.service`` unit lands on disk."""
    from platform_profile import LINUX, set_profile

    runner = _Runner()
    set_profile(LINUX)
    set_scheduler(SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=runner))
    try:
        deploy._install_launchd(
            label="ai.evolve.evolve.seam-probe",
            user="evolve",
            script_path=Path("/bin/sh"),  # extant → passes the preflight
            schedule={"interval": 900},
            result=_new_result(),
        )
    finally:
        set_profile(None)

    services = list(tmp_path.glob("*.service"))
    assert services, (
        "generic _install_launchd did not write a systemd unit — it bypassed "
        f"the seam. files={[p.name for p in tmp_path.iterdir()]}"
    )
    assert "daemon-reload" in runner.verbs()


def test_generic_installer_writes_launchd_plist_on_macos(tmp_path):
    """Same call on macOS materializes a launchd ``.plist`` via the seam —
    never a direct ``/Library/LaunchDaemons`` write."""
    runner = _Runner()
    # conftest pins MACOS already; be explicit for clarity.
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=runner))
    deploy._install_launchd(
        label="ai.evolve.evolve.seam-probe",
        user="evolve",
        script_path=Path("/bin/sh"),
        schedule={"interval": 900},
        result=_new_result(),
    )
    plists = list(tmp_path.glob("*.plist"))
    assert plists, "generic _install_launchd did not write a launchd plist via the seam"
    assert "bootstrap" in runner.verbs()


# ── the four custom-shaped daemons ───────────────────────────────────────────

# (installer fn, label substring). Each used to call _write_plist; now they
# route through _install_spec_via_seam → get_scheduler().install().
_CUSTOM = [
    ("admin-ui", lambda r: deploy._install_launchd_admin_ui("evolve", r)),
    ("signal-subscriber", lambda r: deploy._install_launchd_signal_subscriber(r)),
    ("better", lambda r: deploy._install_launchd_better_engine("evolve", r)),
    ("mcp-bridge", lambda r: deploy._install_launchd_mcp_bridge("evolve", r)),
]


@pytest.fixture
def _quiet_mcp_legacy(monkeypatch):
    """mcp-bridge sweeps legacy LaunchAgents before installing — stub that so
    the test never reaches a real ``launchctl``."""
    from evolve_admin import mcp_service
    monkeypatch.setattr(mcp_service, "_bootout_legacy_agents", lambda: [])


@pytest.mark.parametrize("label_sub,install_fn", _CUSTOM, ids=[c[0] for c in _CUSTOM])
def test_custom_daemon_writes_systemd_unit_on_linux(
    label_sub, install_fn, tmp_path, _quiet_mcp_legacy
):
    from platform_profile import LINUX, set_profile

    runner = _Runner()
    set_profile(LINUX)
    set_scheduler(SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=runner))
    try:
        install_fn(_new_result())
    finally:
        set_profile(None)

    services = list(tmp_path.glob("*.service"))
    assert services, (
        f"{label_sub}: no systemd unit written — custom daemon bypassed the seam"
    )


@pytest.mark.parametrize("label_sub,install_fn", _CUSTOM, ids=[c[0] for c in _CUSTOM])
def test_custom_daemon_writes_launchd_plist_on_macos(
    label_sub, install_fn, tmp_path, _quiet_mcp_legacy
):
    runner = _Runner()
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=runner))
    install_fn(_new_result())

    plists = [p for p in tmp_path.glob("*.plist") if label_sub in p.name]
    assert plists, f"{label_sub}: no launchd plist written via the seam"


def test_custom_daemon_does_not_bounce_on_byte_identical_reinstall(tmp_path):
    """The KeepAlive daemons must NOT restart on an unchanged redeploy — that
    was the whole point of the old ``_write_plist`` byte-identical skip, and
    why ``_install_spec_via_seam`` calls plain ``install()`` (no forced
    restart) rather than ``_install_job_ensuring_restart``. Otherwise the
    puller's deploy hook would bounce the admin server on every PR."""
    runner = _Runner()
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=runner))

    deploy._install_launchd_admin_ui("evolve", _new_result())   # fresh install
    assert "bootstrap" in runner.verbs(), "fresh install must register the job"
    runner.calls.clear()

    deploy._install_launchd_admin_ui("evolve", _new_result())   # identical redeploy
    verbs = runner.verbs()
    assert verbs == [], (
        "byte-identical admin-ui redeploy must NOT bounce the daemon "
        f"(no bootout/bootstrap/kickstart); got {verbs}"
    )


# ── retired per-bot jobs: the teardown must be seam-routed, not macOS-only ────


def test_retired_per_bot_sweep_removes_systemd_units_on_linux(tmp_path):
    """``_bootout_retired_per_bot_jobs`` must actually tear down the retired
    per-bot units on a Linux pod.

    Its predecessor ``_bootout_legacy_test_plists`` early-returned when the
    profile was not macOS and did a hardcoded ``/Library/LaunchDaemons`` rm
    otherwise — correct for the app-test daemon, which never existed on
    Linux, but it would have stranded every ``ai.openclaw.evolve.apply.<bot>``
    systemd unit on the VPS when the apply daemon was retired 2026-08-18.
    Going through ``get_scheduler().remove()`` covers both platforms.
    """
    from platform_profile import LINUX, set_profile

    runner = _Runner()
    set_profile(LINUX)
    set_scheduler(SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=runner))
    try:
        stale = tmp_path / "ai.openclaw.evolve.apply.team_bot_a.service"
        stale.write_text("[Unit]\nDescription=retired apply daemon\n")
        deploy._bootout_retired_per_bot_jobs("team_bot_a", _new_result())
    finally:
        set_profile(None)

    assert not stale.exists(), (
        "retired apply unit survived the sweep on Linux — the teardown is "
        "not platform-correct"
    )
    assert "daemon-reload" in runner.verbs()


def test_retired_per_bot_sweep_removes_launchd_plists_on_macos(tmp_path):
    runner = _Runner()
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=runner))
    stale = tmp_path / "ai.openclaw.evolve.apply.team_bot_a.plist"
    stale.write_text("<plist/>")
    legacy_test = tmp_path / "ai.openclaw.evolve.test.team_bot_a.plist"
    legacy_test.write_text("<plist/>")

    deploy._bootout_retired_per_bot_jobs("team_bot_a", _new_result())

    assert not stale.exists(), "retired apply plist survived the sweep"
    assert not legacy_test.exists(), "legacy app-test plist survived the sweep"


def test_retired_per_bot_sweep_is_idempotent(tmp_path):
    """Nothing installed is the steady state on a fresh pod — the sweep must
    not error, and must not log a bogus "removed" line every deploy."""
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=_Runner()))
    result = _new_result()
    deploy._bootout_retired_per_bot_jobs("team_bot_a", result)
    deploy._bootout_retired_per_bot_jobs("team_bot_a", result)

    assert result.success
    assert not any("removed retired per-bot job" in s for s in result.steps), (
        f"sweep claimed a removal with nothing installed: {result.steps}"
    )


# ── Pod-wide retired-job teardown (retired_jobs.sweep_pod) ────────────────
#
# The second trigger, added after the 2026-08-18 #3705 stall showed that
# ``deploy_bot`` step 6 was reachable on a hands-off pod only via the
# puller's lagging-bot sweep — which skips every HEAD-advancing tick and so
# never ran for the whole of a six-tick merge burst.


def _write_install_json(shared_dir, bots, bot_versions=None):
    import json
    (shared_dir / "install.json").write_text(json.dumps({
        "bots": bots,
        "bot_versions": {b: {"version": "2026.0818.1"} for b in (bot_versions or [])},
    }))


def test_sweep_pod_removes_retired_plists_for_every_pod_bot(tmp_path):
    from evolve_admin import retired_jobs

    shared = tmp_path / "shared"
    plists = tmp_path / "plists"
    shared.mkdir(); plists.mkdir()
    _write_install_json(shared, ["team_bot_a", "team_bot_b"])

    set_scheduler(LaunchdScheduler(plist_dir=plists, use_sudo=False, runner=_Runner()))
    stale = [
        plists / "ai.openclaw.evolve.apply.team_bot_a.plist",
        plists / "ai.openclaw.evolve.apply.team_bot_b.plist",
        plists / "ai.openclaw.evolve.test.team_bot_a.plist",
    ]
    for p in stale:
        p.write_text("<plist/>")
    live = plists / "ai.openclaw.evolve.measure.plist"
    live.write_text("<plist/>")

    removed, errors = retired_jobs.sweep_pod(shared)

    assert errors == {}
    assert not any(p.exists() for p in stale), "retired plists survived the pod sweep"
    assert live.exists(), "pod sweep removed a job that is NOT retired"
    assert sorted(removed) == [
        "ai.openclaw.evolve.apply.team_bot_a",
        "ai.openclaw.evolve.apply.team_bot_b",
        "ai.openclaw.evolve.test.team_bot_a",
    ]


def test_sweep_pod_covers_bots_only_present_as_deploy_stamps(tmp_path):
    """A bot dropped from the roster can still have left units on disk — that
    residue is exactly what a pod-wide sweep is for."""
    from evolve_admin import retired_jobs

    shared = tmp_path / "shared"
    plists = tmp_path / "plists"
    shared.mkdir(); plists.mkdir()
    _write_install_json(shared, ["team_bot_a"], bot_versions=["team_bot_a", "ghost"])

    set_scheduler(LaunchdScheduler(plist_dir=plists, use_sudo=False, runner=_Runner()))
    ghost = plists / "ai.openclaw.evolve.apply.ghost.plist"
    ghost.write_text("<plist/>")

    removed, _ = retired_jobs.sweep_pod(shared)

    assert not ghost.exists()
    assert "ai.openclaw.evolve.apply.ghost" in removed


def test_sweep_pod_spawns_no_subprocess_in_the_steady_state(tmp_path):
    """It runs every 15 minutes on every pod, so "nothing retired installed"
    must cost path checks only — no launchctl/systemctl spawns."""
    from evolve_admin import retired_jobs

    shared = tmp_path / "shared"
    plists = tmp_path / "plists"
    shared.mkdir(); plists.mkdir()
    _write_install_json(shared, ["team_bot_a", "team_bot_b", "team_bot_c"])

    runner = _Runner()
    set_scheduler(LaunchdScheduler(plist_dir=plists, use_sudo=False, runner=runner))
    removed, errors = retired_jobs.sweep_pod(shared)

    assert removed == [] and errors == {}
    assert runner.calls == [], (
        f"steady-state pod sweep shelled out {len(runner.calls)} time(s): "
        f"{runner.verbs()}"
    )


def test_sweep_pod_is_a_no_op_without_an_install_json(tmp_path):
    from evolve_admin import retired_jobs

    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=_Runner()))
    assert retired_jobs.sweep_pod(tmp_path / "no-such-dir") == ([], {})


def test_retired_label_list_is_shared_with_the_deploy_time_sweep(tmp_path):
    """One source of truth: a newly retired label added to the templates must
    reach BOTH teardown paths, or the pod-wide sweep silently under-covers."""
    from evolve_admin import retired_jobs

    labels = retired_jobs.retired_labels_for("team_bot_a")
    assert "ai.openclaw.evolve.apply.team_bot_a" in labels
    assert "ai.openclaw.evolve.test.team_bot_a" in labels

    runner = _Runner()
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path, use_sudo=False, runner=runner))
    for label in labels:
        (tmp_path / f"{label}.plist").write_text("<plist/>")

    deploy._bootout_retired_per_bot_jobs("team_bot_a", _new_result())

    assert not any((tmp_path / f"{label}.plist").exists() for label in labels), (
        "deploy-time sweep does not cover every label in the shared list"
    )


# ── Linux: teardown must leave no failed-unit residue ─────────────────────


def test_systemd_remove_resets_the_recorded_failure_state(tmp_path):
    """Deleting a unit file does not retract systemd's recorded failure: a
    removed unit keeps showing up in ``systemctl list-units --state=failed``
    as ``not-found / failed`` until ``reset-failed`` runs, so a SUCCESSFUL
    teardown trips anything watching for failed units. Observed on the Linux
    pod 2026-08-18 after the #3705 apply-unit retirement.
    """
    from platform_profile import LINUX, set_profile

    runner = _Runner()
    sched = SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=runner)
    set_profile(LINUX)
    set_scheduler(sched)
    try:
        (tmp_path / "ai.openclaw.evolve.apply.team_bot_a.service").write_text("[Unit]\n")
        ok, _msg = sched.remove("ai.openclaw.evolve.apply.team_bot_a")
    finally:
        set_profile(None)

    assert ok
    verbs = runner.verbs()
    assert "reset-failed" in verbs, (
        f"teardown left the unit in systemd's failed list; verbs={verbs}"
    )
    assert verbs.index("daemon-reload") < verbs.index("reset-failed"), (
        "reset-failed must run AFTER daemon-reload — before it, systemd still "
        "has the old unit loaded and re-derives the failed state"
    )


def test_systemd_remove_succeeds_even_if_reset_failed_errors(tmp_path):
    """reset-failed is bookkeeping, not the operation — older systemd errors
    on a unit that was never failed, and that must not turn a teardown that
    removed the files into a reported failure."""
    from platform_profile import LINUX, set_profile

    class _FailingResetRunner(_Runner):
        def __call__(self, argv):
            if "reset-failed" in argv:
                self.calls.append([str(a) for a in argv])
                return 1, "", "Unit not loaded."
            return super().__call__(argv)

    runner = _FailingResetRunner()
    sched = SystemdScheduler(unit_dir=tmp_path, use_sudo=False, runner=runner)
    set_profile(LINUX)
    set_scheduler(sched)
    try:
        unit = tmp_path / "ai.openclaw.evolve.apply.team_bot_a.service"
        unit.write_text("[Unit]\n")
        ok, msg = sched.remove("ai.openclaw.evolve.apply.team_bot_a")
    finally:
        set_profile(None)

    assert ok, f"a failing reset-failed broke an otherwise-successful removal: {msg}"
    assert not unit.exists()

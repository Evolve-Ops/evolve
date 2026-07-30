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
                      "restart", "disable", "daemon-reload"):
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

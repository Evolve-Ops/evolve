"""tests/test_install_opik_companion_bootstrap.py — V1.5-1 fix-up Concern #3.

Regression: install_opik_companion must set result.success=False (via
result.error()) when launchctl bootstrap returns a non-zero exit code.

Before the fix, it called result.log() for the failure branch, so
result.success stayed True and the CLI printed "Opik companion installed"
even though the service was not running.

Hostile input: launchctl bootstrap returncode=1.

Implementation note: we load evolve_admin.deploy explicitly from the
worktree path before running each test and wire fakes directly into that
module's globals. This avoids pytest's editable-install sys.modules
caching issue where install_opik_companion's __globals__ dict (from the
worktree's deploy.py) can be a different object from what `import
evolve_admin.deploy` returns in the test environment, causing
monkeypatch / patch.object calls to silently fail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent


def _load_worktree_deploy():
    """Load evolve_admin.deploy from the worktree via sys.modules manipulation.

    Clears any cached evolve_admin.* modules and forces a fresh load from
    the worktree's path, ensuring that install_opik_companion.__globals__
    is the same dict we patch.
    """
    # Remove any cached evolve_admin modules.
    to_delete = [
        k for k in sys.modules
        if k == "evolve_admin" or k.startswith("evolve_admin.")
    ]
    for k in to_delete:
        del sys.modules[k]

    # Insert the worktree admin dir at the front of sys.path so the
    # plain `import evolve_admin` loads from there.
    _admin_str = str(_ADMIN_DIR)
    if _admin_str not in sys.path:
        sys.path.insert(0, _admin_str)
    else:
        sys.path.remove(_admin_str)
        sys.path.insert(0, _admin_str)

    import evolve_admin.deploy as m  # noqa: PLC0415
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _install_args(tmp_path: Path):
    """Create a minimal fake Opik repo structure in tmp_path."""
    fake_opik_dir = tmp_path / "opik"
    fake_opik_dir.mkdir()
    (fake_opik_dir / ".git").mkdir()
    (fake_opik_dir / "deployment" / "docker-compose").mkdir(parents=True)
    launchd_dir = tmp_path / "LaunchDaemons"
    launchd_dir.mkdir()
    return fake_opik_dir, launchd_dir


# ─────────────────────────────────────────────────────────────────────────────
# Core regression test
# ─────────────────────────────────────────────────────────────────────────────


def test_bootstrap_failure_sets_result_error(tmp_path):
    """Hostile input: launchctl bootstrap returns returncode=1.

    Expected:
      - result.success is False
      - result.errors contains a message referencing 'bootstrap failed'
    """
    deploy = _load_worktree_deploy()
    fake_opik_dir, launchd_dir = _install_args(tmp_path)

    deploy.OPIK_INSTALL_DIR = fake_opik_dir
    deploy.LAUNCHD_DIR = launchd_dir

    def fake_subprocess_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "which" in cmd_str:
            return _make_proc(0, stdout="/usr/local/bin/docker\n")
        return _make_proc(0)

    def fake_run_sudo(cmd, result, check=True):
        # Remaining direct sudo calls (clone-path mkdir/chown) → succeed.
        return _make_proc(0)

    # Wire fakes directly into the module's globals so install_opik_companion
    # sees them through its own __globals__ lookup.
    deploy.subprocess.run = fake_subprocess_run
    deploy._run_sudo = fake_run_sudo

    # The plist install rides the Scheduler seam (4.3C S2): inject a runner
    # where only bootstrap fails (e.g., SIP restriction). Import the runtime
    # module AFTER _load_worktree_deploy so we inject into the same fresh
    # module object deploy.get_scheduler reads from.
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    def runner(argv):
        if "bootstrap" in argv:
            return (1, "", "Bootstrap failed: SIP restriction")
        return (0, "", "")

    set_scheduler(LaunchdScheduler(runner=runner, plist_dir=launchd_dir))
    try:
        result = deploy.install_opik_companion(evolve_dir=tmp_path)
    finally:
        set_scheduler(None)

    # Core assertion: bootstrap failure must surface as result.success=False.
    assert result.success is False, (
        f"Expected result.success=False on bootstrap failure, got True. "
        f"Steps: {result.steps!r}  Errors: {result.errors!r}"
    )

    # Error message must reference the failure so the operator knows what to do.
    assert any("bootstrap failed" in e for e in result.errors), (
        f"Expected 'bootstrap failed' in errors, got: {result.errors!r}"
    )


def test_bootstrap_success_sets_result_log_not_error(tmp_path):
    """Positive case: bootstrap returncode=0 → result.success stays True
    and "Installed launchd" is logged rather than an error.
    """
    deploy = _load_worktree_deploy()
    fake_opik_dir, launchd_dir = _install_args(tmp_path)

    deploy.OPIK_INSTALL_DIR = fake_opik_dir
    deploy.LAUNCHD_DIR = launchd_dir

    def fake_subprocess_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "which" in cmd_str:
            return _make_proc(0, stdout="/usr/local/bin/docker\n")
        return _make_proc(0)

    def fake_run_sudo(cmd, result, check=True):
        # All sudo calls succeed.
        return _make_proc(0)

    deploy.subprocess.run = fake_subprocess_run
    deploy._run_sudo = fake_run_sudo

    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    set_scheduler(LaunchdScheduler(runner=lambda argv: (0, "", ""), plist_dir=launchd_dir))
    try:
        result = deploy.install_opik_companion(evolve_dir=tmp_path)
    finally:
        set_scheduler(None)

    assert result.success is True, (
        f"Expected success on bootstrap returncode=0. "
        f"Errors: {result.errors!r}"
    )
    assert result.errors == [], f"Unexpected errors: {result.errors!r}"
    assert any("Installed launchd" in s for s in result.steps), (
        f"Expected 'Installed launchd' in steps, got: {result.steps!r}"
    )

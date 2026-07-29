"""Deploy/installer canonical paths are platform-keyed, not hardcoded.

Linux deploy-port W7 (docs/design-linux-port-2026-06-10.md §6): a live
install on a real Ubuntu VPS failed because deploy.py hardcoded the macOS
``/Users/Shared/evolve-venv`` / ``-plugin`` paths and installer.py hardcoded
the macOS ``wheel`` group (``wheel`` is NOT gid 0 on Linux). These tests pin
that the deploy layer now resolves both through ``platform_profile`` so the
values track the running platform — and that the macOS values are
byte-identical to the pre-W7 literals (the hard macOS-byte-identity invariant).
"""

from __future__ import annotations

import os
from pathlib import Path


def test_deploy_constants_resolve_from_profile_on_this_platform():
    # deploy.py resolves its venv/plugin singletons from get_profile() at
    # import, so on whatever OS this test runs the constants must equal the
    # profile's values — never a hardcoded literal.
    from platform_profile import get_profile

    from evolve_admin import deploy

    prof = get_profile()
    assert deploy.VENV_PYTHON == prof.venv_python
    assert deploy.VENV_EVOLVE_ADMIN == prof.venv_evolve_admin
    assert deploy.PLUGIN_INSTALL_DIR == Path(prof.plugin_install_dir)


def test_deploy_constants_macos_byte_identical():
    # The macOS-byte-identity invariant: on a macOS host the constants must
    # equal the exact pre-W7 literals daemons/sudoers grants depend on.
    import sys

    if not sys.platform.startswith("darwin"):
        import pytest

        pytest.skip("macOS byte-identity pin only meaningful on darwin")
    from evolve_admin import deploy

    assert deploy.VENV_PYTHON == "/Users/Shared/evolve-venv/bin/python3"
    assert deploy.VENV_EVOLVE_ADMIN == "/Users/Shared/evolve-venv/bin/evolve-admin"
    assert str(deploy.PLUGIN_INSTALL_DIR) == "/Users/Shared/evolve-plugin"


def test_deploy_constants_track_linux_profile_in_fresh_process():
    # A real Linux pod resolves the /var/lib siblings at import (the fix for
    # the real-VPS "No such file or directory: /Users/Shared/evolve-venv/
    # bin/python3"). Prove it HERMETICALLY in a subprocess that pins LINUX
    # before importing deploy — reloading the shared in-process deploy module
    # would pollute its constants for the ~30 other test files that import it.
    import subprocess
    import sys
    import textwrap

    # Point the child at THIS worktree's packages (mirrors the conftest
    # prebind), so we test the worktree's deploy.py, not the editable install's.
    repo_root = Path(__file__).resolve().parents[3]
    pythonpath = os.pathsep.join(
        [
            str(repo_root / "packages" / "analyzer"),
            str(repo_root / "packages" / "admin"),
        ]
    )
    code = textwrap.dedent(
        """
        import platform_profile as pp
        pp.set_profile(pp.LINUX)
        from evolve_admin import deploy
        assert deploy.VENV_PYTHON == "/var/lib/evolve-venv/bin/python3", deploy.VENV_PYTHON
        assert deploy.VENV_EVOLVE_ADMIN == "/var/lib/evolve-venv/bin/evolve-admin", deploy.VENV_EVOLVE_ADMIN
        assert str(deploy.PLUGIN_INSTALL_DIR) == "/var/lib/evolve-plugin", deploy.PLUGIN_INSTALL_DIR
        print("LINUX-OK")
        """
    )
    env = {**os.environ, "PYTHONPATH": pythonpath}
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0 and "LINUX-OK" in r.stdout, (
        f"rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    )


def test_installer_shared_dir_owner_uses_admin_group(monkeypatch, tmp_path):
    # installer.setup_shared chowns the shared dir to <user>:<admin_group>;
    # the group must be platform-keyed (wheel on macOS, root on Linux) so a
    # fresh Ubuntu install doesn't `chown root:wheel` against a nonexistent
    # gid-0 `wheel`. We capture the chown argv instead of touching the host.
    import platform_profile as pp

    from evolve_admin import installer

    calls: list[list[str]] = []

    def _fake_run(argv, *args, **kwargs):
        calls.append(list(argv))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    # No evolve user → owner is root:<admin_group>.
    class _NoEvolve:
        def user_exists(self, user: str) -> bool:
            return False

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        "evolve_admin.runtime.isolation.get_isolation", lambda: _NoEvolve()
    )

    for profile, expected_group in ((pp.MACOS, "wheel"), (pp.LINUX, "root")):
        calls.clear()
        pp.set_profile(profile)
        try:
            installer.setup_shared(tmp_path / "shared")
        finally:
            # Restore MACOS (not None) per the conftest pin convention.
            pp.set_profile(pp.MACOS)
        chowns = [c for c in calls if "chown" in c[1]]
        assert chowns, f"no chown issued for {profile.name}"
        # every recursive owner-set chown uses root:<platform admin group>
        owner_args = [c for c in chowns if "-R" in c]
        assert owner_args, f"no recursive chown for {profile.name}"
        assert any(f"root:{expected_group}" in c for c in owner_args), (
            f"{profile.name}: expected root:{expected_group} in {owner_args}"
        )

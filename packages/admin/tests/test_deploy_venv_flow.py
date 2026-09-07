"""The deploy FLOW builds + uses the canonical venv on Linux (W7, PR 2).

Linux deploy-port W7: a live VPS install failed because the deploy flow
ASSUMED the canonical venv pre-existed (true on macOS, where bootstrap builds
it) and the orchestration hardcoded macOS binary/program paths. These tests
pin that `installer.ensure_evolve_venv()` builds the venv at the platform's
canonical path with the analyzer-first / compat-editable contract, and that
the deploy-flow command/program paths are platform-keyed (byte-identical on
macOS — the hard invariant).
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import platform_profile as pp


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_find_uv_resolution_order(monkeypatch, tmp_path):
    # _find_uv must survive sudo's reset PATH: PATH hit first, then the UV env
    # var, then well-known install dirs; None when uv is genuinely absent.
    from evolve_admin import installer

    # 1) on PATH — returned verbatim, env/dirs not consulted.
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/local/bin/uv")
    monkeypatch.setenv("UV", "/never/used/uv")
    assert installer._find_uv() == "/usr/local/bin/uv"

    # 2) not on PATH → the UV env var (set by `uv run`), if it points at an
    #    executable file.
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    env_uv = tmp_path / "env-uv"
    env_uv.write_text("")
    env_uv.chmod(0o755)  # must be executable (X_OK), not just present
    monkeypatch.setenv("UV", str(env_uv))
    assert installer._find_uv() == str(env_uv)

    # 3) nothing on PATH / env / any candidate dir → None (caller falls back).
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(installer.os.path, "expanduser", lambda _p: str(tmp_path / "nohome"))
    monkeypatch.setattr(installer.Path, "is_file", lambda _self: False)
    assert installer._find_uv() is None


def test_ensure_evolve_venv_is_noop_when_venv_python_exists(monkeypatch, tmp_path):
    # macOS / already-set-up pods: the venv python exists → no build, no
    # subprocess calls (the byte-identical macOS path: deploy assumes it).
    from evolve_admin import installer

    fake_py = tmp_path / "venv" / "bin" / "python3"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("")
    prof = dataclasses.replace(
        pp.MACOS, venv_dir=str(tmp_path / "venv"), venv_python=str(fake_py)
    )

    called: list = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *a, **k: (called.append(a), _ok())[1],
    )
    pp.set_profile(prof)
    try:
        installer.ensure_evolve_venv()
    finally:
        pp.set_profile(pp.MACOS)
    assert called == [], (
        "ensure_evolve_venv must not touch the system when the venv exists"
    )


def test_ensure_evolve_venv_builds_via_uv_analyzer_first_compat_editable(
    monkeypatch, tmp_path
):
    # Fresh Linux pod with uv present (the default): venv python absent → build
    # it at the profile's venv_dir via `uv venv --seed` (ensurepip-free; stock
    # Ubuntu omits python3-venv — W7c), pinned to the running interpreter, then
    # `pip install -e` analyzer THEN admin, both compat-editable (the
    # interpreter contract; analyzer first because admin depends on it and it
    # isn't on PyPI). --seed populated the venv's own pip, used for the installs.
    from evolve_admin import installer

    venv_dir = tmp_path / "evolve-venv"  # absent → triggers the build
    prof = dataclasses.replace(
        pp.LINUX,
        venv_dir=str(venv_dir),
        venv_python=str(venv_dir / "bin" / "python3"),
    )

    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return _ok()

    # Pin the uv resolver and the base-python picker so the create path is
    # deterministic regardless of THIS machine's uv/interpreter layout.
    monkeypatch.setattr(installer, "_find_uv", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(installer, "_pick_venv_base_python", lambda: sys.executable)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    pp.set_profile(prof)
    try:
        installer.ensure_evolve_venv()
    finally:
        pp.set_profile(pp.MACOS)

    assert len(calls) == 3, f"expected venv-create + 2 pip installs, got {calls}"
    # 1) venv create via uv, seeded with pip, pinned to the running interpreter,
    #    --clear for idempotent retry, at the canonical venv_dir
    assert calls[0] == [
        "/usr/local/bin/uv", "venv", "--clear", "--seed",
        "--python", sys.executable, str(venv_dir),
    ]
    # 2) analyzer FIRST, compat-editable, through the venv's seeded pip
    pip = str(venv_dir / "bin" / "pip")
    assert calls[1][0] == pip
    assert "editable_mode=compat" in calls[1]
    assert calls[1][-1].endswith("packages/analyzer")
    # 3) admin SECOND, compat-editable
    assert calls[2][0] == pip
    assert "editable_mode=compat" in calls[2]
    assert calls[2][-1].endswith("packages/admin")


def test_ensure_evolve_venv_falls_back_to_stdlib_venv_when_uv_absent(
    monkeypatch, tmp_path
):
    # A host with python3-venv but no uv: _find_uv → None → fall back to the
    # stdlib `python -m venv` builder (so neither prerequisite alone blocks).
    from evolve_admin import installer

    venv_dir = tmp_path / "evolve-venv"
    prof = dataclasses.replace(
        pp.LINUX,
        venv_dir=str(venv_dir),
        venv_python=str(venv_dir / "bin" / "python3"),
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_find_uv", lambda: None)
    monkeypatch.setattr(installer, "_pick_venv_base_python", lambda: sys.executable)
    monkeypatch.setattr(
        installer.subprocess, "run",
        lambda argv, *a, **k: (calls.append(list(argv)), _ok())[1],
    )
    pp.set_profile(prof)
    try:
        installer.ensure_evolve_venv()
    finally:
        pp.set_profile(pp.MACOS)

    assert calls[0] == [sys.executable, "-m", "venv", str(venv_dir)]
    # the install loop is identical either way
    assert calls[1][0] == str(venv_dir / "bin" / "pip")


def test_ensure_evolve_venv_raises_on_create_failure(monkeypatch, tmp_path):
    from evolve_admin import installer

    venv_dir = tmp_path / "evolve-venv"
    prof = dataclasses.replace(
        pp.LINUX, venv_dir=str(venv_dir), venv_python=str(venv_dir / "bin" / "python3")
    )

    def boom(argv, *a, **k):
        return subprocess.CompletedProcess(argv, 1, "", "ensurepip is not available")

    # Force the stdlib path so the failure surfaces the same RuntimeError
    # regardless of whether this machine has uv.
    monkeypatch.setattr(installer, "_find_uv", lambda: None)
    monkeypatch.setattr(installer, "_pick_venv_base_python", lambda: sys.executable)
    monkeypatch.setattr(installer.subprocess, "run", boom)
    pp.set_profile(prof)
    try:
        import pytest

        with pytest.raises(RuntimeError, match="venv create failed"):
            installer.ensure_evolve_venv()
    finally:
        pp.set_profile(pp.MACOS)


def test_world_exec_ok_rejects_0700_ancestor(tmp_path):
    # The 203/EXEC family: an interpreter under a 0700 directory (uv's managed
    # CPython in /root/.local/share/uv after a `sudo uv sync` bootstrap) is not
    # exec-able by service accounts, however permissive the binary itself is.
    from evolve_admin import installer

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    py = private / "python3"
    py.write_text("")
    py.chmod(0o755)
    assert installer._world_exec_ok(py) is False

    # A world-executable chain passes (a real system binary — pytest tmp dirs
    # are themselves 0700, so a positive fixture can't live under tmp_path).
    assert installer._world_exec_ok("/bin/ls") is True


def test_pick_venv_base_python_falls_back_to_system_python(monkeypatch, tmp_path):
    # sys.executable resolves under a 0700 dir → the picker must skip it and
    # return the first world-executable system candidate instead of building
    # a venv every daemon 203/EXECs on.
    from evolve_admin import installer

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    hidden_py = private / "python3"
    hidden_py.write_text("")
    hidden_py.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(hidden_py))
    # /bin/ls stands in for a world-executable system python.
    monkeypatch.setattr(installer, "_SYSTEM_PYTHON_CANDIDATES", ("/bin/ls",))
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    assert installer._pick_venv_base_python() == "/bin/ls"


def test_pick_venv_base_python_raises_when_nothing_execable(monkeypatch, tmp_path):
    # No candidate any service account can exec → fail the build loudly with
    # the 203/EXEC diagnosis, never ship a venv whose daemons can't start.
    import pytest

    from evolve_admin import installer

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    hidden_py = private / "python3"
    hidden_py.write_text("")
    hidden_py.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(hidden_py))
    monkeypatch.setattr(installer, "_SYSTEM_PYTHON_CANDIDATES", ())
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="203/EXEC"):
        installer._pick_venv_base_python()


def test_ensure_evolve_venv_builds_against_picked_interpreter(monkeypatch, tmp_path):
    # End-to-end through ensure_evolve_venv: when the picker rejects the
    # running interpreter, the create command must carry the fallback — for
    # BOTH the uv and the stdlib-venv builders.
    from evolve_admin import installer

    venv_dir = tmp_path / "evolve-venv"
    prof = dataclasses.replace(
        pp.LINUX,
        venv_dir=str(venv_dir),
        venv_python=str(venv_dir / "bin" / "python3"),
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_pick_venv_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(installer, "_find_uv", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(
        installer.subprocess, "run",
        lambda argv, *a, **k: (calls.append(list(argv)), _ok())[1],
    )
    pp.set_profile(prof)
    try:
        installer.ensure_evolve_venv()
        assert calls[0] == [
            "/usr/local/bin/uv", "venv", "--clear", "--seed",
            "--python", "/usr/bin/python3", str(venv_dir),
        ]

        calls.clear()
        monkeypatch.setattr(installer, "_find_uv", lambda: None)
        installer.ensure_evolve_venv()
        assert calls[0] == ["/usr/bin/python3", "-m", "venv", str(venv_dir)]
    finally:
        pp.set_profile(pp.MACOS)


def test_reinstall_evolve_admin_ensures_venv_first(monkeypatch):
    # The deploy-flow funnel: reinstall_evolve_admin must ensure the venv
    # exists BEFORE probing/installing into it (else the real-VPS
    # "No such file or directory: .../python3" recurs).
    from evolve_admin import deploy, installer

    order: list[str] = []
    monkeypatch.setattr(installer, "ensure_evolve_venv", lambda: order.append("venv"))
    monkeypatch.setattr(
        deploy, "ensure_analyzer_installed", lambda: order.append("analyzer") or None
    )
    deploy.reinstall_evolve_admin()
    assert order == ["venv", "analyzer"]


def test_sudo_cmd_paths_chown_is_platform_keyed():
    from platform_profile import get_profile

    from evolve_admin import deploy

    assert deploy._SUDO_CMD_PATHS["chown"] == get_profile().chown


def test_admin_ui_plist_program_is_platform_keyed_venv_evolve_admin():
    from evolve_admin import deploy
    from evolve_admin.runtime import render_launchd_plist

    # The emitter now returns a JobSpec (W10-C seam sweep); render it to the
    # launchd plist to assert the program path the same way.
    plist = render_launchd_plist(deploy._admin_ui_jobspec("ai.evolve.evolve.admin-ui"))
    assert deploy.VENV_EVOLVE_ADMIN in plist
    if sys.platform.startswith("darwin"):
        # byte-identity: the rendered plist still carries the exact macOS path
        assert "/Users/Shared/evolve-venv/bin/evolve-admin" in plist


def test_build_plugin_compiles_via_local_npm_run_build_not_npx(monkeypatch, tmp_path):
    """Contract: the plugin TypeScript compile MUST invoke the plugin's LOCAL
    compiler via ``npm run build`` (package.json ``"build": "tsc"``), never
    ``npx --yes tsc``.

    Regression guard for the DigitalOcean pathfinder Step-14 fresh-install
    blocker (2026-06-17, Linux-port platform aspect): on Ubuntu 24.04 with
    npm 11.x, ``npx --yes tsc`` forces install-from-registry semantics, so npx
    ignores ``packages/plugin/node_modules/.bin/tsc`` and instead fetches the
    unrelated DEPRECATED standalone registry package literally named ``tsc``
    (tsc@2.0.4) — which only prints "This is not the tsc command you are looking
    for" and exits non-zero, wedging the deploy. ``npm run build`` prepends
    node_modules/.bin to PATH and runs the real compiler; byte-identical on
    macOS (same ``tsc``). This test pins the argv so the macOS-ism can't return.
    """
    from evolve_admin import deploy

    src = tmp_path / "plugin"
    (src / "node_modules").mkdir(parents=True)  # present → npm install skipped
    install = tmp_path / "evolve-plugin"
    monkeypatch.setattr(deploy, "PLUGIN_SRC_DIR", src)
    monkeypatch.setattr(deploy, "PLUGIN_INSTALL_DIR", install)

    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    deploy.build_plugin()

    # The compile step is exactly `env PATH=... npm run build` — the local tsc.
    build = [c for c in calls if c[-3:] == ["npm", "run", "build"]]
    assert len(build) == 1, f"expected exactly one `npm run build`, got: {calls}"
    assert build[0][0] == "env" and build[0][1].startswith("PATH="), build[0]

    # The bug it replaces must not reappear in ANY invocation: no `npx`, and
    # nothing that pairs `--yes` with `tsc` (the install-from-registry trap).
    for c in calls:
        assert "npx" not in c, f"forbidden npx invocation reintroduced: {c}"
        assert not ("--yes" in c and "tsc" in c), f"forbidden npx --yes tsc form: {c}"

"""Pod health flags a deploy-venv interpreter service accounts cannot exec.

The 203/EXEC family (live Ubuntu 24.04 install, 2026-08): the Linux runbook's
bootstrap ``sudo uv sync`` made uv download its managed CPython into
``/root/.local/share/uv`` (0700), the wizard built ``/var/lib/evolve-venv``
against it, and every timer unit died at exec with ``status=203/EXEC`` while
the wizard reported success. ``installer._pick_venv_base_python`` now prevents
building such a venv; this check catches a pod that already has one (or a
Homebrew Cellar tree left 750 by an upgrade — the macOS twin).
"""

from __future__ import annotations

import subprocess

from evolve_admin import health


def _ok(*_a, **_k) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="1.0", stderr="")


def _run_check(monkeypatch, tmp_path, venv_py):
    monkeypatch.setattr(health, "VENV_PYTHON", str(venv_py))
    monkeypatch.setattr(health.subprocess, "run", _ok)
    report = health.HealthReport()
    health._check_runtime_environment(report, tmp_path)
    return {c.name: c for c in report.checks}


def test_flags_interpreter_under_0700_dir(monkeypatch, tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    real_py = private / "python3.10"
    real_py.write_text("")
    real_py.chmod(0o755)

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python3"
    venv_py.symlink_to(real_py)

    checks = _run_check(monkeypatch, tmp_path, venv_py)
    c = checks["venv:python:exec"]
    assert c.status == health.FAIL
    assert "203/EXEC" in c.detail
    assert c.fix_cmd and "editable_mode=compat" in c.fix_cmd


def test_passes_on_world_executable_chain(monkeypatch, tmp_path):
    # /bin/ls stands in for a world-executable interpreter chain — pytest tmp
    # dirs are themselves 0700, so a positive fixture can't live under tmp_path.
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python3"
    venv_py.symlink_to("/bin/ls")

    checks = _run_check(monkeypatch, tmp_path, venv_py)
    assert checks["venv:python:exec"].status == health.PASS

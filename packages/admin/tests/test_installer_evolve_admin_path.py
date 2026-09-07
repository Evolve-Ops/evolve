"""evolve-admin PATH resolution + /usr/local/bin shim (FIND-A).

A ``uv sync`` durable pod builds its venv at ``<deploy_checkout>/.venv``, which
is NOT on sudo's ``secure_path`` — so a bare ``sudo evolve-admin …`` dies
"command not found" (live ``evolve-vsp-pod``). Two robustness layers, both here:

  * ``resolve_evolve_admin_bin`` — hand sudo an ABSOLUTE path, never a bare name.
  * ``ensure_evolve_admin_on_path`` — symlink onto ``/usr/local/bin`` (which IS
    on secure_path) so operator-facing ``sudo evolve-admin`` hints also resolve.
    Linux-only; macOS (pip/brew already on PATH) must stay byte-identical.

No real symlinks land in ``/usr/local/bin`` — the link dir + resolver are
injected so the tests stay hermetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import installer  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_profile():
    yield
    set_profile(LINUX)


# ── resolve_evolve_admin_bin ─────────────────────────────────────────────────


def test_resolver_prefers_interpreter_adjacent_console_script(monkeypatch, tmp_path):
    """The console script next to the running interpreter wins — it is provably
    the venv currently executing the code."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").write_text("#!/bin/sh\n")
    ea = bindir / "evolve-admin"
    ea.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(bindir / "python3"))

    assert installer.resolve_evolve_admin_bin() == str(ea)


def test_resolver_falls_back_to_argv0(monkeypatch, tmp_path):
    """No interpreter-adjacent script → use argv[0] when it names evolve-admin."""
    # Interpreter dir has no evolve-admin beside it.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "executable", str(empty / "python3"))

    ea = tmp_path / "venv" / "bin" / "evolve-admin"
    ea.parent.mkdir(parents=True)
    ea.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(ea), "setup"])
    monkeypatch.setattr(installer.shutil, "which", lambda _n: None)

    assert installer.resolve_evolve_admin_bin() == str(ea.resolve())


def test_resolver_last_resort_is_bare_name(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "executable", str(empty / "python3"))
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(installer.shutil, "which", lambda _n: None)

    assert installer.resolve_evolve_admin_bin() == "evolve-admin"


# ── ensure_evolve_admin_on_path ──────────────────────────────────────────────


def _target(tmp_path) -> str:
    t = tmp_path / "real" / "evolve-admin"
    t.parent.mkdir(parents=True)
    t.write_text("#!/bin/sh\n")
    return str(t)


def test_symlink_created_on_linux(tmp_path):
    set_profile(LINUX)
    target = _target(tmp_path)
    link_dir = tmp_path / "usr-local-bin"
    link_dir.mkdir()

    out = installer.ensure_evolve_admin_on_path(
        link_dir=link_dir, bin_resolver=lambda: target,
    )
    link = link_dir / "evolve-admin"
    assert out == str(link)
    assert link.is_symlink()
    assert os.readlink(link) == target


def test_symlink_is_idempotent(tmp_path):
    set_profile(LINUX)
    target = _target(tmp_path)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()

    first = installer.ensure_evolve_admin_on_path(link_dir=link_dir, bin_resolver=lambda: target)
    second = installer.ensure_evolve_admin_on_path(link_dir=link_dir, bin_resolver=lambda: target)
    assert first == second
    assert os.readlink(link_dir / "evolve-admin") == target


def test_stale_symlink_is_repointed(tmp_path):
    set_profile(LINUX)
    target = _target(tmp_path)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    # Pre-existing symlink to a stale target.
    (link_dir / "evolve-admin").symlink_to(tmp_path / "old-evolve-admin")

    installer.ensure_evolve_admin_on_path(link_dir=link_dir, bin_resolver=lambda: target)
    assert os.readlink(link_dir / "evolve-admin") == target


def test_real_binary_not_clobbered(tmp_path):
    """A pip/brew-installed real (non-symlink) evolve-admin is left in place."""
    set_profile(LINUX)
    target = _target(tmp_path)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    real = link_dir / "evolve-admin"
    real.write_text("#!/bin/sh\n# a real installed binary\n")

    installer.ensure_evolve_admin_on_path(link_dir=link_dir, bin_resolver=lambda: target)
    assert not real.is_symlink()
    assert "real installed binary" in real.read_text()


def test_macos_is_noop(tmp_path):
    """macOS must stay byte-identical — no symlink behavior at all."""
    set_profile(MACOS)
    target = _target(tmp_path)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()

    out = installer.ensure_evolve_admin_on_path(link_dir=link_dir, bin_resolver=lambda: target)
    assert out is None
    assert not (link_dir / "evolve-admin").exists()


def test_unresolvable_binary_makes_no_link(tmp_path):
    set_profile(LINUX)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()

    out = installer.ensure_evolve_admin_on_path(
        link_dir=link_dir, bin_resolver=lambda: "evolve-admin",
    )
    assert out is None
    assert not (link_dir / "evolve-admin").exists()

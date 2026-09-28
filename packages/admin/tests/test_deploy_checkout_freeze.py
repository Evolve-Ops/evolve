"""tests/test_deploy_checkout_freeze.py — the 2026-06-23 Linux pod freeze.

The freeze: on a Linux pod the deploy checkout is a CHILD of shared_dir
(``/var/lib/evolve/repo`` under ``/var/lib/evolve``). A recursive perms pass
over shared_dir (``deploy_shared_dir``'s ``chmod -R a+rX`` and the installer's
``chmod -R 755``) therefore descended INTO the git checkout and flipped tracked
files' exec bits (100644→100755). With ``core.fileMode=true`` git reported 3096
files "modified", so ``git pull --ff-only`` refused and the fleet froze ~37
commits behind origin. On macOS the checkout is a SIBLING of shared_dir, so the
same pass never touches it.

This pins the unit-level fix:
- ``shared_dir_targets_excluding_checkout`` prunes the nested checkout (Linux)
  and is a single recursive pass over the root (macOS — byte-identical).
- ``widen_shared_dir_world_read`` keeps a real nested git checkout CLEAN while
  still widening the rest of shared_dir — with a NON-VACUOUS guard proving the
  naive blanket pass WOULD dirty it.

The system-level proof (a real deploy on Ubuntu, then ``git pull --ff-only``
fast-forwards) lives in ``tests/e2e_linux/test_ubuntu_e2e.py``.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

import platform_profile as pp  # noqa: E402
from evolve_admin import secret_config_perms as scp  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _make_nested_layout(tmp_path: Path) -> tuple[Path, Path, pp.PlatformProfile]:
    """A {shared}/{repo, secrets, metrics, network.json} tree with a real git
    checkout at the (nested, Linux-shape) ``repo`` child, plus a LINUX profile
    pinned to these tmp paths so the prune logic targets ``repo``."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "secrets").mkdir()
    (shared / "metrics").mkdir()
    (shared / "metrics" / "m.json").write_text("{}")
    (shared / "network.json").write_text("{}")
    repo = shared / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.fileMode", "true")
    (repo / "a.py").write_text("x = 1\n")  # mode 644, non-executable
    (repo / "sub").mkdir()
    (repo / "sub" / "b.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    prof = dataclasses.replace(
        pp.LINUX, shared_dir_default=str(shared), deploy_checkout_default=str(repo)
    )
    return shared, repo, prof


# ── shared_dir_targets_excluding_checkout ──────────────────────────────────────


def test_targets_linux_prunes_the_nested_checkout(tmp_path: Path):
    shared, repo, prof = _make_nested_layout(tmp_path)
    got = list(scp.shared_dir_targets_excluding_checkout(shared, profile=prof))
    # The repo checkout must NOT appear at any recursion depth.
    assert (repo, True) not in got and (repo, False) not in got
    # The root is widened non-recursively; the non-checkout children recursively.
    assert (shared, False) in got
    assert (shared / "secrets", True) in got
    assert (shared / "metrics", True) in got
    assert (shared / "network.json", True) in got


def test_targets_macos_sibling_is_single_recursive_pass(tmp_path: Path):
    # macOS layout: checkout is a SIBLING of shared_dir → one (-R root), and the
    # generator must never even enumerate children (byte-identical to a blanket
    # ``chmod -R``).
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "child").mkdir()
    prof = dataclasses.replace(
        pp.MACOS, shared_dir_default=str(shared),
        deploy_checkout_default=str(tmp_path / "shared-repo"),
    )
    got = list(scp.shared_dir_targets_excluding_checkout(shared, profile=prof))
    assert got == [(shared, True)]


def test_targets_unenumerable_dir_yields_root_only_not_blanket_recurse(monkeypatch, tmp_path: Path):
    # If shared_dir can't be listed on the nested layout, the generator must NOT
    # fall back to a recursive pass over the root (that would re-enter the
    # checkout) — it yields only the root, non-recursively.
    shared, repo, prof = _make_nested_layout(tmp_path)

    def _boom(self):
        raise PermissionError("cannot list")

    monkeypatch.setattr(Path, "iterdir", _boom)
    got = list(scp.shared_dir_targets_excluding_checkout(shared, profile=prof))
    assert got == [(shared, False)], (
        "on an unenumerable nested shared_dir the generator must yield ONLY the "
        "root non-recursively — never a recursive pass that re-enters the checkout"
    )


# ── widen_shared_dir_world_read (real git + real chmod) ─────────────────────────


def test_widen_keeps_nested_checkout_clean_and_widens_the_rest(tmp_path: Path):
    shared, repo, prof = _make_nested_layout(tmp_path)
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # NON-VACUOUS guard: the naive blanket pass (what the installer ran) DOES
    # dirty the nested checkout under fileMode=true — so this test can fail if
    # the fix regresses.
    subprocess.run(["chmod", "-R", "755", str(shared)], capture_output=True)
    naive = _git(repo, "status", "--porcelain").stdout
    assert naive.strip(), (
        "precondition: a blanket `chmod -R 755 {shared}` must dirty the nested "
        "checkout (the freeze) — if it doesn't, this test no longer guards anything"
    )
    # Recover to a pristine 644 checkout.
    _git(repo, "checkout", "--", ".")
    for f in repo.rglob("*.py"):
        f.chmod(0o644)
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # THE FIX: the prune-aware widen must leave the checkout untouched.
    scp.widen_shared_dir_world_read(shared, chmod="/bin/chmod", profile=prof)
    assert _git(repo, "status", "--porcelain").stdout.strip() == "", (
        "widen_shared_dir_world_read descended into the nested deploy checkout "
        "and dirtied it — the 2026-06-23 freeze would recur"
    )
    # ...and a subsequent ff-only-style state check: HEAD is unmoved & clean, so
    # a real `git pull --ff-only` could fast-forward.
    assert _git(repo, "status", "--porcelain").stdout == ""

    # The rest of shared_dir WAS widened (the feature still works).
    mode = (shared / "metrics" / "m.json").stat().st_mode & 0o044
    assert mode == 0o044, "non-checkout child should be world+group readable after widen"


def test_widen_macos_runs_exactly_one_blanket_chmod(monkeypatch, tmp_path: Path):
    # On the macOS sibling layout the widen must be exactly one
    # ``chmod -R a+rX {shared_dir}`` — byte-identical to the pre-fix behavior.
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "child").mkdir()
    prof = dataclasses.replace(
        pp.MACOS, shared_dir_default=str(shared),
        deploy_checkout_default=str(tmp_path / "shared-repo"),
    )
    calls: list[list[str]] = []

    def _fake_run(argv, *a, **k):
        calls.append(argv)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(scp.subprocess, "run", _fake_run)
    scp.widen_shared_dir_world_read(shared, chmod="/bin/chmod", profile=prof)
    assert calls == [["/bin/chmod", "-R", "a+rX", str(shared)]]


def test_widen_falls_back_to_sudo_only_on_spawn_failure(monkeypatch, tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    prof = dataclasses.replace(
        pp.MACOS, shared_dir_default=str(shared),
        deploy_checkout_default=str(tmp_path / "shared-repo"),
    )

    def _boom(argv, *a, **k):
        raise OSError("cannot spawn chmod")

    fallback_calls: list[list[str]] = []
    monkeypatch.setattr(scp.subprocess, "run", _boom)
    scp.widen_shared_dir_world_read(
        shared, chmod="/bin/chmod", sudo_fallback=fallback_calls.append, profile=prof
    )
    assert fallback_calls == [["chmod", "-R", "a+rX", str(shared)]]

"""Guard tests for tools/scan_scope.py — the shared repo-wide scan scope.

Why this file exists
--------------------
Repo-wide guards enumerate with ``git ls-files``, which lists files tracked
at HEAD. A file that has not been ``git add``ed yet is invisible to them, so
``tools/preflight`` could report a gate PASS on a diff CI would reject. That
is not hypothetical: PR #4281 pushed a green preflight and took three CI jobs
red, because a brand-new test file sat untracked carrying reserved bot names.

These tests pin the two halves of the fix so it cannot silently unwind:

  1. the SCOPE — ``scan_files`` widens to untracked-and-not-ignored files when
     asked, and never to ignored ones;
  2. the WIRING — preflight asks for the widening, and each repo-wide guard
     resolves its file list through this one helper rather than re-inlining a
     raw ``git ls-files``;
  3. the REGISTRY — every scanner in the gate surface carries an explicit
     scope decision, so a NEW gate cannot reopen the blind spot unnoticed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _REPO_ROOT / "tools"


def _load(name: str, path: Path):
    """Load a module by path. SourceFileLoader rather than
    ``spec_from_file_location`` because the tools/ gates are extensionless
    scripts, which the extension-based finder refuses."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


scan_scope = _load("scan_scope_under_test", _TOOLS / "scan_scope.py")


# ---------------------------------------------------------------------------
# 1. Scope
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one tracked, one untracked and one ignored
    file. Real git, because git's own ignore semantics are the thing under
    test — a stubbed lister would pass while the real scope was wrong."""
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    (tmp_path / ".gitignore").write_text("ignored.txt\nscratch/\n")
    (tmp_path / "tracked.txt").write_text("tracked\n")
    git("add", ".gitignore", "tracked.txt")
    git("commit", "-q", "-m", "init")
    # Created after the commit — this is the blind-spot case.
    (tmp_path / "brand_new.txt").write_text("new\n")
    (tmp_path / "ignored.txt").write_text("ignored\n")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "notes.txt").write_text("scratch\n")
    return tmp_path


def test_default_scope_is_tracked_only(repo: Path) -> None:
    """Default == today's CI semantics. This is what keeps the fix from
    changing any CI job's behaviour as a side effect."""
    assert scan_scope.scan_files(repo, include_untracked=False) == [
        ".gitignore",
        "tracked.txt",
    ]


def test_widened_scope_includes_untracked_but_not_ignored(repo: Path) -> None:
    """The whole point: a file created but not yet added IS scanned, while a
    gitignored scratch file still costs nothing."""
    scanned = scan_scope.scan_files(repo, include_untracked=True)
    assert "brand_new.txt" in scanned, (
        "untracked-but-not-ignored file missing from the widened scope — the "
        "PR #4281 blind spot has reopened"
    )
    assert "ignored.txt" not in scanned
    assert "scratch/notes.txt" not in scanned


def test_env_var_drives_the_default(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``include_untracked=None`` defers to the environment — the seam
    preflight uses to widen every gate at once."""
    monkeypatch.delenv(scan_scope.ENV_VAR, raising=False)
    assert "brand_new.txt" not in scan_scope.scan_files(repo)

    monkeypatch.setenv(scan_scope.ENV_VAR, "1")
    assert scan_scope.untracked_scanning_enabled()
    assert "brand_new.txt" in scan_scope.scan_files(repo)


def test_explicit_argument_beats_the_environment(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that must stay tracked-only (the publisher, the predicate drift
    check) can pin its scope and not be widened by an operator's env."""
    monkeypatch.setenv(scan_scope.ENV_VAR, "1")
    assert "brand_new.txt" not in scan_scope.scan_files(repo, include_untracked=False)


def test_pathspec_is_honoured_in_both_halves(repo: Path) -> None:
    """The seam gate scans a pathspec; the widening must apply to it too, or
    it silently keeps the tracked-only scope."""
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new_module.py").write_text("x = 1\n")
    scanned = scan_scope.scan_files(repo, "pkg/**", include_untracked=True)
    assert scanned == ["pkg/new_module.py"]


def test_enable_mutates_the_mapping_it_is_given() -> None:
    """preflight hands gate subprocesses a copied env, not os.environ."""
    env: dict[str, str] = {}
    scan_scope.enable_untracked_scanning(env)
    assert scan_scope.untracked_scanning_enabled(env)


# ---------------------------------------------------------------------------
# 2. Wiring
# ---------------------------------------------------------------------------


def test_preflight_widens_every_gate() -> None:
    """The regression that started this: preflight ran the guards with the
    narrow scope, so `scrub-guard` reported PASS on an untracked violation."""
    preflight = _load("preflight_under_test", _TOOLS / "preflight")
    gate = preflight.Gate("probe", "job", "always", ["true"])
    env = preflight.gate_env({}, gate)
    assert env.get(scan_scope.ENV_VAR) == "1", (
        "tools/preflight no longer enables untracked scanning — every "
        "repo-wide guard it runs is blind to new files again"
    )


def test_gate_specific_env_does_not_clobber_the_widening() -> None:
    preflight = _load("preflight_under_test2", _TOOLS / "preflight")
    gate = preflight.Gate("probe", "job", "always", ["true"], env={"FOO": "bar"})
    env = preflight.gate_env({}, gate)
    assert env["FOO"] == "bar"
    assert env.get(scan_scope.ENV_VAR) == "1"


# Each repo-wide guard, and the attribute on it that must resolve through
# scan_scope. Keyed by the module-level scan helper so a re-inlined
# `git ls-files` is caught even if the helper keeps its name.
_WIRED_GUARDS = (
    ("test_public_launch_scrub", "_scan_rels"),
    ("test_no_personal_pii_in_source", "_scanned_files"),
    ("test_launchctl_seam_gates", "_scanned_non_test_python"),
)


@pytest.mark.parametrize("mod_name,fn_name", _WIRED_GUARDS)
def test_guard_resolves_its_file_list_through_scan_scope(mod_name: str, fn_name: str) -> None:
    """Each guard must ASK scan_scope for its scope. Stubbing the helper and
    watching the call proves the delegation, without touching this checkout."""
    mod = _load(f"{mod_name}_wiring", Path(__file__).parent / f"{mod_name}.py")
    calls: list[tuple] = []

    class _Stub:
        ENV_VAR = scan_scope.ENV_VAR

        @staticmethod
        def scan_files(root, *pathspec, **kwargs):
            calls.append((root, pathspec))
            return []

        @staticmethod
        def untracked_files(root, *pathspec, **kwargs):
            return []

        @staticmethod
        def untracked_scanning_enabled(env=None):
            return True

    original = mod._scan_scope
    mod._scan_scope = _Stub
    try:
        # The guard may reject the stub's empty file list with its own
        # scope-collapsed assertion — that is fine. What is under test is
        # whether it ASKED scan_scope for the list at all.
        try:
            getattr(mod, fn_name)()
        except AssertionError:
            pass
    finally:
        mod._scan_scope = original

    assert calls, (
        f"{mod_name}.{fn_name}() did not go through scan_scope.scan_files — "
        f"it has been re-inlined onto a tracked-only `git ls-files`"
    )


# ---------------------------------------------------------------------------
# 3. Registry
# ---------------------------------------------------------------------------

# A file in the gate surface is a "repo scanner candidate" if it names a git
# ls-files argv or calls one of the scan-scope listers. Deliberately broad: a
# false positive costs one registry line with a reason, which is exactly the
# review-time decision we want a new gate's author to make.
_SCANNER_SIGNAL = re.compile(
    r'"ls-files"|\b(?:tracked_files|untracked_files|scan_files)\s*\('
)
_GATE_SURFACE = ("tools", "packages/admin/tests")


def _scanner_candidates() -> list[str]:
    found: list[str] = []
    for base in _GATE_SURFACE:
        for path in sorted((_REPO_ROOT / base).rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue  # build residue, not a scanner
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _SCANNER_SIGNAL.search(text):
                found.append(path.relative_to(_REPO_ROOT).as_posix())
    return found


def test_every_repo_scanner_declares_its_scope() -> None:
    """The tripwire: a NEW gate that enumerates the repo must say whether it
    sees untracked files, and why. This is what stops the blind spot from
    reopening somewhere other than where it was found."""
    unregistered = [
        rel for rel in _scanner_candidates()
        if rel not in scan_scope.REPO_SCANNERS
    ]
    assert not unregistered, (
        "These files enumerate the repo but are not in REPO_SCANNERS "
        "(tools/scan_scope.py):\n"
        + "\n".join(f"  - {rel}" for rel in unregistered)
        + "\n\nAdd an entry naming the scope and the reason. If it should see "
        "uncommitted files, route it through scan_scope.scan_files() and say "
        "SCAN_SCOPE; if tracked-only is deliberate, say TRACKED_ONLY and why "
        "seeing an untracked file would be WRONG — 'nobody got round to it' "
        "is how PR #4281 happened."
    )


def test_registry_has_no_stale_entries() -> None:
    """A registered file that no longer enumerates the repo should lose its
    entry, so the registry stays a true census rather than folklore."""
    candidates = set(_scanner_candidates())
    stale = [rel for rel in scan_scope.REPO_SCANNERS if rel not in candidates]
    assert not stale, (
        "REPO_SCANNERS entries no longer enumerate the repo — remove them "
        "from tools/scan_scope.py:\n" + "\n".join(f"  - {rel}" for rel in stale)
    )


def test_every_registry_entry_carries_a_reason() -> None:
    for rel, reason in scan_scope.REPO_SCANNERS.items():
        assert len(reason.split()) >= 6, f"{rel}: reason is too thin to review"
        assert any(
            tag in reason for tag in ("SCAN_SCOPE", "TRACKED_ONLY", "Not a repo scanner")
        ), f"{rel}: reason must classify the scope (SCAN_SCOPE / TRACKED_ONLY / not a scanner)"

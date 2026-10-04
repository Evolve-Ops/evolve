"""tests/test_refresh_sudoers_uses_evolve_user.py — regression for the
`sudo evolve-admin refresh-sudoers` SSH-key footgun.

The repo-pull step inside refresh-sudoers runs `git fetch` against
GitHub. refresh-sudoers is invoked as root (via outer sudo), but the
deploy SSH key lives under /Users/evolve/.ssh/ and is owned by the
evolve user. Running git as root therefore fails publickey auth:

    git fetch failed: git@github.com: Permission denied (publickey).

The fix routes the git invocations through `sudo -u evolve git ...` so
the deploy key is in scope, matching what the repo-puller daemon does
naturally. This test pins the call shape so a future refactor doesn't
silently revert.

We do not exec git here — we patch subprocess.run and structurally
assert on the captured argv.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin import cli as _cli  # noqa: E402


def _make_completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _argv_for(call: Any) -> list[str]:
    """Pull the argv list out of a captured subprocess.run mock call."""
    if call.args:
        first = call.args[0]
        if isinstance(first, list):
            return first
    argv = call.kwargs.get("args")
    if isinstance(argv, list):
        return argv
    return []


def _is_git_invocation(argv: list[str]) -> bool:
    if not argv:
        return False
    return argv[0] == "git" or (
        len(argv) >= 4
        and argv[0] == "sudo"
        and argv[1] == "-u"
        and argv[3] == "git"
    )


def _is_evolve_git_invocation(argv: list[str]) -> bool:
    return (
        len(argv) >= 4
        and argv[0] == "sudo"
        and argv[1] == "-u"
        and argv[2] == "evolve"
        and argv[3] == "git"
    )


def _is_bare_git_invocation(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == "git"


def _fake_run_factory(repo_root: Path):
    """Build a subprocess.run replacement that satisfies every guard
    in _pull_repo_for_sudoers_refresh so we reach the fetch+merge calls.

    Branch returns 'main', porcelain status is empty, fetch + merge OK,
    rev-parse HEAD returns short hashes so the function returns success.
    """
    state = {"head_calls": 0}

    def _run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        # Every git invocation through _git() should now be sudo -u evolve git ...
        # We accept any well-formed call and return the right canned output
        # based on the git subcommand. The structural assertions live in
        # the test bodies; this fake just keeps the function flowing.
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return _make_completed(stdout="main\n")
        if "status" in cmd and "--porcelain" in cmd:
            return _make_completed(stdout="")
        if "fetch" in cmd:
            return _make_completed()
        if "rev-parse" in cmd and "--short" in cmd:
            state["head_calls"] += 1
            sha = "abc123" if state["head_calls"] == 1 else "def456"
            return _make_completed(stdout=f"{sha}\n")
        if "merge" in cmd:
            return _make_completed(stdout="Fast-forward\n")
        # Anything else: a harmless OK so the function can keep going.
        return _make_completed()

    return _run


def _resolve_repo_path() -> Path:
    """Mirror _pull_repo_for_sudoers_refresh's repo discovery so the
    test plants a .git/ marker the function will find.

    The function walks up from the evolve_admin package looking for a
    .git directory. In this worktree, that's the worktree root.
    """
    import evolve_admin as _mod
    start = Path(_mod.__file__).resolve().parent
    candidate = start
    for _ in range(8):
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    raise RuntimeError("could not find .git for test setup")


def test_pull_uses_sudo_u_evolve_for_git():
    """Every captured git invocation must be `sudo -u evolve git ...`,
    and no captured call may be bare `git ...` (which would run as the
    current user — root, under sudo)."""
    repo = _resolve_repo_path()
    fake_run = _fake_run_factory(repo)
    captured: list[Any] = []

    def _capturing(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        # Record a stand-in for the call so we can introspect argv later.
        captured.append(mock.call(cmd, *args, **kwargs))
        return fake_run(cmd, *args, **kwargs)

    with mock.patch.object(_cli.subprocess, "run", side_effect=_capturing):
        ok, msg = _cli._pull_repo_for_sudoers_refresh()

    assert ok, f"pull helper returned failure: {msg!r}"

    git_calls = [c for c in captured if _is_git_invocation(_argv_for(c))]
    assert git_calls, "expected at least one git invocation captured"

    # Every git invocation in this function must run as evolve.
    for c in git_calls:
        argv = _argv_for(c)
        assert _is_evolve_git_invocation(argv), (
            f"expected `sudo -u evolve git ...`, got argv={argv!r}"
        )
        assert not _is_bare_git_invocation(argv), (
            f"bare git invocation leaked through: argv={argv!r}"
        )

    # Spot-check: fetch and merge specifically must be routed via evolve,
    # since those are the calls that actually hit GitHub / mutate the
    # working tree.
    fetch_calls = [c for c in git_calls if "fetch" in _argv_for(c)]
    merge_calls = [c for c in git_calls if "merge" in _argv_for(c)]
    assert fetch_calls, "no git fetch captured — function flow broke"
    assert merge_calls, "no git merge captured — function flow broke"
    for c in fetch_calls + merge_calls:
        argv = _argv_for(c)
        assert argv[:4] == ["sudo", "-u", "evolve", "git"], (
            f"network/mutating git call not routed via evolve: argv={argv!r}"
        )


def test_no_bare_git_invocation_in_pull():
    """Belt-and-suspenders: assert separately that no captured subprocess
    call is bare `git ...`. If a future edit reintroduces one — e.g.
    re-adds a quick `git remote -v` debug call — this fails loudly."""
    repo = _resolve_repo_path()
    fake_run = _fake_run_factory(repo)
    captured: list[Any] = []

    def _capturing(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured.append(mock.call(cmd, *args, **kwargs))
        return fake_run(cmd, *args, **kwargs)

    with mock.patch.object(_cli.subprocess, "run", side_effect=_capturing):
        _cli._pull_repo_for_sudoers_refresh()

    bare = [c for c in captured if _is_bare_git_invocation(_argv_for(c))]
    assert not bare, f"bare git invocation(s) captured: {[_argv_for(c) for c in bare]!r}"

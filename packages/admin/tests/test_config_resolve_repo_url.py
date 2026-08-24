"""``config.resolve_repo_url`` ownership-tolerance (FIND-D).

The freeze this guards: on a correctly-cloned durable VPS (origin =
``git@github.com:owner/evolve.git``, deploy key authed), the wizard's Step-14
repo-access check fired a FALSE "no 'origin' remote" warning. Root cause —
``resolve_repo_url`` runs ``git remote get-url origin`` as ROOT (inside
``sudo evolve-admin setup``) against a checkout owned by the ``evolve`` user, so
git's CVE-2022-24765 "dubious ownership" guard made the probe exit non-zero and
return ``""``. The puller worked because it runs AS ``evolve``.

The fix mirrors ``repo_puller._git``: pass ``-c safe.directory=<repo>`` so the
read-only ``remote get-url`` succeeds regardless of who owns the checkout. The
subprocess is injected (monkeypatched stdlib ``run``) — no test shells out.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import config  # noqa: E402


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_network_repo_url_wins_without_touching_git(monkeypatch):
    """An explicit pod.repo_url short-circuits — no git probe at all."""
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("git should not be invoked when pod.repo_url is set")

    monkeypatch.setattr(subprocess, "run", boom)
    url = config.resolve_repo_url({"pod": {"repo_url": "https://github.com/acme/evolve"}})
    assert url == "https://github.com/acme/evolve"


def test_git_probe_sets_safe_directory(monkeypatch):
    """The git command MUST carry `-c safe.directory=<candidate>` so a
    root-vs-evolve-owned checkout doesn't trip the dubious-ownership guard."""
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return _Res(returncode=0, stdout="git@github.com:evolve-ops/evolve.git")

    monkeypatch.setattr(subprocess, "run", fake_run)
    url = config.resolve_repo_url({})  # no pod.repo_url → falls to git

    assert seen, "git was never invoked"
    cmd = seen[0]
    assert cmd[0] == "git"
    assert "-c" in cmd
    sd = [cmd[i + 1] for i, t in enumerate(cmd) if t == "-c"][0]
    assert sd.startswith("safe.directory="), f"no safe.directory in {cmd}"
    # safe.directory targets the same path -C queries (the single trusted repo).
    candidate = sd.split("=", 1)[1]
    assert ["-C", candidate] == cmd[cmd.index("-C"):cmd.index("-C") + 2]
    # git@ SSH form normalized to a clickable https URL, .git suffix dropped.
    assert url == "https://github.com/evolve-ops/evolve"


def test_dubious_ownership_no_longer_yields_empty(monkeypatch):
    """Regression for the live false-positive: a probe that carries
    safe.directory resolves the URL where the bare probe returned ''."""
    def fake_run(cmd, **kwargs):
        # Honour safe.directory exactly as real git would: refuse (rc=128, empty
        # stdout) ONLY when the ownership guard is NOT bypassed.
        has_safe_dir = any(
            t == "-c" and cmd[i + 1].startswith("safe.directory=")
            for i, t in enumerate(cmd[:-1])
        )
        if not has_safe_dir:
            return _Res(returncode=128, stdout="",
                        stderr="fatal: detected dubious ownership in repository")
        return _Res(returncode=0, stdout="git@github.com:owner/evolve.git")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert config.resolve_repo_url({}) == "https://github.com/owner/evolve"


def test_falls_through_to_second_candidate(monkeypatch):
    """First candidate errors → the second (package-relative) is tried."""
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        # candidate path is the -C arg
        candidate = cmd[cmd.index("-C") + 1]
        calls.append(candidate)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=3)
        return _Res(returncode=0, stdout="https://github.com/acme/evolve.git")

    monkeypatch.setattr(subprocess, "run", fake_run)
    url = config.resolve_repo_url({})
    assert len(calls) == 2
    assert url == "https://github.com/acme/evolve"


def test_returns_empty_when_no_remote_anywhere(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _Res(returncode=128, stdout="", stderr="no such remote 'origin'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert config.resolve_repo_url({}) == ""

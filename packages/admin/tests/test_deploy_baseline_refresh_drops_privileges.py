"""Regression test for the 2026-05-30 backup privilege leak.

``deploy_bot`` previously imported ``backup.commit_baseline_local`` and
called it in-process. Under ``sudo evolve-admin deploy`` that meant the
function's ``git config`` writes + ``backup_dir.mkdir`` happened as root,
leaving ``.git/config`` (mode 0600) and ``evolve-backup/`` owned by
``root:staff``. The bot's nightly backup daemon (running as the bot user)
then could not read ``.git/config`` or write into ``evolve-backup/``,
wedging backups silently. Symptom on the pod: 8/8 bots showed
BACKED UP=failed/skipped on 2026-05-30.

These tests pin two invariants on the replacement code path:

  1. ``_baseline_refresh_as_bot_user`` invokes the new CLI via
     ``sudo -H -u <bot_user>``, never in-process. (Without -u, the
     subprocess inherits root and the original bug returns.)
  2. ``_reclaim_baseline_ownership`` chowns root-owned
     ``.git/`` + ``evolve-backup/`` back to the bot user — so the
     replacement self-heals pods that already have corrupted state
     from the buggy era.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parent.parent / "evolve_admin"
if str(_DEPLOY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_DIR.parent))

from evolve_admin import deploy as _deploy  # noqa: E402


class _FakeResult:
    """Minimal stand-in for DeployResult that just captures log lines."""
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.success = True
        self.errors: list[str] = []

    def log(self, msg: str) -> None:
        self.messages.append(msg)


class _UidOverride:
    """Real ``os.stat_result`` with ``st_uid`` swapped out.

    ``os.stat_result`` attributes are read-only, so wrap rather than mutate;
    every field other than ``st_uid`` stays truthful.
    """
    def __init__(self, real: object, uid: int) -> None:
        self._real = real
        self.st_uid = uid

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def _fake_stat_uid(monkeypatch, uid_by_path: dict) -> None:
    """Fake ``st_uid`` for the given paths only; delegate for everything else.

    ``_deploy.Path`` *is* ``pathlib.Path``, so any ``setattr`` here is global.
    That matters more than it looks: ``Path.exists()`` and ``Path.is_dir()``
    route through ``self.stat(follow_symlinks=...)``, and pytest's own
    traceback formatter calls ``Path.exists()`` (``_pytest._code.code.Code.path``)
    while monkeypatch is still active. A stub that drops the ``follow_symlinks``
    keyword — or that answers for paths it was never meant to fake — turns any
    ordinary failure in these tests into a session-killing ``INTERNALERROR``
    instead of a readable report. So: pass every argument straight through
    (``*a, **k`` rather than a pinned signature, so a future interpreter can't
    reintroduce the same breakage), and only answer for the paths under test.
    """
    real_stat = Path.stat
    faked = {str(p): uid for p, uid in uid_by_path.items()}

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        uid = faked.get(str(self))
        return st if uid is None else _UidOverride(st, uid)

    monkeypatch.setattr(Path, "stat", fake_stat)


# ─────────────────────────────────────────────────────────────────────────────
# Privilege drop — the new code path must sudo to the bot user
# ─────────────────────────────────────────────────────────────────────────────


def test_baseline_refresh_invokes_sudo_with_bot_user(monkeypatch, tmp_path):
    """The replacement helper must invoke ``sudo -H -u <bot_user> python3
    backup.py --commit-baseline-local <bot_id>``. Anything missing -u
    or pointing at a different user reintroduces the bug.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_net = {"sharedDir": str(tmp_path / "shared")}
    monkeypatch.setattr(_deploy, "load_network", lambda _path: fake_net)
    monkeypatch.setattr(
        _deploy, "get_bot_workspace", lambda bid, net: str(workspace),
    )
    # Ownership-reclaim no-op for this test (separate test below covers it).
    monkeypatch.setattr(_deploy, "_reclaim_baseline_ownership", lambda *a, **kw: [])

    captured: dict = {}

    def fake_run(cmd, *_a, **_kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="[backup] baseline-refresh team_bot_a: ok\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _FakeResult()
    _deploy._baseline_refresh_as_bot_user(
        "team_bot_a", "personal_bot_user", Path("/dev/null"), result,
    )

    cmd = captured["cmd"]
    assert cmd[:4] == ["sudo", "-H", "-u", "personal_bot_user"], (
        f"expected `sudo -H -u personal_bot_user …` prefix, got {cmd[:4]}. "
        "Missing -u means the subprocess inherits root and the 2026-05-30 "
        "ownership corruption returns."
    )
    # python3 must be a full path so the bot user's PATH can't shadow it.
    assert cmd[4] == "/usr/bin/python3"
    # The script path must point at packages/analyzer/backup.py.
    assert cmd[5].endswith("/analyzer/backup.py")
    assert cmd[6:] == ["--commit-baseline-local", "team_bot_a"]
    # Successful run logged without [warn] prefix.
    assert any("baseline-refresh" in m and "[warn]" not in m for m in result.messages)


def test_baseline_refresh_warns_but_does_not_raise_on_subprocess_failure(monkeypatch, tmp_path):
    """A non-zero exit from the sudo'd backup CLI must surface as a [warn]
    log line, never raise — baseline refresh is best-effort, a failure
    here must not roll back the deploy. (Original in-process call had
    the same try/except guarantee.)
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(_deploy, "load_network", lambda _path: {"sharedDir": str(tmp_path)})
    monkeypatch.setattr(_deploy, "get_bot_workspace", lambda bid, net: str(workspace))
    monkeypatch.setattr(_deploy, "_reclaim_baseline_ownership", lambda *a, **kw: [])
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom\n",
        ),
    )

    result = _FakeResult()
    _deploy._baseline_refresh_as_bot_user(
        "team_bot_a", "personal_bot_user", Path("/dev/null"), result,
    )

    assert any("[warn]" in m and "rc=1" in m for m in result.messages)


def test_baseline_refresh_timeout_warns_no_raise(monkeypatch, tmp_path):
    """If the sudo subprocess hangs past the timeout, deploy still finishes."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(_deploy, "load_network", lambda _path: {"sharedDir": str(tmp_path)})
    monkeypatch.setattr(_deploy, "get_bot_workspace", lambda bid, net: str(workspace))
    monkeypatch.setattr(_deploy, "_reclaim_baseline_ownership", lambda *a, **kw: [])

    def hang(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["sudo"], timeout=30)

    monkeypatch.setattr(subprocess, "run", hang)

    result = _FakeResult()
    _deploy._baseline_refresh_as_bot_user(
        "team_bot_a", "personal_bot_user", Path("/dev/null"), result,
    )

    assert any("[warn]" in m and "timed out" in m for m in result.messages)


# ─────────────────────────────────────────────────────────────────────────────
# Ownership reclaim — heal pre-2026-05-30 corruption when sudo drops priv
# ─────────────────────────────────────────────────────────────────────────────


def test_reclaim_skips_when_not_root(monkeypatch, tmp_path):
    """Chown of root-owned files requires euid=0. Non-root callers must
    no-op rather than spam the deploy log with EPERM stderr (the helper
    is also reachable from `evolve-admin deploy --dry-run` style flows
    where root isn't required and shouldn't be silently faked).
    """
    monkeypatch.setattr(_deploy.os, "geteuid", lambda: 501)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "evolve-backup").mkdir()
    reclaimed = _deploy._reclaim_baseline_ownership(workspace, "personal_bot_user")
    assert reclaimed == []


def test_reclaim_chowns_root_owned_baseline_dirs(monkeypatch, tmp_path):
    """When .git/ and evolve-backup/ exist and are root-owned, the
    helper chowns them back to <bot_user>:staff so the sudo'd
    backup.py CLI can write into them on the next deploy.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "evolve-backup").mkdir()

    monkeypatch.setattr(_deploy.os, "geteuid", lambda: 0)

    # Pretend the two baseline dirs are owned by root (uid=0) so the helper
    # proceeds. Every other path keeps its real stat.
    _fake_stat_uid(
        monkeypatch,
        {workspace / ".git": 0, workspace / "evolve-backup": 0},
    )

    chown_calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        chown_calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    reclaimed = _deploy._reclaim_baseline_ownership(workspace, "personal_bot_user")

    # One chown per subdir, recursive, with personal_bot_user:staff target.
    targets = {Path(c[-1]).name for c in chown_calls}
    assert targets == {".git", "evolve-backup"}
    for cmd in chown_calls:
        assert cmd[:3] == ["/usr/sbin/chown", "-R", "personal_bot_user:staff"]
    assert len(reclaimed) == 2


def test_reclaim_skips_when_already_bot_owned(monkeypatch, tmp_path):
    """When .git/ is already owned by a non-root uid (the bot), skip the
    chown entirely. Avoids stomping on legitimate mixed ownership.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    monkeypatch.setattr(_deploy.os, "geteuid", lambda: 0)

    # 502 = any non-zero uid — pretend the bot user already owns it.
    _fake_stat_uid(monkeypatch, {workspace / ".git": 502})

    chown_calls: list = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **kw: (chown_calls.append(cmd), subprocess.CompletedProcess(args=cmd, returncode=0))[1],
    )

    reclaimed = _deploy._reclaim_baseline_ownership(workspace, "personal_bot_user")

    assert reclaimed == []
    assert chown_calls == [], "must not chown when dir is already bot-owned"

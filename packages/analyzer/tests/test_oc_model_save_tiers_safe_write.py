"""tests/test_oc_model_save_tiers_safe_write.py — evolve-tiers.json writes
go through the ownership-robust safe-write pattern, never a naive in-place
``open("w")``.

Incident (2026-06-10): clicking "Reconcile catalog for team-bot-a" on the
AI-Optimization page died with::

    Reconcile failed: reconcile write failed for team-bot-a:
    [Errno 13] Permission denied: '/Users/team_bot_a/.openclaw/evolve-tiers.json'

Root cause: ``oc_model._save_tiers_file`` truncated the live file in place
via ``path.open("w")``, which needs write permission on the *file itself*.
When the live evolve-tiers.json was owned by a different uid than the
running process (root-owned after a migration; or bot-owned with the admin
server running as the evolve user), the write hit EACCES.

Locked here:
  1. Happy path: ``os.replace`` (atomic temp-in-dir + rename) is used, so
     the write needs only *directory* write permission — ownership-robust.
  2. Fallback path: on ``PermissionError`` from the atomic write, the
     helper stages in ``/tmp`` and shells out to ``sudo /bin/cp`` — the
     CLAUDE.md sanctioned path for the evolve user to land a bot-owned
     file. It NEVER does ``sudo -u <bot>`` (no such grant exists).
  3. The naive ``open(path, "w")`` truncate-in-place is gone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_save_tiers_writes_atomically(fake_home):
    """Happy path lands the JSON content; no leftover .tmp file."""
    oc_model._save_tiers_file("team_bot_a", {"cascade": {"enabled": True}})
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    assert json.loads(tiers_path.read_text()) == {"cascade": {"enabled": True}}
    # The atomic temp-in-dir file must have been renamed away, not left behind.
    leftovers = list((fake_home / ".openclaw").glob("*.tmp"))
    assert leftovers == [], f"stray temp files: {leftovers}"


def test_save_tiers_uses_atomic_replace_not_inplace_truncate(fake_home, monkeypatch):
    """The writer must use ``os.replace`` (rename), never ``Path.open('w')``
    truncate-in-place on the live file — that is the EACCES bug.

    We assert by tripping a sentinel: if anything calls ``open`` in write
    mode on the *live* path, fail. The atomic helper only opens the temp.
    """
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    # Seed an existing file so an in-place truncate WOULD be attempted by the
    # naive implementation.
    tiers_path.write_text(json.dumps({"old": True}))

    real_open = Path.open

    def guard_open(self, mode="r", *a, **k):
        if "w" in mode and self == tiers_path:
            raise AssertionError(
                "in-place truncate of live evolve-tiers.json — must use "
                "os.replace on a temp file instead"
            )
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", guard_open)
    oc_model._save_tiers_file("team_bot_a", {"new": True})
    assert json.loads(tiers_path.read_text()) == {"new": True}


def test_save_tiers_falls_back_to_sudo_cp_on_permission_error(fake_home, monkeypatch):
    """When the atomic write raises PermissionError (evolve user can't write
    the bot's home), fall back to /tmp staging + ``sudo /bin/cp``. Assert the
    exact command shape and that we never invoke ``sudo -u <bot>``."""
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"

    # Force the in-dir atomic path to fail with PermissionError so the
    # fallback engages.
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)

    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        # Emulate sudo /bin/cp by actually copying so the assertion below
        # can verify the content landed.
        src, dst = cmd[2], cmd[3]
        Path(dst).write_text(Path(src).read_text())

        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)

    oc_model._save_tiers_file("team_bot_a", {"viafallback": True})

    cmd = captured["cmd"]
    assert cmd[:2] == ["sudo", "/bin/cp"], cmd
    assert cmd[2].startswith("/tmp/evolve-tiers-"), cmd
    assert cmd[3] == str(tiers_path), cmd
    # Must NOT be a `sudo -u <bot>` invocation — evolve has no such grant.
    assert "-u" not in cmd, cmd
    assert json.loads(tiers_path.read_text()) == {"viafallback": True}


def test_save_tiers_raises_when_sudo_cp_fails(fake_home, monkeypatch):
    """A failed ``sudo /bin/cp`` surfaces as PermissionError (so the route
    returns a real error, not a silent no-op)."""
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)

    def failing_run(cmd, *a, **k):
        class R:
            returncode = 1
            stderr = "cp: permission denied"
            stdout = ""
        return R()

    monkeypatch.setattr(oc_model.subprocess, "run", failing_run)

    with pytest.raises(PermissionError):
        oc_model._save_tiers_file("team_bot_a", {"x": 1})


def test_save_tiers_chowns_back_to_bot_after_sudo_cp(fake_home, monkeypatch):
    """After the ``sudo /bin/cp`` fallback, the writer must chown the dest back
    to the bot + chmod 644. A bare cp to a *fresh* dest lands it root:wheel
    0600, which locks the bot out of reading its OWN tier config (the
    2026-06-16 fleet repo-puller heal failure). Wiring is asserted by spying on
    ``_restore_tiers_bot_ownership``."""
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )

    restored: list = []
    monkeypatch.setattr(oc_model, "_restore_tiers_bot_ownership", lambda p: restored.append(p))

    oc_model._save_tiers_file("team_bot_a", {"x": 1})
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    assert restored == [tiers_path], restored


def test_restore_tiers_bot_ownership_chowns_and_chmods_644(monkeypatch):
    """The repair derives the bot user from the path and issues
    ``chown <bot>:staff`` + ``chmod 644`` (never 600 — tiers are routing
    config, not a secret)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: (calls.append(list(cmd)),
                              type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1],
    )
    oc_model._restore_tiers_bot_ownership(Path("/Users/team_bot_a/.openclaw/evolve-tiers.json"))
    dst = "/Users/team_bot_a/.openclaw/evolve-tiers.json"
    assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", dst] in calls, calls
    assert ["sudo", "/bin/chmod", "644", dst] in calls, calls
    assert not any(c[:3] == ["sudo", "/bin/chmod", "600"] for c in calls), calls


def test_restore_tiers_bot_ownership_noop_for_non_users_path(monkeypatch):
    """A non-/Users path (test tmpdir, Linux home) never reaches the sudo
    branch in production, so the repair must not shell out for it."""
    called = False

    def fake_run(*a, **k):
        nonlocal called
        called = True
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)
    oc_model._restore_tiers_bot_ownership(Path("/tmp/evolve-tiers-x.json"))
    assert not called

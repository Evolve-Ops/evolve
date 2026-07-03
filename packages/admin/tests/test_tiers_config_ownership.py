"""Guard: evolve-tiers.json stays BOT-OWNED so the bot can read its own tiers.

Regression test for the 2026-06-16 fleet-wide repo-puller heal failure. A bare
``sudo /bin/cp`` (no ``-p``) to a *fresh* dest lands ``evolve-tiers.json`` at
``root:wheel 0600``. The bot user runs ``oc_model.py`` to read/rewrite its OWN
tier config, so a root-owned file makes every tier read/write 500 with
``[Errno 13] Permission denied: .../evolve-tiers.json`` — the looping EACCES
seen across all 10 bots every deploy until the file is repaired.

Two layers are covered (mirrors test_oc_config_secret_mode for the 0600 path):
  1. WRITER — ``chown_chmod_bot_config`` issues ``chown <bot>:staff`` +
     ``chmod 644`` after the cp (NOT 600 — tiers are routing config, not a
     secret).
  2. SELF-HEAL — ``ensure_pod_perms``' ``check_bot_tiers_ownership`` flags a
     root-owned file and its fix runs the chown+chmod.

Placeholder bot names per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evolve_admin import secret_config_perms as scp


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class _FakePwd:
    """Minimal ``pwd`` stand-in: getpwnam(name) → object with pw_uid, or
    KeyError for names not in ``table`` (the dev-box / unknown-user path)."""

    def __init__(self, table: dict[str, int]):
        self._table = table

    def getpwnam(self, name):
        if name not in self._table:
            raise KeyError(name)
        return type("PW", (), {"pw_uid": self._table[name]})()


class TestBotUserFromPath:
    def test_derives_user_from_users_path(self):
        assert (
            scp._bot_user_from_path("/Users/team_bot_a/.openclaw/evolve-tiers.json")
            == "team_bot_a"
        )

    def test_none_for_non_users_path(self):
        # tmpdirs / Linux homes never reach the sudo branch — no chown target.
        assert scp._bot_user_from_path("/tmp/evolve-tiers-x.json") is None
        assert scp._bot_user_from_path("/home/team_bot_a/.openclaw/evolve-tiers.json") is None


class TestChownChmodBotConfig:
    """The writer-side repair: chown back to the bot + chmod 644 (never 600 —
    tiers are routing config, not a secret)."""

    def test_issues_chown_then_chmod_644(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        path = "/Users/team_bot_a/.openclaw/evolve-tiers.json"
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", path] in calls, calls
        assert ["sudo", "/bin/chmod", "644", path] in calls, calls
        # Never 600 — that would be the secret-file mode.
        assert not any(c[:3] == ["sudo", "/bin/chmod", "600"] for c in calls), calls

    def test_returns_false_when_chown_fails(self, monkeypatch):
        def fake_run(argv, *a, **k):
            if argv[1].endswith("chown"):
                return subprocess.CompletedProcess(argv, 1, "", "denied")
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config("/Users/team_bot_a/.openclaw/evolve-tiers.json") is False

    def test_noop_false_for_non_users_path(self, monkeypatch):
        # No /Users user → no chown target → False without shelling out.
        called = False

        def fake_run(*a, **k):
            nonlocal called
            called = True
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config("/tmp/x.json") is False
        assert not called

    def test_chown_routes_through_linux_profile_binary(self, monkeypatch):
        """On Linux the chown binary is /usr/bin/chown, NOT the macOS
        /usr/sbin/chown — a hardcoded macOS path is absent from the Linux
        NOPASSWD allowlist, so sudo would fall to a password prompt and the
        TTY-less admin daemon fails ("sudo: a terminal is required"). Route
        through the platform profile so the INVOKED path matches the granted
        one. (A /Users path is synthetic on Linux — _bot_user_from_path only
        matches /Users today — but it isolates the binary-routing logic the
        forthcoming /home support will rely on.) FAKE bot name only."""
        from platform_profile import LINUX, set_profile

        set_profile(LINUX)  # conftest autouse fixture restores MACOS in teardown
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        path = "/Users/team_bot_a/.openclaw/evolve-tiers.json"
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/bin/chown", "team_bot_a:staff", path] in calls, calls
        assert not any(c[1] == "/usr/sbin/chown" for c in calls), calls
        # chmod is /bin/chmod on both OSes, but still routed via the profile.
        assert ["sudo", "/bin/chmod", "644", path] in calls, calls

    def test_chown_macos_byte_identical(self, monkeypatch):
        """macOS resolves the SAME /usr/sbin/chown it did before the seam
        routing — existing-pod behavior is byte-identical. (conftest pins
        MACOS; assert explicitly for the record.)"""
        from platform_profile import MACOS, set_profile

        set_profile(MACOS)
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        path = "/Users/team_bot_a/.openclaw/evolve-tiers.json"
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", path] in calls, calls

    def test_does_not_hardcode_macos_chown_path(self):
        """Guard against regressing to a literal /usr/sbin/chown next to `sudo`
        in chown_chmod_bot_config (mirrors test_cat_seam_profile_path)."""
        from pathlib import Path as _P

        src = _P(scp.__file__).read_text()
        assert "prof.chown" in src, "chown must route through the profile"
        # The only acceptable mention is the MACOS profile definition itself,
        # which lives in platform_profile.py — not in this module.
        assert '"/usr/sbin/chown"' not in src, (
            "secret_config_perms still hardcodes the macOS /usr/sbin/chown — "
            "route it through get_profile().chown instead"
        )


class TestCheckBotTiersOwnership:
    """``check_bot_tiers_ownership`` is the deploy-time self-heal (wired into
    ensure_pod_perms ahead of the tier heal) that converges a root-owned
    evolve-tiers.json back to bot-owned 0644."""

    @staticmethod
    def _make_tiers(home: Path) -> Path:
        oc_dir = home / ".openclaw"
        oc_dir.mkdir(parents=True, exist_ok=True)
        f = oc_dir / "evolve-tiers.json"
        f.write_text("{}")
        return f

    def _tiers_check(self, checks):
        t = [c for c in checks if c.target.endswith("/evolve-tiers.json")]
        assert t, "no evolve-tiers.json check produced"
        return t[0]

    def test_passes_when_bot_owned(self, tmp_path, monkeypatch):
        self._make_tiers(tmp_path)  # owned by the running test user
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        # Bot uid == the file's actual owner (the test user) → no drift.
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team-bot-a": os.getuid()}))
        t = self._tiers_check(scp.check_bot_tiers_ownership("team-bot-a"))
        assert t.ok, t.detail

    def test_flags_root_owned_and_offers_repair(self, tmp_path, monkeypatch):
        self._make_tiers(tmp_path)  # owned by the test user (uid = getuid())
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        # Bot uid differs from the file owner → simulates root-owned drift.
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team-bot-a": os.getuid() + 1}))
        t = self._tiers_check(scp.check_bot_tiers_ownership("team-bot-a"))
        assert not t.ok
        assert "can't read its own tier config" in t.detail
        assert t.apply is not None  # a chown+chmod repair is wired

    def test_missing_file_is_noop_ok(self, tmp_path, monkeypatch):
        (tmp_path / ".openclaw").mkdir(parents=True)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team-bot-a": os.getuid()}))
        t = self._tiers_check(scp.check_bot_tiers_ownership("team-bot-a"))
        assert t.ok
        assert "not present" in t.detail

    def test_unreachable_parent_is_self_healing_drift(self, tmp_path, monkeypatch):
        # Linux/Py3.12: the OC gateway hardens .openclaw to 0700 after deploy's
        # read-ACL grant, clamping evolve's traverse ACE mask → path.stat() RAISES
        # PermissionError instead of returning a result. The check must classify
        # it as an unreachable drift with an ACL-reassert self-heal (NOT crash —
        # the W10-G round-6 class, and the bug the openclaw-eacces lint surfaced).
        self._make_tiers(tmp_path)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team-bot-a": os.getuid()}))
        real_stat = Path.stat

        def fake_stat(self, *a, **k):
            if self.name == "evolve-tiers.json":
                raise PermissionError(13, "Permission denied")
            return real_stat(self, *a, **k)

        monkeypatch.setattr(Path, "stat", fake_stat)
        t = self._tiers_check(scp.check_bot_tiers_ownership("team-bot-a"))
        assert not t.ok
        assert "unreachable" in t.detail
        assert t.apply is not None  # re-assert evolve read ACL recomputes the mask

    def test_unknown_user_returns_empty(self, monkeypatch):
        # Dev box / tests without real bot accounts: pwd lookup misses →
        # nothing to enforce (don't fabricate a uid and flag every file).
        monkeypatch.setattr(scp, "pwd", _FakePwd({}))
        assert scp.check_bot_tiers_ownership("nonexistent-bot") == []

    def test_tiers_relpath_registered(self):
        assert "evolve-tiers.json" in scp.BOT_OWNED_CONFIG_RELPATHS
        assert scp.BOT_OWNED_CONFIG_MODE == 0o644  # routing config, not a secret

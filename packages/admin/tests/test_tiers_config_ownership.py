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

import dataclasses
import os
import subprocess
from pathlib import Path

from evolve_admin import secret_config_perms as scp


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fake_pod(tmp_path: Path, base, bot: str = "team_bot_a") -> Path:
    """Pin ``base``'s profile at a REAL home root under ``tmp_path`` and return
    the bot's ``.openclaw`` dir (created).

    Every test below that reaches the sudo argv needs a destination that exists
    on disk: ``chown_chmod_bot_config`` now gates on
    ``evolve_util.assert_safe_sudo_dest``, which lstats the dest and its parent
    and FAILS CLOSED when it cannot verify them (#3566 audit D-2). The old
    fixture-free ``/Users/team_bot_a/...`` string is not a real path on any CI
    runner, so it would trip that gate. Re-pointing ``user_home_root`` keeps the
    path-derivation contract (``<home_root>/<user>/.openclaw/<file>``) exact
    while making the tree real — better than stubbing the gate out, which is how
    a gate silently stops being called.

    conftest's autouse fixture restores MACOS in teardown.
    """
    from platform_profile import set_profile

    set_profile(dataclasses.replace(base, user_home_root=str(tmp_path)))
    oc = tmp_path / bot / ".openclaw"
    oc.mkdir(parents=True, exist_ok=True)
    return oc


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
    """The home root comes from the platform profile, so the derivation works on
    BOTH pods. Every test pins the profile explicitly (conftest pins MACOS and
    restores it in teardown) so the assertions hold on the Linux CI runner too."""

    def test_derives_user_from_macos_home_path(self):
        from platform_profile import MACOS, set_profile

        set_profile(MACOS)
        assert (
            scp._bot_user_from_path("/Users/team_bot_a/.openclaw/evolve-tiers.json")
            == "team_bot_a"
        )

    def test_derives_user_from_linux_home_path(self):
        # #3566 audit B-1: a hardcoded ``/Users`` returned None here, so the
        # chown+chmod repair issued no argv at all on the Linux VPS pod and a
        # fresh root-owned evolve-tiers.json stayed unreadable by its own bot.
        from platform_profile import LINUX, set_profile

        set_profile(LINUX)
        assert (
            scp._bot_user_from_path("/home/team_bot_a/.openclaw/evolve-tiers.json")
            == "team_bot_a"
        )

    def test_none_for_path_outside_the_profile_home_root(self):
        # tmpdirs and the OTHER platform's home root never reach the sudo
        # branch — there is no chown target to derive.
        from platform_profile import LINUX, MACOS, set_profile

        set_profile(MACOS)
        assert scp._bot_user_from_path("/tmp/evolve-tiers-x.json") is None
        assert scp._bot_user_from_path("/home/team_bot_a/.openclaw/evolve-tiers.json") is None
        set_profile(LINUX)
        assert scp._bot_user_from_path("/tmp/evolve-tiers-x.json") is None
        assert scp._bot_user_from_path("/Users/team_bot_a/.openclaw/evolve-tiers.json") is None

    def test_none_for_off_shape_paths(self):
        """Only the EXACT ``<home_root>/<user>/.openclaw/<file>`` shape derives a
        user. A looser test hands back the wrong chown target: the shallow path
        yields ".openclaw" as the account, and the nested one names "bots" (which
        is what the pre-fix ``parts[1] == "Users"`` check did)."""
        from platform_profile import MACOS, set_profile

        set_profile(MACOS)
        assert scp._bot_user_from_path("/Users/.openclaw/evolve-tiers.json") is None
        assert scp._bot_user_from_path(
            "/Users/bots/team_bot_a/.openclaw/evolve-tiers.json") is None
        assert scp._bot_user_from_path(
            "/Users/team_bot_a/.openclaw/sub/evolve-tiers.json") is None
        assert scp._bot_user_from_path("/Users/team_bot_a/.config/evolve-tiers.json") is None

    def test_none_for_unsafe_account_name(self):
        """The name lands in a ``sudo chown <user>:staff`` argv, so re-assert the
        account shape at the sink (mirrors tier_prefs_acl, #3565 audit)."""
        from platform_profile import MACOS, set_profile

        set_profile(MACOS)
        assert scp._bot_user_from_path("/Users/bad user/.openclaw/evolve-tiers.json") is None
        assert scp._bot_user_from_path("/Users/bad:name/.openclaw/evolve-tiers.json") is None

    def test_owned_relpaths_stay_flat(self):
        """The exact-shape check only matches a flat filename under
        ``.openclaw/``, so a nested BOT_OWNED_CONFIG_RELPATHS entry would make
        the repair a silent no-op."""
        for rel in scp.BOT_OWNED_CONFIG_RELPATHS:
            assert "/" not in rel, f"{rel!r} is nested — _bot_user_from_path would return None"


class TestChownChmodBotConfig:
    """The writer-side repair: chown back to the bot + chmod 644 (never 600 —
    tiers are routing config, not a secret)."""

    def test_issues_chown_then_chmod_644(self, tmp_path, monkeypatch):
        from platform_profile import MACOS

        oc = _fake_pod(tmp_path, MACOS)
        path = oc / "evolve-tiers.json"
        path.write_text("{}")
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", str(path)] in calls, calls
        assert ["sudo", "/bin/chmod", "644", str(path)] in calls, calls
        # Never 600 — that would be the secret-file mode.
        assert not any(c[:3] == ["sudo", "/bin/chmod", "600"] for c in calls), calls

    def test_returns_false_when_chown_fails(self, tmp_path, monkeypatch):
        from platform_profile import MACOS

        oc = _fake_pod(tmp_path, MACOS)
        path = oc / "evolve-tiers.json"
        path.write_text("{}")

        def fake_run(argv, *a, **k):
            if argv[1].endswith("chown"):
                return subprocess.CompletedProcess(argv, 1, "", "denied")
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config(path) is False

    def test_noop_false_for_unrecognized_path(self, monkeypatch):
        # No derivable account → no chown target → False without shelling out.
        called = False

        def fake_run(*a, **k):
            nonlocal called
            called = True
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config("/tmp/x.json") is False
        assert not called

    def test_chown_routes_through_linux_profile_binary(self, tmp_path, monkeypatch):
        """On Linux the chown binary is /usr/bin/chown, NOT the macOS
        /usr/sbin/chown — a hardcoded macOS path is absent from the Linux
        NOPASSWD allowlist, so sudo would fall to a password prompt and the
        TTY-less admin daemon fails ("sudo: a terminal is required"). Route
        through the platform profile so the INVOKED path matches the granted
        one. The path is a real Linux home (``/home/…``) — under #3566 audit
        B-1 that derived no bot user at all, so this repair never issued a
        single argv on the VPS pod. FAKE bot name only."""
        from platform_profile import LINUX

        # conftest autouse fixture restores MACOS in teardown
        oc = _fake_pod(tmp_path, LINUX)
        path = oc / "evolve-tiers.json"
        path.write_text("{}")
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/bin/chown", "team_bot_a:staff", str(path)] in calls, calls
        assert not any(c[1] == "/usr/sbin/chown" for c in calls), calls
        # chmod is /bin/chmod on both OSes, but still routed via the profile.
        assert ["sudo", "/bin/chmod", "644", str(path)] in calls, calls

    def test_chown_macos_byte_identical(self, tmp_path, monkeypatch):
        """macOS resolves the SAME /usr/sbin/chown it did before the seam
        routing — existing-pod behavior is byte-identical. (conftest pins
        MACOS; assert explicitly for the record.)"""
        from platform_profile import MACOS

        oc = _fake_pod(tmp_path, MACOS)
        path = oc / "evolve-tiers.json"
        path.write_text("{}")
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config(path) is True
        assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", str(path)] in calls, calls

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
        # read-ACL grant, clamping evolve's traverse ACE mask → path.lstat() RAISES
        # PermissionError instead of returning a result. The check must classify
        # it as an unreachable drift with an ACL-reassert self-heal (NOT crash —
        # the W10-G round-6 class, and the bug the openclaw-eacces lint surfaced).
        self._make_tiers(tmp_path)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path)
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team-bot-a": os.getuid()}))
        real_lstat = Path.lstat

        def fake_lstat(self, *a, **k):
            if self.name == "evolve-tiers.json":
                raise PermissionError(13, "Permission denied")
            return real_lstat(self, *a, **k)

        monkeypatch.setattr(Path, "lstat", fake_lstat)
        t = self._tiers_check(scp.check_bot_tiers_ownership("team-bot-a"))
        assert not t.ok
        assert "unreachable" in t.detail
        assert t.apply is not None  # re-assert evolve read ACL recomputes the mask

    def test_unknown_user_returns_empty(self, monkeypatch):
        # Dev box / tests without real bot accounts: pwd lookup misses →
        # nothing to enforce (don't fabricate a uid and flag every file).
        monkeypatch.setattr(scp, "pwd", _FakePwd({}))
        assert scp.check_bot_tiers_ownership("nonexistent-bot") == []

    def test_linux_pod_repair_reaches_the_chown(self, tmp_path, monkeypatch):
        """End-to-end on the shape the Linux VPS pod actually has: the check's
        ``apply`` must issue ``sudo /usr/bin/chown team_bot_a:staff …``.

        Under #3566 audit B-1 this whole chain was a no-op there — the path is
        ``/home/<user>/…``, ``_bot_user_from_path`` hardcoded ``/Users``, so
        ``chown_chmod_bot_config`` returned False without running anything and
        the root-owned file stayed unreadable by its own bot. FAKE bot name.

        The tree is REAL (under tmp_path, with the Linux profile's home root
        re-pointed at it) rather than a ``Path.stat`` fake: the repair now lstats
        the destination through the D-2 gate, so a fake-stat'd path that does not
        exist on disk would fail closed and hide the very argv under test.
        Ownership drift is simulated at the pwd seam instead — the file is owned
        by the test user, the "bot" uid is a different one."""
        from platform_profile import LINUX

        oc = _fake_pod(tmp_path, LINUX)
        target = oc / "evolve-tiers.json"
        target.write_text("{}")
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path / "team_bot_a")
        monkeypatch.setattr(scp, "pwd", _FakePwd({"team_bot_a": os.getuid() + 1}))
        t = self._tiers_check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok, t.detail
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert t.apply() is True
        assert ["sudo", "/usr/bin/chown", "team_bot_a:staff", str(target)] in calls, calls
        assert ["sudo", "/bin/chmod", "644", str(target)] in calls, calls

    def test_tiers_relpath_registered(self):
        assert "evolve-tiers.json" in scp.BOT_OWNED_CONFIG_RELPATHS
        assert scp.BOT_OWNED_CONFIG_MODE == 0o644  # routing config, not a secret

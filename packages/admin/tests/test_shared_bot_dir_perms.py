"""Tests for shared_bot_dir_perms — the EVOLVE-direction write ACE on
``{sharedDir}/{botId}/``, twin of ``tier_prefs_acl``'s bot-direction grant.

Why it exists: ``{sharedDir}/{botId}/`` has no ownership guarantee (whoever
mkdir's first owns it, and the plugin's ``writeTurnToShared`` does so as the
BOT user), so evolve-user jobs that write a file at that level —
``usage-by-app.json``, AL-1.3 — get EACCES on a plugin-created dir. Observed
live on the mini 2026-08-17: 8 of 9 bots wrote, ``atlas`` failed.

Contract under test:
  * grant is OWNER-DIRECT — argv never begins with ``sudo``
  * Linux: setfacl access ACL, never raw chmod, never ``-d``
  * macOS: ``chmod +a`` dir-form verbs, no inherit flags
  * NO cross-bot widening — the fix must never be a 1777 on the per-bot dir
  * idempotent: ACE present → no subprocess at all
  * missing dir → informational no-op
  * the SAFETY GATE is tier_prefs_acl's, imported not copied (symlink /
    out-of-tree / malformed-id targets refuse, with no ``apply``)
  * wired into ensure_pod_perms (drift check + self-heal)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

# deploy is imported EAGERLY (not just for the wiring test): the module's
# _evolve_user() lazily imports it, and deploy runs git at import time — under
# a patched subprocess.run (which is process-global, not module-local) that
# would land in the captured argv of whichever test triggered the first import.
from evolve_admin import deploy  # noqa: E402,F401
from evolve_admin import shared_bot_dir_perms as sbd  # noqa: E402
from evolve_admin import tier_prefs_acl  # noqa: E402
from evolve_admin.shared_bot_dir_perms import (  # noqa: E402
    EVOLVE_BOT_DIR_ACL_BITS,
    EVOLVE_BOT_DIR_ACL_PERMS,
    _apply_evolve_bot_dir_acl,
    check_evolve_bot_dir_acl,
    ensure_evolve_bot_dir_acl,
    per_bot_dir_ids,
    reassert_per_bot_dir_perms,
)

BOT = "team_bot_a"
EVOLVE = "evolve"


@pytest.fixture
def linux_profile():
    set_profile(LINUX)
    try:
        yield LINUX
    finally:
        set_profile(None)


@pytest.fixture
def macos_profile():
    set_profile(MACOS)
    try:
        yield MACOS
    finally:
        set_profile(None)


class _FakeSeam:
    def __init__(self, present: bool = False):
        self._present = present
        self.calls: list[tuple[str, str, str]] = []

    def acl_user_effective(self, path, user, required):
        self.calls.append((str(path), user, required))
        return self._present


class _OK:
    returncode = 0
    stdout = ""
    stderr = ""


def _seam(present: bool):
    return patch("evolve_admin.shared_bot_dir_perms._get_perms",
                 return_value=_FakeSeam(present))


# ── the grant ────────────────────────────────────────────────────────────────

class TestApplyGrant:
    def test_linux_owner_direct_setfacl_no_sudo_no_default_acl(self, tmp_path, linux_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        captured: list[list[str]] = []

        with patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                   lambda cmd, **kw: (captured.append(list(cmd)), _OK())[1]):
            assert _apply_evolve_bot_dir_acl(bot_dir) is True

        assert captured == [
            [LINUX.setfacl, "-m", f"u:{EVOLVE}:{EVOLVE_BOT_DIR_ACL_BITS}", str(bot_dir)],
        ]
        argv = captured[0]
        assert "sudo" not in argv
        # -d would mint a default ACL onto every future file here (#3198 class).
        assert "-d" not in argv
        # raw chmod recalculates the ACL mask and can lock evolve back out.
        assert "chmod" not in argv[0]

    def test_macos_owner_direct_chmod_plus_a_no_inherit(self, tmp_path, macos_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        captured: list[list[str]] = []

        with patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                   lambda cmd, **kw: (captured.append(list(cmd)), _OK())[1]):
            assert _apply_evolve_bot_dir_acl(bot_dir) is True

        assert captured == [
            [MACOS.chmod, "+a", f"user:{EVOLVE} allow {EVOLVE_BOT_DIR_ACL_PERMS}",
             str(bot_dir)],
        ]
        assert "sudo" not in captured[0]
        # No inherit flags — dir-level ACE only.
        for token in ("file_inherit", "directory_inherit", "inherit"):
            assert token not in captured[0][2]

    def test_macos_duplicate_ace_is_success(self, tmp_path, macos_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()

        class _Dup:
            returncode = 1
            stdout = ""
            stderr = "chmod: Failed to set ACL on file ...: exists"

        with patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                   lambda cmd, **kw: _Dup()):
            assert _apply_evolve_bot_dir_acl(bot_dir) is True

    def test_grant_failure_is_not_fatal(self, tmp_path, linux_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()

        def _boom(cmd, **kw):
            raise OSError("setfacl missing")

        with patch("evolve_admin.shared_bot_dir_perms.subprocess.run", _boom):
            assert _apply_evolve_bot_dir_acl(bot_dir) is False


class TestEnsure:
    def test_idempotent_when_ace_already_present(self, tmp_path, macos_profile):
        (tmp_path / BOT).mkdir()
        ran: list[list[str]] = []
        with _seam(True), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                lambda cmd, **kw: (ran.append(list(cmd)), _OK())[1]):
            assert ensure_evolve_bot_dir_acl(tmp_path, BOT) is True
        assert ran == [], "a correct dir must not re-fire the grant"

    def test_missing_dir_is_a_noop_pass(self, tmp_path, macos_profile):
        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: _OK()):
            assert ensure_evolve_bot_dir_acl(tmp_path, "not_deployed_yet") is True

    def test_symlinked_bot_dir_is_refused(self, tmp_path, macos_profile):
        """{sharedDir} is sticky world-writable, so a bot can plant a symlink
        at a not-yet-created bot-id name. The grant must refuse rather than
        write an ACE onto the target."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / BOT).symlink_to(outside)
        ran: list[list[str]] = []
        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: (ran.append(list(cmd)), _OK())[1]):
            assert ensure_evolve_bot_dir_acl(tmp_path, BOT) is False
        assert ran == []

    @pytest.mark.parametrize("bad_id", ["../etc", "/etc", "bot id", "bot,id", ""])
    def test_malformed_bot_id_is_refused(self, tmp_path, macos_profile, bad_id):
        ran: list[list[str]] = []
        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: (ran.append(list(cmd)), _OK())[1]):
            assert ensure_evolve_bot_dir_acl(tmp_path, bad_id) is False
        assert ran == []

    def test_gate_is_the_tier_prefs_gate_not_a_copy(self):
        """Same call site, same hazard — a re-implemented gate is how a
        chokepoint ends up closed for one class and open for its sibling."""
        assert sbd.resolve_bot_dir_for_acl is tier_prefs_acl.resolve_bot_dir_for_acl


class TestCheck:
    def test_present_ace_passes(self, tmp_path, macos_profile):
        (tmp_path / BOT).mkdir()
        with _seam(True):
            c = check_evolve_bot_dir_acl(tmp_path, BOT)
        assert c.ok is True

    def test_missing_ace_drifts_with_a_working_apply(self, tmp_path, macos_profile):
        (tmp_path / BOT).mkdir()
        with _seam(False):
            c = check_evolve_bot_dir_acl(tmp_path, BOT)
        assert c.ok is False
        assert "usage-by-app.json" in c.fix_description
        assert c.apply is not None

        ran: list[list[str]] = []
        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: (ran.append(list(cmd)), _OK())[1]):
            assert c.apply() is True
        assert ran and ran[0][0] == MACOS.chmod

    def test_unsafe_target_drifts_with_no_apply(self, tmp_path, macos_profile):
        """The self-heal must never be the thing that lands an ACE on an
        attacker-chosen path — report it, let a human look."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / BOT).symlink_to(outside)
        with _seam(False):
            c = check_evolve_bot_dir_acl(tmp_path, BOT)
        assert c.ok is False
        assert c.apply is None
        assert "SYMLINK" in c.detail

    def test_absent_dir_is_informational_pass(self, tmp_path, macos_profile):
        with _seam(False):
            c = check_evolve_bot_dir_acl(tmp_path, "not_deployed_yet")
        assert c.ok is True


# ── the pod-wide pass ────────────────────────────────────────────────────────

class TestPodWidePass:
    def test_ids_union_members_and_on_disk_dirs(self, tmp_path):
        import json
        (tmp_path / "network.json").write_text(json.dumps({"members": ["bot_a", "bot_b"]}))
        (tmp_path / "bot_c" / "turns").mkdir(parents=True)   # retired/renamed bot
        (tmp_path / "metrics").mkdir()                        # pod-wide dir, not a bot
        ids = per_bot_dir_ids(tmp_path)
        assert ids == ["bot_a", "bot_b", "bot_c", "evolve"]
        assert "metrics" not in ids

    def test_ids_degrade_to_on_disk_when_network_unreadable(self, tmp_path):
        (tmp_path / "bot_c" / "turns").mkdir(parents=True)
        assert per_bot_dir_ids(tmp_path) == ["bot_c"]

    def test_turns_dir_gets_sticky_1777_and_parent_gets_the_ace(self, tmp_path, macos_profile):
        import json
        import stat as _stat
        (tmp_path / "network.json").write_text(json.dumps({"members": [BOT]}))
        turns = tmp_path / BOT / "turns"
        turns.mkdir(parents=True)
        turns.chmod(0o755)

        granted: list[str] = []
        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: (granted.append(str(cmd[-1])), _OK())[1]):
            reassert_per_bot_dir_perms(tmp_path, lambda p: None)

        assert _stat.S_IMODE(turns.stat().st_mode) == 0o1777
        assert str(tmp_path / BOT) in granted

    def test_parent_dir_is_never_widened_to_1777(self, tmp_path, macos_profile):
        """The tempting fix — make {sharedDir}/{bot} multi-writer like
        metrics/ — is a CROSS-BOT widening: every bot could then replace
        every other bot's files. The ACE is bot-scoped on purpose."""
        import json
        import stat as _stat
        (tmp_path / "network.json").write_text(json.dumps({"members": [BOT]}))
        bot_dir = tmp_path / BOT
        (bot_dir / "turns").mkdir(parents=True)
        bot_dir.chmod(0o755)

        with _seam(False), patch("evolve_admin.shared_bot_dir_perms.subprocess.run",
                                 lambda cmd, **kw: _OK()):
            reassert_per_bot_dir_perms(tmp_path, lambda p: None)

        mode = _stat.S_IMODE(bot_dir.stat().st_mode)
        assert mode == 0o755, f"per-bot dir must not be widened, got {oct(mode)}"
        assert not mode & _stat.S_IWOTH

    def test_unwritable_turns_dir_falls_back_to_the_callers_sudo(self, tmp_path, macos_profile):
        import json
        (tmp_path / "network.json").write_text(json.dumps({"members": [BOT]}))
        turns = tmp_path / BOT / "turns"
        turns.mkdir(parents=True)
        fell_back: list[Path] = []

        def _chmod_denied(self, mode):
            raise PermissionError("not the owner")

        with _seam(True), patch.object(Path, "chmod", _chmod_denied):
            reassert_per_bot_dir_perms(tmp_path, fell_back.append)
        assert fell_back == [turns]


# ── wiring ───────────────────────────────────────────────────────────────────

class TestWiring:
    def test_ensure_pod_perms_carries_the_evolve_bot_dir_check(self, monkeypatch):
        """The drift check must be reachable from deploy under the alias
        ensure_pod_perms calls — that wiring is what lets the hourly
        pod_perms_drift_monitor catch a dir the plugin re-created bot-owned
        between deploys. The failure it guards is silent (one bot's rollup
        file just stops updating), so it needs a watcher, not just a
        deploy-time repair."""
        assert deploy._bot_dir_perms is sbd
        import inspect
        src = inspect.getsource(deploy.ensure_pod_perms)
        assert "_bot_dir_perms.check_evolve_bot_dir_acl(shared_dir, bid)" in src

    def test_deploy_shared_dir_delegates_the_per_bot_pass(self):
        """deploy_shared_dir must call the module (the turns-1777 loop moved
        here), not keep its own inline copy — one home for the contract."""
        import inspect
        src = inspect.getsource(deploy.deploy_shared_dir)
        assert "_bot_dir_perms.reassert_per_bot_dir_perms" in src
        assert 'glob("*/turns")' not in src, "inline copy left behind"

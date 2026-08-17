"""Tests for tier_prefs_acl — the owner-direct bot write ACE on
``{sharedDir}/{botId}/`` that makes the plugin's standing tier-default write
(``ModelRouter.setStandingUserTierDefault``, PR #3562) work on real pods.

Contract under test:
  * the grant is OWNER-DIRECT — argv never begins with ``sudo`` (no sudoers
    dependency; the #3223 silent-denial class)
  * Linux uses the setfacl access-ACL entry, NEVER raw chmod (mask-recalc
    lockout class) and NO ``-d`` default ACL (group/world-readable minting
    class)
  * macOS uses ``chmod +a`` with dir-form verbs and NO inherit flags
  * bot-scoped: exactly this bot's user on exactly its own per-bot dir —
    never ``{sharedDir}`` root, never another bot's dir
  * idempotent: ACE already present → no grant subprocess at all
  * missing dir → informational no-op (bot not yet deployed)
  * wired into ensure_pod_perms (self-heal backstop) and
    fix_shared_dir_permissions (at-deploy grant)

Run with: python3 -m pytest packages/admin/tests/test_tier_prefs_acl.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import deploy, tier_prefs_acl  # noqa: E402
from evolve_admin.tier_prefs_acl import (  # noqa: E402
    BOT_TIER_PREFS_ACL_BITS,
    BOT_TIER_PREFS_ACL_PERMS,
    _apply_bot_tier_prefs_acl,
    check_bot_tier_prefs_acl,
    ensure_bot_tier_prefs_acl,
)

BOT = "atlas"
BOT_USER = "atlas"


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
    """Perms-seam double answering acl_user_effective presence.

    The apply path is OWNER-DIRECT subprocess, NOT routed through the seam —
    so this double only needs the CHECK method, mirroring the
    evo_socket_acl test double.
    """

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
    return patch("evolve_admin.tier_prefs_acl._get_perms",
                 return_value=_FakeSeam(present))


# ── the apply (grant) path ────────────────────────────────────────────────────

class TestApplyGrant:
    def test_linux_owner_direct_setfacl_no_sudo_no_default_acl(
        self, tmp_path, linux_profile,
    ):
        """Linux grant: one plain access-ACL setfacl — owner-direct (no sudo),
        no ``-d`` (a default ACL would mint bot-write ACEs onto future
        evolve-written files), and never a raw chmod (the ACL-mask-recalc
        lockout class)."""
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        captured: list[list[str]] = []

        def _run(cmd, **kwargs):
            captured.append(list(cmd))
            return _OK()

        with patch("evolve_admin.tier_prefs_acl.subprocess.run", _run):
            ok = _apply_bot_tier_prefs_acl(bot_dir, BOT_USER)
        assert ok is True
        assert captured == [
            [LINUX.setfacl, "-m", f"u:{BOT_USER}:{BOT_TIER_PREFS_ACL_BITS}",
             str(bot_dir)],
        ]
        argv = captured[0]
        assert "sudo" not in argv
        assert "-d" not in argv
        assert "chmod" not in argv[0]

    def test_macos_owner_direct_chmod_plus_a_no_sudo_no_inherit(
        self, tmp_path, macos_profile,
    ):
        """macOS grant: one ``chmod +a`` with dir-form verbs — owner-direct
        (no sudo) and no inherit flags (dir-level ACE only)."""
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        captured: list[list[str]] = []

        def _run(cmd, **kwargs):
            captured.append(list(cmd))
            return _OK()

        with patch("evolve_admin.tier_prefs_acl.subprocess.run", _run):
            ok = _apply_bot_tier_prefs_acl(bot_dir, BOT_USER)
        assert ok is True
        assert captured == [
            [MACOS.chmod, "+a",
             f"user:{BOT_USER} allow {BOT_TIER_PREFS_ACL_PERMS}",
             str(bot_dir)],
        ]
        assert "sudo" not in captured[0]
        assert "inherit" not in captured[0][2]

    def test_macos_duplicate_ace_response_is_success(self, tmp_path, macos_profile):
        """macOS answers an already-present ACE with rc 1 + 'exists' —
        idempotence contract, same as the Perms seam's _chmod_plus_a."""
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()

        class _Dup:
            returncode = 1
            stdout = ""
            stderr = "chmod: Failed to set ACL on file: entry already exists"

        with patch("evolve_admin.tier_prefs_acl.subprocess.run",
                   return_value=_Dup()):
            assert _apply_bot_tier_prefs_acl(bot_dir, BOT_USER) is True

    def test_linux_nonzero_rc_is_failure(self, tmp_path, linux_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()

        class _Err:
            returncode = 2
            stdout = ""
            stderr = "setfacl: Option -m: Invalid argument near character 3"

        with patch("evolve_admin.tier_prefs_acl.subprocess.run",
                   return_value=_Err()):
            assert _apply_bot_tier_prefs_acl(bot_dir, BOT_USER) is False

    def test_subprocess_exception_is_fail_soft(self, tmp_path, linux_profile):
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        with patch("evolve_admin.tier_prefs_acl.subprocess.run",
                   side_effect=OSError("boom")):
            assert _apply_bot_tier_prefs_acl(bot_dir, BOT_USER) is False


# ── ensure (check-first grant) ────────────────────────────────────────────────

class TestEnsure:
    def test_noop_true_when_bot_dir_missing(self, tmp_path, linux_profile):
        """Bot not yet deployed: nothing to enforce, no subprocess at all."""
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is True
        run.assert_not_called()

    def test_skips_grant_when_ace_already_present(self, tmp_path, linux_profile):
        """Idempotence: a correct dir never re-fires the grant (no unbounded
        duplicate-ACE growth on macOS, no redundant setfacl on Linux)."""
        (tmp_path / BOT).mkdir()
        with _seam(True), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is True
        run.assert_not_called()

    def test_grant_scoped_to_this_bots_own_dir_only(self, tmp_path, linux_profile):
        """Bot-scoped narrowness: the single grant argv targets exactly
        ``{sharedDir}/{botId}`` and names exactly this bot's user — never the
        shared root, never a sibling bot's dir."""
        (tmp_path / BOT).mkdir()
        (tmp_path / "otherbot").mkdir()
        captured: list[list[str]] = []

        def _run(cmd, **kwargs):
            captured.append(list(cmd))
            return _OK()

        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run", _run):
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is True
        assert len(captured) == 1
        argv = captured[0]
        assert argv[-1] == str(tmp_path / BOT)
        assert str(tmp_path) not in argv[:-1]  # never the shared root
        assert not any("otherbot" in a for a in argv)
        assert f"u:{BOT_USER}:" in argv[2]


# ── the drift check ───────────────────────────────────────────────────────────

class TestCheck:
    def test_dir_missing_is_informational_pass(self, tmp_path, linux_profile):
        check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is True
        assert "not yet created" in check.detail
        assert check.apply is None

    def test_ace_present_passes(self, tmp_path, linux_profile):
        (tmp_path / BOT).mkdir()
        with _seam(True):
            check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is True
        assert f"user:{BOT_USER}" in check.detail

    def test_ace_missing_is_drift_with_owner_direct_fix(
        self, tmp_path, linux_profile,
    ):
        (tmp_path / BOT).mkdir()
        with _seam(False):
            check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is False
        assert "missing" in check.detail
        assert callable(check.apply)
        fix = check.fix_description
        assert f"setfacl -m u:{BOT_USER}:{BOT_TIER_PREFS_ACL_BITS}" in fix
        assert "no sudo" in fix
        assert "chown" not in fix

    def test_macos_fix_description_is_chmod_plus_a(self, tmp_path, macos_profile):
        (tmp_path / BOT).mkdir()
        with _seam(False):
            check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is False
        assert "chmod +a" in check.fix_description
        assert "no sudo" in check.fix_description

    def test_self_heal_loop_drift_apply_pass(self, tmp_path, linux_profile):
        """End-to-end self-heal: drift check → apply re-grants owner-direct →
        subsequent check passes."""
        (tmp_path / BOT).mkdir()
        seam = _FakeSeam(present=False)

        def _run(cmd, **kwargs):
            seam._present = True  # the owner-direct setfacl took effect
            return _OK()

        with patch("evolve_admin.tier_prefs_acl._get_perms", return_value=seam), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run", _run):
            first = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
            assert first.ok is False
            assert first.apply() is True
            second = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert second.ok is True


# ── wiring ────────────────────────────────────────────────────────────────────

class TestWiring:
    def _network_file(self, tmp_path, members):
        import json
        np = tmp_path / "network.json"
        np.write_text(json.dumps({
            "networkId": "t", "sharedDir": str(tmp_path),
            "members": members,
            "bots": {m: {"role": "member"} for m in members},
        }))
        return np

    def test_ensure_pod_perms_runs_check_per_bot(self, tmp_path, monkeypatch):
        """The tier-prefs check is wired into the per-bot section of
        ensure_pod_perms — one invocation per (bot_id, bot_user) pair."""
        np = self._network_file(tmp_path, ["atlas", "ledger"])
        # Neutralize the heavy unrelated checks, same shape as the app-cron
        # wiring tests in test_ensure_pod_perms.py.
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        ok = deploy._PermCheck(category="x", target="y", ok=True)
        monkeypatch.setattr(deploy, "_check_bot_acl", lambda *a, **k: [])
        for name in ("_check_apply_lock", "_check_apply_plist",
                     "_check_cli_device_scopes"):
            monkeypatch.setattr(deploy, name, lambda *a, **k: ok)
        monkeypatch.setattr(deploy._secret_perms, "check_evolve_access",
                            lambda *a, **k: ok)
        monkeypatch.setattr(deploy._secret_perms, "check_bot_secret_modes",
                            lambda *a, **k: [])
        monkeypatch.setattr(deploy._secret_perms, "check_bot_tiers_ownership",
                            lambda *a, **k: [])

        calls: list[tuple[str, str, str]] = []

        def _spy(shared_dir, bid, bot_user):
            calls.append((str(shared_dir), bid, bot_user))
            return ok

        monkeypatch.setattr(deploy._tier_prefs_acl, "check_bot_tier_prefs_acl",
                            _spy)
        heal = MagicMock(return_value={"checked": 0, "missing": [],
                                       "healed": [], "failed": []})
        with patch("evolve_admin.applications.install_helpers."
                   "repair_app_cron_env_paths", heal):
            deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=True)
        assert [(bid, user) for _, bid, user in calls] == \
            [("atlas", "atlas"), ("ledger", "ledger")]
        assert all(sd == str(tmp_path) for sd, _, _ in calls)

    def test_fix_shared_dir_permissions_applies_grant(self, tmp_path, monkeypatch):
        """The at-deploy path grants the ACE for exactly the deployed bot."""
        calls: list[tuple[str, str, str]] = []

        def _spy(shared_dir, bid, bot_user):
            calls.append((str(shared_dir), bid, bot_user))
            return True

        monkeypatch.setattr(deploy._tier_prefs_acl, "ensure_bot_tier_prefs_acl",
                            _spy)
        with patch.object(deploy.subprocess, "run", return_value=_OK()), \
             patch.object(deploy, "_bot_user_for", return_value="atlas-user"):
            deploy.fix_shared_dir_permissions("atlas", tmp_path)
        assert calls == [(str(tmp_path), "atlas", "atlas-user")]

    def test_fix_shared_dir_permissions_grant_failure_is_nonfatal(
        self, tmp_path, monkeypatch,
    ):
        """Graceful degradation: a failed grant logs and never aborts the
        deploy — the plugin's loud 'use evo tier-default' fallback is the
        behavioural floor until the ACL lands."""
        monkeypatch.setattr(deploy._tier_prefs_acl, "ensure_bot_tier_prefs_acl",
                            lambda *a, **k: False)
        with patch.object(deploy.subprocess, "run", return_value=_OK()), \
             patch.object(deploy, "_bot_user_for", return_value="atlas"):
            deploy.fix_shared_dir_permissions("atlas", tmp_path)  # no raise


# ── the pre-grant safety gate ────────────────────────────────────────────────
#
# Post-hoc security audit of #3565. ``{sharedDir}`` is mode 1777 (sticky,
# world-writable — re-asserted on every deploy by fix_shared_dir_permissions),
# so any bot user can create an entry there, and the owner of an entry may
# rename/delete it under sticky semantics. Both grant argv forms FOLLOW a
# symlink given on the command line, and both original guards were
# symlink-transparent (``Path.is_dir()`` follows; ``ls -lde <link>`` reports
# the LINK's empty ACL, so the check-first skip never fires). Without the gate
# a planted symlink turned the deploy / drift-monitor pass into a write of
# ``add_file,delete_child`` for that bot onto an arbitrary directory.

class TestPreGrantSafetyGate:
    def test_symlinked_bot_dir_is_refused_not_granted(
        self, tmp_path, macos_profile,
    ):
        """The headline case: ``{sharedDir}/{botId}`` swapped for a symlink at
        another bot's dir. No grant subprocess may run at all."""
        victim = tmp_path / "victimbot"
        victim.mkdir()
        (tmp_path / BOT).symlink_to(victim)
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is False
        run.assert_not_called()

    def test_symlink_check_is_drift_with_no_auto_apply(
        self, tmp_path, macos_profile,
    ):
        """The self-heal must never be the thing that lands the ACE on an
        attacker-chosen path: report the drift, refuse to fix it."""
        victim = tmp_path / "victimbot"
        victim.mkdir()
        (tmp_path / BOT).symlink_to(victim)
        with _seam(False):
            check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is False          # rides pod_perms_drift_monitor's Signal
        assert check.apply is None        # ...but never auto-applies
        assert "SYMLINK" in check.detail

    def test_dangling_symlink_is_refused(self, tmp_path, macos_profile):
        """``Path.is_dir()`` would call this "missing" and pass; ``lstat``
        sees the link and refuses, so a plant that is armed later still
        surfaces."""
        (tmp_path / BOT).symlink_to(tmp_path / "nope")
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is False
        run.assert_not_called()

    def test_non_directory_entry_is_refused(self, tmp_path, linux_profile):
        (tmp_path / BOT).write_text("not a dir")
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is False
        run.assert_not_called()

    @pytest.mark.parametrize("bad_id", [
        "",                 # empty → Path(shared) / "" == shared root itself
        "..",               # the shared root's PARENT
        "../escape",        # traversal
        "/etc",             # absolute → Path.__truediv__ ROOT-ANCHORS, silently
        "a/b",              # nested
        "bot id",           # whitespace
        "-leading",         # not an identifier start
    ])
    def test_malformed_bot_id_is_refused(self, tmp_path, bad_id, linux_profile):
        """``bot_id`` is interpolated into a path that then receives a
        privileged ACL. Validation exists at provision_bot's validate stage;
        this re-asserts it at the sink, where the consequence lands."""
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, bad_id, BOT_USER) is False
        run.assert_not_called()

    @pytest.mark.parametrize("bad_user", [
        "",
        "atlas allow write,delete",   # would append verbs to the macOS ACE
        "atlas,root",                 # comma = ACE verb separator
        "atlas:extra",                # colon = ACE field separator
        "atlas evil",
    ])
    def test_malformed_bot_user_is_refused(self, tmp_path, bad_user, macos_profile):
        """``bot_user`` lands inside ``"user:<u> allow <verbs>"`` — a name
        carrying whitespace / ``,`` / ``:`` could change what is granted."""
        (tmp_path / BOT).mkdir()
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, bad_user) is False
        run.assert_not_called()

    def test_real_directory_still_grants(self, tmp_path, linux_profile):
        """The gate is a refusal, not a regression: the ordinary path is
        unchanged."""
        (tmp_path / BOT).mkdir()
        captured: list[list[str]] = []

        def _run(cmd, **kwargs):
            captured.append(list(cmd))
            return _OK()

        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run", _run):
            assert ensure_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER) is True
        assert len(captured) == 1
        assert captured[0][-1] == str(tmp_path / BOT)

    def test_apply_re_gates_after_the_check(self, tmp_path, macos_profile):
        """``ensure_pod_perms`` runs every check first and applies afterwards.
        A target that was a real dir at check time and a symlink by apply time
        must still be refused — so the apply re-runs the gate rather than
        closing over the path it validated earlier."""
        bot_dir = tmp_path / BOT
        bot_dir.mkdir()
        with _seam(False):
            check = check_bot_tier_prefs_acl(tmp_path, BOT, BOT_USER)
        assert check.ok is False and callable(check.apply)
        # ...attacker swaps the entry between check and apply.
        victim = tmp_path / "victimbot"
        victim.mkdir()
        bot_dir.rmdir()
        (tmp_path / BOT).symlink_to(victim)
        with _seam(False), \
             patch("evolve_admin.tier_prefs_acl.subprocess.run") as run:
            assert check.apply() is False
        run.assert_not_called()

"""
Unit tests for ensure_pod_perms — the idempotent pod-side perm enforcer added
after the 2026-04-25 apply.py-zombie incident.

Run with: python3 -m pytest packages/admin/tests/test_ensure_pod_perms.py -v
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.evo_socket_acl import (  # noqa: E402
    ADMIN_SOCKET_NAME,
    BOT_SOCKET_ACL_BITS,
    _check_bot_socket_acl,
    _ensure_bot_socket_acl,
)
from evolve_admin.deploy import (  # noqa: E402
    EVO_GATEWAY_USER,
    EVO_WRITE_ACL_PERMS,
    EVO_WRITE_SHARED_SUBDIRS,
    EVOLVE_OWNED_SHARED_SUBDIRS,
    EVOLVE_SERVICE_USER,
    POD_ACL_PERMS,
    POD_ALERTS_MODE,
    POD_EVOLVE_OWNED_DIR_MODE,
    POD_PROPOSALS_MODE,
    POD_PROPOSALS_SUBDIRS,
    _acl_user_present,
    _check_alerts_dir,
    _check_bot_acl,
    _check_dir_mode,
    _check_evo_write_acl,
    _check_evolve_owned_dir,
    _check_proposals_dir,
    _PermCheck,
    ensure_pod_perms,
    pod_acl_users,
)

# Sample ACL allow-set used by the per-bot tests below. Built the way
# `pod_acl_users` would build it on a typical pod: admin user (would
# come from SUDO_USER), evolve service user, security bot from
# network.security.botId. The literal here is for test clarity.
SAMPLE_ACL_USERS: tuple[str, ...] = ("opadmin", EVOLVE_SERVICE_USER, "secbot")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _network(bots: list[str]) -> dict:
    return {
        "networkId": "test-pod",
        "sharedDir": "/Users/Shared/evolve",
        "members": list(bots),
        "bots": {b: {"role": "member"} for b in bots},
    }


def _make_acl_lines(users: list[str], perms: str = POD_ACL_PERMS) -> str:
    """Build a fake `ls -lde` output with the given users in the ACL allow-set."""
    header = "drwx------+ 5 evolve staff 160 Apr 25 12:00 /Users/admin_bot/.openclaw"
    body = "\n".join(
        f" {i}: user:{u} allow {perms}" for i, u in enumerate(users)
    )
    return header + "\n" + body if body else header


# ── _acl_user_present ─────────────────────────────────────────────────────────

class TestAclUserPresent:
    """ACL parsing on `ls -lde` output."""

    def _patch_ls(self, stdout: str):
        proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        return patch("evolve_admin.deploy.subprocess.run", return_value=proc)

    def test_user_present_when_acl_includes_full_perm_set(self, tmp_path):
        path = tmp_path / "fake.openclaw"
        path.mkdir()
        with self._patch_ls(_make_acl_lines(["evolve", "opadmin"])):
            assert _acl_user_present(path, "evolve") is True
            assert _acl_user_present(path, "opadmin") is True

    def test_user_absent_returns_false(self, tmp_path):
        path = tmp_path / "fake.openclaw"
        path.mkdir()
        with self._patch_ls(_make_acl_lines(["evolve"])):
            assert _acl_user_present(path, "secbot") is False

    def test_returns_false_when_perms_subset_missing(self, tmp_path):
        """Acl entry exists but with weaker perms than the contract requires."""
        path = tmp_path / "fake.openclaw"
        path.mkdir()
        # only `list,search` — missing readattr/readextattr/readsecurity
        weak = _make_acl_lines(["evolve"], perms="list,search")
        with self._patch_ls(weak):
            assert _acl_user_present(path, "evolve") is False

    def test_returns_false_when_ls_fails(self, tmp_path):
        path = tmp_path / "fake.openclaw"
        path.mkdir()
        proc = MagicMock(returncode=1, stdout="", stderr="ls: error")
        with patch("evolve_admin.deploy.subprocess.run", return_value=proc):
            assert _acl_user_present(path, "evolve") is False

    def test_handles_extra_perms_gracefully(self, tmp_path):
        """Hand-added more permissive entry still satisfies the contract."""
        path = tmp_path / "fake.openclaw"
        path.mkdir()
        # superset of POD_ACL_PERMS — this is fine, the contract is "at least"
        rich = _make_acl_lines(["evolve"], perms="list,search,readattr,readextattr,readsecurity,write,delete")
        with self._patch_ls(rich):
            assert _acl_user_present(path, "evolve") is True

    def test_resolves_source_form_perms_on_directory_acl(self, tmp_path):
        """`chmod +a "… allow read,write,append,delete" <dir>` stores the
        translated names (list/add_file/add_subdirectory/delete). The check
        must resolve source-form expected perms before comparing, otherwise
        every evo-write ACL on {sharedDir}/proposals/ etc. looks like drift.
        Regression for the 2026-06-06 pod_perms_drift signal that fired even
        after a successful ensure-pod-perms apply.
        """
        path = tmp_path / "fake.proposals"
        path.mkdir()
        # What `ls -lde` actually prints after `chmod +a "user:evo allow
        # read,write,execute,delete,append,readattr,writeattr,readextattr,
        # writeextattr,readsecurity,file_inherit,directory_inherit" <dir>`.
        resolved = (
            "list,add_file,search,delete,add_subdirectory,readattr,writeattr,"
            "readextattr,writeextattr,readsecurity,file_inherit,directory_inherit"
        )
        ls_out = _make_acl_lines(["evo"], perms=resolved)
        with self._patch_ls(ls_out):
            assert _acl_user_present(path, "evo", EVO_WRITE_ACL_PERMS) is True

    def test_dir_resolution_still_flags_missing_perms(self, tmp_path):
        """Sanity: an evo ACL that only grants read should still fail a check
        that requires write — the resolution must not mask genuine drift.
        """
        path = tmp_path / "fake.proposals"
        path.mkdir()
        # Only `list` — equivalent to `read` only, missing the rest.
        ls_out = _make_acl_lines(["evo"], perms="list,readattr")
        with self._patch_ls(ls_out):
            assert _acl_user_present(path, "evo", EVO_WRITE_ACL_PERMS) is False


# ── _check_bot_acl ────────────────────────────────────────────────────────────

class TestCheckBotAcl:
    def test_skips_when_openclaw_dir_does_not_exist(self):
        # Bot not yet bootstrapped — should produce a single informational pass
        with patch.object(Path, "exists", return_value=False):
            checks = _check_bot_acl("freshbot", SAMPLE_ACL_USERS)
        assert len(checks) == 1
        assert checks[0].ok is True
        assert "not yet bootstrapped" in checks[0].detail

    def test_one_check_per_acl_user(self, tmp_path):
        # Post-platform_profile sweep the .openclaw path is built via the
        # evolve_config.user_home seam (deploy imports it as _user_home), so
        # rehome the bot's home onto tmp_path and create the real nested dir.
        (tmp_path / ".openclaw").mkdir()
        # all three users present in ACL
        ls_out = _make_acl_lines(list(SAMPLE_ACL_USERS))
        proc = MagicMock(returncode=0, stdout=ls_out, stderr="")
        with patch("evolve_admin.deploy._user_home", lambda u: tmp_path), \
             patch("evolve_admin.deploy.subprocess.run", return_value=proc):
            checks = _check_bot_acl("examplebot", SAMPLE_ACL_USERS)
        assert len(checks) == len(SAMPLE_ACL_USERS)
        assert all(c.ok for c in checks), [c.detail for c in checks]

    def test_drift_emits_fix_callable(self, tmp_path):
        (tmp_path / ".openclaw").mkdir()
        # only evolve in ACL — opadmin and secbot missing
        ls_out = _make_acl_lines(["evolve"])
        proc = MagicMock(returncode=0, stdout=ls_out, stderr="")
        with patch("evolve_admin.deploy._user_home", lambda u: tmp_path), \
             patch("evolve_admin.deploy.subprocess.run", return_value=proc):
            checks = _check_bot_acl("examplebot", SAMPLE_ACL_USERS)
        # drift entries have a fix; pass entries don't
        evolve_entry = next(c for c in checks if "evolve" in c.detail)
        opadmin_entry = next(c for c in checks if "opadmin" in c.detail)
        assert evolve_entry.ok is True
        assert evolve_entry.apply is None
        assert opadmin_entry.ok is False
        assert opadmin_entry.apply is not None
        assert "opadmin" in opadmin_entry.fix_description


class TestPodAclUsers:
    """The data-driven derivation of the .openclaw/ ACL allow-set."""

    def test_no_network_no_sudo_user_returns_just_evolve(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        assert pod_acl_users(None) == (EVOLVE_SERVICE_USER,)

    def test_includes_sudo_user_first(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "opadmin")
        users = pod_acl_users({})
        assert users[0] == "opadmin"
        assert EVOLVE_SERVICE_USER in users

    def test_includes_security_bot_from_network(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        users = pod_acl_users({"security": {"botId": "secbot"}})
        assert "secbot" in users

    def test_dedupes(self, monkeypatch):
        """If SUDO_USER coincidentally matches the security bot or evolve,
        the list should not have duplicates."""
        monkeypatch.setenv("SUDO_USER", EVOLVE_SERVICE_USER)
        users = pod_acl_users({"security": {"botId": EVOLVE_SERVICE_USER}})
        assert len(users) == len(set(users))

    def test_no_hardcoded_personal_names(self, monkeypatch):
        """Whatever SUDO_USER and network.security.botId are unset, the
        function must NOT smuggle a hardcoded admin name into the list."""
        monkeypatch.delenv("SUDO_USER", raising=False)
        users = pod_acl_users({"bots": {"someone": {}}})
        for forbidden in ("pod_admin_user", "personal_bot_user", "pod_admin"):
            assert forbidden not in users


# ── _check_dir_mode + _check_proposals_dir ────────────────────────────────────

class TestProposalsDir:
    """Mode-focused tests. Assertions filter to ``category == "dir-mode"`` so
    they're insulated from the owner check (which would fail in the test
    runner's account but is exercised separately in ``TestProposalsOwner``)."""

    def test_proposals_mode_constant_has_no_sticky_bit(self):
        """2026-06-08 regression: POD_PROPOSALS_MODE was 0o1777, which
        ``ensure_pod_perms`` reasserted on every pass, silently re-imposing
        the sticky bit that ``deploy_shared_dir`` already strips. Sticky
        blocks evo's MCP tools from atomic-renaming evolve-owned proposal
        files (the still_motivated "archive write failed [Errno 13]"
        symptom) — and it blocks regardless of the inherited evo write ACL,
        because sticky overrides the file's delete bit at the parent-dir
        level. The constant itself must lack the sticky bit so neither
        deploy nor the hourly drift-monitor recreates it."""
        assert POD_PROPOSALS_MODE & stat.S_ISVTX == 0, (
            f"POD_PROPOSALS_MODE={oct(POD_PROPOSALS_MODE)} has sticky bit "
            "set — every ensure_pod_perms pass will re-impose sticky on "
            "proposals/, blocking evo's atomic-rename of evolve-owned "
            "files (still_motivated archive sweep, action.proposal.*)"
        )
        # World-writable is still required (bot appliers, evo MCP, and the
        # admin daemon all write here). Pin that too — a future cleanup
        # that swung to 0o0755 would break bot-side writes silently.
        assert POD_PROPOSALS_MODE & 0o007 == 0o007, (
            f"POD_PROPOSALS_MODE={oct(POD_PROPOSALS_MODE)} is not "
            "world-writable; bot users (running apply.py and analyzer "
            "generators) won't be able to write proposals."
        )

    def test_proposals_root_drift_when_mode_wrong(self, tmp_path):
        proposals = tmp_path / "proposals"
        proposals.mkdir(mode=0o755)
        # subdirs exist with wrong mode too
        for s in POD_PROPOSALS_SUBDIRS:
            (proposals / s).mkdir(mode=0o755, exist_ok=True)
        mode_checks = [
            c for c in _check_proposals_dir(tmp_path) if c.category == "dir-mode"
        ]
        # All should report drift (mode 755 != POD_PROPOSALS_MODE)
        assert all(not c.ok for c in mode_checks), [c.detail for c in mode_checks]
        # Each check should have a fix
        assert all(c.apply is not None for c in mode_checks)

    def test_apply_strips_sticky_from_existing_proposals_dir(self, tmp_path):
        """2026-06-08 production state on the mini: proposals/pending/ et al
        were left at 0o1777 (sticky) by a prior pass when POD_PROPOSALS_MODE
        was 0o1777. Once the constant is updated to 0o0777, the next
        ensure_pod_perms apply must strip sticky from every existing dir —
        the recovery path, no separate cleanup script needed.

        Simulates the exact production state and asserts the auto-fix.
        """
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, 0o1777)
        for s in POD_PROPOSALS_SUBDIRS:
            sub = proposals / s
            sub.mkdir()
            os.chmod(sub, 0o1777)
        # Confirm the bad pre-state actually took.
        assert proposals.stat().st_mode & stat.S_ISVTX, "fixture setup failed"

        mode_checks = [
            c for c in _check_proposals_dir(tmp_path) if c.category == "dir-mode"
        ]
        # Every dir should be reported as drift now (sticky != POD_PROPOSALS_MODE).
        assert all(not c.ok for c in mode_checks)
        # Apply each fix and verify sticky is stripped.
        for c in mode_checks:
            assert c.apply is not None
            assert c.apply() is True, f"apply failed for {c.target}"
        assert proposals.stat().st_mode & 0o7777 == POD_PROPOSALS_MODE
        for s in POD_PROPOSALS_SUBDIRS:
            sub = proposals / s
            assert sub.stat().st_mode & 0o7777 == POD_PROPOSALS_MODE, (
                f"sticky bit not stripped from proposals/{s}: "
                f"mode={oct(sub.stat().st_mode & 0o7777)}"
            )

    def test_proposals_correct_mode_passes(self, tmp_path):
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, POD_PROPOSALS_MODE)
        for s in POD_PROPOSALS_SUBDIRS:
            sub = proposals / s
            sub.mkdir(exist_ok=True)
            os.chmod(sub, POD_PROPOSALS_MODE)
        mode_checks = [
            c for c in _check_proposals_dir(tmp_path) if c.category == "dir-mode"
        ]
        assert all(c.ok for c in mode_checks), [c.detail for c in mode_checks if not c.ok]

    def test_proposals_creates_missing_subdirs(self, tmp_path):
        # Only the root exists (with right mode); subdirs are missing
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, POD_PROPOSALS_MODE)
        mode_checks = [
            c for c in _check_proposals_dir(tmp_path) if c.category == "dir-mode"
        ]
        # root passes, subdirs all missing
        root_check = mode_checks[0]
        assert root_check.ok is True
        sub_checks = mode_checks[1:]
        assert all(not c.ok for c in sub_checks)
        assert all(c.detail == "missing" for c in sub_checks)
        assert all(c.apply is not None for c in sub_checks)


class TestProposalsOwner:
    """Owner-drift tests. The 2026-05-29 regression: personal_bot's gateway raced
    ahead of ``ensure_pod_perms`` on a fresh install, won the mkdir, and
    became dir-owner of ``proposals/`` + every subdir. Mode was correct,
    ACL was present (granted to evo), so neither ``_check_dir_mode``
    nor ``_check_evo_write_acl`` flagged drift. personal_bot-owned dirs
    then locked evolve out of cross-dir renames (the evolve user is
    neither file-owner, dir-owner, nor in the ACL — only evo is). The
    dir-owner-is-evolve invariant is the second-half guard the ACL alone
    doesn't cover."""

    def test_owner_correct_when_dir_owned_by_evolve(self, tmp_path):
        import pwd as _pwd
        cur_user = _pwd.getpwuid(os.getuid()).pw_name
        # Verify owner check passes when current-user matches expected.
        # Run against the test user as the "expected" owner so the test
        # works on any host without needing an `evolve` account.
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, POD_PROPOSALS_MODE)
        owner_check = deploy._check_dir_owner(proposals, cur_user)
        assert owner_check.ok is True
        assert owner_check.category == "dir-owner"
        assert owner_check.apply is None

    def test_owner_drift_when_dir_owned_by_someone_else(self, tmp_path):
        # Test against an arbitrary "expected" owner that isn't the test
        # runner; the check should flag drift and offer a fix.
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, POD_PROPOSALS_MODE)
        owner_check = deploy._check_dir_owner(proposals, "definitely-not-the-test-user")
        assert owner_check.ok is False
        assert owner_check.category == "dir-owner"
        assert "definitely-not-the-test-user" in owner_check.detail
        assert owner_check.apply is not None
        assert "chown" in owner_check.fix_description

    def test_owner_missing_dir_returns_ok_skip(self, tmp_path):
        # When the dir doesn't exist, owner check returns ok=True (skipped)
        # — the mode check's create branch handles creation; this pass just
        # has nothing to enforce yet. Next ensure_pod_perms pass catches it.
        nonexistent = tmp_path / "proposals" / "pending"
        owner_check = deploy._check_dir_owner(nonexistent, "evolve")
        assert owner_check.ok is True
        assert "not yet created" in owner_check.detail
        assert owner_check.apply is None

    def test_proposals_dir_includes_owner_checks_for_root_and_subdirs(self, tmp_path):
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        os.chmod(proposals, POD_PROPOSALS_MODE)
        for s in POD_PROPOSALS_SUBDIRS:
            sub = proposals / s
            sub.mkdir()
            os.chmod(sub, POD_PROPOSALS_MODE)
        owner_checks = [
            c for c in _check_proposals_dir(tmp_path) if c.category == "dir-owner"
        ]
        # One per dir: root + every standard subdir.
        assert len(owner_checks) == 1 + len(POD_PROPOSALS_SUBDIRS)
        # All target "evolve" as the expected owner.
        assert all("expected evolve" in c.detail or c.detail.startswith("owner=evolve")
                   for c in owner_checks), [c.detail for c in owner_checks]


# ── _check_alerts_dir ────────────────────────────────────────────────────────


class TestAlertsDir:
    """Mode-focused tests. ``_check_alerts_dir`` returns
    ``[mode_check, owner_check]``; these tests pick the mode check
    explicitly so they're insulated from owner drift (covered separately
    in ``TestAlertsOwner``).

    The bug from 2026-05-07: deploy_bot was chowning shared_dir/alerts/ to
    the most-recently-deployed bot. audit.py runs as the `evolve` user and
    couldn't write its CRITICAL dedup file. Mode 1777 is the right contract —
    sticky + world-writable makes writes mode-permitted; the owner check
    added 2026-05-29 closes the second half of the gap (cleanup ops still
    need dir-ownership to bypass the sticky bit).
    """

    def _mode_check(self, tmp_path):
        return next(
            c for c in _check_alerts_dir(tmp_path) if c.category == "dir-mode"
        )

    def test_missing_alerts_dir_drifts_with_creation_fix(self, tmp_path):
        check = self._mode_check(tmp_path)
        assert check.ok is False
        assert check.detail == "missing"
        assert check.apply is not None

    def test_wrong_mode_is_drift_with_fix(self, tmp_path):
        # The historical broken state: dir exists but at default umask mode.
        alerts = tmp_path / "alerts"
        alerts.mkdir(mode=0o755)
        check = self._mode_check(tmp_path)
        assert check.ok is False
        assert "0o755" in check.detail
        assert "0o1777" in check.detail
        assert check.apply is not None

    def test_correct_mode_passes(self, tmp_path):
        alerts = tmp_path / "alerts"
        alerts.mkdir()
        os.chmod(alerts, POD_ALERTS_MODE)
        check = self._mode_check(tmp_path)
        assert check.ok is True

    def test_apply_creates_with_correct_mode(self, tmp_path):
        check = self._mode_check(tmp_path)
        assert check.apply()
        alerts = tmp_path / "alerts"
        assert alerts.is_dir()
        assert (alerts.stat().st_mode & 0o7777) == POD_ALERTS_MODE

    def test_apply_fixes_wrong_mode(self, tmp_path):
        alerts = tmp_path / "alerts"
        alerts.mkdir(mode=0o755)
        check = self._mode_check(tmp_path)
        assert check.apply()
        assert (alerts.stat().st_mode & 0o7777) == POD_ALERTS_MODE


class TestAlertsOwner:
    """Owner-drift tests for alerts/, mirroring ``TestProposalsOwner``.

    Same regression shape as proposals/: a non-evolve writer could win the
    mkdir race on a fresh install and own the dir. Writes still succeed
    (mode 1777), so the daemon never notices; the failure mode would only
    surface during cleanup / repair, when evolve can't bypass the sticky
    bit because it's neither file-owner, dir-owner, nor in any ACL (alerts/
    has no ACL grants by design — pure mode-based access).
    """

    def _owner_check(self, tmp_path):
        return next(
            c for c in _check_alerts_dir(tmp_path) if c.category == "dir-owner"
        )

    def test_owner_drift_when_dir_owned_by_someone_else(self, tmp_path):
        alerts = tmp_path / "alerts"
        alerts.mkdir()
        os.chmod(alerts, POD_ALERTS_MODE)
        with patch.object(deploy, "_check_dir_owner",
                          return_value=_PermCheck(
                              category="dir-owner", target=str(alerts), ok=False,
                              detail="owner=somebot != expected evolve",
                              fix_description=f"chown evolve:wheel {alerts}",
                              apply=lambda: True,
                          )):
            check = self._owner_check(tmp_path)
        assert check.ok is False
        assert check.category == "dir-owner"
        assert "expected evolve" in check.detail
        assert check.apply is not None

    def test_owner_missing_dir_returns_ok_skip(self, tmp_path):
        # When alerts/ doesn't exist, owner check returns ok=True (skipped);
        # mode check's create branch handles creation; next pass repairs.
        check = self._owner_check(tmp_path)
        assert check.ok is True
        assert "not yet created" in check.detail

    def test_alerts_dir_includes_owner_check(self, tmp_path):
        alerts = tmp_path / "alerts"
        alerts.mkdir()
        os.chmod(alerts, POD_ALERTS_MODE)
        checks = _check_alerts_dir(tmp_path)
        categories = [c.category for c in checks]
        assert categories == ["dir-mode", "dir-owner"]


# ── _check_evolve_owned_dir ───────────────────────────────────────────────────


@pytest.mark.parametrize("subdir", EVOLVE_OWNED_SHARED_SUBDIRS)
class TestEvolveOwnedDirs:
    """The 2026-06-06 incident as a class, not a single dir: hand-placing
    the first files in any single-writer evolve subdir as a non-evolve user
    leaves that dir wrong-owned, and every subsequent atomic write from the
    daemon hits EACCES on tempfile.mkstemp. The original symptom was on
    config_intents/ (PR #2299), but the same shape applies to signals/,
    observations/, profiles/, generators/, watchdog/, calibration/,
    cost-settings/, cascade/, recovery/, handover-tokens/, and
    handover-prefs/ — every entry in EVOLVE_OWNED_SHARED_SUBDIRS.
    ensure_pod_perms must enforce the [dir-mode, dir-owner] pair on each.
    """

    def _mode_check(self, tmp_path, subdir):
        return next(
            c for c in _check_evolve_owned_dir(tmp_path, subdir)
            if c.category == "dir-mode"
        )

    def _owner_check(self, tmp_path, subdir):
        return next(
            c for c in _check_evolve_owned_dir(tmp_path, subdir)
            if c.category == "dir-owner"
        )

    def test_missing_dir_drifts_with_creation_fix(self, tmp_path, subdir):
        check = self._mode_check(tmp_path, subdir)
        assert check.ok is False
        assert check.detail == "missing"
        assert check.apply is not None

    def test_apply_creates_with_correct_mode(self, tmp_path, subdir):
        check = self._mode_check(tmp_path, subdir)
        assert check.apply()
        p = tmp_path / subdir
        assert p.is_dir()
        assert (p.stat().st_mode & 0o7777) == POD_EVOLVE_OWNED_DIR_MODE

    def test_correct_mode_passes(self, tmp_path, subdir):
        p = tmp_path / subdir
        p.mkdir()
        os.chmod(p, POD_EVOLVE_OWNED_DIR_MODE)
        check = self._mode_check(tmp_path, subdir)
        assert check.ok is True

    def test_owner_drift_uses_recursive_chown_apply(self, tmp_path, subdir):
        """The fix must recurse so existing wrong-owner files inside are
        normalized in the same pass — that's the differentiator from
        the generic _check_dir_owner apply, which is non-recursive.
        Single-writer = no other claimant on any file in the tree, so
        `chown -R` is safe.
        """
        p = tmp_path / subdir
        p.mkdir()
        os.chmod(p, POD_EVOLVE_OWNED_DIR_MODE)
        # Simulate the bug: dir owned by someone-not-evolve.
        with patch.object(deploy, "_check_dir_owner",
                          return_value=_PermCheck(
                              category="dir-owner", target=str(p), ok=False,
                              detail="owner=pod-admin-user != expected evolve",
                              fix_description=f"chown evolve:wheel {p}",
                              apply=lambda: True,  # generic non-recursive
                          )):
            with patch.object(deploy, "_ensure_evolve_owned_dir_perms",
                              return_value=True) as recursive_apply:
                check = self._owner_check(tmp_path, subdir)
                assert check.ok is False
                # Calling the patched apply must route through the
                # recursive helper, not the generic non-recursive one.
                assert check.apply()
                recursive_apply.assert_called_once_with(p)

    def test_owner_missing_dir_returns_ok_skip(self, tmp_path, subdir):
        # When the dir doesn't exist, owner check returns ok=True (skip);
        # mode check's create branch handles the initial mkdir.
        check = self._owner_check(tmp_path, subdir)
        assert check.ok is True
        assert "not yet created" in check.detail

    def test_check_includes_both_mode_and_owner(self, tmp_path, subdir):
        p = tmp_path / subdir
        p.mkdir()
        os.chmod(p, POD_EVOLVE_OWNED_DIR_MODE)
        checks = _check_evolve_owned_dir(tmp_path, subdir)
        categories = [c.category for c in checks]
        assert categories == ["dir-mode", "dir-owner"]


class TestEvolveOwnedSharedSubdirsConstant:
    """Sanity checks on the constant's contents.

    The list is load-bearing — every entry triggers a [dir-mode, dir-owner]
    check pair in ensure_pod_perms. New shared-dir stores that are
    single-writer evolve-only should be added here rather than getting
    ad-hoc check helpers.
    """

    def test_config_intents_is_in_list(self):
        """The dir that surfaced the class must still be covered."""
        assert "config_intents" in EVOLVE_OWNED_SHARED_SUBDIRS

    def test_signals_is_in_list(self):
        """signals/ was the historical owner-drift gap — the evo write ACL
        was checked, but the dir owner was not. This is the fix."""
        assert "signals" in EVOLVE_OWNED_SHARED_SUBDIRS

    def test_excludes_multi_writer_dirs(self):
        """proposals/ and alerts/ are mode 1777 multi-writer; they have
        their own check helpers. Including them here would set the wrong
        mode (0o755) and break per-bot writers."""
        assert "proposals" not in EVOLVE_OWNED_SHARED_SUBDIRS
        assert "alerts" not in EVOLVE_OWNED_SHARED_SUBDIRS

    def test_excludes_keystore(self):
        """keystore/'s owner is handled by _check_evo_write_acl's apply
        (recursive chown side effect). Including it here would create
        a second apply path for the same invariant."""
        assert "keystore" not in EVOLVE_OWNED_SHARED_SUBDIRS

    def test_no_duplicates(self):
        assert len(EVOLVE_OWNED_SHARED_SUBDIRS) == len(set(EVOLVE_OWNED_SHARED_SUBDIRS))


class TestConfigIntentsBackCompat:
    """The original config_intents PR introduced _check_config_intents_dir
    and _ensure_config_intents_perms; the audit PR generalized them. Keep
    the old names available as aliases for the brief window where both
    PRs are landing simultaneously.
    """

    def test_old_helper_still_callable(self, tmp_path):
        from evolve_admin.deploy import _check_config_intents_dir
        p = tmp_path / "config_intents"
        p.mkdir()
        os.chmod(p, POD_EVOLVE_OWNED_DIR_MODE)
        checks = _check_config_intents_dir(tmp_path)
        categories = [c.category for c in checks]
        assert categories == ["dir-mode", "dir-owner"]

    def test_old_constant_aliases_new_one(self):
        from evolve_admin.deploy import POD_CONFIG_INTENTS_MODE
        assert POD_CONFIG_INTENTS_MODE == POD_EVOLVE_OWNED_DIR_MODE


# ── ensure_pod_perms — end-to-end with mocks ──────────────────────────────────

class TestEnsurePodPerms:
    """Integration-ish — mock just the pod-wide checks, verify dispatch."""

    def _mock_ls(self, present_users: list[str]) -> MagicMock:
        ls_out = _make_acl_lines(present_users)
        return MagicMock(returncode=0, stdout=ls_out, stderr="")

    def test_check_only_does_not_invoke_fixes(self, tmp_path, monkeypatch):
        # Set up a network with one bot that we'll claim has no .openclaw dir.
        net = _network(["admin_bot"])
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        # No fixes should run since check_only=True. Track via counter.
        calls = {"apply": 0}
        with patch.object(deploy, "_check_cli_device_scopes",
                          return_value=_PermCheck(
                              category="plist", target="x", ok=False,
                              detail="missing",
                              fix_description="would regen",
                              apply=lambda: (calls.update(apply=calls["apply"] + 1), True)[1],
                          )):
            with patch.object(deploy, "_check_bot_acl", return_value=[]):
                result = ensure_pod_perms(
                    bot_id=None, network_path=np, check_only=True,
                )
        # At least one drift detected (the cli-device mock), but no apply ran.
        assert any(not c.ok for c in result.checks)
        assert calls["apply"] == 0
        assert result.applied == []

    def test_apply_invokes_fixes_for_drift_only(self, tmp_path, monkeypatch):
        net = _network(["admin_bot"])
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        # Hermetic: stub the real cli-device check (added to ensure_pod_perms
        # in #2706) so it can't reach ambient oc_cli_device state for a real
        # /Users/<bot> path — that dependency made this class shard-order
        # fragile (a prior test could leave drift, failing the apply phase).
        monkeypatch.setattr(
            deploy, "_check_cli_device_scopes",
            lambda bid, bot_user: _PermCheck(
                category="cli-device", target="t", ok=True, detail="ok"),
        )

        calls = {"plist_fix": 0, "ok_fix": 0}

        def _plist_fix():
            calls["plist_fix"] += 1
            return True

        def _ok_fix():  # this one should NEVER run — its check is ok=True
            calls["ok_fix"] += 1
            return True

        # Every passing check carries a fix that would prove the apply phase
        # skipped the ok-gate if it ever ran.
        ok_check = _PermCheck(category="x", target="x", ok=True, detail="ok",
                              apply=_ok_fix)
        with patch.object(deploy, "_check_cli_device_scopes",
                          return_value=_PermCheck(
                              category="plist", target="p", ok=False,
                              detail="missing", fix_description="regen",
                              apply=_plist_fix,
                          )):
            with patch.object(deploy, "_check_bot_acl", return_value=[]):
                with patch.object(deploy, "_check_proposals_dir",
                                  return_value=[ok_check]):
                    with patch.object(deploy, "_check_alerts_dir",
                                      return_value=[ok_check]):
                        with patch.object(deploy, "_check_evolve_owned_dir",
                                          return_value=[ok_check]):
                            with patch.object(deploy, "_check_cellar_perms",
                                              return_value=ok_check):
                                result = ensure_pod_perms(
                                    bot_id=None, network_path=np, check_only=False,
                                )

        assert calls["plist_fix"] == 1, "drift fix should have run exactly once"
        assert calls["ok_fix"] == 0, "passing-check fix must not be invoked"
        assert len(result.applied) == 1
        assert "plist/p" in result.applied[0]

    def test_no_drift_means_no_changes(self, tmp_path, monkeypatch):
        """Every check passes → apply phase is a complete no-op."""
        net = _network(["admin_bot"])
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        # Hermetic: stub the real cli-device check (added in #2706) — see the
        # note in test_apply_invokes_fixes_for_drift_only.
        monkeypatch.setattr(
            deploy, "_check_cli_device_scopes",
            lambda bid, bot_user: _PermCheck(
                category="cli-device", target="t", ok=True, detail="ok"),
        )

        ok_check = _PermCheck(
            category="x", target="y", ok=True, detail="all good",
        )
        with patch.object(deploy, "_check_cli_device_scopes", return_value=ok_check):
            with patch.object(deploy, "_check_bot_acl", return_value=[ok_check]):
                with patch.object(deploy, "_check_proposals_dir",
                                  return_value=[ok_check]):
                    with patch.object(deploy, "_check_alerts_dir",
                                      return_value=[ok_check]):
                        with patch.object(deploy, "_check_evolve_owned_dir",
                                          return_value=[ok_check]):
                            with patch.object(deploy, "_check_store_lock_file",
                                              return_value=ok_check):
                                result = ensure_pod_perms(
                                    bot_id=None, network_path=np, check_only=False,
                                )
        assert result.applied == []
        assert result.errors == []
        assert all(c.ok for c in result.checks)

    def test_idempotency_second_pass_is_noop(self, tmp_path, monkeypatch):
        """Calling apply twice in a row should produce zero changes the second time."""
        net = _network(["admin_bot"])
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        # Hermetic: stub the real cli-device check (added in #2706) — see the
        # note in test_apply_invokes_fixes_for_drift_only.
        monkeypatch.setattr(
            deploy, "_check_cli_device_scopes",
            lambda bid, bot_user: _PermCheck(
                category="cli-device", target="t", ok=True, detail="ok"),
        )

        # First call: a check that returns ok=False the first time but flips
        # to ok=True after fix runs (modeling a real drift+fix).
        state = {"fixed": False}

        def _make_check():
            return _PermCheck(
                category="x", target="y", ok=state["fixed"],
                detail="missing" if not state["fixed"] else "ok",
                fix_description="repair",
                apply=lambda: (state.update(fixed=True), True)[1] if not state["fixed"] else True,
            )

        with patch.object(deploy, "_check_cli_device_scopes", side_effect=lambda bid, u: _make_check()):
            with patch.object(deploy, "_check_bot_acl", side_effect=lambda u, allow=None: [_make_check()]):
                with patch.object(deploy, "_check_proposals_dir",
                                  side_effect=lambda d: [_make_check()]):
                    with patch.object(deploy, "_check_alerts_dir",
                                      side_effect=lambda d: [_make_check()]):
                        with patch.object(deploy, "_check_evolve_owned_dir",
                                          side_effect=lambda d, sd: [_make_check()]):
                            with patch.object(deploy, "_check_cellar_perms",
                                              side_effect=_make_check):
                                with patch.object(deploy, "_check_store_lock_file",
                                                  side_effect=lambda d, sd: _make_check()):
                                    r1 = ensure_pod_perms(
                                        bot_id=None, network_path=np, check_only=False,
                                    )
                                    r2 = ensure_pod_perms(
                                        bot_id=None, network_path=np, check_only=False,
                                    )

        # First pass: must have applied at least one fix.
        assert len(r1.applied) >= 1
        # Second pass: state is now "fixed=True", every check returns ok, no apply runs.
        assert r2.applied == []
        assert r2.errors == []


# ── _check_evo_write_acl ─────────────────────────────────────────────────────
#
# Post-evo-account-separation (spec-evo-account-separation-2026-05-25.md +
# 2026-05-25 dismiss bug): {sharedDir}/{proposals,signals}/ must grant the
# `evo` macOS user write+inherit ACL so evo's MCP tools can rename/delete
# state files in evolve-owned dirs. Skipped when evo isn't a separate user.

class TestCheckEvoWriteAcl:
    """The drift detector for the evo write ACL on shared proposal/signal dirs."""

    def _patch_ls(self, stdout: str):
        proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        return patch("evolve_admin.deploy.subprocess.run", return_value=proc)

    def test_evo_user_missing_returns_informational_pass(self, tmp_path):
        """Fresh pod, no Phase-E.2.a: check no-ops, doesn't claim drift."""
        (tmp_path / "proposals").mkdir()
        with patch("evolve_admin.deploy._evo_user_exists", return_value=False):
            check = _check_evo_write_acl(tmp_path, "proposals")
        assert check.ok is True
        assert "evo macOS user does not exist" in check.detail
        assert check.apply is None

    def test_target_dir_missing_returns_informational_pass(self, tmp_path):
        """proposals/signals dir not yet created: nothing to enforce, no fix."""
        with patch("evolve_admin.deploy._evo_user_exists", return_value=True):
            check = _check_evo_write_acl(tmp_path, "signals")
        assert check.ok is True
        assert "dir not yet created" in check.detail
        assert check.apply is None

    def test_acl_present_passes(self, tmp_path):
        target = tmp_path / "proposals"
        target.mkdir()
        # Full evo write ACE present — the canonical post-fix state. On a
        # directory, `chmod +a` translates `read,write,append` into the
        # directory-equivalent names (list/add_file/add_subdirectory), so
        # `ls -lde` prints the resolved form below — NOT the source form
        # we wrote on the command line. Pre-2026-06-06 this test used the
        # source form and passed only because the literal-set check was
        # broken; the bug masked itself.
        resolved = (
            "list,add_file,search,delete,add_subdirectory,readattr,writeattr,"
            "readextattr,writeextattr,readsecurity,file_inherit,directory_inherit"
        )
        ls_out = _make_acl_lines([EVO_GATEWAY_USER], perms=resolved)
        with patch("evolve_admin.deploy._evo_user_exists", return_value=True):
            with self._patch_ls(ls_out):
                check = _check_evo_write_acl(tmp_path, "proposals")
        assert check.ok is True
        assert f"user:{EVO_GATEWAY_USER}" in check.detail

    def test_acl_missing_is_drift_with_fix(self, tmp_path):
        target = tmp_path / "proposals"
        target.mkdir()
        # Only evolve is in the ACL — evo missing → drift.
        ls_out = _make_acl_lines(["evolve"], perms=POD_ACL_PERMS)
        with patch("evolve_admin.deploy._evo_user_exists", return_value=True):
            with self._patch_ls(ls_out):
                check = _check_evo_write_acl(tmp_path, "proposals")
        assert check.ok is False
        assert "missing" in check.detail
        assert callable(check.apply)
        # Fix description should mention both the chown and the chmod +a steps
        # so operators inspecting --check-only output can copy-paste-debug.
        assert "chown" in check.fix_description
        assert "chmod" in check.fix_description
        assert EVO_GATEWAY_USER in check.fix_description

    def test_acl_with_only_read_perms_is_drift(self, tmp_path):
        """Evo present with read-only ACE → still drift; write contract not met."""
        target = tmp_path / "proposals"
        target.mkdir()
        # Evo has read perms only — not enough to rename/delete state files.
        ls_out = _make_acl_lines([EVO_GATEWAY_USER], perms=POD_ACL_PERMS)
        with patch("evolve_admin.deploy._evo_user_exists", return_value=True):
            with self._patch_ls(ls_out):
                check = _check_evo_write_acl(tmp_path, "proposals")
        assert check.ok is False

    def test_subdirs_covered_by_constant(self):
        """Sanity: the constant covers exactly the two stores evo writes to.

        If a new shared-dir store gets cross-user-write requirements, expand
        EVO_WRITE_SHARED_SUBDIRS rather than adding ad-hoc patches.
        """
        assert "proposals" in EVO_WRITE_SHARED_SUBDIRS
        assert "signals" in EVO_WRITE_SHARED_SUBDIRS


# ── _check_bot_socket_acl / _ensure_bot_socket_acl ────────────────────────────
#
# admin-daemon.sock shared-bot-group connect ACL (connect-EACCES pod-wide on the
# Linux pod, 2026-06-25). Every bot gateway runs as a user in the shared
# `evolve-bots` group but NOT in the socket-owning `evolve` group, so mode-0660
# leaves it on `other` = `r--` and a unix-socket connect() (a WRITE on the
# inode) hits EACCES. A NAMED GROUP write ACE, applied OWNER-DIRECT (no sudo),
# fixes every bot at once without widening `other`. Self-heal backstop to the
# at-bind grant in web.unix_socket_server. macOS = no-op (group ownership).

import contextlib

from platform_profile import LINUX, MACOS, set_profile


@pytest.fixture
def linux_profile():
    """Pin the platform profile to LINUX (the OS where the named group ACE is
    load-bearing) for the body of the test, then restore autodetection."""
    set_profile(LINUX)
    try:
        yield LINUX
    finally:
        set_profile(None)


@contextlib.contextmanager
def _socket_file_at(path: Path):
    """Make `path.is_socket()` return True for the duration of the block.

    A real AF_UNIX bind would exceed the ~104-char sun_path limit under
    pytest's deep tmp dirs, so we patch the predicate instead — these tests
    drive the Perms seam through a fake, not the kernel, so an inode of the
    exact socket type isn't needed. The file is touched so it exists on disk
    (the check guards on is_socket(), not exists(), but a present path keeps
    the fixture honest).
    """
    path.touch()
    orig = Path.is_socket

    def _is_socket(self):  # noqa: ANN001
        return str(self) == str(path) or orig(self)

    with patch.object(Path, "is_socket", _is_socket):
        yield path


class _FakeSeam:
    """Minimal Perms-seam double answering acl_group_effective presence.

    The apply path (_ensure_bot_socket_acl) is OWNER-DIRECT subprocess, NOT
    routed through the seam — so this double only needs the CHECK method, not a
    grant().
    """

    def __init__(self, present: bool = False):
        self._present = present

    def acl_group_effective(self, path, group, required):
        return self._present


class TestCheckBotSocketAcl:
    """Drift detector + apply for the bot-group write(connect) ACE on the socket."""

    def test_macos_returns_informational_pass(self, tmp_path):
        """macOS: bots reach the socket via `staff` group OWNERSHIP, no named
        ACE — informational pass, never claims drift, never an apply."""
        set_profile(MACOS)
        try:
            with _socket_file_at(tmp_path / ADMIN_SOCKET_NAME):
                check = _check_bot_socket_acl(tmp_path)
        finally:
            set_profile(None)
        assert check.ok is True
        assert "macOS" in check.detail
        assert check.apply is None

    def test_socket_not_bound_returns_informational_pass(self, tmp_path, linux_profile):
        """No socket file (TCP-only or daemon not yet bound): nothing to enforce."""
        check = _check_bot_socket_acl(tmp_path)
        assert check.ok is True
        assert "not bound" in check.detail
        assert check.apply is None

    def test_acl_present_passes(self, tmp_path, linux_profile):
        seam = _FakeSeam(present=True)
        with _socket_file_at(tmp_path / ADMIN_SOCKET_NAME):
            with patch("evolve_admin.evo_socket_acl._get_perms", return_value=seam):
                check = _check_bot_socket_acl(tmp_path)
        assert check.ok is True
        assert "group:evolve-bots" in check.detail
        assert "write(connect)" in check.detail

    def test_acl_missing_is_drift_with_owner_direct_fix(self, tmp_path, linux_profile):
        seam = _FakeSeam(present=False)
        with _socket_file_at(tmp_path / ADMIN_SOCKET_NAME):
            with patch("evolve_admin.evo_socket_acl._get_perms", return_value=seam):
                check = _check_bot_socket_acl(tmp_path)
        assert check.ok is False
        assert "missing" in check.detail
        assert callable(check.apply)
        # Fix is an OWNER-DIRECT setfacl group grant — no sudo, no chown (the
        # socket is owned by the live admin daemon; re-chowning it is a footgun).
        fix = check.fix_description
        assert f"setfacl -m g:evolve-bots:{BOT_SOCKET_ACL_BITS}" in fix
        assert "no sudo" in fix
        assert "chown" not in fix

    def test_apply_grants_group_ace_owner_direct_no_sudo(self, tmp_path, linux_profile):
        """The apply path runs ``setfacl -m g:evolve-bots:rwx`` OWNER-DIRECT —
        argv must not begin with sudo, must be the platform setfacl binary, and
        must name the shared bot GROUP (never u:evo, never widening other)."""
        target = tmp_path / ADMIN_SOCKET_NAME
        target.touch()
        captured: list[list[str]] = []

        class _OK:
            returncode = 0
            stdout = ""
            stderr = ""

        def _run(cmd, **kwargs):
            captured.append(list(cmd))
            return _OK()

        with patch("evolve_admin.evo_socket_acl.subprocess.run", _run):
            ok = _ensure_bot_socket_acl(target)
        assert ok is True
        assert len(captured) == 1
        argv = captured[0]
        assert "sudo" not in argv
        assert argv == [LINUX.setfacl, "-m", "g:evolve-bots:rwx", str(target)]

    def test_socket_acl_re_asserted_after_self_heal_pass(self, tmp_path, linux_profile):
        """End-to-end self-heal: a drift check whose apply re-grants the ACE,
        and a subsequent check that now passes (simulating a fixed socket)."""
        seam = _FakeSeam(present=False)

        class _OK:
            returncode = 0
            stdout = ""
            stderr = ""

        def _run(cmd, **kwargs):
            seam._present = True  # the owner-direct setfacl took effect
            return _OK()

        with _socket_file_at(tmp_path / ADMIN_SOCKET_NAME):
            with patch("evolve_admin.evo_socket_acl._get_perms", return_value=seam), \
                 patch("evolve_admin.evo_socket_acl.subprocess.run", _run):
                first = _check_bot_socket_acl(tmp_path)
                assert first.ok is False
                assert first.apply() is True
                second = _check_bot_socket_acl(tmp_path)
        assert second.ok is True

    def test_ensure_pod_perms_includes_socket_check(self, tmp_path, monkeypatch, linux_profile):
        """The socket check is wired into the pod-wide ensure_pod_perms pass."""
        seam = _FakeSeam(present=True)
        np = tmp_path / "network.json"
        import json
        np.write_text(json.dumps({
            "networkId": "t", "sharedDir": str(tmp_path), "members": [],
        }))
        # Neutralize the heavy unrelated pod-wide checks; we only assert the
        # socket check appears in the result.
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        with _socket_file_at(tmp_path / ADMIN_SOCKET_NAME):
            with patch("evolve_admin.evo_socket_acl._get_perms", return_value=seam):
                res = ensure_pod_perms(network_path=np, check_only=True)
        socket_checks = [
            c for c in res.checks
            if c.target.endswith(ADMIN_SOCKET_NAME) and c.category == "ACL"
        ]
        assert len(socket_checks) == 1
        assert socket_checks[0].ok is True


class TestEnsurePodPermsIncludesEvoChecks:
    """Verify ensure_pod_perms emits one evo-ACL check per EVO_WRITE_SHARED_SUBDIRS entry."""

    def test_evo_acl_checks_added_to_pod_wide_pass(self, tmp_path, monkeypatch):
        net = _network([])
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")

        ok_check = _PermCheck(category="x", target="y", ok=True, detail="ok")
        # Count how many times _check_evo_write_acl is invoked + with which subdirs.
        calls: list[str] = []
        def _spy(shared_dir, subdir):
            calls.append(subdir)
            return ok_check

        with patch.object(deploy, "_check_proposals_dir", return_value=[ok_check]):
            with patch.object(deploy, "_check_alerts_dir", return_value=[ok_check]):
                with patch.object(deploy, "_check_evo_write_acl", side_effect=_spy):
                    ensure_pod_perms(
                        bot_id=None, network_path=np, check_only=True,
                    )
        # One call per subdir in the constant; subdirs match exactly.
        assert calls == list(EVO_WRITE_SHARED_SUBDIRS)


# ── app-cron PATH self-heal wiring ────────────────────────────────────────────
#
# repair_app_cron_env_paths heals launchd app-cron plists that lack a PATH
# (openclaw exit-127 silent-non-delivery, 2026-06-22). It used to run ONLY from
# the manual `application repair-app-crons` CLI, so a stale plist stayed broken
# until an operator ran it by hand (atlas daily-digest was dark ~10 days). It is
# now wired into the per-deploy self-heal (ensure_pod_perms). These tests prove
# ensure_pod_perms invokes the heal with the bot(s) in scope + check_only
# threaded through, and that a heal failure is non-fatal.

class TestAppCronPathSelfHealWiring:
    _HEAL = "evolve_admin.applications.install_helpers.repair_app_cron_env_paths"

    def _prep(self, tmp_path, monkeypatch, members):
        net = _network(members)
        np = tmp_path / "network.json"
        np.write_text(__import__("json").dumps(net))
        # Neutralize the heavy unrelated pod-wide + per-bot checks so the test
        # exercises only the app-cron heal wiring.
        monkeypatch.setattr(deploy, "POD_CELLAR_ROOT", tmp_path / "no-cellar")
        monkeypatch.setattr(deploy, "get_bot_user", lambda b, n: b)
        for name in ("_check_bot_acl",):
            monkeypatch.setattr(deploy, name, lambda *a, **k: [])
        ok = _PermCheck(category="x", target="y", ok=True)
        monkeypatch.setattr(deploy, "_check_cli_device_scopes", lambda *a, **k: ok)
        monkeypatch.setattr(deploy._secret_perms, "check_evolve_access",
                            lambda *a, **k: ok)
        monkeypatch.setattr(deploy._secret_perms, "check_bot_secret_modes",
                            lambda *a, **k: [])
        monkeypatch.setattr(deploy._secret_perms, "check_bot_tiers_ownership",
                            lambda *a, **k: [])
        return np

    def test_single_bot_deploy_invokes_heal_with_that_bot(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas", "ledger"])
        heal = MagicMock(return_value={"checked": 1, "missing": [], "healed": [],
                                       "failed": []})
        with patch(self._HEAL, heal):
            res = deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                          check_only=False)
        heal.assert_called_once()
        args, kwargs = heal.call_args
        assert args[0] == ["atlas"]              # scoped to the deployed bot only
        assert kwargs["check_only"] is False
        assert res is not None

    def test_check_only_threaded_through(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas"])
        heal = MagicMock(return_value={"checked": 0, "missing": [], "healed": [],
                                       "failed": []})
        with patch(self._HEAL, heal):
            deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                    check_only=True)
        assert heal.call_args.kwargs["check_only"] is True

    def test_pod_wide_passes_full_member_list(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas", "ledger"])
        heal = MagicMock(return_value={"checked": 0, "missing": [], "healed": [],
                                       "failed": []})
        with patch(self._HEAL, heal):
            deploy.ensure_pod_perms(bot_id=None, network_path=np, check_only=False)
        assert heal.call_args.args[0] == ["atlas", "ledger"]

    def test_healed_and_missing_surface_in_checks(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas"])
        heal = MagicMock(return_value={
            "checked": 2, "missing": ["ai.evolve.atlas.digest"],
            "healed": ["ai.evolve.atlas.digest"], "failed": []})
        with patch(self._HEAL, heal):
            res = deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                          check_only=False)
        healed = [c for c in res.checks
                  if c.category == "app-cron-path" and c.ok]
        assert any(c.target == "ai.evolve.atlas.digest" for c in healed)

    def test_check_only_missing_surfaces_as_drift(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas"])
        heal = MagicMock(return_value={
            "checked": 1, "missing": ["ai.evolve.atlas.digest"],
            "healed": [], "failed": []})
        with patch(self._HEAL, heal):
            res = deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                          check_only=True)
        drift = [c for c in res.checks
                 if c.category == "app-cron-path" and not c.ok]
        assert any(c.target == "ai.evolve.atlas.digest" for c in drift)

    def test_failed_heal_surfaces_error_and_is_nonfatal(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas"])
        heal = MagicMock(return_value={
            "checked": 1, "missing": ["ai.evolve.atlas.digest"], "healed": [],
            "failed": [{"label": "ai.evolve.atlas.digest", "error": "boom"}]})
        with patch(self._HEAL, heal):
            res = deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                          check_only=False)
        assert any("app-cron-path" in e and "boom" in e for e in res.errors)
        assert any(c.category == "app-cron-path" and not c.ok
                   for c in res.checks)

    def test_heal_exception_is_nonfatal(self, tmp_path, monkeypatch):
        np = self._prep(tmp_path, monkeypatch, ["atlas"])
        heal = MagicMock(side_effect=RuntimeError("kaboom"))
        with patch(self._HEAL, heal):
            # Must NOT raise — a heal failure never aborts a deploy.
            res = deploy.ensure_pod_perms(bot_id="atlas", network_path=np,
                                          check_only=False)
        assert res is not None
        assert any(c.category == "app-cron-path" for c in res.checks)


# ── _check_store_lock_file ───────────────────────────────────────────────────
#
# 7.1 Phase A (spec-state-store-and-deploy-resilience-2026-06-10.md §1.4):
# the store lock files must be pre-created evolve-owned 0666 — bot users
# take the signals lock transitively but can't create files in the
# evolve-owned signals/ dir. Repair is strictly in place (never
# replace/rename — that splits flock across two inodes).

class TestCheckStoreLockFile:
    def test_missing_lock_file_flags_drift(self, tmp_path):
        (tmp_path / "proposals").mkdir()
        check = deploy._check_store_lock_file(tmp_path, "proposals")
        assert check.ok is False
        assert check.detail == "missing"
        assert callable(check.apply)

    def test_correct_lock_file_passes(self, tmp_path, monkeypatch):
        import pwd as _pwd
        (tmp_path / "signals").mkdir()
        lock = tmp_path / "signals" / deploy.STORE_LOCK_FILE_NAME
        lock.touch()
        os.chmod(lock, deploy.STORE_LOCK_MODE)
        # Can't chown in a test — pretend the current uid resolves to evolve.
        real_struct = _pwd.getpwuid(os.getuid())
        fake = MagicMock()
        fake.pw_name = "evolve"
        monkeypatch.setattr(deploy.pwd, "getpwuid", lambda uid: fake)
        check = deploy._check_store_lock_file(tmp_path, "signals")
        assert check.ok is True, check.detail
        assert "0o666" in check.detail

    def test_wrong_owner_with_correct_mode_passes(self, tmp_path, monkeypatch):
        """Owner is informational: flock only needs the 0666 read access.
        A lazily-created lock owned by root/evo must not page hourly."""
        (tmp_path / "signals").mkdir()
        lock = tmp_path / "signals" / deploy.STORE_LOCK_FILE_NAME
        lock.touch()
        os.chmod(lock, deploy.STORE_LOCK_MODE)
        fake = MagicMock()
        fake.pw_name = "root"
        monkeypatch.setattr(deploy.pwd, "getpwuid", lambda uid: fake)
        check = deploy._check_store_lock_file(tmp_path, "signals")
        assert check.ok is True, check.detail

    def test_wrong_mode_reports_drift(self, tmp_path, monkeypatch):
        (tmp_path / "signals").mkdir()
        lock = tmp_path / "signals" / deploy.STORE_LOCK_FILE_NAME
        lock.touch()
        os.chmod(lock, 0o644)
        fake = MagicMock()
        fake.pw_name = "evolve"
        monkeypatch.setattr(deploy.pwd, "getpwuid", lambda uid: fake)
        check = deploy._check_store_lock_file(tmp_path, "signals")
        assert check.ok is False
        assert "0o644" in check.detail
        assert callable(check.apply)

    def test_ensure_repairs_in_place_never_recreates(self, tmp_path, monkeypatch):
        """An existing lock file's inode must survive the repair —
        replacing it would split flock mutual exclusion."""
        lock = tmp_path / ".store.lock"
        lock.touch()
        ino_before = lock.stat().st_ino
        ran = []

        def _fake_run(cmd, **kw):
            ran.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(deploy.subprocess, "run", _fake_run)
        assert deploy._ensure_store_lock_file(lock) is True
        assert lock.stat().st_ino == ino_before
        # chown + chmod only; no rm/mv/cp of the lock path.
        joined = [" ".join(c) for c in ran]
        assert any("/usr/sbin/chown" in c for c in joined)
        assert any("/bin/chmod" in c for c in joined)
        assert not any(("/bin/rm" in c) or ("/bin/mv" in c) or ("/bin/cp" in c) for c in joined)

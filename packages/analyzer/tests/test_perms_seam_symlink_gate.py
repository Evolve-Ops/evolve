"""test_perms_seam_symlink_gate.py — no privileged ACL command through a redirect.

The perms seam's argv are ROOT commands (``sudo setfacl`` on Linux, ``sudo
/bin/chmod +a`` / ``-N`` on macOS) and every one of them FOLLOWS a symlink given
as its path argument. That was verified live on the Ubuntu 24.04 pod (acl 2.3.2)
rather than read off a man page:

    $ setfacl -m u:nobody:rwx outside/victim && chmod 600 outside/victim
    $ ln -s outside linkroot
    $ getfacl -p outside | grep mask      →  mask::---
    $ setfacl -m m::rwX linkroot          →  rc=0
    $ getfacl -p outside | grep mask      →  mask::rwx      ← the victim widened

The reachable leg is the hourly one: ``secret_config_perms.reassert_evolve_access``
calls ``reassert_mask`` on ``<oc>/agents``, ``<oc>/agents/main``,
``<oc>/agents/main/agent``, ``<oc>/workspace`` and ``<oc>/workspace/.git`` — the
exact intermediate components #3601 proved a bot can replace, since the bot owns
every directory from ``.openclaw`` down.

Two halves, and the second matters as much as the first:

* **security** — a planted component issues NO ``setfacl``/``getfacl``/``chmod``
  argv at all. Asserted on the RECORDED ARGV, never on the returned bool: a
  gate that returns False after already spawning the root command has not
  closed anything.
* **availability** — a legitimate real-directory tree still gets its mask
  re-widened. A false refusal here starves every evolve read on the pod, so the
  legitimate shapes are pinned just as hard as the attack ones.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from evolve_util import assert_no_symlink_in_path
from runtime.perms import GETFACL, SETFACL, LinuxPerms, MacOSPerms

# ── harness ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """Same booby-trap as test_perms_seam: a real spawn would mutate host ACLs."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(f"a REAL subprocess spawn was attempted. args={a!r}")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# A getfacl block for a path whose mask IS capping an entry — the only shape
# that makes reassert_mask decide to fire the privileged setfacl. Without a
# `#effective:` annotation `_mask_caps_entries` short-circuits and the test
# would pass vacuously.
_CAPPED_GETFACL = (
    "# file: x\n"
    "# owner: root\n"
    "# group: root\n"
    "user::rwx\n"
    "user:evolve:r-x\t#effective:---\n"
    "group::---\n"
    "mask::---\n"
    "other::---\n"
)


@pytest.fixture
def recorded_argv():
    """(runner, calls) where calls is the list of argv the seam tried to spawn.

    The runner answers every ``getfacl`` with a capped-mask block, so any test
    that reaches the privileged step actually reaches it.
    """
    calls: "list[list[str]]" = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == GETFACL:
            if "-R" in cmd:
                return _Result(0, f"# file: {cmd[-1]}\nmask::---\nuser:evolve:r-x\t#effective:---\n\n")
            return _Result(0, _CAPPED_GETFACL)
        return _Result(0)

    return run, calls


def _pod(tmp_path: Path) -> Path:
    """A realistic bot ``.openclaw`` with the nested secret-relpath tree."""
    oc = tmp_path / "home" / "team_bot_a" / ".openclaw"
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    (oc / "workspace" / ".git").mkdir(parents=True)
    (oc / "logs").mkdir()
    return oc


def _victim_tree(tmp_path: Path, name: str = "victim") -> Path:
    v = tmp_path / name
    v.mkdir()
    (v / "inner").mkdir()
    return v


# The parent dirs `reassert_evolve_access` sweeps once an hour — the live target
# list, kept in the same shape as `_secret_relpath_parent_dirs` produces.
_SWEPT_RELPATHS = ["agents", "agents/main", "agents/main/agent",
                   "workspace", "workspace/.git", "logs"]


# ── 1. the primitive ─────────────────────────────────────────────────────────


class TestAssertNoSymlinkInPath:
    def test_real_tree_passes(self, tmp_path):
        oc = _pod(tmp_path)
        for rel in _SWEPT_RELPATHS:
            assert_no_symlink_in_path(oc / rel)  # no raise

    def test_leaf_symlink_refused(self, tmp_path):
        oc = _pod(tmp_path)
        link = oc / "planted"
        link.symlink_to(_victim_tree(tmp_path))
        with pytest.raises(PermissionError, match="SYMLINK"):
            assert_no_symlink_in_path(link)

    @pytest.mark.parametrize(
        "plant_at, leaf",
        [
            ("agents", "agents/main/agent"),
            ("agents/main", "agents/main/agent"),
            ("workspace", "workspace/.git"),
        ],
    )
    def test_intermediate_symlink_refused(self, tmp_path, plant_at, leaf):
        """The residual the chip is about: the leaf and its parent are perfectly
        real objects, so a check that lstats only those two sails through."""
        oc = _pod(tmp_path)
        # A victim tree shaped exactly like the real one below the plant point,
        # so the leaf resolves to a genuine, existing directory.
        suffix = Path(leaf).relative_to(plant_at)
        victim = _victim_tree(tmp_path, f"victim-{plant_at.replace('/', '-')}")
        (victim / suffix).mkdir(parents=True)
        _rmtree(oc / plant_at)
        (oc / plant_at).symlink_to(victim)

        assert (oc / leaf).is_dir() and not (oc / leaf).is_symlink()  # deceptive
        with pytest.raises(PermissionError, match="SYMLINK"):
            assert_no_symlink_in_path(oc / leaf)

    def test_dangling_symlink_refused(self, tmp_path):
        """``lstat`` sees the link even though ``exists()`` says no — the state a
        plant is in before the attacker creates the target."""
        oc = _pod(tmp_path)
        link = oc / "dangling"
        link.symlink_to(tmp_path / "not-there-yet")
        with pytest.raises(PermissionError, match="SYMLINK"):
            assert_no_symlink_in_path(link)

    def test_dotdot_component_is_refused(self, tmp_path):
        """``abspath`` normalizes ``a/link/../b`` LEXICALLY while the kernel
        resolves it PHYSICALLY through ``link``, so a ``..`` would let a
        component slip past the walk. No caller builds one; refusing removes the
        discrepancy instead of reasoning about it."""
        oc = _pod(tmp_path)
        link = oc / "planted"
        link.symlink_to(_victim_tree(tmp_path, "v-dotdot"))
        # Lexically this is `<oc>/inner`, a real directory — physically it is
        # `<victim>/..`/inner, i.e. reached THROUGH the planted link.
        with pytest.raises(PermissionError, match=r"\.\."):
            assert_no_symlink_in_path(oc / "planted" / ".." / "agents")

    def test_hard_linked_leaf_is_refused(self, tmp_path):
        """The variant that needs no symlink at all, and that the walk above
        cannot see: a hard link IS a real regular file, and ``lstat`` reports
        the victim inode's own uid and mode because there is no indirection.

        Reachable at the FILE-targeted ACL calls — ``set_evolve_read_acl``'s
        ``workspace/`` retro-grant loop selects members with ``is_file()``, so it
        picks a planted hard link up as an ordinary member and hands it to a root
        ``chmod +a "evolve allow …,write,delete,…"``. On macOS an unprivileged
        user can hard-link a file it neither owns nor can read (#3597)."""
        oc = _pod(tmp_path)
        victim = tmp_path / "victim-inode"
        victim.write_text("VICTIM")
        planted = oc / "workspace" / "AGENTS.md"
        os.link(victim, planted)

        assert planted.is_file() and not planted.is_symlink()  # the deceptive shape
        with pytest.raises(PermissionError, match="HARD LINK"):
            assert_no_symlink_in_path(planted)

    def test_ordinary_single_link_file_still_passes(self, tmp_path):
        """Availability: every legitimate file target has ``st_nlink == 1``."""
        f = _pod(tmp_path) / "workspace" / "AGENTS.md"
        f.write_text("hello")
        assert_no_symlink_in_path(f)  # no raise

    def test_directories_are_exempt_from_the_nlink_check(self, tmp_path):
        """A directory's ``st_nlink`` is structural (``2 + subdirs``) and
        unprivileged users cannot hard-link one — so applying the check to dirs
        would refuse every non-empty directory the seam ever touches, which is
        most of them."""
        oc = _pod(tmp_path)
        assert os.lstat(oc).st_nlink > 1, "fixture must have subdirs to be a real test"
        assert_no_symlink_in_path(oc)              # no raise
        assert_no_symlink_in_path(oc / "agents")   # nor an intermediate

    def test_unverifiable_is_marked_but_a_plant_is_not(self, tmp_path):
        """The marker the seam logs on. Both are refusals; only the benign one
        is quiet, because ERROR admin-log lines are mirrored into Signals."""
        oc = _pod(tmp_path)
        link = oc / "planted"
        link.symlink_to(_victim_tree(tmp_path, "v-mark"))
        with pytest.raises(PermissionError) as plant:
            assert_no_symlink_in_path(link)
        assert getattr(plant.value, "unverifiable", False) is False

        if os.geteuid() == 0:
            pytest.skip("root bypasses directory perms")
        clamped = oc / "agents"
        os.chmod(clamped, 0o000)
        try:
            with pytest.raises(PermissionError) as unver:
                assert_no_symlink_in_path(clamped / "main")
            assert getattr(unver.value, "unverifiable", False) is True
        finally:
            os.chmod(clamped, 0o755)

    def test_nonexistent_path_is_allowed_not_refused(self, tmp_path):
        """Nothing exists from that component down, so nothing can redirect; the
        privileged command fails on its own ENOENT. Refusing here would turn
        every not-yet-created target into a logged error."""
        assert_no_symlink_in_path(_pod(tmp_path) / "nope" / "deeper" / "x")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_unverifiable_component_fails_closed(self, tmp_path):
        oc = _pod(tmp_path)
        clamped = oc / "agents"
        os.chmod(clamped, 0o000)
        try:
            with pytest.raises(PermissionError, match="cannot verify"):
                assert_no_symlink_in_path(clamped / "main")
        finally:
            os.chmod(clamped, 0o755)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root creates uid-0 links itself")
    def test_non_root_owned_symlink_component_is_refused(self, tmp_path):
        """Only uid 0 is trusted. The sudoers grants make this process's setfacl
        argv root-equivalent, so an ``evolve``-planted link would be an
        escalation for a compromised service user, not a safe artifact — the
        link the (non-root) test process creates stands in for exactly that."""
        oc = _pod(tmp_path)
        link = oc / "planted"
        link.symlink_to(_victim_tree(tmp_path, "ev-target"))
        assert os.lstat(link).st_uid != 0
        with pytest.raises(PermissionError, match="SYMLINK"):
            assert_no_symlink_in_path(link)

    def test_root_owned_symlink_component_is_trusted(self):
        """The carve-out that lets this gate live at a seam with no caller-
        supplied anchor: ``ln -s`` stamps the creating uid onto the link and
        re-owning one needs root, so a uid-0 component cannot be an
        unprivileged plant. macOS's ``/tmp`` → ``/private/tmp`` and ``/var`` →
        ``/private/var``, Debian's ``/bin`` → ``usr/bin``, and an operator who
        relocated ``{shared_dir}`` onto another volume are the real cases —
        every one of which the strict version of this gate would refuse,
        fleet-wide and silently.

        Asserted against a GENUINE root-owned system symlink rather than a
        mocked ``lstat``, so it proves the rule as the kernel reports it."""
        candidates = [Path(p) for p in ("/tmp", "/var", "/bin", "/lib", "/sbin")]
        roots = [
            p for p in candidates
            if p.is_symlink() and os.lstat(p).st_uid == 0
        ]
        if not roots:  # pragma: no cover — no such link on this host
            pytest.skip("no root-owned system symlink available to test against")
        for link in roots:
            assert_no_symlink_in_path(link)                  # the link itself
            assert_no_symlink_in_path(link / "probe-child")  # and through it


def _rmtree(p: Path) -> None:
    import shutil

    shutil.rmtree(p)


# ── 2. LinuxPerms — the hourly reassert leg ──────────────────────────────────


class TestLinuxReassertMaskRedirect:
    @pytest.mark.parametrize("rel", _SWEPT_RELPATHS)
    def test_planted_swept_dir_issues_no_argv(self, tmp_path, recorded_argv, rel):
        """Every path the hourly Tier-1 sweep hands ``reassert_mask``. A plant at
        the LEAF of the swept path is the cheapest version of the attack: the
        bot owns the containing directory, so it can swap the real dir for a
        link between two sweeps."""
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        target = oc / rel
        _rmtree(target)
        target.symlink_to(_victim_tree(tmp_path, f"v-{rel.replace('/', '-')}"))

        assert LinuxPerms(runner).reassert_mask(target) is False
        assert calls == [], calls

    def test_planted_intermediate_issues_no_argv(self, tmp_path, recorded_argv):
        """The chip's exact shape: plant at ``<oc>/agents``, sweep reaches
        ``<oc>/agents/main/agent`` whose own lstat is a real directory inside
        the victim tree."""
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        victim = _victim_tree(tmp_path, "v-agents")
        (victim / "main" / "agent").mkdir(parents=True)
        _rmtree(oc / "agents")
        (oc / "agents").symlink_to(victim)
        deep = oc / "agents" / "main" / "agent"
        assert deep.is_dir() and not deep.is_symlink()  # the deceptive shape

        assert LinuxPerms(runner).reassert_mask(deep) is False
        assert calls == [], calls

    def test_recursive_form_also_refuses(self, tmp_path, recorded_argv):
        """``reassert_mask(ws_root, recursive=True)`` — the ``workspace`` /
        ``logs`` / ``cron`` legs. Same plant, and the refusal has to land before
        ``getfacl -R -s`` enumerates the victim tree."""
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        _rmtree(oc / "workspace")
        (oc / "workspace").symlink_to(_victim_tree(tmp_path, "v-ws"))

        assert LinuxPerms(runner).reassert_mask(oc / "workspace", recursive=True) is False
        assert calls == [], calls

    @pytest.mark.parametrize("rel", _SWEPT_RELPATHS)
    def test_real_dir_still_widens_the_mask(self, tmp_path, recorded_argv, rel):
        """The availability half. A genuine directory with a capping mask must
        still get its ``setfacl -m m::rwX`` — every hour, on every bot."""
        runner, calls = recorded_argv
        target = _pod(tmp_path) / rel

        assert LinuxPerms(runner).reassert_mask(target) is True
        assert calls == [
            [GETFACL, "-p", str(target)],
            ["sudo", SETFACL, "-m", "m::rwX", str(target)],
        ], calls

    def test_real_dir_recursive_still_widens(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        ws = _pod(tmp_path) / "workspace"

        assert LinuxPerms(runner).reassert_mask(ws, recursive=True) is True
        assert calls[0] == [GETFACL, "-R", "-s", "-p", str(ws)]
        assert ["sudo", SETFACL, "-m", "m::rwX", str(ws)] in calls, calls


class TestLinuxGrantsAndCarveOutsRedirect:
    """The sweep the chip asked for: ``reassert_mask`` is not the only leg that
    takes an attacker-plantable path. ``grant``/``grant_read_recursive``/
    ``grant_write_recursive``/``clear_acl`` all funnel through the same
    ``_setfacl`` chokepoint, which is what makes this a boundary rather than one
    more patch — each is pinned here so a future bypass has to delete a test.
    """

    def _planted(self, tmp_path):
        oc = _pod(tmp_path)
        _rmtree(oc / "workspace")
        (oc / "workspace").symlink_to(_victim_tree(tmp_path, "v"))
        return oc / "workspace"

    def test_grant_refuses(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        assert LinuxPerms(runner).grant(self._planted(tmp_path), "evolve", "read") is False
        assert calls == [], calls

    def test_grant_read_recursive_refuses(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        p = self._planted(tmp_path)
        assert LinuxPerms(runner).grant_read_recursive(p, "evolve") is False
        assert calls == [], calls

    def test_grant_write_recursive_refuses(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        p = self._planted(tmp_path)
        assert LinuxPerms(runner).grant_write_recursive(p, "evolve", "read,write") is False
        assert calls == [], calls

    def test_grant_traverse_refuses(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        assert LinuxPerms(runner).grant_traverse(self._planted(tmp_path), "evolve") is False
        assert calls == [], calls

    def test_clear_acl_refuses(self, tmp_path, recorded_argv):
        """The carve-out primitive — following a plant here STRIPS a victim's
        ACL rather than adding one."""
        runner, calls = recorded_argv
        assert LinuxPerms(runner).clear_acl(self._planted(tmp_path)) is False
        assert calls == [], calls

    def test_real_tree_grants_still_emit_the_pair(self, tmp_path, recorded_argv):
        """Availability: the access + default ACL pair is unchanged on a real
        tree — the golden this module's other suite pins, re-asserted here so a
        gate regression cannot pass by loosening only that file."""
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        assert LinuxPerms(runner).grant_read_recursive(oc, "evolve") is True
        assert calls == [
            ["sudo", SETFACL, "-R", "-m", "u:evolve:rX", str(oc)],
            ["sudo", SETFACL, "-R", "-d", "-m", "u:evolve:rX", str(oc)],
        ], calls


class TestLinuxProbesRedirect:
    """``getfacl`` is unprivileged and NOT sudo-granted, but it FOLLOWS a symlink
    argument too — so on a planted path the effective-perm checks answer from
    the VICTIM's ACL. Refusing keeps every privileged decision off a redirected
    observation."""

    def test_acl_user_effective_is_false_on_a_planted_path(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        _rmtree(oc / "agents")
        (oc / "agents").symlink_to(_victim_tree(tmp_path, "v-probe"))

        assert LinuxPerms(runner).acl_user_effective(oc / "agents", "evolve", "read") is False
        assert calls == [], calls

    def test_acl_masked_owner_only_is_false_on_a_planted_path(self, tmp_path, recorded_argv):
        """Fail-closed in the honest direction: the OC exposure finding FIRES
        rather than being suppressed by a victim's mask."""
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        link = oc / "planted.json"
        link.symlink_to(_victim_tree(tmp_path, "v-mask"))

        assert LinuxPerms(runner).acl_masked_owner_only(link) is False
        assert calls == [], calls


# ── 3. MacOSPerms — same defect, no argv change ──────────────────────────────


class TestMacOSRedirect:
    """macOS's ``reassert_mask`` is a structural no-op (no ACL mask), so the
    chip's specific leg does not exist there — but ``chmod +a`` / ``chmod -N``
    follow a symlink argument exactly the same way, and ``set_evolve_read_acl``'s
    ``workspace/`` retro-grant loop selects members with ``is_file()``, which
    FOLLOWS. Gating in the seam covers both backends with ZERO argv change, so
    the byte-exact sudoers goldens are untouched (``chmod -h`` would have
    changed them)."""

    def test_grant_refuses_on_a_planted_path(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        link = oc / "workspace" / "AGENTS.md"
        link.symlink_to(_victim_tree(tmp_path, "v-macos") / "inner")

        assert MacOSPerms(runner).grant(link, "evolve", "read") is False
        assert calls == [], calls

    def test_clear_acl_refuses_on_a_planted_path(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        link = oc / "credentials"
        link.symlink_to(_victim_tree(tmp_path, "v-creds"))

        assert MacOSPerms(runner).clear_acl(link) is False
        assert calls == [], calls

    def test_real_path_still_emits_the_byte_exact_ace(self, tmp_path, recorded_argv):
        runner, calls = recorded_argv
        oc = _pod(tmp_path)
        assert MacOSPerms(runner).grant(oc, "evolve", "read") is True
        assert calls == [["sudo", "/bin/chmod", "+a", "evolve allow read", str(oc)]], calls


# ── 4. the review's residuals (PR #3605 independent two-pass review) ─────────


class TestRefusalLogSeverity:
    """``reporter.emit_error_signals`` mirrors every ERROR admin-log line into
    the Signal store, so the level a refusal logs at decides whether it pages an
    operator. A replaced component should; an EACCES from a mask clamp racing
    the hourly reassert should not — that state was a SILENT no-op before this
    gate existed."""

    def test_plant_logs_error(self, tmp_path, recorded_argv, caplog):
        import logging

        runner, _ = recorded_argv
        oc = _pod(tmp_path)
        _rmtree(oc / "agents")
        (oc / "agents").symlink_to(_victim_tree(tmp_path, "v-sev"))

        with caplog.at_level(logging.WARNING, logger="runtime.perms"):
            LinuxPerms(runner).reassert_mask(oc / "agents")
        assert [r.levelno for r in caplog.records] == [logging.ERROR], caplog.text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_unverifiable_logs_warning(self, tmp_path, recorded_argv, caplog):
        import logging

        runner, _ = recorded_argv
        oc = _pod(tmp_path)
        os.chmod(oc / "agents", 0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="runtime.perms"):
                LinuxPerms(runner).reassert_mask(oc / "agents" / "main")
            assert [r.levelno for r in caplog.records] == [logging.WARNING], caplog.text
        finally:
            os.chmod(oc / "agents", 0o755)


class TestMaskedPathsStayUnderTheArgument:
    """``getfacl``'s ``# file:`` headers are attacker-influenced data — the
    filenames come from the bot's own tree — and each becomes the argv of a root
    ``setfacl``. acl >= 2.2.52 escapes newlines in those headers so a filename
    cannot forge a block boundary today; the containment filter makes that a
    belt rather than the whole trousers."""

    def test_a_forged_out_of_tree_block_is_dropped(self, tmp_path):
        oc = _pod(tmp_path)
        ws = oc / "workspace"
        forged = (
            f"# file: {ws}\nuser:evolve:r-x\t#effective:---\nmask::---\n\n"
            f"# file: /etc/sudoers\nuser:evolve:r-x\t#effective:---\nmask::---\n\n"
            f"# file: {ws}/real\nuser:evolve:r-x\t#effective:---\nmask::---\n\n"
        )
        calls: "list[list[str]]" = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[0] == GETFACL:
                return _Result(0, forged)
            return _Result(0)

        assert LinuxPerms(run).reassert_mask(ws, recursive=True) is True
        widened = [c[-1] for c in calls if c[:4] == ["sudo", SETFACL, "-m", "m::rwX"]]
        assert "/etc/sudoers" not in widened, widened
        assert sorted(widened) == sorted([str(ws), f"{ws}/real"]), widened

"""#3566 audit D-2 — the read-contract's NON-ACL root legs must not follow a plant.

#3605 gated the perms seam's privileged ACL argv (``setfacl`` / ``chmod +a`` /
``chmod -N``) at a single chokepoint, ``runtime.perms._redirect_safe``. What it
did NOT reach are the sibling root commands interleaved with those grants inside
``deploy._apply_openclaw_read_contract``, which shell out from deploy.py
directly and never touch the seam:

  * ``sudo /bin/chmod 700 <oc>/credentials``      (the carve-out's mode leg)
  * ``sudo /bin/mkdir -p <oc>/workspace/<sub>``   (``_ensure_evolve_write_dir``)
  * ``sudo chown [-R] <bot>:staff <same>``        (ditto — an ownership transfer)
  * ``sudo /usr/bin/touch <oc>/.metadata_never_index`` (Spotlight marker, macOS)

Each runs as ROOT against a path under ``.openclaw``, which the BOT owns and can
therefore replace with a symlink; none of the four commands is passed a
no-follow flag. The sharpest instance is the ``credentials`` pair: the seam
REFUSES ``perms.clear_acl(creds_dir)`` on a planted path and logs that a root
chmod would follow it — and the very next statement issued exactly that root
``chmod 700``. Gating one half of a two-line pair is not a gate.

Reachable well beyond a full deploy: ``set_evolve_read_acl`` is step 1 of
``secret_config_perms.heal_evolve_access``, which the hourly
``reassert_evolve_access`` Tier-2 escalation, ``oc_auth_store``'s EACCES
recovery (a bot induces it by clamping its own ``.openclaw``), the forge, and a
``_PermCheck`` apply all invoke.

The gate is ``sudo_dest.redirect_refusal`` — the DIRECTORY-shaped sibling of
``sudo_dest_refusal``, wrapping ``evolve_util.assert_no_symlink_in_path``. The
file-shaped ``assert_safe_sudo_dest`` cannot be used here: it asserts
absent-or-regular-file, which every destination above fails by being a
directory. That mismatch is why #3602 descoped these sites rather than
mis-gating them.

Asserted on RECORDED ARGV throughout, never on a return bool alone — a helper
that returns False while still having shelled out is the exact failure these
pin against. The availability half is pinned just as hard: the benign tree and
the live Linux gateway-clamp state must still issue their commands.

Placeholder bot names per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess

import pytest

from evolve_admin import deploy, deploy_resilience
from evolve_admin import secret_config_perms as scp
from evolve_admin.sudo_dest import redirect_refusal


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A real ``<home_root>/<bot>/.openclaw`` tree on the macOS profile.

    macOS is deliberate: ``plant_never_index_marker`` is macOS-only, so the
    ``touch`` leg is only reachable (and only pinnable) under that profile.
    """
    from platform_profile import MACOS, set_profile
    from runtime.perms import MacOSPerms, set_perms

    real_profile_set = set_profile
    real_profile_set(dataclasses.replace(MACOS, user_home_root=str(tmp_path)))

    home = tmp_path / "team_bot_a"
    home.mkdir(parents=True)
    victim = tmp_path / "victim-tree"
    victim.mkdir()

    argv: list[list[str]] = []

    def runner(cmd, **kwargs):  # the perms seam's injected runner
        argv.append([str(x) for x in cmd])
        return _ok_proc()

    set_perms(MacOSPerms(runner=runner))

    real_run = subprocess.run

    def rec(cmd, *a, **k):
        c = [str(x) for x in cmd]
        if c and c[0] == "sudo":
            argv.append(c)
            return _ok_proc()
        return real_run(cmd, *a, **k)

    for mod in (deploy, deploy_resilience, scp):
        monkeypatch.setattr(mod.subprocess, "run", rec)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id, network=None: "team_bot_a")

    yield {"home": home, "oc": home / ".openclaw", "victim": victim, "argv": argv}

    set_perms(None)
    real_profile_set(MACOS)


def _root_argv(argv, *needles):
    """Every recorded root argv whose command matches one of ``needles``."""
    return [c for c in argv if c and c[0] == "sudo"
            and any(n in " ".join(c) for n in needles)]


def _touching(argv, path):
    """Recorded argv naming ``path`` (or anything under it) as an operand."""
    return [c for c in argv if any(str(path) == t or t.startswith(f"{path}/") for t in c)]


# ── the plant at .openclaw itself — the chip's literal attack ────────────────


class TestPlantedOpenclawRoot:
    def test_no_root_argv_reaches_a_symlinked_openclaw(self, pod):
        """THE finding. ``rm -rf ~/.openclaw; ln -s <victim> ~/.openclaw`` — the
        bot owns its home, so this needs no privilege and no race.

        Every root leg below resolves into the victim tree: ``mkdir -p`` creates
        directories there, ``chown -R`` hands the bot ownership of them, and
        ``touch`` root-creates a file. Pinned on argv: the assertion is that NONE
        of the four commands was issued at all."""
        pod["oc"].symlink_to(pod["victim"])

        deploy.set_evolve_read_acl("team_bot_a")

        assert _root_argv(pod["argv"], "mkdir") == []
        assert _root_argv(pod["argv"], "chown") == []
        assert _root_argv(pod["argv"], "touch") == []
        assert _root_argv(pod["argv"], "chmod 700") == []
        # And nothing at all resolved into the victim tree.
        assert _touching(pod["argv"], pod["victim"]) == []

    def test_heal_evolve_access_is_the_reachable_entry_point(self, pod, monkeypatch):
        """Not deploy-only. ``heal_evolve_access`` runs ``set_evolve_read_acl``
        as step 1, and the hourly reassert / oc_auth_store's EACCES recovery /
        the forge all call it — so the plant fires without an operator."""
        pod["oc"].symlink_to(pod["victim"])
        monkeypatch.setattr(scp, "verify_evolve_access", lambda *a, **k: [])

        scp.heal_evolve_access("team_bot_a", "team_bot_a")

        assert _root_argv(pod["argv"], "mkdir", "chown", "touch") == []
        assert _touching(pod["argv"], pod["victim"]) == []


# ── the plant one level in — credentials/ ────────────────────────────────────


class TestPlantedCredentialsDir:
    def test_no_root_chmod_follows_a_symlinked_credentials(self, pod):
        """The two-line pair. ``creds_dir.exists()`` FOLLOWS, so a symlinked
        ``credentials`` reads as present; the seam then refuses ``clear_acl`` on
        that path while the un-gated ``chmod 700`` on the NEXT line relabelled
        the link target to 0700 (``chmod 700 /etc`` bricks non-root userland)."""
        pod["oc"].mkdir()
        (pod["oc"] / "credentials").symlink_to(pod["victim"])

        deploy.set_evolve_read_acl("team_bot_a")

        assert _root_argv(pod["argv"], "chmod 700") == []
        assert _touching(pod["argv"], pod["victim"]) == []

    def test_the_rest_of_the_contract_still_runs(self, pod):
        """The gate is per-path, not a blanket abort: a plant at ``credentials``
        must not suppress the workspace legs on the bot's own real tree.
        Availability regressions hide easily behind a security fix."""
        pod["oc"].mkdir()
        (pod["oc"] / "credentials").symlink_to(pod["victim"])

        deploy.set_evolve_read_acl("team_bot_a")

        assert _root_argv(pod["argv"], "mkdir"), "workspace mkdir was suppressed"
        assert _root_argv(pod["argv"], "chown"), "workspace chown was suppressed"


# ── availability: the benign tree and the live Linux clamp ───────────────────


class TestBenignTreeStillConverges:
    def test_real_tree_issues_every_root_leg(self, pod):
        """The control. Without this a gate that refuses EVERYTHING passes every
        security assertion above while silently breaking every deploy."""
        pod["oc"].mkdir()

        deploy.set_evolve_read_acl("team_bot_a")

        ws = pod["oc"] / "workspace"
        assert _root_argv(pod["argv"], "mkdir"), "no mkdir on a clean tree"
        assert _root_argv(pod["argv"], "chown"), "no chown on a clean tree"
        assert _root_argv(pod["argv"], "touch"), "never-index marker not planted"
        assert _touching(pod["argv"], ws), "workspace legs did not run"

    def test_clamped_openclaw_still_repairs(self, pod, monkeypatch):
        """The live Linux VPS state, and the regression that would hurt most.

        The OC gateway's ``chmod 700 ~/.openclaw`` zeroes the POSIX-ACL mask, so
        evolve loses traverse INTO ``.openclaw``. Crucially it does NOT lose
        ``lstat`` OF ``.openclaw`` — that needs traverse on ``<home>``, which is
        untouched — so the component walk still resolves and the recursive read
        grant that recomputes the clamped mask must still be issued. A gate that
        fail-closed here would strand the fleet's only self-heal."""
        pod["oc"].mkdir()
        real_lstat = os.lstat

        def clamped(p, *a, **k):
            if str(p).startswith(f"{pod['oc']}{os.sep}"):
                raise PermissionError(13, "Permission denied", str(p))
            return real_lstat(p, *a, **k)

        monkeypatch.setattr(os, "lstat", clamped)
        deploy.set_evolve_read_acl("team_bot_a")

        assert _root_argv(pod["argv"], "+a"), "the recursive read grant was refused"


# ── the primitive itself ─────────────────────────────────────────────────────


class TestRedirectRefusal:
    def test_real_directory_passes(self, tmp_path):
        d = tmp_path / "real"
        d.mkdir()
        assert redirect_refusal(d) == ""

    def test_absent_path_passes(self, tmp_path):
        """``mkdir -p`` targets legitimately do not exist yet — refusing an
        absent path would break every first deploy."""
        assert redirect_refusal(tmp_path / "not-yet") == ""

    def test_symlinked_leaf_is_refused(self, tmp_path):
        (tmp_path / "victim").mkdir()
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "victim")
        assert "SYMLINK" in redirect_refusal(link)

    def test_symlinked_intermediate_is_refused(self, tmp_path):
        """A dir-shaped dest's exposure is the whole chain, not the leaf: the
        leaf may not exist while an ancestor redirects it out of tree."""
        (tmp_path / "victim").mkdir()
        (tmp_path / "mid").symlink_to(tmp_path / "victim")
        assert "SYMLINK" in redirect_refusal(tmp_path / "mid" / "workspace" / "evolve")

    def test_unverifiable_component_is_refused(self, tmp_path, monkeypatch):
        """Fail-closed: an EACCES that blinds the walk is a state the bot can
        arrange (``chmod 700 ~``), so "cannot see it" must not mean "proceed"."""
        d = tmp_path / "clamped"
        d.mkdir()
        real_lstat = os.lstat

        def blind(p, *a, **k):
            if str(p) == str(d):
                raise PermissionError(13, "Permission denied", str(p))
            return real_lstat(p, *a, **k)

        monkeypatch.setattr(os, "lstat", blind)
        assert "cannot verify" in redirect_refusal(d)

    def test_first_refusal_wins_across_several_paths(self, tmp_path):
        (tmp_path / "victim").mkdir()
        good = tmp_path / "good"
        good.mkdir()
        bad = tmp_path / "bad"
        bad.symlink_to(tmp_path / "victim")
        assert "SYMLINK" in redirect_refusal(good, bad)


# ── the never-index marker leg, in isolation ─────────────────────────────────


class TestNeverIndexMarkerGate:
    def test_planted_parent_issues_no_touch(self, pod):
        pod["oc"].symlink_to(pod["victim"])
        assert deploy_resilience.plant_never_index_marker(pod["oc"], via_sudo=True) is False
        assert _root_argv(pod["argv"], "touch") == []

    def test_real_parent_still_plants(self, pod):
        pod["oc"].mkdir()
        assert deploy_resilience.plant_never_index_marker(pod["oc"], via_sudo=True) is True
        assert _root_argv(pod["argv"], "touch"), "marker not planted on a clean tree"


# ── the three sibling sites in the same D-2 class ────────────────────────────


class TestWorkspaceGitInitGate:
    """``ensure_workspace_git_init``: root ``cp -R`` + ``chown -R`` into
    ``<oc>/workspace/.git``. Carried a ``# D-2 KNOWN-UNGATED, descoped`` marker
    because the dest is a DIRECTORY and the file-shaped primitive could not
    express it; ``redirect_refusal`` is what makes it gateable."""

    def test_symlinked_workspace_issues_no_cp_or_chown(self, pod):
        """``workspace`` is bot-owned, and ``.git`` legitimately does not exist
        yet — so the leaf tells you nothing and the exposure is the chain."""
        pod["oc"].mkdir()
        (pod["oc"] / "workspace").symlink_to(pod["victim"])

        ok, status = deploy.ensure_workspace_git_init("team_bot_a")

        assert ok is False and "unsafe-dest" in status
        assert _root_argv(pod["argv"], "cp") == []
        assert _root_argv(pod["argv"], "chown") == []

    def test_real_workspace_still_initializes(self, pod):
        """Availability control — the gate must not break first-time init,
        whose dest legitimately does not exist."""
        (pod["oc"] / "workspace").mkdir(parents=True)

        ok, status = deploy.ensure_workspace_git_init("team_bot_a")

        assert ok is True and status == "initialized"
        assert _root_argv(pod["argv"], "cp"), "cp -R was suppressed"
        assert _root_argv(pod["argv"], "chown"), "chown -R was suppressed"


class TestRecoveryWriteBotOpenclawGate:
    """``recovery._write_bot_openclaw``: the rollback path's ``sudo -n cp`` +
    ``chown`` onto ``openclaw.json`` — the same shape
    ``deploy.safe_write_bot_config`` already gates, missed on this path.
    FILE-shaped dest, so ``sudo_dest_refusal`` is the right primitive."""

    def test_symlinked_dest_issues_no_cp_or_chown(self, tmp_path, monkeypatch):
        from evolve_admin import recovery

        argv: list[list[str]] = []
        real_run = subprocess.run

        def rec(cmd, *a, **k):
            c = [str(x) for x in cmd]
            if c and c[0] == "sudo":
                argv.append(c)
                return _ok_proc()
            return real_run(cmd, *a, **k)

        victim = tmp_path / "victim.conf"
        victim.write_text("root-owned")
        oc = tmp_path / "team_bot_a" / ".openclaw"
        oc.mkdir(parents=True)
        (oc / "openclaw.json").symlink_to(victim)

        monkeypatch.setattr(recovery.subprocess, "run", rec)
        monkeypatch.setattr(recovery, "get_bot_user", lambda b, n: "team_bot_a")
        # The module builds an absolute /Users/... dest; point it at the fixture.
        monkeypatch.setattr(recovery, "Path", lambda p=None, *a: (
            oc / "openclaw.json" if p and str(p).endswith("/.openclaw/openclaw.json")
            else __import__("pathlib").Path(p)))
        # Direct copy must fail so the sudo fallback (the gated leg) is reached.
        monkeypatch.setattr(recovery.shutil, "copy2",
                            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))

        ok, msg = recovery._write_bot_openclaw("team_bot_a", {}, '{"a": 1}')

        assert ok is False and "refusing sudo write" in msg
        assert argv == [], f"privileged argv escaped: {argv}"


class TestEvoSshDirGate:
    """``setup_wizard._evo_cutover_ensure_evo_ssh_dir``: root ``mkdir``/
    ``chmod 700``/``chown evo:staff`` on ``/Users/evo/.ssh``. ``evo`` owns its
    own home, so it can plant the link; the ``.exists()``/``.stat()`` probes
    above the legs both FOLLOW."""

    def _run(self, monkeypatch, target):
        from evolve_admin import setup_wizard

        argv: list[list[str]] = []
        real_run = subprocess.run

        def rec(cmd, *a, **k):
            c = [str(x) for x in cmd]
            if c and c[0] == "sudo":
                argv.append(c)
                return _ok_proc()
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(setup_wizard.subprocess, "run", rec)
        monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", target)
        return setup_wizard._evo_cutover_ensure_evo_ssh_dir(), argv

    def test_symlinked_ssh_dir_issues_nothing(self, tmp_path, monkeypatch):
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        target = tmp_path / "evo" / ".ssh"
        target.parent.mkdir(parents=True)
        target.symlink_to(victim)

        (ok, reason), argv = self._run(monkeypatch, target)

        assert ok is False and "refusing to repair" in reason
        assert argv == [], f"privileged argv escaped: {argv}"

    def test_absent_dir_is_still_created(self, tmp_path, monkeypatch):
        """Availability control: the create path's dest does not exist yet."""
        target = tmp_path / "evo" / ".ssh"
        target.parent.mkdir(parents=True)

        (ok, reason), argv = self._run(monkeypatch, target)

        assert ok is True and reason is None
        joined = [" ".join(c) for c in argv]
        assert any("mkdir" in j for j in joined), "mkdir suppressed"
        assert any("chmod" in j for j in joined), "chmod suppressed"
        assert any("chown" in j for j in joined), "chown suppressed"

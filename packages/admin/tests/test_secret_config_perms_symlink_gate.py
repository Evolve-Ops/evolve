"""#3566 audit D-2 — root chown/chmod must never follow a symlinked dest.

``secret_config_perms`` is the module that shells out to ROOT against paths
inside ``/Users/<bot>/.openclaw/`` — a directory the BOT owns and can therefore
replace any file in with a symlink. None of those commands is passed ``-h``, so
each of them FOLLOWS a link at the destination:

  * ``chown_chmod_bot_config``  — ``chown <bot>:staff`` + ``chmod 644``. Through
    a link the bot is handed OWNERSHIP of whatever the link names. The strongest
    leg: ``check_bot_tiers_ownership`` used to ``stat()`` (following), so a link
    aimed at a root-owned file reported ``owner_uid = 0`` — indistinguishable
    from the fresh-``cp`` drift the repair exists to fix — and got the repair
    attached automatically on every deploy and every hourly drift-monitor pass.
  * ``chmod_secret_config``     — ``chmod 600``. DoS-shaped rather than an
    ownership transfer, but the same root-follows-link mechanism.
  * ``strip_bot_private_acl``   — ACL clear + ``chmod 600``, same shape.

The gate is ``evolve_util.assert_safe_sudo_dest`` (its own unit tests live in
``packages/analyzer/tests/test_evolve_util.py``); what is pinned HERE is that
each call site actually calls it, BEFORE shelling out, and that a refusal
issues no privileged argv at all — asserted on the recorded subprocess argv,
never on the bool alone.

Placeholder bot names per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from evolve_admin import secret_config_perms as scp


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def recorded_argv(monkeypatch) -> list[list[str]]:
    """Record every PRIVILEGED argv the module would run (and run none).

    Records only ``sudo …`` invocations, deliberately. ``scp.subprocess`` is the
    shared stdlib module, so patching ``run`` on it is GLOBAL — an unrelated
    helper shelling out during the same test (observed: a ``git log`` version
    lookup) lands in the list and turns an exact-equality assertion into an
    order-dependent flake. Filtering to ``sudo`` also states the contract these
    tests are actually about: what runs as root."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        argv = list(argv)
        if argv and argv[0] == "sudo":
            calls.append(argv)
        return _ok_proc()

    monkeypatch.setattr(scp.subprocess, "run", fake_run)
    return calls


def _pod(tmp_path: Path, bot: str = "team_bot_a") -> Path:
    """A real ``<home_root>/<bot>/.openclaw`` tree, with the macOS profile's
    home root re-pointed at tmp_path so ``_bot_user_from_path`` derives the
    account from a path that also EXISTS (the gate lstats it)."""
    from platform_profile import MACOS, set_profile

    set_profile(dataclasses.replace(MACOS, user_home_root=str(tmp_path)))
    oc = tmp_path / bot / ".openclaw"
    oc.mkdir(parents=True, exist_ok=True)
    return oc


def _victim(tmp_path: Path) -> Path:
    """A stand-in for the root-owned file an attacker aims the link at."""
    v = tmp_path / "victim-root-owned.conf"
    v.write_text("root-owned content")
    os.chmod(v, 0o600)
    return v


# ── chown_chmod_bot_config — the ownership-transfer leg ──────────────────────


class TestChownChmodBotConfigSymlinkGate:
    def test_symlinked_dest_issues_no_chown_or_chmod(self, tmp_path, recorded_argv):
        """THE finding. Without the gate this hands the bot ownership of the
        victim and relabels it 0644."""
        oc = _pod(tmp_path)
        victim = _victim(tmp_path)
        dest = oc / "evolve-tiers.json"
        dest.symlink_to(victim)

        assert scp.chown_chmod_bot_config(dest) is False
        assert recorded_argv == [], recorded_argv
        # Nothing about the victim was touched — and the link is still a link
        # (the helper must not "helpfully" resolve or replace it).
        assert victim.read_text() == "root-owned content"
        assert dest.is_symlink()

    def test_dangling_symlink_dest_issues_nothing(self, tmp_path, recorded_argv):
        """A link to a not-yet-existing path is the CREATION variant: the root
        chown would mint ownership of a path the bot chose."""
        oc = _pod(tmp_path)
        dest = oc / "evolve-tiers.json"
        dest.symlink_to(tmp_path / "not-yet.conf")

        assert scp.chown_chmod_bot_config(dest) is False
        assert recorded_argv == [], recorded_argv

    def test_symlinked_parent_issues_nothing(self, tmp_path, recorded_argv):
        """``<link>/evolve-tiers.json`` redirects the whole repair out of tree
        even with nothing planted at the leaf name."""
        real = tmp_path / "team_bot_a" / "real-openclaw"
        oc = _pod(tmp_path)
        oc.rmdir()
        real.mkdir(parents=True)
        oc.symlink_to(real)
        (real / "evolve-tiers.json").write_text("{}")

        assert scp.chown_chmod_bot_config(oc / "evolve-tiers.json") is False
        assert recorded_argv == [], recorded_argv

    def test_real_file_repair_is_unchanged(self, tmp_path, recorded_argv):
        """The legitimate case the gate must not disturb: a real regular file
        still gets chown + chmod 644, in that order."""
        oc = _pod(tmp_path)
        dest = oc / "evolve-tiers.json"
        dest.write_text("{}")

        assert scp.chown_chmod_bot_config(dest) is True
        assert recorded_argv == [
            ["sudo", "/usr/sbin/chown", "team_bot_a:staff", str(dest)],
            ["sudo", "/bin/chmod", "644", str(dest)],
        ], recorded_argv

    def test_absent_dest_still_repairs(self, tmp_path, recorded_argv):
        """A dest that does not exist yet is NOT a refusal — the gate's
        absent-dest allowance keeps the post-``cp`` repair working when the cp
        raced ahead of the check."""
        oc = _pod(tmp_path)
        assert scp.chown_chmod_bot_config(oc / "evolve-tiers.json") is True
        assert len(recorded_argv) == 2, recorded_argv

    def test_hard_linked_dest_issues_no_chown_or_chmod(self, tmp_path, recorded_argv):
        """The `ln` variant of the same attack — no symlink anywhere, so every
        symlink check waves it through. Ownership of the victim INODE, which is
        strictly worse than the symlink case: it is permanent, and there is no
        race to win because the detector reads the victim's uid as drift."""
        oc = _pod(tmp_path)
        victim = _victim(tmp_path)
        dest = oc / "evolve-tiers.json"
        os.link(victim, dest)

        assert not dest.is_symlink()
        assert scp.chown_chmod_bot_config(dest) is False
        assert recorded_argv == [], recorded_argv
        assert victim.read_text() == "root-owned content"

    def test_post_chown_swap_stops_the_chmod(self, tmp_path, monkeypatch):
        """The gate is re-asserted BETWEEN the chown and the chmod, because a
        successful chown hands the file to the BOT — from that instant the
        attacker owns the path the chmod is about to take. Simulated by swapping
        a link in from inside the chown's own subprocess call."""
        oc = _pod(tmp_path)
        victim = _victim(tmp_path)
        dest = oc / "evolve-tiers.json"
        dest.write_text("{}")
        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            argv = list(argv)
            if not argv or argv[0] != "sudo":  # global patch — see recorded_argv
                return _ok_proc()
            calls.append(argv)
            if "chown" in argv[1]:  # the window opens the moment this returns
                dest.unlink()
                dest.symlink_to(victim)
            return _ok_proc()

        monkeypatch.setattr(scp.subprocess, "run", fake_run)
        assert scp.chown_chmod_bot_config(dest) is False
        assert len(calls) == 1 and "chown" in calls[0][1], calls
        assert not any("chmod" in c[1] for c in calls), calls

    def test_gate_runs_after_the_bot_user_derivation(self, tmp_path, recorded_argv):
        """Ordering pin: an unrecognised path issues no argv anyway, so it must
        short-circuit BEFORE the gate rather than logging a refusal for every
        tmpdir. (Also keeps the helper cheap on the hot deploy path.)"""
        _pod(tmp_path)
        stray = tmp_path / "elsewhere.json"
        stray.write_text("{}")
        assert scp.chown_chmod_bot_config(stray) is False
        assert recorded_argv == [], recorded_argv


# ── the detector: a link is its OWN finding, not ownership drift ─────────────


class _FakePwd:
    def __init__(self, table: dict[str, int]):
        self._table = table

    def getpwnam(self, name):
        if name not in self._table:
            raise KeyError(name)
        return type("PW", (), {"pw_uid": self._table[name]})()


class TestCheckBotTiersOwnershipSymlinkDetection:
    """``check_bot_tiers_ownership`` detected drift with a FOLLOWING ``stat()``,
    which is what made the repair reliably reachable: a link pointed at any
    root-owned file reads as ``owner_uid = 0``, i.e. exactly the drift the
    repair is wired to fix. It now lstats."""

    def _check(self, checks):
        t = [c for c in checks if c.target.endswith("/evolve-tiers.json")]
        assert t, "no evolve-tiers.json check produced"
        return t[0]

    def _wire(self, tmp_path, monkeypatch, uid_delta: int = 0):
        oc = _pod(tmp_path)
        monkeypatch.setattr(scp, "_user_home", lambda u: tmp_path / "team_bot_a")
        monkeypatch.setattr(
            scp, "pwd", _FakePwd({"team_bot_a": os.getuid() + uid_delta})
        )
        return oc

    def test_symlink_is_flagged_as_symlink_not_ownership_drift(
        self, tmp_path, monkeypatch
    ):
        oc = self._wire(tmp_path, monkeypatch)
        victim = _victim(tmp_path)
        (oc / "evolve-tiers.json").symlink_to(victim)

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert str(victim) in t.detail
        # Explicitly NOT the ownership-drift wording — an operator (and the
        # pod_perms_drift Signal body) must be able to tell them apart.
        assert "can't read its own tier config" not in t.detail

    def test_symlink_finding_offers_no_repair(self, tmp_path, monkeypatch):
        """The load-bearing half: the repair must not be ATTACHED to a planted
        link. Both the chown (follows) and an unlink (destructive, ungranted)
        are wrong answers, so the finding is report-only.

        ``uid_delta=1`` is what makes this test mean anything. With the bot uid
        equal to the test user's, a FOLLOWING stat would report the victim as
        already bot-owned and hand back ``apply is None`` for entirely the wrong
        reason — the assertion would hold against the pre-fix code. The mismatch
        is what makes the pre-fix path attach the repair."""
        oc = self._wire(tmp_path, monkeypatch, uid_delta=1)
        (oc / "evolve-tiers.json").symlink_to(_victim(tmp_path))

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert t.apply is None, "a symlink must never be handed the root repair"
        assert t.fix_description == ""

    def test_hard_link_is_flagged_and_offers_no_repair(self, tmp_path, monkeypatch):
        """lstat does NOT save you here — it reports the victim inode's uid, so
        a hard link to a root-owned file lands in the ownership-drift branch and
        summons the chown. It needs its own classification, or the fix for the
        symlink variant leaves the worse variant fully armed."""
        oc = self._wire(tmp_path, monkeypatch, uid_delta=1)
        victim = _victim(tmp_path)
        os.link(victim, oc / "evolve-tiers.json")

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "HARD LINK" in t.detail
        assert "can't read its own tier config" not in t.detail
        assert t.apply is None, "a hard link must never be handed the root chown"

    def test_dangling_symlink_is_not_reported_as_absent(self, tmp_path, monkeypatch):
        """A following stat raised FileNotFoundError on a dangling link and the
        check reported "not present — nothing to enforce", silently hiding the
        plant. lstat sees the link."""
        oc = self._wire(tmp_path, monkeypatch)
        (oc / "evolve-tiers.json").symlink_to(tmp_path / "does-not-exist.conf")

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert "not present" not in t.detail

    def test_real_file_round_trip_still_detects_and_repairs(
        self, tmp_path, monkeypatch, recorded_argv
    ):
        """Detector → repair round trip on the shape this all exists for: a real
        regular file whose owner is not the bot."""
        oc = self._wire(tmp_path, monkeypatch, uid_delta=1)
        dest = oc / "evolve-tiers.json"
        dest.write_text("{}")

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "can't read its own tier config" in t.detail
        assert t.apply is not None
        assert t.apply() is True
        assert ["sudo", "/usr/sbin/chown", "team_bot_a:staff", str(dest)] in recorded_argv
        assert ["sudo", "/bin/chmod", "644", str(dest)] in recorded_argv

    def test_bot_owned_real_file_still_passes(self, tmp_path, monkeypatch):
        oc = self._wire(tmp_path, monkeypatch)
        (oc / "evolve-tiers.json").write_text("{}")
        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert t.ok, t.detail


class TestCheckBotTiersOwnershipIntermediateSymlinkDetection:
    """The INTERMEDIATE-component twin of the class above — the same shape
    ``TestCheckBotSecretModesIntermediateSymlinkDetection`` covers for the 0600
    secrets, one detector over.

    ``check_bot_tiers_ownership`` lstats only the LEAF.
    ``BOT_OWNED_CONFIG_RELPATHS`` is flat by contract
    (``test_owned_relpaths_stay_flat``), so its ONLY intermediate is
    ``.openclaw`` itself — which is plantable, because the bot owns its home and
    can replace that entry. ``lstat`` does not follow a link at the leaf, but the
    kernel resolves every component above it, so with ``.openclaw`` planted the
    check reads the VICTIM's ``evolve-tiers.json`` and reports the victim's uid.

    The REPAIR side needs no change and gets none: because the relpath is flat,
    ``.openclaw`` is ``path.parent``, and "the parent must be a real directory"
    is one of ``assert_safe_sudo_dest``'s BASE lstats — it holds with or without
    the anchor ``_sudo_dest_ok`` adds, and is pinned by
    ``TestChownChmodBotConfigSymlinkGate::test_symlinked_parent_issues_nothing``
    above. So the gate HOLDS (no argv, victim untouched) and what is broken is
    only what the check REPORTS: permanently ``ok=False`` with an apply that can
    never succeed, naming a benign-looking root-owned tier config instead of the
    plant. Hence: a distinct, report-only finding, ``apply=None``.
    """

    def _check(self, checks):
        t = [c for c in checks if c.target.endswith("/evolve-tiers.json")]
        assert t, "no evolve-tiers.json check produced"
        return t[0]

    def _wire(self, tmp_path, monkeypatch, uid_delta: int = 0,
              mkdir_oc: bool = True) -> Path:
        """As the sibling class's ``_wire``, plus a ``mkdir_oc=False`` mode: the
        plant REPLACES ``.openclaw``, so the dir must not be created first. The
        profile still has to be pinned at ``tmp_path`` (``set_profile`` is
        process-global and the repair derives the bot user through it), so an
        unrelated bot name anchors it."""
        home = tmp_path / "team_bot_a"
        if mkdir_oc:
            oc = _pod(tmp_path)
        else:
            home.mkdir(parents=True, exist_ok=True)
            _pod(tmp_path, "profile-anchor-only")  # set_profile without the dir
            oc = home / ".openclaw"
        monkeypatch.setattr(scp, "_user_home", lambda u: home)
        monkeypatch.setattr(
            scp, "pwd", _FakePwd({"team_bot_a": os.getuid() + uid_delta})
        )
        return oc

    def test_openclaw_plant_names_the_component_not_the_victim_uid(
        self, tmp_path, monkeypatch, recorded_argv
    ):
        """THE finding. ``uid_delta=1`` is what makes it mean anything: with the
        victim owned by the test user and the bot uid one off, the pre-fix leaf
        lstat reports ``owner uid=<test user> (expected bot uid=…; root-owned →
        bot can't read its own tier config)`` — the victim's uid, attributed to
        this bot's tier path, with the root repair ATTACHED and permanently
        refusing."""
        oc = self._wire(tmp_path, monkeypatch, uid_delta=1, mkdir_oc=False)
        victim_tree = tmp_path / "victim-tree"
        victim_tree.mkdir()
        victim_file = victim_tree / "evolve-tiers.json"
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o600)
        oc.symlink_to(victim_tree)

        # The deceptive shape: the leaf is a real regular file, not a link.
        leaf = oc / "evolve-tiers.json"
        assert not leaf.is_symlink() and leaf.is_file()

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert str(oc) in t.detail, "must name the PLANTED component"
        assert str(victim_tree) in t.detail, "must name what it aims at"
        # Explicitly NOT the ownership-drift wording — the whole point is that
        # the victim's uid is not this bot's tier config drifting.
        assert "can't read its own tier config" not in t.detail
        assert "expected bot uid" not in t.detail
        assert t.apply is None, "unlinking is destructive; no repair is offered"
        assert t.fix_description == ""
        assert recorded_argv == [], recorded_argv
        # Never relabelled — the gate held before this fix too, and still does.
        assert victim_file.stat().st_uid == os.getuid()
        assert oct(victim_file.stat().st_mode)[-3:] == "600"

    def test_dangling_openclaw_plant_is_not_reported_as_absent(
        self, tmp_path, monkeypatch, recorded_argv
    ):
        """A ``.openclaw`` aimed at nothing made the leaf lstat raise
        FileNotFoundError, so the plant was reported as ``(not present — nothing
        to enforce)`` with ``ok=True`` — completely silent. The walk runs first,
        so it sees the link itself."""
        oc = self._wire(tmp_path, monkeypatch, mkdir_oc=False)
        oc.symlink_to(tmp_path / "gone-tree")

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert "not present" not in t.detail
        assert t.apply is None
        assert recorded_argv == [], recorded_argv

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_unreachable_branch_is_unchanged(self, tmp_path, monkeypatch,
                                             recorded_argv):
        """The EACCES branch has the one genuinely SELF-HEALING apply in this
        function (``_reassert_evolve_read_acl`` recomputes the clamped mask) and
        it is the live Linux state. The walk must not swallow it into the
        report-only shape — an unwalkable component is not a plant.

        A 0000 ``.openclaw`` is exactly that shape: ``lstat`` on the dir ITSELF
        still succeeds (it needs traverse on the HOME, not on the dir), so the
        walk completes cleanly and the LEAF lstat is what EACCESes."""
        oc = self._wire(tmp_path, monkeypatch)
        (oc / "evolve-tiers.json").write_text("{}")
        os.chmod(oc, 0o000)
        try:
            t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
            assert not t.ok
            assert "unreachable" in t.detail
            assert "SYMLINK" not in t.detail
            assert t.apply is not None, "the self-healing ACL re-assert must survive"
            assert t.fix_description == f"re-assert evolve read ACL on {oc}"
        finally:
            os.chmod(oc, 0o700)
        assert recorded_argv == [], recorded_argv

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_unreachable_home_defers_to_the_leaf_too(self, tmp_path, monkeypatch,
                                                     recorded_argv):
        """The other OSError flavour, one level up: a clamped HOME makes
        ``_planted_intermediate``'s OWN lstat of ``.openclaw`` raise, so the
        ``OSError → None`` branch is what defers. Same requirement — the
        repairable ``unreachable`` finding, not a bogus plant.

        KNOWN GAP this assertion locks in, stated so it is a decision and not an
        oversight (independent review of this change; PRE-EXISTING, and strictly
        NARROWED by the walk this change adds). Clamped-home is the one state
        where a planted ``.openclaw`` is invisible to the walk — and the apply
        preserved here, ``_reassert_evolve_read_acl``, is NOT gated by
        ``_sudo_dest_ok``: ``grant_read_recursive`` is a root ``setfacl -R`` /
        ``chmod +a`` that FOLLOWS a symlink argument. A bot that chmods its own
        home to 0700 and points ``.openclaw`` at a victim tree therefore still
        summons a root ACL write onto it. Gating the ACL grants is its own chip
        (a natural fit alongside the deferred ``sudo_dest.py`` consolidation);
        deferring on OSError remains right here, because the alternative strands
        the live Linux mask clamp with no repair at all."""
        oc = self._wire(tmp_path, monkeypatch)
        (oc / "evolve-tiers.json").write_text("{}")
        os.chmod(oc.parent, 0o000)
        try:
            t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
            assert not t.ok
            assert "unreachable" in t.detail
            assert "SYMLINK" not in t.detail
            assert t.apply is not None
        finally:
            os.chmod(oc.parent, 0o700)
        assert recorded_argv == [], recorded_argv

    def test_real_tree_still_detects_and_repairs(self, tmp_path, monkeypatch,
                                                 recorded_argv):
        """The availability half: a real ``.openclaw`` keeps the ordinary
        ownership-drift finding AND its working repair."""
        oc = self._wire(tmp_path, monkeypatch, uid_delta=1)
        dest = oc / "evolve-tiers.json"
        dest.write_text("{}")

        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert not t.ok
        assert "SYMLINK" not in t.detail
        assert "can't read its own tier config" in t.detail
        assert t.apply is not None and t.apply() is True
        assert recorded_argv == [
            ["sudo", "/usr/sbin/chown", "team_bot_a:staff", str(dest)],
            ["sudo", "/bin/chmod", "644", str(dest)],
        ], recorded_argv

    def test_absent_file_under_a_real_openclaw_is_still_a_clean_skip(
        self, tmp_path, monkeypatch
    ):
        """A bot that has no ``evolve-tiers.json`` yet must not start reporting a
        plant — an absent leaf under a real dir is not a symlink."""
        self._wire(tmp_path, monkeypatch)
        t = self._check(scp.check_bot_tiers_ownership("team_bot_a"))
        assert t.ok and "not present" in t.detail


# ── the two sibling sudo-chmod sites ─────────────────────────────────────────


class TestSecretConfigChmodSymlinkGate:
    def test_symlinked_dest_issues_no_chmod(self, tmp_path, recorded_argv):
        oc = _pod(tmp_path)
        victim = _victim(tmp_path)
        dest = oc / "openclaw.json"
        dest.symlink_to(victim)

        assert scp.chmod_secret_config(dest) is False
        assert recorded_argv == [], recorded_argv
        assert oct(victim.stat().st_mode)[-3:] == "600"  # untouched
        assert dest.is_symlink()

    def test_real_file_still_chmods_600(self, tmp_path, recorded_argv):
        dest = _pod(tmp_path) / "openclaw.json"
        dest.write_text("{}")
        assert scp.chmod_secret_config(dest) is True
        assert recorded_argv == [["sudo", "/bin/chmod", "600", str(dest)]], recorded_argv

    def test_nested_relpath_still_chmods_600(self, tmp_path, recorded_argv):
        """``BOT_SECRET_CONFIG_RELPATHS`` is not all flat filenames — the
        anchored ancestor walk must not reject the nested ones' real,
        legitimate shape (every intermediate is a genuine directory here)."""
        oc = _pod(tmp_path)
        for rel in scp.BOT_SECRET_CONFIG_RELPATHS:
            dest = oc / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("{}")
            recorded_argv.clear()
            assert scp.chmod_secret_config(dest) is True, rel
            assert recorded_argv == [["sudo", "/bin/chmod", "600", str(dest)]], rel

    def test_dest_outside_any_openclaw_tree_is_refused(self, tmp_path, recorded_argv):
        """The anchor is derived from the path's ``.openclaw`` component, so a
        dest that has none cannot have its trust boundary established. Refuse
        rather than fall back to the weaker unanchored check — pinned because
        the fallback is the tempting change and it silently reopens the hole
        for exactly the caller that is off the known shape."""
        dest = tmp_path / "openclaw.json"   # no `.openclaw` ancestor anywhere
        dest.write_text("{}")
        assert scp.chmod_secret_config(dest) is False
        assert recorded_argv == [], recorded_argv

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_unverifiable_dest_fails_closed_and_defers(self, tmp_path, recorded_argv):
        """DELIBERATE, and the riskiest behavior change in the D-2 fix: when
        evolve cannot lstat the dest — the live Linux state where the OC gateway
        0700-hardens ``.openclaw`` and clamps evolve's traverse ACE mask — the
        chmod is REFUSED rather than issued, even though root would have carried
        it out. Enforcement defers to ``check_bot_secret_modes``' unreachable →
        re-assert-ACL drift, which converges the mode on the next
        ``ensure_pod_perms`` pass.

        Not softened to "proceed when unverifiable": the bot controls
        ``.openclaw``'s modes, so a blinding EACCES is itself attacker-arrangeable
        and pairing it with a planted link would restore the primitive."""
        parent = _pod(tmp_path)          # a real `.openclaw`, so the refusal
        dest = parent / "openclaw.json"  # can only be the unverifiable-dest one
        dest.write_text("{}")
        os.chmod(parent, 0o000)
        try:
            assert scp.chmod_secret_config(dest) is False
            assert recorded_argv == [], recorded_argv
        finally:
            os.chmod(parent, 0o700)


class TestCheckBotSecretModesSymlinkDetection:
    """The tiers detector's twin. It ALSO used a following ``stat()``, and the
    two failure shapes there are worse: a live link reports the VICTIM's mode as
    this bot's token file drifting, and a dangling link reports "not present"."""

    def _wire(self, tmp_path, monkeypatch):
        home = tmp_path / "team_bot_a"
        (home / ".openclaw").mkdir(parents=True)
        monkeypatch.setattr(scp, "_user_home", lambda u: home)
        return home / ".openclaw"

    def _check(self, checks, rel):
        t = [c for c in checks if c.target.endswith(rel)]
        assert t, f"no {rel} check produced"
        return t[0]

    def test_live_symlink_is_flagged_as_symlink_not_mode_drift(
        self, tmp_path, monkeypatch
    ):
        oc = self._wire(tmp_path, monkeypatch)
        victim = tmp_path / "victim-root-owned.conf"
        victim.write_text("x")
        os.chmod(victim, 0o644)  # a following stat would report THIS as drift
        (oc / "openclaw.json").symlink_to(victim)

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), "/openclaw.json")
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert str(victim) in t.detail
        assert "token-bearing; expected" not in t.detail  # not the mode wording
        assert t.apply is None, "a symlink must never be handed the root chmod"

    def test_dangling_symlink_is_not_reported_as_absent(self, tmp_path, monkeypatch):
        oc = self._wire(tmp_path, monkeypatch)
        (oc / "openclaw.json").symlink_to(tmp_path / "gone.conf")

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), "/openclaw.json")
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert "not present" not in t.detail

    def test_hard_link_is_flagged_and_offers_no_repair(self, tmp_path, monkeypatch):
        oc = self._wire(tmp_path, monkeypatch)
        victim = tmp_path / "victim-root-owned.conf"
        victim.write_text("x")
        os.chmod(victim, 0o644)  # would read as mode drift on the victim
        os.link(victim, oc / "openclaw.json")

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), "/openclaw.json")
        assert not t.ok
        assert "HARD LINK" in t.detail
        assert "token-bearing; expected" not in t.detail
        assert t.apply is None

    def test_real_file_mode_drift_still_detected_and_repaired(
        self, tmp_path, monkeypatch, recorded_argv
    ):
        oc = self._wire(tmp_path, monkeypatch)
        dest = oc / "openclaw.json"
        dest.write_text("{}")
        os.chmod(dest, 0o644)

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), "/openclaw.json")
        assert not t.ok
        assert "token-bearing; expected" in t.detail
        assert t.apply is not None
        assert t.apply() is True
        assert recorded_argv == [["sudo", "/bin/chmod", "600", str(dest)]]

    def test_correct_mode_still_passes(self, tmp_path, monkeypatch):
        oc = self._wire(tmp_path, monkeypatch)
        dest = oc / "openclaw.json"
        dest.write_text("{}")
        os.chmod(dest, 0o600)
        t = self._check(scp.check_bot_secret_modes("team_bot_a"), "/openclaw.json")
        assert t.ok, t.detail


class TestStripBotPrivateAclSymlinkGate:
    def test_symlinked_private_secret_is_skipped(self, tmp_path, monkeypatch,
                                                 recorded_argv):
        """The ACL clear follows a link just as the chmod does, so BOTH must be
        skipped — assert the perms seam was never asked to clear the victim."""
        cleared: list[str] = []

        class _FakePerms:
            def clear_acl(self, path):
                cleared.append(str(path))
                return True

        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms())
        oc = tmp_path / ".openclaw"
        oc.mkdir()
        victim = _victim(tmp_path)
        (oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]).symlink_to(victim)

        assert scp.strip_bot_private_acl(oc) is False
        assert cleared == [], cleared
        assert recorded_argv == [], recorded_argv
        assert oct(victim.stat().st_mode)[-3:] == "600"

    def test_dangling_private_secret_link_is_not_swallowed(self, tmp_path,
                                                           monkeypatch,
                                                           recorded_argv):
        """``exists_or_unreachable`` is ``Path.exists()``, which FOLLOWS — a
        dangling link read as "absent" and the plant was skipped silently with
        ``ok`` left True. The gate runs FIRST now, so it sees the link."""
        cleared: list[str] = []

        class _FakePerms:
            def clear_acl(self, path):
                cleared.append(str(path))
                return True

        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms())
        oc = tmp_path / ".openclaw"
        oc.mkdir()
        (oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]).symlink_to(tmp_path / "gone.json")

        assert scp.strip_bot_private_acl(oc) is False
        assert cleared == [] and recorded_argv == []

    def test_absent_private_secret_is_still_a_clean_skip(self, tmp_path, monkeypatch,
                                                        recorded_argv):
        """Gating before the existence probe must not turn a genuinely absent
        file into a refusal — the gate allows an absent dest."""
        class _FakePerms:
            def clear_acl(self, path):
                raise AssertionError("no clear_acl expected")

        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms())
        oc = tmp_path / ".openclaw"
        oc.mkdir()
        assert scp.strip_bot_private_acl(oc) is True
        assert recorded_argv == []

    def test_real_private_secret_still_stripped(self, tmp_path, monkeypatch,
                                                recorded_argv):
        cleared: list[str] = []

        class _FakePerms:
            def clear_acl(self, path):
                cleared.append(str(path))
                return True

        monkeypatch.setattr(scp, "_get_perms", lambda: _FakePerms())
        oc = tmp_path / ".openclaw"
        oc.mkdir()
        tokens = oc / scp.BOT_PRIVATE_SECRET_RELPATHS[0]
        tokens.write_text("{}")

        assert scp.strip_bot_private_acl(oc) is True
        assert cleared == [str(tokens)]
        assert recorded_argv == [["sudo", "/bin/chmod", "600", str(tokens)]]


# ── the intermediate-component leg (#3566 audit D-2 residual) ────────────────


class TestNestedRelpathIntermediateComponents:
    """The residual #3597 deliberately left open, and why it is a call-site test
    rather than only a primitive one.

    ``BOT_SECRET_CONFIG_RELPATHS`` carries two NESTED entries —
    ``agents/main/agent/auth-profiles.json`` and ``workspace/.git/config`` —
    whose ``agents`` / ``agents/main`` / ``workspace`` components live inside
    the bot-owned ``.openclaw/``. The unanchored gate lstats only the dest and
    its parent, both of which are real objects when the plant is upstream of
    them, so it passed. This module is where the anchor gets supplied
    (``_bot_home_anchor``); the assertion that matters is that a refusal issues
    NO privileged argv, so it is made on the recorded argv, never the bool.

    ``chown_chmod_bot_config`` — the ownership-transfer leg, the one that would
    be an escalation rather than a relabel — is genuinely out of reach here:
    ``_bot_user_from_path`` pins the exact ``<home_root>/<user>/.openclaw/<file>``
    shape and ``BOT_OWNED_CONFIG_RELPATHS`` is flat by contract (pinned by
    ``test_owned_relpaths_stay_flat`` in test_tiers_config_ownership.py). What
    is reachable is ``chmod_secret_config``'s root ``chmod 600`` — DoS/relabel,
    but a root command an attacker aims.
    """

    NESTED = [r for r in scp.BOT_SECRET_CONFIG_RELPATHS if "/" in r]

    def test_the_nested_relpaths_are_still_nested(self):
        """If someone flattens these, this whole class stops testing anything —
        fail loudly instead of passing vacuously."""
        assert sorted(self.NESTED) == [
            "agents/main/agent/auth-profiles.json",
            "workspace/.git/config",
        ], scp.BOT_SECRET_CONFIG_RELPATHS

    @pytest.mark.parametrize("rel", NESTED)
    def test_symlinked_intermediate_issues_no_chmod(self, tmp_path, recorded_argv, rel):
        """Plant at the first bot-owned component. Both of the unanchored gate's
        lstats see real objects — the dest is a real regular file and its parent
        a real directory — so this is the case that used to sail through."""
        oc = _pod(tmp_path)
        head, *tail = Path(rel).parts
        victim_tree = tmp_path / "victim-tree"
        victim_file = victim_tree.joinpath(*tail)
        victim_file.parent.mkdir(parents=True)
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o644)
        (oc / head).symlink_to(victim_tree)
        dest = oc / rel

        assert not dest.is_symlink() and dest.is_file()  # the deceptive shape

        assert scp.chmod_secret_config(dest) is False
        assert recorded_argv == [], recorded_argv
        assert oct(victim_file.stat().st_mode)[-3:] == "644"  # never relabelled

    @pytest.mark.parametrize("rel", NESTED)
    def test_symlinked_deep_intermediate_issues_no_chmod(
        self, tmp_path, recorded_argv, rel
    ):
        """The plant at the LAST intermediate — one level above ``path.parent``,
        the closest an attacker can get while still being invisible to the two
        unanchored lstats."""
        oc = _pod(tmp_path)
        parts = Path(rel).parts
        real_prefix, planted, tail = parts[:-3], parts[-3], parts[-2:]
        (oc.joinpath(*real_prefix)).mkdir(parents=True, exist_ok=True)
        victim_tree = tmp_path / "victim-deep"
        victim_file = victim_tree.joinpath(*tail)
        victim_file.parent.mkdir(parents=True)
        victim_file.write_text("VICTIM")
        (oc.joinpath(*real_prefix, planted)).symlink_to(victim_tree)

        assert scp.chmod_secret_config(oc / rel) is False
        assert recorded_argv == [], recorded_argv

    @pytest.mark.parametrize("rel", NESTED)
    def test_drift_apply_also_refuses(self, tmp_path, monkeypatch, recorded_argv, rel):
        """The repair the hourly ``pod_perms_drift`` pass calls is
        ``chmod_secret_config`` via ``check_bot_secret_modes``' ``apply``. A
        plant that survives one deploy is re-offered every hour, so the gate has
        to hold on the apply path too — otherwise the detector SUMMONS the root
        command onto the attacker's target rather than merely permitting it."""
        home = tmp_path / "team_bot_a"
        oc = _pod(tmp_path)
        monkeypatch.setattr(scp, "_user_home", lambda u: home)
        head, *tail = Path(rel).parts
        victim_tree = tmp_path / "victim-tree"
        victim_file = victim_tree.joinpath(*tail)
        victim_file.parent.mkdir(parents=True)
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o644)  # so the detector sees "drift" and offers a repair
        (oc / head).symlink_to(victim_tree)

        checks = [
            c for c in scp.check_bot_secret_modes("team_bot_a")
            if c.target.endswith(rel)
        ]
        assert checks, f"no check produced for {rel}"
        if checks[0].apply is not None:
            assert checks[0].apply() is False
        assert recorded_argv == [], recorded_argv
        assert oct(victim_file.stat().st_mode)[-3:] == "644"

    @pytest.mark.parametrize("rel", NESTED)
    def test_real_nested_tree_still_chmods_600(self, tmp_path, recorded_argv, rel):
        """The availability half of the contract: a legitimate nested dest whose
        every intermediate is a genuine directory must still be repaired."""
        oc = _pod(tmp_path)
        dest = oc / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}")
        assert scp.chmod_secret_config(dest) is True
        assert recorded_argv == [["sudo", "/bin/chmod", "600", str(dest)]]

    def test_anchor_is_the_bot_home_not_the_profile_home_root(self, tmp_path):
        """``_bot_home_anchor`` derives the trust boundary from the path's own
        ``.openclaw`` component, NOT from ``_get_profile().user_home_root``.

        Pinned because the profile-derived version is the tempting one and it
        reintroduces a known failure shape: an unset or mismatched profile would
        make every repair in this module refuse — fleet-wide, silently — which
        is exactly what ``_bot_user_from_path``'s hardcoded ``/Users`` did to the
        Linux pod (#3566 audit B-1). ``.openclaw`` is spelled the same on both
        platforms."""
        import dataclasses

        from platform_profile import LINUX, MACOS, get_profile, set_profile

        p = Path("/Users/team_bot_a/.openclaw/agents/main/agent/auth-profiles.json")
        saved = get_profile()  # set_profile is process-global; don't leak a bogus
        try:                   # home root into whatever runs next in this shard
            for prof in (
                MACOS, LINUX, dataclasses.replace(MACOS, user_home_root="/nope")
            ):
                set_profile(prof)
                assert scp._bot_home_anchor(p) == Path("/Users/team_bot_a")
        finally:
            set_profile(saved)
        assert scp._bot_home_anchor(Path("/tmp/x/openclaw.json")) is None
        assert scp._bot_home_anchor(Path(".openclaw/openclaw.json")) is None  # relative
        # Absolute `/.openclaw/…` anchors at `/` — degenerate, but walks strictly
        # more, so it is allowed rather than refused.
        assert scp._bot_home_anchor(Path("/.openclaw/openclaw.json")) == Path("/")
        # Two `.openclaw` components → the SHALLOWEST wins (walks strictly more).
        assert scp._bot_home_anchor(
            Path("/home/b/.openclaw/workspace/.openclaw/openclaw.json")
        ) == Path("/home/b")


class TestCheckBotSecretModesIntermediateSymlinkDetection:
    """The DETECTOR side of the intermediate-component residual.

    ``TestNestedRelpathIntermediateComponents`` above pins that the anchored gate
    stops the root ``chmod 600`` from firing through a planted intermediate. What
    it does NOT fix is what the detector REPORTS: ``path.lstat()`` does not follow
    a link at the LEAF (that is #3597) but the kernel resolves every component
    ABOVE it, so with ``.openclaw/workspace`` planted the detector lstats the
    VICTIM's ``.git/config`` and reports the victim's mode as this bot's token
    file drifting. The gate holds — no privileged argv, victim untouched — but
    the finding is permanently ``ok=False`` with an apply that can never succeed:
    ``ensure_pod_perms`` logs "fix did not return success" every deploy, the
    hourly ``pod_perms_drift`` Signal never ``sweep_resolve``s, and the reason
    goes only to the module logger.

    So: same treatment as the leaf-symlink branch — a distinct, report-only
    finding naming the planted component, ``apply=None``.
    """

    NESTED = [r for r in scp.BOT_SECRET_CONFIG_RELPATHS if "/" in r]

    def _wire(self, tmp_path, monkeypatch, bot: str = "team_bot_a",
              mkdir_oc: bool = True) -> Path:
        """Point ``_user_home`` at a real tree AND pin the macOS profile at
        ``tmp_path`` (via ``_pod``): ``check_bot_secret_modes`` reads the mode
        through the perms seam, and ``set_profile`` is process-global, so a
        sibling test that left LINUX in place would otherwise send this at
        ``getfacl``."""
        home = tmp_path / bot
        oc = _pod(tmp_path, bot) if mkdir_oc else (home / ".openclaw")
        if not mkdir_oc:
            home.mkdir(parents=True, exist_ok=True)
            _pod(tmp_path, "profile-anchor-only")  # set_profile without the dir
        monkeypatch.setattr(scp, "_user_home", lambda u: home)
        return oc

    def _check(self, checks, rel):
        t = [c for c in checks if c.target.endswith(rel)]
        assert t, f"no {rel} check produced"
        return t[0]

    @pytest.mark.parametrize("rel", NESTED)
    def test_first_intermediate_plant_names_the_component_not_the_victim_mode(
        self, tmp_path, monkeypatch, recorded_argv, rel
    ):
        """THE finding. Victim at 0644 so the old code reported
        ``mode=0o644 (token-bearing; expected 0o600)`` — the victim's mode,
        attributed to this bot's token path, with a repair that can only refuse.
        """
        oc = self._wire(tmp_path, monkeypatch)
        head, *tail = Path(rel).parts
        victim_tree = tmp_path / "victim-tree"
        victim_file = victim_tree.joinpath(*tail)
        victim_file.parent.mkdir(parents=True)
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o644)
        (oc / head).symlink_to(victim_tree)

        # The deceptive shape: the leaf is a real regular file, not a link.
        assert not (oc / rel).is_symlink() and (oc / rel).is_file()

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert str(oc / head) in t.detail, "must name the PLANTED component"
        assert str(victim_tree) in t.detail, "must name what it aims at"
        # Not the mode wording — the whole point is that the victim's mode is
        # not this bot's token file drifting.
        assert "token-bearing; expected" not in t.detail
        assert t.apply is None, "unlinking is destructive; no repair is offered"
        assert t.fix_description == ""
        assert recorded_argv == [], recorded_argv
        assert oct(victim_file.stat().st_mode)[-3:] == "644"  # never relabelled

    @pytest.mark.parametrize("rel", NESTED)
    def test_deep_intermediate_plant_is_also_named(
        self, tmp_path, monkeypatch, recorded_argv, rel
    ):
        """The plant at the LAST intermediate — one level above ``path.parent``,
        the closest an attacker gets while the leaf lstat still sees a real
        regular file."""
        oc = self._wire(tmp_path, monkeypatch)
        parts = Path(rel).parts
        real_prefix, planted, tail = parts[:-3], parts[-3], parts[-2:]
        (oc.joinpath(*real_prefix)).mkdir(parents=True, exist_ok=True)
        victim_tree = tmp_path / "victim-deep"
        victim_file = victim_tree.joinpath(*tail)
        victim_file.parent.mkdir(parents=True)
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o644)
        link = oc.joinpath(*real_prefix, planted)
        link.symlink_to(victim_tree)

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert not t.ok
        assert str(link) in t.detail
        assert "token-bearing; expected" not in t.detail
        assert t.apply is None
        assert recorded_argv == [], recorded_argv

    def test_openclaw_itself_is_in_the_walk(self, tmp_path, monkeypatch,
                                            recorded_argv):
        """``.openclaw`` is the SHALLOWEST attacker-writable component — the bot
        owns its home, so it can replace that entry — and it is an intermediate
        of the FLAT relpaths too. Same boundary ``_bot_home_anchor`` picks (it
        anchors one level above, so the walk starts here)."""
        oc = self._wire(tmp_path, monkeypatch, mkdir_oc=False)
        home = oc.parent
        victim_tree = tmp_path / "victim-oc"
        victim_tree.mkdir()
        victim_file = victim_tree / "openclaw.json"
        victim_file.write_text("VICTIM")
        os.chmod(victim_file, 0o644)
        (home / ".openclaw").symlink_to(victim_tree)

        checks = scp.check_bot_secret_modes("team_bot_a")
        # EVERY relpath routes through .openclaw, so every one of them is
        # unverifiable and must say so — none may report a mode.
        assert len(checks) == len(scp.BOT_SECRET_CONFIG_RELPATHS)
        for c in checks:
            assert not c.ok, c
            assert str(home / ".openclaw") in c.detail
            assert "token-bearing; expected" not in c.detail
            assert c.apply is None
        assert recorded_argv == [], recorded_argv
        assert oct(victim_file.stat().st_mode)[-3:] == "644"

    @pytest.mark.parametrize("rel", NESTED)
    def test_dangling_intermediate_is_not_reported_as_absent(
        self, tmp_path, monkeypatch, rel
    ):
        """A link to nowhere made the leaf lstat raise FileNotFoundError, so the
        plant was reported as ``(not present — nothing to enforce)`` with
        ``ok=True`` — completely silent. The walk runs first, so it sees it."""
        oc = self._wire(tmp_path, monkeypatch)
        head = Path(rel).parts[0]
        (oc / head).symlink_to(tmp_path / "gone-tree")

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert not t.ok
        assert "SYMLINK" in t.detail
        assert "not present" not in t.detail

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    @pytest.mark.parametrize("clamp_at", ["agents/main/agent", "agents/main"])
    def test_unreachable_branch_is_unchanged(self, tmp_path, monkeypatch,
                                             recorded_argv, clamp_at):
        """The EACCES branch has a genuinely SELF-HEALING apply
        (``_reassert_evolve_read_acl`` recomputes the clamped mask) — the one
        finding in this function whose repair converges. The intermediate walk
        must not swallow it into the report-only shape: an unwalkable component
        is not a plant.

        Parametrized over BOTH clamp depths because they exit through different
        code paths and only one of them is the live shape:
          * ``agents/main/agent`` — the LEAF's parent, and what the OC gateway
            actually hardens on auth writes. The walk completes cleanly (every
            component lstats fine) and the LEAF lstat raises. This is the fleet
            path, so it is the one that must not regress.
          * ``agents/main`` — mid-walk, so ``_planted_intermediate``'s own
            ``lstat`` is what EACCESes and the OSError → ``None`` branch is what
            defers. Covers the new code directly."""
        oc = self._wire(tmp_path, monkeypatch)
        rel = "agents/main/agent/auth-profiles.json"
        assert rel in scp.BOT_SECRET_CONFIG_RELPATHS
        dest = oc / rel
        dest.parent.mkdir(parents=True)
        dest.write_text("{}")
        clamped = oc / clamp_at
        os.chmod(clamped, 0o000)
        try:
            t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
            assert not t.ok
            assert "unreachable" in t.detail
            assert "SYMLINK" not in t.detail
            assert t.apply is not None, "the self-healing ACL re-assert must survive"
            assert t.fix_description == f"re-assert evolve read ACL on {oc}"
        finally:
            os.chmod(clamped, 0o700)
        assert recorded_argv == [], recorded_argv

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory perms")
    def test_clamp_hiding_a_deeper_plant_is_accepted_and_self_limiting(
        self, tmp_path, monkeypatch, recorded_argv
    ):
        """The ACCEPTED consequence of deferring on OSError, pinned so the
        trade-off is a decision rather than an accident: a 0000 dir hides a plant
        BELOW it. The finding degrades to ``unreachable`` — still ``ok=False``
        drift, never silent — and that finding's apply re-asserts the read ACL,
        which recomputes the clamp, so the next pass names the plant."""
        oc = self._wire(tmp_path, monkeypatch)
        rel = "agents/main/agent/auth-profiles.json"
        (oc / "agents" / "main").mkdir(parents=True)
        (oc / "agents" / "main" / "agent").symlink_to(tmp_path / "victim-hidden")
        os.chmod(oc / "agents" / "main", 0o000)
        try:
            t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
            assert not t.ok and "unreachable" in t.detail
            assert t.apply is not None
        finally:
            os.chmod(oc / "agents" / "main", 0o700)
        # Clamp lifted (what the apply achieves): the plant is now named.
        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert not t.ok
        assert str(oc / "agents" / "main" / "agent") in t.detail
        assert "SYMLINK" in t.detail and t.apply is None
        assert recorded_argv == [], recorded_argv

    @pytest.mark.parametrize("rel", NESTED)
    def test_real_nested_tree_still_detects_and_repairs(
        self, tmp_path, monkeypatch, recorded_argv, rel
    ):
        """The availability half: a legitimate nested dest whose every component
        is a real directory keeps the ordinary mode-drift finding AND its
        working repair."""
        oc = self._wire(tmp_path, monkeypatch)
        dest = oc / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}")
        os.chmod(dest, 0o644)

        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert not t.ok
        assert "token-bearing; expected" in t.detail
        assert t.apply is not None and t.apply() is True
        assert recorded_argv == [["sudo", "/bin/chmod", "600", str(dest)]]

        recorded_argv.clear()
        os.chmod(dest, 0o600)
        assert self._check(scp.check_bot_secret_modes("team_bot_a"), rel).ok

    @pytest.mark.parametrize("rel", NESTED)
    def test_absent_nested_tree_is_still_a_clean_skip(self, tmp_path, monkeypatch,
                                                      rel):
        """A bot whose ``agents/``/``workspace/`` were never created must not
        start reporting a plant — an absent component is not a symlink."""
        self._wire(tmp_path, monkeypatch)
        t = self._check(scp.check_bot_secret_modes("team_bot_a"), rel)
        assert t.ok and "not present" in t.detail


class TestSecretRelpathComponentHelpers:
    """``_secret_relpath_parent_dirs`` was re-expressed on top of the new
    per-relpath ``_oc_relpath_ancestors`` so the two cannot drift. Pin that
    the union it feeds — the ACL-reassert callers' shallow-first ordering
    contract — is byte-for-byte what it was."""

    OC = Path("/Users/team_bot_a/.openclaw")

    def test_ancestors_start_at_openclaw_and_stop_at_the_parent(self):
        assert scp._oc_relpath_ancestors(self.OC, "openclaw.json") == [self.OC]
        assert scp._oc_relpath_ancestors(
            self.OC, "agents/main/agent/auth-profiles.json"
        ) == [
            self.OC,
            self.OC / "agents",
            self.OC / "agents/main",
            self.OC / "agents/main/agent",
        ]
        assert scp._oc_relpath_ancestors(self.OC, "workspace/.git/config") == [
            self.OC, self.OC / "workspace", self.OC / "workspace/.git",
        ]

    def test_parent_dirs_union_is_unchanged_and_shallow_first(self):
        """The pre-refactor implementation, inlined — this is the ordering the
        ``reassert_mask`` callers depend on (a clamped ancestor hides a clamped
        child until the ancestor is re-widened first)."""
        expected: set[Path] = set()
        for rel in scp.BOT_SECRET_CONFIG_RELPATHS:
            parent = (self.OC / rel).parent
            while parent != self.OC:
                expected.add(parent)
                parent = parent.parent
        assert scp._secret_relpath_parent_dirs(self.OC) == sorted(
            expected, key=lambda p: (len(p.parts), str(p))
        )
        assert self.OC not in scp._secret_relpath_parent_dirs(self.OC)

    def test_secret_relpaths_are_relative_and_dotdot_free(self):
        """The CONTRACT ``_oc_relpath_ancestors`` cannot enforce itself.

        The gate side (``evolve_util._intermediates_below_anchor``) RAISES on an
        absolute or escaping relpath. The detector side cannot: the same helper
        feeds the ACL-reassert callers, where a raise aborts a deploy. So the
        shape is pinned on the constant instead — an absolute entry would make
        ``oc_dir / rel`` reset to the filesystem root and walk components outside
        the bot tree while the gate refuses that very path, splitting the two
        classifications apart. (The pre-refactor loop INFINITE-LOOPED on the same
        input, so nothing regressed; this pins that it stays unreachable.)"""
        for rel in scp.BOT_SECRET_CONFIG_RELPATHS:
            p = Path(rel)
            assert rel, "empty relpath"
            assert not p.is_absolute(), rel
            assert ".." not in p.parts, rel
            assert p.parts[-1] not in (".", ""), rel

    def test_planted_intermediate_is_none_when_unwalkable(self, tmp_path):
        """``None`` on ANY OSError, deliberately — the leaf stat owns the
        absent / EACCES classification and both of its findings are better than
        the report-only one this helper produces. Both OSError flavours:
        ENOENT (absent component / absent ``.openclaw``) and EACCES (the live
        Linux mask clamp), which are different kernel paths."""
        oc = tmp_path / ".openclaw"
        oc.mkdir()
        # ENOENT — the intermediate, then the whole .openclaw
        assert scp._planted_intermediate(oc, "workspace/.git/config") is None
        assert scp._planted_intermediate(
            tmp_path / "nope" / ".openclaw", "openclaw.json"
        ) is None
        # EACCES — a real but untraversable intermediate mid-walk
        if os.geteuid() != 0:  # root bypasses directory perms
            (oc / "workspace" / ".git").mkdir(parents=True)
            os.chmod(oc / "workspace", 0o000)
            try:
                assert scp._planted_intermediate(
                    oc, "workspace/.git/config"
                ) is None
            finally:
                os.chmod(oc / "workspace", 0o700)
        # …and the healthy case is still detected, so the above isn't vacuous
        (oc / "workspace" / ".git").mkdir(parents=True, exist_ok=True)
        assert scp._planted_intermediate(oc, "workspace/.git/config") is None
        comp = oc / "agents"
        comp.symlink_to(tmp_path / "elsewhere")
        found = scp._planted_intermediate(
            oc, "agents/main/agent/auth-profiles.json"
        )
        assert found is not None and found[0] == comp


# ── the gate is reachable from this module at all ────────────────────────────


def test_module_imports_the_shared_gate():
    """The gate must be the SHARED ``evolve_util`` one, not a local re-roll —
    the same primitive ``oc_model`` and ``migrate_model_roles`` call, so a fix
    to the lstat logic lands everywhere at once (#3591)."""
    from evolve_util import assert_safe_sudo_dest as shared

    assert scp.assert_safe_sudo_dest is shared

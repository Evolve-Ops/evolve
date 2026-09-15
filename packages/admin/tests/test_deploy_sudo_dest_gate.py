"""#3566 audit D-2 remainder — deploy.py's root ``cp``/``chown`` must not follow a plant.

#3597 gated the ``chown``/``chmod`` legs inside ``secret_config_perms``. The
commands gated HERE are the ones that sit beside them in ``deploy.py``, and the
primitive they hand an attacker is strictly larger:

  * ``sudo /bin/cp <staged> <dest>`` — arbitrary root WRITE of file content.
    ``cp`` has no flag that refuses to follow a link at the destination.
  * ``sudo chown <bot>:staff <dest>`` — a PERMANENT ownership transfer. No
    ``-h``, so it relabels the link's target, not the link.

The destinations are all inside ``/Users/<bot>/.openclaw/`` or its
``workspace/`` — directories the BOT owns and can replace a file in at will,
with a symlink or (on macOS) a hard link. Every one of these writers runs on a
deploy or a schedule against that exact path, so a plant is persistent and
self-triggering rather than a race that has to be won.

What is pinned here is not "the helper returns False" — it is that **a refusal
issues no privileged argv at all**, asserted on the recorded ``sudo`` argv, and
that the victim comes out byte-identical. The legitimate path is pinned in the
same breath, because a gate that also breaks the real write is not a fix.

The shared primitive's own unit tests live in
``packages/analyzer/tests/test_evolve_util.py``; the wrapper's are at the top of
this file. Placeholder bot names per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from evolve_admin import deploy
from evolve_admin import sudo_dest


# ── Harness ──────────────────────────────────────────────────────────────────


VICTIM_TEXT = "root-owned content that must never change"


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A REAL ``<home_root>/<bot>/.openclaw`` tree with the home root re-pointed.

    Real, not virtual: the gate lstats the destination on purpose, so a fake
    filesystem would prove nothing about it. ``user_home`` falls back to
    ``{user_home_root}/{user}`` for accounts that do not exist in pwd, which is
    exactly the case for a placeholder bot name on a dev box or CI runner.
    """
    from platform_profile import MACOS, get_profile, set_profile

    original = get_profile()
    set_profile(dataclasses.replace(MACOS, user_home_root=str(tmp_path)))
    monkeypatch.setattr(deploy, "_PROFILE", deploy._get_profile())
    oc = tmp_path / "team_bot_a" / ".openclaw"
    (oc / "workspace").mkdir(parents=True, exist_ok=True)
    try:
        yield oc
    finally:
        set_profile(original)


@pytest.fixture
def victim(tmp_path) -> Path:
    """Stand-in for the root-owned file an attacker aims the plant at."""
    v = tmp_path / "victim-network.json"
    v.write_text(VICTIM_TEXT)
    return v


@pytest.fixture
def recorded_argv(monkeypatch) -> list[list[str]]:
    """Record every PRIVILEGED argv deploy.py would run — and run none of them.

    Records only ``sudo …``, deliberately. ``deploy.subprocess`` IS the shared
    stdlib module, so patching ``run`` on it is process-global: an unrelated
    helper shelling out during the same test lands in the list and turns an
    exact-equality assertion into an order-dependent flake (#3597 hit this with
    a ``git log`` version lookup). Filtering to ``sudo`` also states the
    contract these tests are about — what runs as root.

    ``/bin/cat`` reads are answered from the real file so the read-then-repair
    writers reach their write, and ``openclaw config validate`` is answered
    valid so ``safe_write_bot_config`` gets past its schema check.
    """
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):
        argv = [str(x) for x in argv]
        if not argv or argv[0] != "sudo":
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        calls.append(argv)
        out = ""
        if "validate" in argv:
            out = json.dumps({"valid": True})
        elif "/bin/cat" in argv:
            try:
                out = Path(argv[-1]).read_text()
            except OSError:
                return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="nope")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    return calls


def writes_or_chowns(calls: list[list[str]]) -> list[list[str]]:
    """The recorded root argv that write CONTENT or TRANSFER OWNERSHIP.

    These are the two legs this PR closes. Other privileged argv (``cat``,
    ``rm``, ``chmod``, the bot-user ``openclaw`` invocations) are not asserted
    on here: ``chmod`` is #3597's, and the rest neither write attacker-chosen
    content nor hand over an inode.
    """
    return [c for c in calls if len(c) > 1 and Path(c[1]).name in ("cp", "chown")]


def assert_no_privileged_write(calls: list[list[str]], dest: Path, victim: Path) -> None:
    """No cp/chown names the planted ``dest``, and the victim is untouched.

    Scoped to ``dest`` rather than "no cp/chown at all" because some of these
    functions legitimately touch OTHER privileged paths on the way past — e.g.
    ``repair_security_bot_config`` corrects the ownership of the bot's HOME dir,
    whose parent is root-owned and therefore not bot-plantable. Naming the dest
    keeps the assertion exact: a cp or chown carrying this argument is the one
    that would have followed the link onto the victim.
    """
    touching = [c for c in writes_or_chowns(calls) if str(dest) in c]
    assert touching == [], f"gate refused but privileged argv still ran: {touching}"
    assert victim.read_text() == VICTIM_TEXT


# ── The wrapper itself ───────────────────────────────────────────────────────


class TestSudoDestRefusal:
    def test_clean_regular_file_is_allowed(self, tmp_path):
        dest = tmp_path / "openclaw.json"
        dest.write_text("{}")
        assert sudo_dest.sudo_dest_refusal(dest) == ""

    def test_absent_dest_is_allowed(self, tmp_path):
        """A fresh ``cp`` creates a real file — absence is not a refusal."""
        assert sudo_dest.sudo_dest_refusal(tmp_path / "not-yet.json") == ""

    def test_symlinked_dest_is_refused(self, tmp_path, victim):
        dest = tmp_path / "openclaw.json"
        dest.symlink_to(victim)
        assert "SYMLINK" in sudo_dest.sudo_dest_refusal(dest)

    def test_symlinked_parent_is_refused(self, tmp_path, victim):
        real = tmp_path / "real_dir"
        real.mkdir()
        link_dir = tmp_path / "link_dir"
        link_dir.symlink_to(real)
        assert sudo_dest.sudo_dest_refusal(link_dir / "openclaw.json") != ""

    def test_first_unsafe_path_of_several_is_reported(self, tmp_path, victim):
        good = tmp_path / "fine.json"
        good.write_text("{}")
        bad = tmp_path / "planted.json"
        bad.symlink_to(victim)
        assert "planted.json" in sudo_dest.sudo_dest_refusal(good, bad)

    def test_refusal_is_logged_at_error(self, tmp_path, victim, caplog):
        """A caller whose only channel is a ``print`` still leaves a record."""
        dest = tmp_path / "openclaw.json"
        dest.symlink_to(victim)
        with caplog.at_level("ERROR"):
            sudo_dest.sudo_dest_refusal(dest)
        assert any("D-2 gate refused" in r.getMessage() for r in caplog.records)


class TestEaccesRetry:
    """EACCES means "cannot see it YET" — re-widen once, then re-check.

    Fail-closed on the Linux ACL clamp would take every clamped writer offline
    on the VPS pod, which is a self-inflicted outage rather than a defence. A
    SYMLINK verdict, by contrast, is an answer and must never retry.
    """

    def _clamped(self, tmp_path) -> Path:
        """A dest whose parent denies search, so ``lstat`` on it is EACCES."""
        parent = tmp_path / "clamped"
        parent.mkdir()
        dest = parent / "openclaw.json"
        dest.write_text("{}")
        os.chmod(parent, 0o000)
        return dest

    def test_eacces_triggers_one_mask_repair_then_re_checks(self, tmp_path, monkeypatch):
        dest = self._clamped(tmp_path)
        repaired: list[Path] = []

        class FakePerms:
            def reassert_mask(self, path, **kw):
                repaired.append(Path(path))
                os.chmod(path, 0o700)  # the repair actually works
                return True

        monkeypatch.setattr(sudo_dest, "_get_perms", lambda: FakePerms())
        try:
            assert sudo_dest.sudo_dest_refusal(dest) == ""
            assert repaired == [dest.parent]
        finally:
            os.chmod(dest.parent, 0o700)

    def test_still_refused_when_the_repair_does_not_help(self, tmp_path, monkeypatch):
        dest = self._clamped(tmp_path)

        class NoopPerms:
            def reassert_mask(self, path, **kw):
                return True  # macOS: no ACL mask to re-widen

        monkeypatch.setattr(sudo_dest, "_get_perms", lambda: NoopPerms())
        try:
            assert "cannot verify" in sudo_dest.sudo_dest_refusal(dest)
        finally:
            os.chmod(dest.parent, 0o700)

    def test_repair_only_ever_targets_a_VERIFIED_real_directory(self, tmp_path, monkeypatch):
        """``reassert_mask`` is a privileged ``setfacl``, and ``setfacl`` follows
        symlinks. A bot owns its home, so it can make ``.openclaw`` a symlink AND
        ``chmod 0700 ~`` so evolve cannot lstat far enough to see that. The walk
        must therefore never widen a dir it has not lstat-ed as a real one — here
        it stops at ``bot_home`` (real, lstat-able) and refuses rather than
        reaching for the unreadable ``.openclaw`` below it.
        """
        home = tmp_path / "bot_home"
        home.mkdir()
        oc = home / ".openclaw"
        oc.mkdir()
        dest = oc / "openclaw.json"
        dest.write_text("{}")
        os.chmod(home, 0o000)  # evolve cannot even lstat .openclaw now
        calls: list = []

        class SpyPerms:
            def reassert_mask(self, path, **kw):
                calls.append(Path(path))
                return True  # macOS: nothing to re-widen

        monkeypatch.setattr(sudo_dest, "_get_perms", lambda: SpyPerms())
        try:
            assert sudo_dest.sudo_dest_refusal(dest) != ""
            assert calls == [home], f"repair targeted an unverified dir: {calls}"
            for target in calls:
                st = os.lstat(target)  # every target was lstat-verified as a real dir
                assert stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
        finally:
            os.chmod(home, 0o700)

    def test_symlinked_parent_gets_no_privileged_repair_at_all(self, tmp_path, victim):
        """A parent we CAN see is a symlink is an answer, not blindness."""
        real = tmp_path / "real_dir"
        real.mkdir()
        link_dir = tmp_path / "link_dir"
        link_dir.symlink_to(real)
        calls: list = []

        class SpyPerms:
            def reassert_mask(self, path, **kw):
                calls.append(path)
                return True

        import unittest.mock as _m
        with _m.patch.object(sudo_dest, "_get_perms", lambda: SpyPerms()):
            assert sudo_dest.sudo_dest_refusal(link_dir / "openclaw.json") != ""
        assert calls == []

    def test_clamp_ABOVE_the_parent_is_still_repaired(self, tmp_path, monkeypatch):
        """The regression the first version of this code shipped.

        The gateway clamps ``.openclaw``, but half the gated destinations live a
        level deeper. There it is the PARENT's own lstat that EACCESes, so a
        repair aimed only at ``path.parent`` never fires and the legitimate write
        fails closed on every clamped deploy.
        """
        oc = tmp_path / ".openclaw"
        (oc / "workspace").mkdir(parents=True)
        dest = oc / "workspace" / "AGENTS.md"
        dest.write_text("doc")
        os.chmod(oc, 0o000)  # clamp ABOVE the parent
        repaired: list = []

        class FakePerms:
            def reassert_mask(self, path, **kw):
                repaired.append(Path(path))
                os.chmod(path, 0o700)  # a repair that actually works
                return True

        monkeypatch.setattr(sudo_dest, "_get_perms", lambda: FakePerms())
        try:
            assert sudo_dest.sudo_dest_refusal(dest) == ""
            assert repaired == [oc]
        finally:
            os.chmod(oc, 0o700)

    def test_symlink_verdict_never_retries(self, tmp_path, victim, monkeypatch):
        dest = tmp_path / "openclaw.json"
        dest.symlink_to(victim)
        calls: list = []

        class SpyPerms:
            def reassert_mask(self, path, **kw):
                calls.append(path)
                return True

        monkeypatch.setattr(sudo_dest, "_get_perms", lambda: SpyPerms())
        assert "SYMLINK" in sudo_dest.sudo_dest_refusal(dest)
        assert calls == [], "a symlink is an answer, not blindness — must not repair-and-retry"


# ── safe_write_bot_config — the funnel every openclaw.json write passes ──────


class TestSafeWriteBotConfig:
    def test_planted_config_blocks_backup_write_and_chown(self, pod, victim, recorded_argv):
        (pod / "openclaw.json").symlink_to(victim)
        ok, msg = deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert ok is False
        assert "refused" in msg and "SYMLINK" in msg
        assert_no_privileged_write(recorded_argv, pod / "openclaw.json", victim)

    def test_planted_BACKUP_dest_is_refused_too(self, pod, victim, recorded_argv):
        """``openclaw.json.bak`` is a root-write sink in its own right.

        Gating only the headline dest would leave ``cp <config> <bak>`` free to
        write the config's bytes through a link — and the config is content the
        bot itself controls.
        """
        (pod / "openclaw.json").write_text("{}")
        (pod / "openclaw.json.bak").symlink_to(victim)
        ok, msg = deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert ok is False
        assert "openclaw.json.bak" in msg
        assert_no_privileged_write(recorded_argv, pod / "openclaw.json.bak", victim)

    def test_plant_landing_inside_the_cp_window_still_blocks_the_chown(
        self, pod, victim, recorded_argv, monkeypatch
    ):
        """The re-assert between ``cp`` and ``chown`` is load-bearing.

        A ``cp`` through a link leaves the LINK in place, so without a second
        look the ownership transfer — the permanent leg — still lands on the
        victim.
        """
        config = pod / "openclaw.json"
        config.write_text("{}")
        real_run = deploy.subprocess.run

        def swap_in_plant(argv, *a, **k):
            argv_s = [str(x) for x in argv]
            if len(argv_s) > 1 and Path(argv_s[1]).name == "cp" and argv_s[-1] == str(config):
                config.unlink()
                config.symlink_to(victim)  # attacker wins the race, mid-write
            return real_run(argv, *a, **k)

        monkeypatch.setattr(deploy.subprocess, "run", swap_in_plant)
        ok, msg = deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert ok is False
        chowns = [c for c in recorded_argv if len(c) > 1 and Path(c[1]).name == "chown"]
        assert chowns == [], f"ownership transfer ran against a planted dest: {chowns}"
        assert victim.read_text() == VICTIM_TEXT

    def test_legitimate_write_is_unchanged(self, pod, recorded_argv):
        (pod / "openclaw.json").write_text("{}")
        ok, msg = deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert (ok, msg) == (True, "")
        kinds = [Path(c[1]).name for c in writes_or_chowns(recorded_argv)]
        assert kinds == ["cp", "cp", "chown"], f"expected backup+write+chown, got {kinds}"

    def test_fresh_config_still_writes(self, pod, recorded_argv):
        """No pre-existing config: absent dest is safe, backup is skipped."""
        ok, msg = deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert (ok, msg) == (True, "")
        kinds = [Path(c[1]).name for c in writes_or_chowns(recorded_argv)]
        assert kinds == ["cp", "chown"]

    def test_mask_is_rewidened_before_the_gate_looks(self, pod, recorded_argv, monkeypatch):
        """Ordering pin: ``openclaw config validate`` runs as the BOT user and
        re-clamps ``.openclaw``'s ACL mask, and the gate lstats as EVOLVE. If the
        reassert did not precede the gate, every legitimate Linux write would be
        refused. Recorded as a sequence so a future reorder trips this test.
        """
        seq: list[str] = []

        class SpyPerms:
            def reassert_mask(self, path, **kw):
                seq.append("reassert")
                return True

        monkeypatch.setattr(deploy, "get_perms", lambda: SpyPerms())
        real = sudo_dest.assert_safe_sudo_dest

        def spy_gate(p):
            seq.append("gate")
            return real(p)

        monkeypatch.setattr(sudo_dest, "assert_safe_sudo_dest", spy_gate)
        (pod / "openclaw.json").write_text("{}")
        deploy.safe_write_bot_config("team_bot_a", {"x": 1}, bot_user="team_bot_a")
        assert seq[0] == "reassert", f"gate looked before the mask was re-widened: {seq}"
        assert "gate" in seq


# ── The other openclaw.json writers ──────────────────────────────────────────


class TestStripAgentsMain:
    def _config(self) -> str:
        return json.dumps({"agents": {"main": {}, "defaults": {}}})

    def test_planted_config_blocks_repair_write(self, pod, victim, recorded_argv, monkeypatch):
        monkeypatch.setattr(deploy, "_bot_user_for", lambda b: "team_bot_a")
        real_cfg = pod / "real.json"
        real_cfg.write_text(self._config())
        # `sudo cat` follows the link, so the read still yields a repairable
        # config — the plant is invisible to everything except the gate.
        (pod / "openclaw.json").symlink_to(real_cfg)
        assert deploy.strip_agents_main("team_bot_a") is False
        assert_no_privileged_write(recorded_argv, pod / "openclaw.json", victim)

    def test_legitimate_repair_is_unchanged(self, pod, recorded_argv, monkeypatch):
        monkeypatch.setattr(deploy, "_bot_user_for", lambda b: "team_bot_a")
        monkeypatch.setattr(deploy, "get_scheduler", lambda: _NoopScheduler())
        (pod / "openclaw.json").write_text(self._config())
        assert deploy.strip_agents_main("team_bot_a") is True
        kinds = [Path(c[1]).name for c in writes_or_chowns(recorded_argv)]
        assert kinds == ["cp", "chown"]


class TestClearStalePluginInstallGate:
    def _reach_the_write(self, tmp_path, monkeypatch) -> None:
        """The function bails at once if the SOURCE manifest is unreadable.

        Without a real one it never reaches the gated leg, and the test passes
        for the wrong reason — caught by the revert check, which is the whole
        point of running one.
        """
        src = tmp_path / "plugin_src"
        src.mkdir()
        (src / "openclaw.plugin.json").write_text(json.dumps({"id": "evolve"}))
        monkeypatch.setattr(deploy, "PLUGIN_INSTALL_DIR", src)

    def test_planted_config_blocks_registry_rewrite(self, pod, victim, recorded_argv, tmp_path, monkeypatch):
        self._reach_the_write(tmp_path, monkeypatch)
        real_cfg = pod / "real.json"
        real_cfg.write_text(json.dumps({"plugins": {"installs": {"evolve": {}}}}))
        (pod / "openclaw.json").symlink_to(real_cfg)
        deploy._clear_stale_plugin_install("team_bot_a", "team_bot_a")
        assert_no_privileged_write(recorded_argv, pod / "openclaw.json", victim)

    def test_legitimate_registry_rewrite_is_unchanged(self, pod, recorded_argv, tmp_path, monkeypatch):
        self._reach_the_write(tmp_path, monkeypatch)
        (pod / "openclaw.json").write_text(json.dumps({"plugins": {"installs": {"evolve": {}}}}))
        deploy._clear_stale_plugin_install("team_bot_a", "team_bot_a")
        kinds = [Path(c[1]).name for c in writes_or_chowns(recorded_argv)]
        assert kinds == ["cp", "chown"]


class TestRepairSecurityBotConfigGate:
    def test_planted_config_blocks_cleanup_write(self, pod, victim, recorded_argv, tmp_path, monkeypatch):
        real_cfg = pod / "real.json"
        real_cfg.write_text(json.dumps({"plugins": {"entries": {"evolve": {}}}}))
        (pod / "openclaw.json").symlink_to(real_cfg)
        network = tmp_path / "network.json"
        network.write_text(json.dumps({
            "bots": {"team_bot_a": {"role": "security", "user": "team_bot_a"}},
        }))
        result = deploy.repair_security_bot_config("team_bot_a", network_path=network)
        assert "Refusing" in (result.get("error") or "")
        assert_no_privileged_write(recorded_argv, pod / "openclaw.json", victim)


# ── The workspace doc writers ────────────────────────────────────────────────


class TestWorkspaceDocWriters:
    def test_pod_conduct_plant_blocks_copy_and_chown(self, pod, victim, recorded_argv, monkeypatch, tmp_path):
        src = tmp_path / "POD_CONDUCT.md"
        src.write_text("rules")
        monkeypatch.setattr(deploy, "_CANONICAL_SHARED_DIR", tmp_path)
        monkeypatch.setattr(deploy, "strip_agents_main", lambda b: True)
        (pod / "workspace" / "POD_CONDUCT.md").symlink_to(victim)
        deploy.inject_pod_conduct("team_bot_a", bot_user="team_bot_a")
        assert_no_privileged_write(recorded_argv, pod / "workspace" / "POD_CONDUCT.md", victim)

    def test_handoff_agents_md_plant_blocks_copy_and_chown(self, pod, victim, recorded_argv, monkeypatch):
        agents_md = pod / "workspace" / "AGENTS.md"
        agents_md.symlink_to(victim)
        # Force the sudo fallback: the direct write is what a writable ACL would
        # take, and it is not the leg under test.
        monkeypatch.setattr(Path, "write_text", _raise_permission_error)
        deploy.inject_handoff_check("team_bot_a", bot_user="team_bot_a")
        assert_no_privileged_write(recorded_argv, agents_md, victim)

    def test_session_surface_scrub_plant_blocks_copy_and_chown(self, pod, victim, recorded_argv, monkeypatch):
        agents_md = pod / "workspace" / "AGENTS.md"
        real_doc = pod / "workspace" / "real_agents.md"
        real_doc.write_text(
            f"# Doc\n{deploy.EVOLVE_SURFACE_BEGIN}\nstale\n{deploy.EVOLVE_SURFACE_END}\n"
        )
        agents_md.symlink_to(real_doc)
        deploy.inject_session_surface_check("team_bot_a", bot_user="team_bot_a")
        assert_no_privileged_write(recorded_argv, agents_md, victim)


class TestRequiredToolsPatch:
    """``install_evolve_app``'s openclaw.json patch — a root ``cp``, no chown.

    The sibling suites (test_evolve_app_required_tools_target,
    test_evolve_app_cron_delivery) drive this same path with the gate patched
    off, because they assert on a byte-exact ``/home/evo`` that no test host
    has. This one re-points the primary at a REAL tree so the gate is live.
    """

    def _drive(self, pod, tmp_path, monkeypatch, recorded_argv):
        net = {"bots": {"evo": {"user": "team_bot_a", "role": "primary"}}}
        monkeypatch.setattr(deploy, "load_network", lambda *a, **k: net)
        monkeypatch.setattr(
            deploy, "_resolve_evolve_app_target", lambda network=None: ("evo", "team_bot_a", pod)
        )
        sudo_calls = _stub_run_sudo(monkeypatch)
        monkeypatch.setattr("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True)
        result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)
        return result, [c for c in sudo_calls if c and c[0] == "cp"]

    def test_planted_openclaw_json_blocks_the_patch_cp(
        self, pod, victim, recorded_argv, tmp_path, monkeypatch
    ):
        real_cfg = pod / "real.json"
        real_cfg.write_text(json.dumps({"tools": {}}))
        (pod / "openclaw.json").symlink_to(real_cfg)
        result, cps = self._drive(pod, tmp_path, monkeypatch, recorded_argv)
        assert any("Refusing required_tools patch" in e for e in result.errors), result.errors
        assert [c for c in cps if str(pod / "openclaw.json") in c] == []
        assert victim.read_text() == VICTIM_TEXT

    def test_legitimate_patch_still_writes(self, pod, recorded_argv, tmp_path, monkeypatch):
        (pod / "openclaw.json").write_text(json.dumps({"tools": {}}))
        result, cps = self._drive(pod, tmp_path, monkeypatch, recorded_argv)
        assert result.success, result.errors
        assert any(str(pod / "openclaw.json") in c for c in cps), cps


class TestBotDocSeeding:
    """``bot_doc_seeding.write_doc`` — found by the red-team pass, not the chip.

    It is the THIRD writer of ``workspace/AGENTS.md`` (the other two are gated
    above), it runs on every ``deploy_bot()``, and for the primary bot's
    SOUL.md / AGENTS.md the operator-edit skip is exempted so it writes
    unconditionally. Same root ``cp`` + ``chown`` into a bot-owned dir.
    """

    def _result(self):
        class R:
            def __init__(self):
                self.success, self.steps, self.errors = True, [], []

            def log(self, m):
                self.steps.append(m)

            def error(self, m):
                self.errors.append(m)
                self.success = False

        return R()

    def _run(self, pod, fname="AGENTS.md"):
        from evolve_admin import bot_doc_seeding

        sudo_calls: list[list[str]] = []

        def run_sudo(cmd, result, check=True):
            sudo_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        result = self._result()
        bot_doc_seeding.write_doc(
            run_sudo, workspace_dir=pod / "workspace", fname=fname, content="hello",
            bot_user="team_bot_a", bot_id="team_bot_a", result=result, label="Installed",
        )
        return result, sudo_calls

    def test_planted_doc_blocks_seed_write_and_chown(self, pod, victim):
        (pod / "workspace" / "AGENTS.md").symlink_to(victim)
        result, calls = self._run(pod)
        assert any("Refusing to seed" in e for e in result.errors), result.errors
        assert [c for c in calls if c and c[0] in ("cp", "chown", "chmod")] == [], calls
        assert victim.read_text() == VICTIM_TEXT

    def test_dangling_plant_is_refused_too(self, pod, victim):
        """A DANGLING link is the nastier variant: the operator-edit skip reads
        it as "no existing text" and writes unconditionally, and the root ``cp``
        CREATES the link's target."""
        (pod / "workspace" / "AGENTS.md").symlink_to(victim.parent / "does-not-exist")
        result, calls = self._run(pod)
        assert any("Refusing to seed" in e for e in result.errors), result.errors
        assert [c for c in calls if c and c[0] in ("cp", "chown")] == [], calls

    def test_legitimate_seed_is_unchanged(self, pod):
        result, calls = self._run(pod)
        assert result.success, result.errors
        assert [c[0] for c in calls] == ["mkdir", "cp", "chown", "chmod"], calls


class TestProcedureCopy:
    """``install_evolve_app``'s procedure.md copy — the sibling of the
    required_tools patch, in the same function. Gating one without the other
    would have been incoherent (red-team finding)."""

    def test_planted_procedure_blocks_copy_and_chown(self, pod, victim, recorded_argv, tmp_path, monkeypatch):
        procs = pod / "workspace" / "procedures"
        procs.mkdir(parents=True, exist_ok=True)
        (procs / "security-cve-scan.md").symlink_to(victim)
        net = {"bots": {"evo": {"user": "team_bot_a", "role": "primary"}}}
        monkeypatch.setattr(deploy, "load_network", lambda *a, **k: net)
        monkeypatch.setattr(
            deploy, "_resolve_evolve_app_target", lambda network=None: ("evo", "team_bot_a", pod)
        )
        sudo_calls = _stub_run_sudo(monkeypatch)
        monkeypatch.setattr("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True)
        result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)
        assert any("Refusing procedure install" in e for e in result.errors), result.errors
        target = str(procs / "security-cve-scan.md")
        assert [c for c in sudo_calls if c[0] in ("cp", "chown") and target in c] == [], sudo_calls
        assert victim.read_text() == VICTIM_TEXT


class TestCronJobsMerge:
    """``_merge_cron_entries`` — root ``cp`` + ``chown`` into bot-owned
    ``.openclaw/cron/jobs.json``. Found by the defect pass: it sits BETWEEN two
    legs that were gated, and it is the worst of the three — it reads the
    existing file back through the plant (via ``sudo cat``) and merges, so the
    bot picks essentially every byte root then writes.
    """

    def test_planted_jobs_json_blocks_merge_write_and_chown(self, pod, victim, recorded_argv, monkeypatch):
        cron = pod / "cron"
        cron.mkdir(parents=True, exist_ok=True)
        jobs = cron / "jobs.json"
        real = cron / "real-jobs.json"
        real.write_text(json.dumps({"jobs": []}))
        jobs.symlink_to(real)
        calls = _stub_run_sudo(monkeypatch)
        result = deploy.DeployResult(bot_id="evolve", success=True)
        deploy._merge_cron_entries(
            jobs, [{"name": "x:y", "schedule": "0 1 * * *", "task": "go"}],
            result, owner="team_bot_a",
        )
        assert any("Refusing cron jobs.json" in e for e in result.errors), result.errors
        assert [c for c in calls if c[0] in ("cp", "chown") and str(jobs) in c] == [], calls
        assert victim.read_text() == VICTIM_TEXT


def _stub_run_sudo(monkeypatch) -> "list[list[str]]":
    """Record ``deploy._run_sudo`` argv, but REALLY perform ``mkdir -p``.

    Not gold-plating: root's ``mkdir -p`` is what brings ``workspace/procedures``
    into existence, and the gate refuses a dest whose parent does not exist. A
    fake that swallows the mkdir makes the legitimate path look refused — which
    is exactly what happened, and is a modelling bug rather than a finding.
    """
    calls: list[list[str]] = []

    def fake_run_sudo(cmd, result, check=True):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd and cmd[0] == "mkdir":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(deploy, "_run_sudo", fake_run_sudo)
    return calls


def _raise_permission_error(self, *a, **k):
    raise PermissionError(13, "Permission denied", str(self))


class _NoopScheduler:
    def restart(self, label): return True

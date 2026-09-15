"""Tests for the RUNTIME evolve-access self-heal — secret_config_perms.
``reassert_evolve_access`` / ``reassert_pod_evolve_access``.

These prove the fix for the hourly ACL-lockout FLAP: on Linux the OC gateway
re-hardens ``~/.openclaw`` to 0700 on its own runtime ops, clamping the POSIX
ACL mask so the ``evolve`` service user loses read+traverse. The periodic
reassert re-widens the mask (light) and only escalates to the full re-grant
when a fresh effective-perm VERIFY still fails — and the verify is always the
LAST step (the "false-green: passed then re-hardened" lesson).

Imports are LAZY inside the tests (admin module-level imports can pollute a
shard — see feedback_module_level_routes_admin_import_pollutes_shard). The
perms seam is injected by monkeypatching secret_config_perms's own
``_get_perms`` / ``_get_profile`` names — the exact callables the functions
use — so there is no module-identity ambiguity with the re-exported seam."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


class _FakeProfile:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakePerms:
    """Models a clamped→repaired ACL without subprocess.

    ``effective`` is what ``acl_user_effective`` reports (evolve's effective
    access). ``reassert_mask`` flips it to True when ``heals_on_reassert`` —
    i.e. the light ``setfacl -m m::rwX`` either fixes the clamp or doesn't
    (forcing escalation). ``calls`` records order so the test can assert
    reassert-then-verify (verify is the LAST step)."""

    def __init__(self, *, start: bool, heals_on_reassert: bool) -> None:
        self.effective = start
        self.heals_on_reassert = heals_on_reassert
        self.calls: list[str] = []

    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool:
        self.calls.append("reassert_mask")
        if self.heals_on_reassert:
            self.effective = True
        return True

    def acl_user_effective(self, path: Path, user: str, required: str) -> bool:
        self.calls.append("verify")
        return self.effective

    # protocol filler — unused by this path, present so it duck-types as Perms
    def grant_read_recursive(self, path, user): return True
    def grant_write_recursive(self, path, user, perms, *, prefixed=False): return True
    def grant(self, path, user, perms, *, prefixed=False): return True
    def grant_traverse(self, path, user): return True
    def clear_acl(self, path, *, recursive=False): return True
    def effective_mode(self, path): return 0o700
    def acl_masked_owner_only(self, path): return False


class _PathKeyedPerms:
    """Per-path ACL model — unlike ``_FakePerms`` (one global flag), this keys
    effective access by path so a test can hold ``.openclaw`` healthy while the
    ``workspace/`` root is clamped (the 2026-06-29 evo-vps recurrence shape).

    ``reassert_mask(p)`` flips ``p`` to effective; ``recursive=True`` flips ``p``
    and every seeded path at/under it (models ``setfacl -m m::rwX`` re-widening
    the workspace subtree). Unseeded paths read as healthy (so the secret-file /
    workspace-evolve facets that don't exist on the tmp home never false-fail)."""

    def __init__(self, effective: "dict[str, bool]") -> None:
        self.effective = dict(effective)
        self.calls: list[tuple] = []

    def reassert_mask(self, path: Path, *, recursive: bool = False) -> bool:
        self.calls.append(("reassert_mask", str(path), recursive))
        prefix = str(path)
        for key in self.effective:
            if key == prefix or (recursive and key.startswith(prefix + "/")):
                self.effective[key] = True
        return True

    def acl_user_effective(self, path: Path, user: str, required: str) -> bool:
        self.calls.append(("verify", str(path)))
        return self.effective.get(str(path), True)

    # protocol fillers — unused by this path, present so it duck-types as Perms
    def grant_read_recursive(self, path, user, *, restrict_group_other=False): return True
    def grant_write_recursive(self, path, user, perms, *, prefixed=False, share_group_other_read=False): return True
    def grant(self, path, user, perms, *, prefixed=False): return True
    def grant_traverse(self, path, user): return True
    def clear_acl(self, path, *, recursive=False): return True
    def effective_mode(self, path): return 0o700
    def acl_masked_owner_only(self, path): return False


@pytest.fixture
def linux_scp(tmp_path, monkeypatch):
    """secret_config_perms with the Linux profile pinned and a bot home
    redirected to a real tmp dir (so verify gets past not-bootstrapped)."""
    from evolve_admin import secret_config_perms as scp

    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setattr(scp, "_user_home", lambda user: home)
    monkeypatch.setattr(scp, "_get_profile", lambda: _FakeProfile("linux"))
    return scp


def test_light_reassert_heals_without_escalating(linux_scp, monkeypatch):
    """The common case: a 0700 chmod clamped only .openclaw's mask. The light
    `setfacl -m m::rwX` restores effective access; verify passes; NO full
    re-grant (set_evolve_read_acl) is invoked."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=True)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl",
                        lambda *a, **k: pytest.fail("must not escalate"))

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []
    # reassert BEFORE verify — verify is the last perms step.
    assert fake.calls[0] == "reassert_mask"
    assert fake.calls.index("reassert_mask") < fake.calls.index("verify")


def test_escalates_to_full_regrant_when_light_insufficient(linux_scp, monkeypatch):
    """A deeper clamp the light pass can't fix (a child secret's mask, or the
    named ACE itself stripped) → escalate to set_evolve_read_acl, which
    repairs; the final verify then passes."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy

    def _regrant(bot_id, *a, **k):
        fake.effective = True  # the recursive re-grant fixes it
    monkeypatch.setattr(deploy, "set_evolve_read_acl", _regrant)

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []


def test_still_locked_after_heal_reports_failure(linux_scp, monkeypatch):
    """If even the escalated re-grant can't restore access (missing sudoers
    grant, etc.) the bot is reported unhealed so the monitor can page."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda *a, **k: None)

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok is False
    assert failures  # names the still-broken contract facet


def test_verify_detects_workspace_root_clamp(linux_scp, monkeypatch):
    """verify_evolve_access reports a failure naming the workspace/ traverse when
    ONLY the workspace root mask is clamped (.openclaw healthy). This facet (3)
    was the blind spot: with it missing, a workspace clamp produced zero verify
    failures, so the hourly self-heal never escalated and the content_scan
    'file disappeared' flap persisted until the next full deploy."""
    scp = linux_scp
    oc = scp._user_home("bot") / ".openclaw"
    ws = oc / "workspace"
    ws.mkdir()
    fake = _PathKeyedPerms({str(oc): True, str(ws): False})
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    failures = scp.verify_evolve_access("bot")
    assert any("TRAVERSE" in f and str(ws) in f for f in failures), failures


def test_workspace_root_clamp_self_heals_in_tier1(linux_scp, monkeypatch):
    """The recurrence fix end-to-end: workspace/ root clamped, .openclaw healthy.
    Tier-1 RECURSIVELY re-widens the workspace subtree's mask, the workspace
    verify facet then passes, and the bot heals WITHOUT escalating to the full
    re-grant — and the workspace verify runs AFTER its reassert (verify is last)."""
    scp = linux_scp
    oc = scp._user_home("bot") / ".openclaw"
    ws = oc / "workspace"
    ws.mkdir()
    fake = _PathKeyedPerms({str(oc): True, str(ws): False})
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl",
                        lambda *a, **k: pytest.fail("Tier-1 must cover workspace; no escalation"))

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []
    # a RECURSIVE reassert hit the workspace subtree...
    assert ("reassert_mask", str(ws), True) in fake.calls, fake.calls
    # ...and the workspace traverse was VERIFIED after the last reassert (last step).
    last_reassert = max(i for i, c in enumerate(fake.calls) if c[0] == "reassert_mask")
    ws_verify = max(i for i, c in enumerate(fake.calls) if c == ("verify", str(ws)))
    assert last_reassert < ws_verify


def test_tier1_sweeps_logs_and_cron_subtrees(linux_scp, monkeypatch):
    """The 2026-07-29 VPS finding: the OC gateway mints logs/openclaw.log and
    cron/jobs.json mode 0600, so each rewrite/rotation births the file with a
    clamped ACL mask (create-mode group bits become the mask). Neither subtree
    was in the Tier-1 pass, so the clamp never self-healed and the readers
    lived on their sudo-cat fallbacks. Tier-1 must now RECURSIVELY re-widen
    logs/ and cron/ when they exist — and skip them without error when absent
    (cron/ only exists once a bot has cron jobs)."""
    scp = linux_scp
    oc = scp._user_home("bot") / ".openclaw"
    logs = oc / "logs"
    logs.mkdir()  # cron/ deliberately NOT created
    fake = _PathKeyedPerms({str(oc): True, str(logs / "openclaw.log"): False})
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl",
                        lambda *a, **k: pytest.fail("Tier-1 must cover logs/; no escalation"))

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []
    assert ("reassert_mask", str(logs), True) in fake.calls, fake.calls
    assert ("reassert_mask", str(oc / "cron"), True) not in fake.calls


def test_secret_parent_dir_clamp_self_heals_in_tier1(linux_scp, monkeypatch):
    """The 2026-07-29 evolve-vps recurrence end-to-end: the OC gateway re-hardens
    agents/main/agent to 0700 on auth writes, clamping ONLY that dir's mask
    (.openclaw and workspace/ healthy; auth-profiles.json's own ACL healthy).
    Tier-1 re-widens the secret relpaths' parent dirs, the facet-(0b) verify then
    passes, and the bot heals WITHOUT escalating to the full re-grant."""
    scp = linux_scp
    oc = scp._user_home("bot") / ".openclaw"
    agent_dir = oc / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    fake = _PathKeyedPerms({str(oc): True, str(agent_dir): False})
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl",
                        lambda *a, **k: pytest.fail("Tier-1 must cover the parent dirs; no escalation"))

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []
    # the clamped parent dir got its own (non-recursive) mask re-widen...
    assert ("reassert_mask", str(agent_dir), False) in fake.calls, fake.calls
    # ...and its traverse was VERIFIED after the last reassert (verify is last).
    last_reassert = max(i for i, c in enumerate(fake.calls) if c[0] == "reassert_mask")
    dir_verify = max(i for i, c in enumerate(fake.calls) if c == ("verify", str(agent_dir)))
    assert last_reassert < dir_verify


def test_macos_is_a_structural_noop(monkeypatch):
    """No ACL mask on macOS → reassert_evolve_access is (True, []) without
    touching the perms seam (the profile gate returns before _get_perms)."""
    from evolve_admin import secret_config_perms as scp

    monkeypatch.setattr(scp, "_get_profile", lambda: _FakeProfile("macos"))

    def _boom():
        raise AssertionError("perms seam must not be touched on macOS")
    monkeypatch.setattr(scp, "_get_perms", _boom)

    ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []


def test_pod_driver_isolates_per_bot_failures(linux_scp, monkeypatch):
    """reassert_pod_evolve_access runs every bot; one bot raising becomes that
    bot's (False, [...]) result and never aborts the sweep."""
    scp = linux_scp

    def _fake_one(bot_id, bot_user):
        if bot_id == "boom":
            raise RuntimeError("kaboom")
        return (True, [])
    monkeypatch.setattr(scp, "reassert_evolve_access", _fake_one)

    out = scp.reassert_pod_evolve_access([("evo", "evo"), ("boom", "boom")])
    assert out["evo"] == (True, [])
    assert out["boom"][0] is False
    assert "kaboom" in out["boom"][1][0]


# ── heal-then-verify: ERROR only when the heal LOSES (V-2, evolve-vps flap) ──
#
# The 2026-08-31 alert-fatigue flap: the OC gateway re-hardens
# agents/main/agent to 0700 on every auth write (~2-3h cadence), and the NEXT
# detection pass (ensure_pod_perms's check_evolve_access on every pull-deploy;
# the hourly monitor's Tier-1 verify) logged the loud "contract NOT satisfied"
# ERROR *before* its own heal repaired the mask seconds later — error_reporter
# turned each into a fire+clear alert pair (6-8 operator pushes/day). These pin
# the heal-then-verify contract: a clamp the heal repairs logs NO error; a
# genuinely unhealable gap still logs loudly (fail toward alarming).

_SCP_LOGGER = "evolve_admin.secret_config_perms"


def _error_records(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.ERROR and "NOT satisfied" in r.message]


def test_selfhealing_clamp_logs_no_error(linux_scp, monkeypatch, caplog):
    """Tier-1 misses, Tier-2's full re-grant WINS → the transient clamp must
    produce zero ERROR logs (the detection verify between the tiers is quiet;
    the heal's final verify passes)."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy

    def _regrant(bot_id, *a, **k):
        fake.effective = True  # the recursive re-grant fixes it
    monkeypatch.setattr(deploy, "set_evolve_read_acl", _regrant)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok and failures == []
    assert _error_records(caplog) == []


def test_check_evolve_access_detection_is_quiet(linux_scp, monkeypatch, caplog):
    """The drift-check DETECTION (every pull-deploy + hourly monitor) must not
    log the ERROR — it still reports ok=False with the heal apply attached, so
    nothing is hidden from the drift surface."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        chk = scp.check_evolve_access("bot", "bot")
    assert chk.ok is False
    assert chk.apply is not None  # the heal is still offered
    assert "TRAVERSE" in chk.detail or "READ" in chk.detail
    assert _error_records(caplog) == []


def test_unhealable_gap_still_logs_error(linux_scp, monkeypatch, caplog):
    """Fail toward alarming: when even the escalated full re-grant cannot
    restore access (e.g. a missing sudoers setfacl grant), the heal's FINAL
    verify logs the loud ERROR and the bot is reported unhealed."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        ok, failures = scp.reassert_evolve_access("bot", "bot")
    assert ok is False and failures
    assert _error_records(caplog), "a real lockout must stay loud"


def test_midheal_inline_verify_is_suppressed(linux_scp, monkeypatch, caplog):
    """set_evolve_read_acl ends with an INLINE verify backstop (deploy.py) that
    the heal reaches BEFORE its own belt-and-suspenders mask re-widens — a
    failure there is an intermediate state, not an outcome. A heal that WINS
    must log nothing even though that inline verify saw the clamp."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=True)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy

    def _grant_with_inline_backstop(bot_id, *a, **k):
        # Mimic deploy.set_evolve_read_acl's tail: grants (no-op here — the
        # clamp survives them) then the inline post-grant verify, with the
        # default loud logging an unknown caller gets.
        scp.verify_evolve_access("bot", fake)
    monkeypatch.setattr(deploy, "set_evolve_read_acl", _grant_with_inline_backstop)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        healed = scp.heal_evolve_access("bot", "bot")
    assert healed is True  # step-2 reassert_mask repaired the clamp
    assert _error_records(caplog) == []


def test_midheal_suppression_does_not_leak_past_the_heal(linux_scp, monkeypatch, caplog):
    """The _IN_HEAL guard is scoped to the heal's step 1 only: a direct
    verify_evolve_access call AFTER a completed heal logs at default loudness
    again (the suppression must never stick and mute a later real failure)."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    import evolve_admin.deploy as deploy
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        scp.heal_evolve_access("bot", "bot")   # loses → logs once (final verify)
        n_after_heal = len(_error_records(caplog))
        scp.verify_evolve_access("bot", fake)  # still clamped → logs again
    assert n_after_heal >= 1
    assert len(_error_records(caplog)) == n_after_heal + 1


def test_verify_logs_error_by_default(linux_scp, monkeypatch, caplog):
    """An unknown/direct caller keeps the loud backstop — quiet is opt-in for
    the detection paths only."""
    scp = linux_scp
    fake = _FakePerms(start=False, heals_on_reassert=False)
    monkeypatch.setattr(scp, "_get_perms", lambda: fake)

    with caplog.at_level(logging.ERROR, logger=_SCP_LOGGER):
        failures = scp.verify_evolve_access("bot", fake)
    assert failures
    assert _error_records(caplog)


# ── the hourly sweep against a planted component (#3566 audit D-2 residual) ──


class TestHourlySweepRedirectGate:
    """End-to-end over the REAL ``LinuxPerms``, not a fake.

    Every other test in this file injects a stub perms adapter, which is right
    for proving the reassert/verify *ordering* but structurally blind to what
    argv the seam would emit. The defect here is precisely an argv: this sweep
    hands ``reassert_mask`` five directories — ``agents``, ``agents/main``,
    ``agents/main/agent``, ``workspace`` (recursive), ``workspace/.git`` — plus
    ``logs``/``cron``, and the bot owns every one of them, so it can swap any
    for a symlink between two hourly runs. A root ``setfacl -m m::rwX`` then
    widens the LINK TARGET's ACL mask (verified live on the Ubuntu pod).

    So the adapter here is a real ``LinuxPerms`` with a recording runner, and
    the assertion is on the recorded argv. A gate that returns False *after*
    spawning the root command has closed nothing.
    """

    @staticmethod
    def _seam(tmp_path, monkeypatch, scp):
        """Real LinuxPerms + a runner that records argv and answers getfacl with
        a capping-mask block (so a reached path genuinely fires the setfacl)."""
        from runtime.perms import GETFACL, LinuxPerms

        calls: "list[list[str]]" = []

        class _R:
            def __init__(self, rc=0, out=""):
                self.returncode, self.stdout, self.stderr = rc, out, ""

        def run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[0] == GETFACL:
                return _R(0, f"# file: {cmd[-1]}\nuser::rwx\nuser:evolve:r-x\t"
                             f"#effective:---\ngroup::---\nmask::---\nother::---\n\n")
            return _R(0)

        perms = LinuxPerms(run)
        monkeypatch.setattr(scp, "_get_perms", lambda: perms)
        return calls

    @staticmethod
    def _oc(home: Path) -> Path:
        oc = home / ".openclaw"
        for rel in ("agents/main/agent", "workspace/.git", "logs", "cron"):
            (oc / rel).mkdir(parents=True, exist_ok=True)
        return oc

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        from evolve_admin import secret_config_perms as scp

        h = tmp_path / "home-bot"
        self._oc(h)
        monkeypatch.setattr(scp, "_user_home", lambda user: h)
        monkeypatch.setattr(scp, "_get_profile", lambda: _FakeProfile("linux"))
        return h

    @pytest.mark.parametrize(
        "rel", ["agents", "agents/main", "agents/main/agent",
                "workspace", "workspace/.git", "logs", "cron"]
    )
    def test_planted_component_issues_no_privileged_argv(
        self, home, tmp_path, monkeypatch, rel
    ):
        import shutil

        from evolve_admin import secret_config_perms as scp

        calls = self._seam(tmp_path, monkeypatch, scp)
        victim = tmp_path / f"victim-{rel.replace('/', '-')}"
        (victim / ".git").mkdir(parents=True)
        (victim / "main" / "agent").mkdir(parents=True)
        shutil.rmtree(home / ".openclaw" / rel)
        (home / ".openclaw" / rel).symlink_to(victim)

        scp.reassert_evolve_access("bot", "bot")

        planted = str(home / ".openclaw" / rel)
        offending = [c for c in calls if any(a.startswith(planted) for a in c)]
        assert offending == [], offending

    def test_clean_tree_still_gets_every_mask_re_widened(
        self, home, tmp_path, monkeypatch
    ):
        """The availability half — a false refusal here starves evolve's reads
        pod-wide. Every swept directory must still receive its root
        ``setfacl -m m::rwX``, every hour, on every bot."""
        from evolve_admin import secret_config_perms as scp
        from runtime.perms import SETFACL

        calls = self._seam(tmp_path, monkeypatch, scp)
        scp.reassert_evolve_access("bot", "bot")

        oc = home / ".openclaw"
        widened = {c[-1] for c in calls if c[:4] == ["sudo", SETFACL, "-m", "m::rwX"]}
        for rel in ("", "agents", "agents/main", "agents/main/agent",
                    "workspace", "workspace/.git", "logs", "cron"):
            expected = str(oc / rel) if rel else str(oc)
            assert expected in widened, (rel, sorted(widened))

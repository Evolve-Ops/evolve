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

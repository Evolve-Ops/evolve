"""test_w10g_daemon_layer.py — admin-side W10-G daemon-layer fixes.

The third tier of Linux fresh-install residuals, found on the round-5 live
DigitalOcean Ubuntu VPS `setup --fresh` (diag:
docs/diag-w10g-daemon-layer-2026-06-19.md). Host-mutation-free locks pinning
the LINUX profile + injecting fakes (the live e2e exercises the same paths
against real systemd/setfacl on the CI Linux runner).

Coverage:
  #1  set_evolve_read_acl creates workspace/evolve when absent + grants the
      evolve write ACL (so defer-runner / manifest-reflex-runner can write).
  #2  chmod_secret_config re-grants the evolve read ACE on Linux (the chmod
      600 clobbers the ACL mask there); check_bot_secret_modes reads the
      perms-seam EFFECTIVE mode so the re-grant doesn't read as 0640 drift.
  #4  AuditLog never crashes the bridge when its log dir is unwritable.
  #5  repo_puller SSH paths + infra-jobs call are platform-keyed (no /Users);
      ensure_shared_repo_config no-ops cleanly on a non-git deploy checkout.
  #6  the masked API-key prompt carries an "input hidden" hint;
      _sanitize_mcp_hostname strips a pasted scheme/port/path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import deploy, secret_config_perms, setup_wizard, wizard  # noqa: E402
from evolve_admin.runtime import FakePerms, set_perms, set_scheduler  # noqa: E402


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture(autouse=True)
def _restore_seams():
    yield
    set_profile(None)
    set_perms(None)
    set_scheduler(None)


# ── #1: workspace/evolve write contract ──────────────────────────────────────

def test_set_evolve_read_acl_creates_workspace_evolve_when_missing(tmp_path, monkeypatch):
    """Fresh bot tree: workspace/ exists but workspace/evolve/ doesn't.
    set_evolve_read_acl must mkdir it AND grant the evolve write ACL, so the
    later root/bot-owned dir doesn't inherit a read-only ACL and break
    defer-runner / manifest-reflex-runner writes on Linux."""
    fake = FakePerms()
    set_perms(fake)

    bot_home = tmp_path / "home" / "darwin"
    oc = bot_home / ".openclaw"
    (oc / "workspace").mkdir(parents=True)  # workspace/ yes, workspace/evolve/ NO

    monkeypatch.setattr(deploy, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda b: "darwin")

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        # Materialize sudo mkdir -p so the post-mkdir exists() check passes.
        if cmd[:2] == ["sudo", "/bin/mkdir"] and "-p" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return _Result()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    deploy.set_evolve_read_acl("darwin")

    evolve_ws = str(oc / "workspace" / "evolve")
    assert any(c[:2] == ["sudo", "/bin/mkdir"] and c[-1] == evolve_ws for c in captured), (
        f"expected sudo mkdir for workspace/evolve; got {captured!r}")
    assert any(
        call[0] == "grant_write_recursive" and call[1] == evolve_ws
        for call in fake.calls
    ), f"expected write ACL on workspace/evolve; got {fake.calls!r}"


# ── #2: secret-config read survives chmod 600 on Linux ───────────────────────

def test_chmod_secret_config_regrants_read_ace_on_linux(monkeypatch, tmp_path):
    """On Linux the chmod 600 zeroes the ACL mask; chmod_secret_config must
    re-grant evolve's read ACE so admin daemons keep read access."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    f = tmp_path / "auth-profiles.json"
    f.write_text("{}")
    monkeypatch.setattr(secret_config_perms.subprocess, "run", lambda *a, **k: _Result())

    assert secret_config_perms.chmod_secret_config(f) is True
    assert any(
        c[0] == "grant" and c[1] == str(f) and c[2] == "evolve"
        for c in fake.calls
    ), f"expected evolve read re-grant on Linux; got {fake.calls!r}"


def test_chmod_secret_config_no_acl_change_on_macos(monkeypatch, tmp_path):
    """macOS has no ACL mask — chmod preserves the ACL, so no re-grant (a
    grant with dir-shaped verbs would perturb the byte-exact golden)."""
    set_profile(MACOS)
    fake = FakePerms()
    set_perms(fake)
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    monkeypatch.setattr(secret_config_perms.subprocess, "run", lambda *a, **k: _Result())

    assert secret_config_perms.chmod_secret_config(f) is True
    assert not any(c[0] == "grant" for c in fake.calls), fake.calls


class _EffMode600Perms(FakePerms):
    """effective_mode always reports 0600 — proves the check consults the
    seam (which corrects the Linux ACL-mask display) and not raw stat."""

    def effective_mode(self, path):  # type: ignore[override]
        return 0o600


def test_check_bot_secret_modes_uses_effective_mode(monkeypatch, tmp_path):
    """A file whose RAW mode is 0640 (the Linux ACL-mask display) must NOT
    read as drift when its effective mode is 0600."""
    set_perms(_EffMode600Perms())
    bot_home = tmp_path / "home" / "darwin"
    agent_dir = bot_home / ".openclaw" / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    auth = agent_dir / "auth-profiles.json"
    auth.write_text("{}")
    auth.chmod(0o640)  # raw 0640 — what an ACL'd 0600 file shows on Linux
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    checks = secret_config_perms.check_bot_secret_modes("darwin")
    auth_checks = [c for c in checks if c.target == str(auth)]
    assert auth_checks and auth_checks[0].ok, [
        (c.target, c.ok, c.detail) for c in checks]


def test_check_bot_secret_modes_raw_stat_would_flag_0640(monkeypatch, tmp_path):
    """Control: with the default (real-stat) effective_mode, a raw 0640 file
    DOES read as drift — confirming the test above exercises the seam path."""
    set_perms(FakePerms())  # effective_mode == real stat
    bot_home = tmp_path / "home" / "darwin"
    agent_dir = bot_home / ".openclaw" / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    auth = agent_dir / "auth-profiles.json"
    auth.write_text("{}")
    auth.chmod(0o640)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    checks = secret_config_perms.check_bot_secret_modes("darwin")
    auth_checks = [c for c in checks if c.target == str(auth)]
    assert auth_checks and not auth_checks[0].ok


# ── #2b: verify_evolve_access — the loud backstop on the best-effort grants ───
#
# set_evolve_read_acl's ACL calls are all check=False; the round-6 EACCES bugs
# shipped MUTE because nothing re-read the contract after a Linux chmod 600
# clobbered the mask. verify_evolve_access re-checks the EFFECTIVE perms and
# logs loudly, so the silent-mask-masking class can't ship again. (W10-G r7.)


def _seed_secret_tree(tmp_path):
    """A bot tree with the three secret files + workspace/evolve present."""
    bot_home = tmp_path / "home" / "darwin"
    oc = bot_home / ".openclaw"
    (oc / "agents" / "main" / "agent").mkdir(parents=True)
    (oc / "workspace" / "evolve").mkdir(parents=True)
    for rel in secret_config_perms.BOT_SECRET_CONFIG_RELPATHS:
        (oc / rel).parent.mkdir(parents=True, exist_ok=True)
        (oc / rel).write_text("{}")
    return bot_home, oc


def test_verify_evolve_access_noops_on_macos(tmp_path, monkeypatch):
    """macOS has no ACL mask — the contract is structurally held, so the check
    is a no-op (no seam calls, no golden churn)."""
    set_profile(MACOS)
    fake = FakePerms()
    set_perms(fake)
    bot_home, _ = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    assert secret_config_perms.verify_evolve_access("darwin", fake) == []
    assert fake.calls == [], "macOS verify must touch the seam not at all"


def test_verify_evolve_access_reports_unreadable_secret_on_linux(tmp_path, monkeypatch):
    """The round-6 failure shape: the files exist but evolve has no effective
    ACE (the chmod-600 mask clobbered it / a sudoers grant was missing). Every
    secret file + the queue dir must be reported, and the error logged."""
    set_profile(LINUX)
    fake = FakePerms()  # nothing seeded → acl_user_effective is False everywhere
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    failures = secret_config_perms.verify_evolve_access("darwin", fake)

    # auth-profiles.json is the exact file pod_perms_drift_monitor failed on.
    auth = oc / "agents" / "main" / "agent" / "auth-profiles.json"
    ws_evolve = oc / "workspace" / "evolve"
    joined = "\n".join(failures)
    # W10-G round-8: the .openclaw traverse clamp is reported FIRST (root cause).
    assert f"TRAVERSE {oc}" in joined, joined
    assert str(auth) in joined, joined
    assert str(oc / "openclaw.json") in joined, joined
    assert f"WRITE {ws_evolve}" in joined, joined
    # facet (0b): each secret relpath's intermediate parent dir (the 2026-07-29
    # evolve-vps auth-write clamp on agents/main/agent).
    assert f"TRAVERSE {oc / 'agents' / 'main' / 'agent'}" in joined, joined
    # facet (3): the workspace/ root traverse (content_scan read path) clamp.
    assert f"TRAVERSE {oc / 'workspace'}" in joined, joined
    # .openclaw traverse + each parent dir (workspace/ counted once, by facet
    # (3)) + each secret + the queue write + workspace/ traverse.
    parents = secret_config_perms._secret_relpath_parent_dirs(oc)
    assert (oc / "workspace") in parents  # facet (0b) defers it to facet (3)
    assert len(failures) == (len(secret_config_perms.BOT_SECRET_CONFIG_RELPATHS)
                             + len(parents) - 1 + 3)


def test_verify_evolve_access_passes_when_contract_holds_on_linux(tmp_path, monkeypatch):
    """Seed the read ACE on every secret file + the write ACE on workspace/evolve
    (what the grants produce on a healthy pod) → no failures, nothing logged."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    # The recursive read grant seeds traverse (search) on .openclaw itself —
    # and on every intermediate parent dir of the secret relpaths (facet (0b)).
    fake.seed_acl(oc, "evolve", "search")
    for parent in secret_config_perms._secret_relpath_parent_dirs(oc):
        fake.seed_acl(parent, "evolve", "search")
    for rel in secret_config_perms.BOT_SECRET_CONFIG_RELPATHS:
        fake.seed_acl(oc / rel, "evolve", "read")
    fake.seed_acl(oc / "workspace" / "evolve", "evolve", "write")
    fake.seed_acl(oc / "workspace", "evolve", "search")  # facet (3): content_scan read path
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    assert secret_config_perms.verify_evolve_access("darwin", fake) == []


def test_verify_evolve_access_reports_unreachable_secret_file(tmp_path, monkeypatch):
    """A parent dir whose ACL mask clamps evolve's traverse makes path.exists()
    RAISE PermissionError (round-6's 0700-agent-dir crash). The check must treat
    that as present-and-broken — report it, never propagate the raise."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    auth = oc / "agents" / "main" / "agent" / "auth-profiles.json"

    real_exists = Path.exists

    def raising_exists(self):
        if self == auth:
            raise PermissionError(13, "Permission denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", raising_exists)
    failures = secret_config_perms.verify_evolve_access("darwin", fake)
    assert any(str(auth) in f for f in failures), failures


def test_exists_or_unreachable_treats_eacces_as_present(monkeypatch, tmp_path):
    """The shared permission-safe primitive (durable EACCES sweep): an EACCES from
    a clamped parent is PRESENT, never absent — so set_evolve_read_acl /
    ensure_pod_perms / clean paths proceed to REPAIR a clamp rather than returning
    early or crashing. Py3.12 RAISES where 3.11 returned False
    (feedback_exists_check_lies_under_0700_parents, inverted). Platform-agnostic."""
    present = tmp_path / "real"
    present.write_text("{}")
    missing = tmp_path / "nope"
    clamped = tmp_path / "clamped"

    real_exists = Path.exists

    def raising_exists(self):
        if self == clamped:
            raise PermissionError(13, "Permission denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", raising_exists)
    assert secret_config_perms.exists_or_unreachable(present) is True    # genuinely there
    assert secret_config_perms.exists_or_unreachable(missing) is False   # genuinely absent (ENOENT)
    assert secret_config_perms.exists_or_unreachable(clamped) is True    # EACCES → present, NOT absent
    # the private back-compat alias must resolve to the same callable
    assert secret_config_perms._exists_or_unreachable is secret_config_perms.exists_or_unreachable


# ── round-8: the two new mask-clamp facets ───────────────────────────────────

def test_verify_evolve_access_reports_dir_traverse_clamp(tmp_path, monkeypatch):
    """Round-8 facet 1: the OC gateway re-hardens .openclaw to 0700 → evolve's
    rX ACE caps to ---. Seed read on every leaf + write on the queue, but NOT
    traverse on .openclaw itself → the traverse clamp is reported even though
    every leaf would read fine if reachable (the defer/manifest-reflex EACCES)."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    for parent in secret_config_perms._secret_relpath_parent_dirs(oc):
        fake.seed_acl(parent, "evolve", "search")  # facet (0b) healthy → isolate the oc clamp
    for rel in secret_config_perms.BOT_SECRET_CONFIG_RELPATHS:
        fake.seed_acl(oc / rel, "evolve", "read")
    fake.seed_acl(oc / "workspace" / "evolve", "evolve", "write")
    fake.seed_acl(oc / "workspace", "evolve", "search")  # facet (3) healthy → isolate the oc clamp
    # deliberately do NOT seed traverse on oc
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    failures = secret_config_perms.verify_evolve_access("darwin", fake)
    assert failures == [f"evolve cannot TRAVERSE {oc} (0700 clamps the ACL mask)"], failures


def test_verify_evolve_access_reports_secret_parent_dir_clamp(tmp_path, monkeypatch):
    """The 2026-07-29 evolve-vps recurrence (facet (0b)): the OC gateway
    re-hardens agents/main/agent to 0700 on auth writes, so its mask clamps
    evolve's traverse — auth-profiles.json is unreadable in practice — while
    every OTHER facet (including the file's OWN read ACE, which getfacl still
    reports healthy) passes. Pre-fix the verify returned [] on exactly this
    state and ensure-pod-perms said "0 changes — pod is already in canonical
    state"; the clamped dir must now be reported by name."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    agent_dir = oc / "agents" / "main" / "agent"
    fake.seed_acl(oc, "evolve", "search")
    for parent in secret_config_perms._secret_relpath_parent_dirs(oc):
        if parent != agent_dir:  # the auth-write clamp — the ONLY broken facet
            fake.seed_acl(parent, "evolve", "search")
    for rel in secret_config_perms.BOT_SECRET_CONFIG_RELPATHS:
        fake.seed_acl(oc / rel, "evolve", "read")  # file ACLs all look healthy
    fake.seed_acl(oc / "workspace" / "evolve", "evolve", "write")
    fake.seed_acl(oc / "workspace", "evolve", "search")
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    failures = secret_config_perms.verify_evolve_access("darwin", fake)
    assert failures == [f"evolve cannot TRAVERSE {agent_dir} (ACL mask clamp)"], failures


def test_check_evolve_access_drift_and_apply(tmp_path, monkeypatch):
    """The first-class drift check: a clamped contract is drift with an apply that
    re-runs set_evolve_read_acl (the self-heal wired into ensure_pod_perms +
    pod_perms_drift_monitor). On macOS it is always ok (verify is a no-op)."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "_user_home", lambda u: bot_home)
    # Patch the self-heal BEFORE building the check — check_evolve_access lazily
    # imports set_evolve_read_acl and the apply lambda closes over that import.
    called = {}
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda b: called.setdefault("bot", b))

    # Nothing seeded → the contract is broken → drift with an apply.
    chk = secret_config_perms.check_evolve_access("darwin", "darwin")
    assert chk.category == "evolve-access"
    assert chk.ok is False and chk.apply is not None

    # The apply routes to set_evolve_read_acl.
    chk.apply()
    assert called.get("bot") == "darwin"


def test_check_evolve_access_macos_always_ok(tmp_path, monkeypatch):
    set_profile(MACOS)
    fake = FakePerms()
    set_perms(fake)
    bot_home, _ = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)
    chk = secret_config_perms.check_evolve_access("darwin", "darwin")
    assert chk.ok is True and chk.apply is None


# ── round-9 #1: the post-verify re-harden — heal re-asserts the mask + real bool ─
#
# The round-8 false-green: the gateway re-hardens .openclaw to 0700 AFTER the
# post-gateway verify (the wizard's Telegram channel-add + plugin-install steps
# each invoke `openclaw` against the primary, re-clamping the ACL mask). The heal
# must (a) re-widen the mask via reassert_mask and (b) return a REAL bool —
# set_evolve_read_acl returns None, so wiring it directly as a _PermCheck.apply
# made bool(c.apply()) a perpetual "fix did not return success" false-failure
# (the round-8 evolve-access//home/<bot>/.openclaw deploy-log noise).


def test_heal_evolve_access_reasserts_mask_and_returns_true(tmp_path, monkeypatch):
    """Simulate the clamp: a fully-seeded contract minus traverse on .openclaw.
    The stubbed re-grant restores traverse (as the recursive grant's mask recompute
    would); heal re-widens the mask AND re-verifies → returns True."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)
    # reads + queue write seeded, but NOT traverse → the post-verify 0700 clamp.
    for rel in secret_config_perms.BOT_SECRET_CONFIG_RELPATHS:
        fake.seed_acl(oc / rel, "evolve", "read")
    fake.seed_acl(oc / "workspace" / "evolve", "evolve", "write")
    assert secret_config_perms.verify_evolve_access("darwin", fake) != []  # clamped

    def _regrant(bot_id):
        # the recursive grant recomputes every clamped mask — .openclaw, the
        # workspace/ root (facet 3) AND every secret-relpath parent dir (facet
        # (0b)), exactly what set_evolve_read_acl does live.
        fake.seed_acl(oc, "evolve", "search")
        fake.seed_acl(oc / "workspace", "evolve", "search")
        for parent in secret_config_perms._secret_relpath_parent_dirs(oc):
            fake.seed_acl(parent, "evolve", "search")
        return None  # mirrors the real None-returning signature
    monkeypatch.setattr(deploy, "set_evolve_read_acl", _regrant)

    healed = secret_config_perms.heal_evolve_access("darwin", "darwin")
    assert healed is True  # a REAL bool, not bool(None)==False
    assert (str(oc), False) in fake.masks  # reassert_mask ran on .openclaw
    # ...and on the auth-write-clamped secret-relpath parent (facet (0b)).
    assert (str(oc / "agents" / "main" / "agent"), False) in fake.masks


def test_heal_evolve_access_returns_false_when_still_broken(tmp_path, monkeypatch):
    """If the re-grant cannot restore the contract, heal returns False (a real
    bool) so the apply phase reports the genuine failure — not a false success."""
    set_profile(LINUX)
    fake = FakePerms()
    set_perms(fake)
    bot_home, oc = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda b: None)  # heals nothing

    assert secret_config_perms.heal_evolve_access("darwin", "darwin") is False
    assert (str(oc), False) in fake.masks  # the mask re-widen was still attempted


def test_heal_evolve_access_macos_returns_true_noop(tmp_path, monkeypatch):
    """macOS has no ACL mask: reassert_mask + verify are structural no-ops, so the
    heal is a re-grant + a constant True — byte-identical to prior behaviour."""
    set_profile(MACOS)
    fake = FakePerms()
    set_perms(fake)
    bot_home, _ = _seed_secret_tree(tmp_path)
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)
    monkeypatch.setattr(deploy, "set_evolve_read_acl", lambda b: None)

    assert secret_config_perms.heal_evolve_access("darwin", "darwin") is True


def test_add_acl_sets_default_acl_on_linux(tmp_path, monkeypatch):
    """Round-8 root cause 2: the drift repair must (re)plant the DEFAULT ACL, not
    just the access ACE — else OC-runtime-created children inherit nothing. On
    Linux that is the setfacl -R -m (access) + -R -d -m (default) pair.

    #3198 sibling-call-site gap: the repair must ALSO carry the bot-private
    group/other CLAMP (``g::---,o::---,m::rX``) on BOTH the access and default ACL.
    Without it the self-heal re-planted the permissive default ACL and re-widened
    ``other::``→``r-x`` between full deploys, re-arming world-readable minting of
    every gateway-created file under ``.openclaw``."""
    from runtime.perms import LinuxPerms
    calls = []
    monkeypatch.setattr(LinuxPerms, "_setfacl",
                        lambda self, args, timeout=10: (calls.append(args) or True))
    # workspace channel pre-seeded so the post-clamp re-widen grants in-process
    # (no real sudo mkdir); _user_home resolves the contract under tmp.
    d = tmp_path / ".openclaw"
    for sub in ("evolve", "manifests", "evolve-backup"):
        (d / "workspace" / sub).mkdir(parents=True)
    monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
    set_profile(LINUX)
    set_perms(LinuxPerms())
    assert deploy._add_acl("examplebot", "evolve") is True
    # both the access (-R -m) and the default-ACL (-R -d -m) grant ran, each
    # carrying the clamp (g::---,o::---,m::rX) — NOT the bare u:evolve:rX.
    clamped = "u:evolve:rX,g::---,o::---,m::rX"
    assert ["-R", "-m", clamped, str(d)] in calls, calls
    assert ["-R", "-d", "-m", clamped, str(d)] in calls, calls
    # the OLD unclamped spec must NOT appear — that was the re-widening regression.
    assert ["-R", "-m", "u:evolve:rX", str(d)] not in calls, calls


def test_slack_signals_jobspec_passes_bot_arg(monkeypatch):
    """The slack-signals daemon died every fire on `--bot` required; the JobSpec
    must now pass `--bot <primary>` (+ --network). Capture the program_args the
    seam would install."""
    import json as _json
    import os
    import tempfile
    captured = {}

    def fake_install(label, user, script_path, schedule, result, extra_args=None, **kw):
        captured["label"] = label
        captured["extra_args"] = extra_args

    fd, net = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        # Generic placeholder bot id (NOT a real pod bot name — scrub-guard clean).
        _json.dump({"primary": "primarybot", "bots": {"primarybot": {"role": "primary"}}}, f)

    monkeypatch.setattr(deploy, "_install_launchd", fake_install)
    monkeypatch.setattr(deploy, "_CANONICAL_NETWORK_JSON", net)
    res = deploy.DeployResult(bot_id="evolve", success=True)
    deploy._install_launchd_slack_signals("evolve", Path("/tmp"), res)
    assert "--bot" in (captured["extra_args"] or []), captured
    bot_idx = captured["extra_args"].index("--bot")
    assert captured["extra_args"][bot_idx + 1] == "primarybot"
    assert "--network" in captured["extra_args"]


# ── #4: AuditLog must never take down the bridge ─────────────────────────────

def test_auditlog_survives_unwritable_log_dir(tmp_path):
    """A log dir that can't be created must not raise out of __init__ (it used
    to crash the bridge before it bound :5051) and log() must no-op."""
    from evolve_admin.mcp_bridge.audit import AuditLog

    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a FILE where a dir is expected → mkdir raises OSError
    bad = blocker / "logs" / "mcp-audit.jsonl"

    a = AuditLog(bad)  # must NOT raise
    a.log(session_id="s", client_ip="1.2.3.4", tool="t", bot=None,
          input_summary="", result="ok", duration_ms=1)  # must NOT raise


def test_auditlog_writes_when_dir_ok(tmp_path):
    from evolve_admin.mcp_bridge.audit import AuditLog

    path = tmp_path / "logs" / "mcp-audit.jsonl"
    a = AuditLog(path)
    a.log(session_id="s", client_ip="1.2.3.4", tool="list_bots", bot="x",
          input_summary="", result="ok", duration_ms=2)
    assert path.exists() and "list_bots" in path.read_text()


# ── #5: repo_puller is platform-keyed; non-git checkout no-ops ───────────────

def test_repo_puller_ssh_paths_not_hardcoded_users():
    """The deploy-key SSH paths must be platform-keyed, not /Users/evolve."""
    src = (Path(_ADMIN_DIR) / "evolve_admin" / "repo_puller.py").read_text()
    assert '"/Users/evolve/.ssh"' not in src
    assert 'install_evolve_infra_jobs(Path("/Users/evolve"))' not in src
    assert "_PROFILE.user_home_root" in src  # the blessed root


def test_ensure_shared_repo_config_noops_on_non_git_checkout(tmp_path):
    """A tarball-staged single-VPS deploy checkout (no .git) must not error
    with 'not in a git directory' — return success cleanly."""
    from evolve_admin import repo_puller

    repo = tmp_path / "repo"
    repo.mkdir()  # real source, but NO .git
    ok, msg = repo_puller.ensure_shared_repo_config(repo)
    assert ok is True
    assert "not a git working tree" in msg


# ── #6: wizard UX papercuts ──────────────────────────────────────────────────

def test_api_key_prompts_signal_hidden_input():
    """Both masked API-key prompts must tell the operator the input is hidden
    (otherwise a blank line reads as a failed paste)."""
    sw_src = (Path(_ADMIN_DIR) / "evolve_admin" / "setup_wizard.py").read_text()
    wz_src = (Path(_ADMIN_DIR) / "evolve_admin" / "wizard.py").read_text()
    assert "input hidden" in sw_src
    assert "input hidden" in wz_src


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("evolve-vsp-pod.tail00233d.ts.net:5051", "evolve-vsp-pod.tail00233d.ts.net"),
        ("mini.tail1234.ts.net", "mini.tail1234.ts.net"),
        ("http://mini.tail1234.ts.net:5051/sse", "mini.tail1234.ts.net"),
        ("https://mini.tail1234.ts.net/", "mini.tail1234.ts.net"),
        ("  mini.tail1234.ts.net  ", "mini.tail1234.ts.net"),
        ("", ""),
    ],
)
def test_sanitize_mcp_hostname(raw, expected):
    assert setup_wizard._sanitize_mcp_hostname(raw) == expected


def test_sanitize_mcp_hostname_url_has_single_port():
    host = setup_wizard._sanitize_mcp_hostname("evolve-vsp-pod.tail00233d.ts.net:5051")
    url = f"http://{host}:5051/sse"
    assert url == "http://evolve-vsp-pod.tail00233d.ts.net:5051/sse"
    assert url.count(":5051") == 1


# ══════════════════════════════════════════════════════════════════════════════
# ROUND-6 RESIDUALS (the three that didn't land on the live re-run)
# ══════════════════════════════════════════════════════════════════════════════

# ── round-6 #2: mcp-bridge constructs with the lifespan API ───────────────────

def test_build_starlette_app_constructs_with_lifespan(tmp_path):
    """The bridge crashed post-fork (before binding :5051) with
    ``TypeError: Starlette.__init__() got an unexpected keyword argument
    'on_shutdown'`` — current Starlette only accepts ``lifespan=``. Constructing
    the real app guards the kwarg against regression; driving the lifespan proves
    shutdown still stops the registry."""
    import asyncio

    from starlette.applications import Starlette

    from evolve_admin.mcp_bridge.audit import AuditLog
    from evolve_admin.mcp_bridge.auth import Auth
    from evolve_admin.mcp_bridge.registry import BotRegistry
    from evolve_admin.mcp_bridge.server import build_starlette_app

    net = tmp_path / "network.json"
    net.write_text('{"networkId": "t", "bots": {}}')
    registry = BotRegistry(network_path=net, poll_interval=3600)
    try:
        app = build_starlette_app(
            registry, AuditLog(tmp_path / "audit.jsonl"),
            Auth(mode="tailscale"), port=5051,
        )
        assert isinstance(app, Starlette)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/health" in paths and "/sse" in paths

        # Drive the lifespan: shutdown must call registry.stop().
        async def _cycle():
            async with app.router.lifespan_context(app):
                pass
        asyncio.run(_cycle())
        assert registry._stop_event.is_set(), "lifespan shutdown didn't stop the registry"
    finally:
        registry.stop()


# ── round-6 #1b: secret-mode check survives an unreachable (mask-clamped) parent

def test_check_bot_secret_modes_survives_unreachable_secret(monkeypatch, tmp_path):
    """When a parent dir's ACL mask clamps evolve's traverse ACE, ``path.stat()``
    (and the old ``path.exists()``) raise PermissionError — which used to escape
    ``check_bot_secret_modes`` and crash pod_perms_drift_monitor. It must instead
    record an actionable drift with a self-healing apply, never raise."""
    bot_home = tmp_path / "home" / "darwin"
    agent_dir = bot_home / ".openclaw" / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    auth = agent_dir / "auth-profiles.json"
    auth.write_text("{}")
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if self == auth:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)

    checks = secret_config_perms.check_bot_secret_modes("darwin")  # must NOT raise
    auth_checks = [c for c in checks if c.target == str(auth)]
    assert auth_checks, [c.target for c in checks]
    c = auth_checks[0]
    assert not c.ok and "unreachable" in c.detail
    assert c.apply is not None  # self-heal available for ensure-pod-perms apply


def test_check_bot_secret_modes_unreachable_apply_reasserts_read_acl(monkeypatch, tmp_path):
    """The unreachable-secret drift's apply must re-grant evolve's recursive read
    ACL (the granted ``setfacl -R -m u:evolve:rX`` that recomputes the clamped
    mask)."""
    fake = FakePerms()
    set_perms(fake)
    bot_home = tmp_path / "home" / "darwin"
    agent_dir = bot_home / ".openclaw" / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    auth = agent_dir / "auth-profiles.json"
    auth.write_text("{}")
    monkeypatch.setattr(secret_config_perms, "_user_home", lambda u: bot_home)

    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: (
        (_ for _ in ()).throw(PermissionError(13, "denied")) if self == auth
        else real_stat(self, *a, **k)))

    checks = secret_config_perms.check_bot_secret_modes("darwin")
    c = [c for c in checks if c.target == str(auth)][0]
    assert c.apply() is True
    oc_dir = str(bot_home / ".openclaw")
    assert any(
        call[0] == "grant_read_recursive" and call[1] == oc_dir and call[2] == "evolve"
        for call in fake.calls
    ), f"apply didn't re-grant the evolve read ACL; got {fake.calls!r}"


# ── round-6 #3: pod_config.json path is platform-keyed (no /Users on Linux) ───

def test_pod_config_path_no_users_on_linux(monkeypatch):
    """The last residual /Users writer: the save_network → sync_all_pods hook
    materialised /Users/<bot>/.openclaw/workspace/evolve/pod_config.json on
    Linux. Platform-key it via user_home."""
    from evolve_admin.applications import audit_pod_config

    set_profile(LINUX)
    p = str(audit_pod_config.pod_config_path("w10g_no_such_bot_xyz"))
    assert "/Users/" not in p, p
    assert p.startswith("/home/"), p
    assert p.endswith("/.openclaw/workspace/evolve/pod_config.json"), p


def test_pod_config_path_macos_unchanged(monkeypatch):
    from evolve_admin.applications import audit_pod_config

    set_profile(MACOS)
    p = str(audit_pod_config.pod_config_path("w10g_no_such_bot_xyz"))
    assert p == "/Users/w10g_no_such_bot_xyz/.openclaw/workspace/evolve/pod_config.json", p

"""test_deploy_perms_seam.py — deploy.py's ACL flows on the Perms seam (W4a).

Two proofs, the swappability pattern from test_scheduler_swappability.py:

1. **macOS argv parity goldens** — the full ``set_evolve_read_acl``
   ritual, ``_add_acl``, ``_ensure_evo_write_acl`` and the
   ``fix_shared_dir_permissions`` subdir grants are pinned against the
   EXACT argv the pre-seam deploy.py emitted (captured verbatim from
   origin/main). The ACE strings here are deliberately literal — never
   derived from the deploy constants — so a constant drift and a
   renderer drift both fail the pin. Sudoers grants match on these
   shapes (`/bin/chmod +a * /Users/*/.openclaw`, see
   test_provision_pipeline_sudoers.py) and ``chmod +a`` idempotence
   depends on byte-identical re-rendering.

2. **Zero-subprocess flows on FakePerms** — real deploy call paths run
   end-to-end against the in-memory adapter under a module-wide
   subprocess booby-trap; a passing run IS the evidence that every ACL
   operation rides the seam. The residual deploy-side subprocess calls
   (mkdir/chown/chmod-700) are stubbed and pinned to never contain an
   ACL verb (``+a`` / ``-N``).

Every test runs under BOTH platform profiles (parametrized autouse
pin): once an adapter is injected, the deploy flows must be
profile-independent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.runtime import FakePerms, MacOSPerms, set_perms  # noqa: E402

# ── harness ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, params=["macos", "linux"])
def _both_profiles(request):
    """Run every flow under both platform profiles — with the adapter
    injected through the seam, deploy's ACL flows must not key on the
    host profile (overrides the conftest macOS pin for the linux leg)."""
    from platform_profile import LINUX, MACOS, set_profile

    set_profile(LINUX if request.param == "linux" else MACOS)
    yield request.param
    set_profile(None)


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO real spawns — a real ``sudo chmod`` here would mutate host
    ACLs (and hang on the sudo prompt in local runs)."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"a REAL subprocess spawn was attempted in a deploy perms-seam test. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


@pytest.fixture(autouse=True)
def _reset_perms():
    yield
    set_perms(None)


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def deploy_runner(monkeypatch):
    """Record (and swallow) deploy.py's residual non-ACL subprocess
    calls (chmod 700 / mkdir / chown). NOTE: deploy.subprocess is the
    shared stdlib module, so this also covers any module that resolves
    ``subprocess.run`` at call time."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    return calls


@pytest.fixture
def seam_runner():
    """Recorded runner injected INTO the seam adapter — sees exactly the
    argv the platform backend emits."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    return _run, calls


@pytest.fixture
def bot_tree(tmp_path, monkeypatch):
    """A real .openclaw tree under tmp, with deploy's resolvers pinned
    to it. Placeholder bot naming per the scrub guard."""
    home = tmp_path / "examplebot"
    oc = home / ".openclaw"
    (oc / "credentials").mkdir(parents=True)
    (oc / "profiles").mkdir()
    (oc / "profiles" / "owner-profile.md").write_text("private\n")
    (oc / "profiles" / ".owner-profile.status.json").write_text("{}\n")
    ws = oc / "workspace"
    (ws / "evolve").mkdir(parents=True)
    (ws / "manifests").mkdir()
    (ws / "evolve-backup").mkdir()
    (ws / "AGENTS.md").write_text("# agents\n")
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".zshrc").write_text("# shell config\n")
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")
    monkeypatch.setattr(deploy, "_user_home", lambda user: home)
    return home


# The origin/main ACE strings, verbatim. LITERAL on purpose — deriving
# them from deploy's constants would let a constant drift pass the pin.
_READ_ACE = (
    "evolve allow list,search,readattr,readextattr,readsecurity,"
    "file_inherit,directory_inherit"
)
_WS_WRITE_ACE = (
    "evolve allow list,search,add_file,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,file_inherit,directory_inherit"
)
_WS_ROOT_ACE = (
    "evolve allow list,search,add_file,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,file_inherit"
)
_WS_FILE_ACE = (
    "evolve allow read,write,readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,append"
)
_ZSHRC_ACE = "evolve allow read,readattr,readextattr,readsecurity"
_EVO_WRITE_ACE = (
    "user:evo allow read,write,delete,append,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)
_BOT_SUBDIR_READ_ACE = (
    "evolve allow read,readattr,list,search,file_inherit,directory_inherit"
)


# ── 1. macOS argv parity goldens ──────────────────────────────────────────────


def test_set_evolve_read_acl_full_ritual_argv_parity(bot_tree, deploy_runner, seam_runner):
    """THE golden pin: the complete chmod sequence for a fully populated
    bot tree, byte-identical (command, flags, ACE, path, order) to what
    origin/main's open-coded set_evolve_read_acl emitted."""
    run, seam_calls = seam_runner
    set_perms(MacOSPerms(runner=run))

    deploy.set_evolve_read_acl("examplebot")

    oc = str(bot_tree / ".openclaw")
    ws = f"{oc}/workspace"
    assert seam_calls == [
        # .openclaw/ read contract: inheritable ACE + recursive backfill
        ["sudo", "/bin/chmod", "+a", _READ_ACE, oc],
        ["sudo", "/bin/chmod", "-R", "+a", _READ_ACE, oc],
        # credentials/ carve-out (the 700 chmod rides deploy.subprocess)
        ["sudo", "/bin/chmod", "-N", f"{oc}/credentials"],
        # per-user profile carve-out (.md only — the .status.json sibling
        # keeps the inherited ACL)
        ["sudo", "/bin/chmod", "-N", f"{oc}/profiles/owner-profile.md"],
        # workspace/evolve/ write contract
        ["sudo", "/bin/chmod", "+a", _WS_WRITE_ACE, f"{ws}/evolve"],
        ["sudo", "/bin/chmod", "-R", "+a", _WS_WRITE_ACE, f"{ws}/evolve"],
        # workspace/manifests/ — same shape
        ["sudo", "/bin/chmod", "+a", _WS_WRITE_ACE, f"{ws}/manifests"],
        ["sudo", "/bin/chmod", "-R", "+a", _WS_WRITE_ACE, f"{ws}/manifests"],
        # workspace/evolve-backup/ — same shape (accept_drift baseline)
        ["sudo", "/bin/chmod", "+a", _WS_WRITE_ACE, f"{ws}/evolve-backup"],
        ["sudo", "/bin/chmod", "-R", "+a", _WS_WRITE_ACE, f"{ws}/evolve-backup"],
        # .claude/projects/ read grant (auto-memory inventory)
        ["sudo", "/bin/chmod", "+a", _READ_ACE, str(bot_tree / ".claude/projects")],
        ["sudo", "/bin/chmod", "-R", "+a", _READ_ACE, str(bot_tree / ".claude/projects")],
        # .zshrc single-file read grant (tamper-detection hashing)
        ["sudo", "/bin/chmod", "+a", _ZSHRC_ACE, str(bot_tree / ".zshrc")],
        # workspace/ root: file_inherit only (no cascade into subdirs)
        ["sudo", "/bin/chmod", "+a", _WS_ROOT_ACE, ws],
        # per-file backfill for files directly in workspace/
        ["sudo", "/bin/chmod", "+a", _WS_FILE_ACE, f"{ws}/AGENTS.md"],
    ]
    # deploy.py's own residual subprocess: the credentials 700, then the
    # Spotlight-exclusion marker plant (Part A, deploy-resilience 2026-06-24) —
    # macOS-only, keyed on deploy_resilience._PROFILE (import-pinned to macOS in
    # tests), so it appears in BOTH _both_profiles legs. Never an ACL verb.
    assert deploy_runner == [
        ["sudo", "/bin/chmod", "700", f"{oc}/credentials"],
        ["sudo", "/usr/bin/touch", f"{oc}/.metadata_never_index"],
    ]


def test_set_evolve_read_acl_skips_everything_when_openclaw_missing(
        tmp_path, monkeypatch, deploy_runner, seam_runner):
    run, seam_calls = seam_runner
    set_perms(MacOSPerms(runner=run))
    home = tmp_path / "examplebot"
    home.mkdir()
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")
    monkeypatch.setattr(deploy, "_user_home", lambda user: home)
    deploy.set_evolve_read_acl("examplebot")
    assert seam_calls == []
    assert deploy_runner == []


def test_add_acl_repair_applies_full_clamped_contract(bot_tree, deploy_runner):
    """_add_acl (the ensure_pod_perms drift repair — every deploy, the pod-wide
    pass, AND the hourly self-heal) routes through the SAME
    ``_apply_openclaw_read_contract`` helper the full deploy uses, so the self-heal
    can never re-widen what the deploy clamped (#3198 sibling-call-site gap). For
    the granted user it must: (1) clamp the .openclaw read grant
    (``restrict_group_other=True`` — the previously-missing piece that re-armed
    world-readable minting of every gateway-created file), (2) carve out
    ``credentials/`` + the profile ``.md``, and (3) re-widen the workspace shared
    channel AFTER the clamp so the bot's cross-read is not starved. Idempotent."""
    fake = FakePerms()
    set_perms(fake)
    oc = bot_tree / ".openclaw"
    ws = oc / "workspace"

    assert deploy._add_acl("examplebot", "evolve") is True

    # (1) the read grant on .openclaw carries the bot-private group/other CLAMP
    #     (trailing field == restrict_group_other) — the regression this fix exists
    #     for: an unclamped repair re-planted the permissive default ACL.
    assert ("grant_read_recursive", str(oc), "evolve", True) in fake.calls, fake.calls
    assert fake.acl_user_effective(oc, "evolve", deploy.POD_ACL_PERMS) is True
    # (2) credentials/ + profile .md carve-out (exactly those, nothing else)
    assert fake.cleared == [
        str(oc / "credentials"),
        str(oc / "profiles" / "owner-profile.md"),
    ]
    assert fake.acl_user_effective(oc / "credentials", "evolve", "read") is False
    # creds 700 rode deploy's residual subprocess; no ACL verb leaked there
    assert ["sudo", "/bin/chmod", "700", str(oc / "credentials")] in deploy_runner
    assert not any("+a" in c or "-N" in c for c in deploy_runner)
    # (3) the workspace shared-channel re-widen — share_group_other_read=True
    #     (trailing field) on the three channel subdirs, so the clamp above does
    #     not starve the bot's read of evolve-written files it does not own.
    for subdir in (ws / "evolve", ws / "manifests", ws / "evolve-backup"):
        assert ("grant_write_recursive", str(subdir), "evolve",
                deploy.EVOLVE_WS_WRITE_ACL_PERMS, True) in fake.calls, subdir

    # Idempotent: a second repair re-applies cleanly; creds stays bot-private.
    assert deploy._add_acl("examplebot", "evolve") is True
    assert fake.acl_user_effective(oc / "credentials", "evolve", "read") is False


def test_ensure_evo_write_acl_argv_parity(tmp_path, deploy_runner, seam_runner):
    """The evo-account-separation grant: chown -R first (deploy-side),
    then the prefixed +a / -R +a pair through the seam — all three argv
    byte-exact vs origin/main."""
    run, seam_calls = seam_runner
    set_perms(MacOSPerms(runner=run))
    target = tmp_path / "proposals"
    target.mkdir()

    assert deploy._ensure_evo_write_acl(target) is True

    assert deploy_runner == [
        ["sudo", "/usr/sbin/chown", "-R", "evolve:wheel", str(target)],
    ]
    assert seam_calls == [
        ["sudo", "/bin/chmod", "+a", _EVO_WRITE_ACE, str(target)],
        ["sudo", "/bin/chmod", "-R", "+a", _EVO_WRITE_ACE, str(target)],
    ]


def test_fix_shared_dir_permissions_subdir_acl_argv_parity(
        tmp_path, monkeypatch, deploy_runner, seam_runner):
    """Every evolve-read ACL that fix_shared_dir_permissions grants on
    bot-owned shared subdirs keeps the origin/main ACE verbatim, on
    exactly the five contract dirs."""
    run, seam_calls = seam_runner
    set_perms(MacOSPerms(runner=run))
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")
    # The tier-prefs write ACE (PR #3562 follow-up) is OWNER-DIRECT by design
    # (no sudo, not an evolve-read ACE) — out of scope for this parity pin,
    # covered by test_tier_prefs_acl.py. Neutralize so the five-read-ACE
    # contract below stays byte-exact.
    monkeypatch.setattr(deploy._tier_prefs_acl, "ensure_bot_tier_prefs_acl",
                        lambda *a, **k: True)
    shared = tmp_path / "shared"
    shared.mkdir()

    deploy.fix_shared_dir_permissions("examplebot", shared)

    acl_calls = [c for c in seam_calls if "+a" in c]
    expected_targets = [
        str(shared / "annotations" / "examplebot"),
        str(shared / "examplebot" / "turns"),
        str(shared / "metrics" / "examplebot"),
        str(shared / "examplebot" / "spans"),
        str(shared / "examplebot" / "cascade"),
    ]
    assert acl_calls == [
        ["sudo", "/bin/chmod", "+a", _BOT_SUBDIR_READ_ACE, t]
        for t in expected_targets
    ]
    # the seam saw ONLY ACL ops; chown/chmod-mode stayed deploy-side
    assert acl_calls == seam_calls
    assert not any("+a" in c or "-N" in c for c in deploy_runner), (
        f"deploy.py still open-codes ACL chmod calls: {deploy_runner!r}"
    )


# ── 2. zero-subprocess flows on FakePerms ─────────────────────────────────────


def test_set_evolve_read_acl_flow_on_fake_perms(bot_tree, deploy_runner):
    """The full deploy flow against the in-memory adapter: contract
    grants land in the fake's table, carve-outs are recorded, and no
    ACL op leaks into deploy's residual subprocess calls."""
    fake = FakePerms()
    set_perms(fake)

    deploy.set_evolve_read_acl("examplebot")

    oc = bot_tree / ".openclaw"
    ws = oc / "workspace"
    # the read contract on .openclaw/ — effective for evolve afterwards
    assert fake.acl_user_effective(oc, "evolve", deploy.POD_ACL_PERMS) is True
    # write contracts on the three workspace subdirs
    for subdir in (ws / "evolve", ws / "manifests", ws / "evolve-backup"):
        assert fake.acl_user_effective(
            subdir, "evolve", deploy.EVOLVE_WS_WRITE_ACL_PERMS) is True, subdir
    # carve-outs: credentials/ + the profile .md (and only those)
    assert fake.cleared == [
        str(oc / "credentials"),
        str(oc / "profiles" / "owner-profile.md"),
    ]
    # the carve-out revoked nothing it shouldn't: no grant remains
    assert fake.acl_user_effective(
        oc / "credentials", "evolve", "read") is False
    # zshrc + workspace root + AGENTS.md single-shot grants
    assert fake.acl_user_effective(
        bot_tree / ".zshrc", "evolve", deploy.FILE_READ_ACL_PERMS) is True
    assert fake.acl_user_effective(
        ws, "evolve", deploy.WORKSPACE_ROOT_WRITE_ACL_PERMS) is True
    assert fake.acl_user_effective(
        ws / "AGENTS.md", "evolve", deploy.WORKSPACE_FILE_WRITE_ACL_PERMS) is True
    # ACL ops never leak to deploy's subprocess
    assert not any("+a" in c or "-N" in c for c in deploy_runner)


def test_evo_write_acl_drift_repair_loop_on_fake_perms(
        tmp_path, monkeypatch, deploy_runner):
    """find-drift → apply → re-check, entirely through the seam: the
    _check_evo_write_acl detector flags a missing grant, its apply
    routes through _ensure_evo_write_acl into the fake, and the second
    pass reads clean. Zero subprocess for the ACL half; the chown half
    is pinned deploy-side."""
    fake = FakePerms()
    set_perms(fake)
    monkeypatch.setattr(deploy, "_evo_user_exists", lambda: True)
    (tmp_path / "proposals").mkdir()

    drift = deploy._check_evo_write_acl(tmp_path, "proposals")
    assert drift.ok is False
    assert callable(drift.apply)
    assert drift.apply() is True

    healthy = deploy._check_evo_write_acl(tmp_path, "proposals")
    assert healthy.ok is True, healthy.detail
    # Trailing field = share_group_other_read (default False — the shared_dir
    # evo write channel is owned evolve:wheel and read via the named ACE, not
    # group/other, so it is NOT a workspace-style cross-read channel).
    assert ("grant_write_recursive", str(tmp_path / "proposals"),
            deploy.EVO_GATEWAY_USER, deploy.EVO_WRITE_ACL_PERMS, False) in fake.calls
    # the chown side of the repair stayed deploy-side and ACL-free
    assert any("/usr/sbin/chown" in c[1] for c in deploy_runner if len(c) > 1)
    assert not any("+a" in c for c in deploy_runner)


def test_bot_acl_drift_repair_loop_on_fake_perms(tmp_path, monkeypatch):
    """Same loop for the per-bot .openclaw read contract: one user
    present, one missing → exactly one repair, then both pass. The repair now
    re-establishes the full clamped contract (workspace channels pre-seeded so the
    re-widen grants through the fake instead of a real mkdir)."""
    fake = FakePerms()
    set_perms(fake)
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    for sub in ("evolve", "manifests", "evolve-backup"):
        (oc / "workspace" / sub).mkdir(parents=True)
    monkeypatch.setattr(deploy, "_user_home", lambda u: tmp_path)
    fake.seed_acl(oc, "evolve", deploy.POD_ACL_PERMS)

    checks = deploy._check_bot_acl("examplebot", ("evolve", "opadmin"))
    by_detail = {c.detail: c for c in checks}
    assert by_detail["user:evolve present"].ok is True
    drifted = by_detail["user:opadmin missing"]
    assert drifted.ok is False
    assert drifted.apply() is True

    # The repair clamped the read grant for the drifted user (#3198 sibling gap):
    # an unclamped repair was the regression that re-widened other:: to r-x.
    assert ("grant_read_recursive", str(oc), "opadmin", True) in fake.calls, fake.calls

    rechecks = deploy._check_bot_acl("examplebot", ("evolve", "opadmin"))
    assert all(c.ok for c in rechecks), [c.detail for c in rechecks]


# ── 3. forward-looking guard: every .openclaw read-contract grant clamps ──────

# Functions allowed to grant the .openclaw read contract WITHOUT the clamp. ONLY
# the narrow runtime mask-repair, which deliberately does NOT re-run the workspace
# re-widen and so must stay unclamped (else it starves the bot's channel read
# until the next full deploy — see _reassert_evolve_read_acl docstring / #3200 /
# CLAUDE.md). The full recursive repair (_add_acl) has no such excuse and routes
# through _apply_openclaw_read_contract, which always clamps.
_UNCLAMPED_OPENCLAW_GRANT_ALLOWLIST = {"_reassert_evolve_read_acl"}


def _grant_read_recursive_call_sites(module):
    """(enclosing_func, arg0_src, has_clamp) for every grant_read_recursive call
    in `module`'s source — a static AST scan, no execution."""
    import ast
    import inspect

    src = inspect.getsource(module)
    tree = ast.parse(src)
    sites: list[tuple[str, str, bool]] = []

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Call(self, node):  # noqa: N802
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "grant_read_recursive":
                arg0 = ast.get_source_segment(src, node.args[0]) if node.args else ""
                has_clamp = any(
                    kw.arg == "restrict_group_other"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in node.keywords
                )
                sites.append((self.stack[-1] if self.stack else "<module>",
                              arg0 or "", has_clamp))
            self.generic_visit(node)

    _V().visit(tree)
    return sites


def test_every_openclaw_read_contract_grant_carries_the_clamp():
    """Guard against a future #3198-style sibling-call-site miss: ANY
    grant_read_recursive that establishes the bot-private .openclaw read contract
    (arg0 is the `oc_dir` / a `.openclaw` path) MUST pass restrict_group_other=True
    so it cannot re-widen group/other and re-arm world-readable minting. The only
    sanctioned unclamped grant is the narrow runtime mask-repair. Non-.openclaw
    grants (`.claude/projects`, the repo packages dir) are out of scope."""
    from evolve_admin import deploy, secret_config_perms

    # CONVENTION: the variable holding a bot's `.openclaw` dir is named `oc_dir`
    # (or the path literal contains "openclaw"). This static scan keys on arg0's
    # SOURCE TEXT, so a future contract grant that binds the path to a differently
    # named variable would slip the guard — keep naming the dir `oc_dir`.
    offenders: list[str] = []
    for module in (deploy, secret_config_perms):
        for func, arg0, has_clamp in _grant_read_recursive_call_sites(module):
            establishes_contract = "oc_dir" in arg0 or "openclaw" in arg0.lower()
            if not establishes_contract:
                continue  # e.g. .claude/projects, evolve_repo_pkgs — not the contract
            if func in _UNCLAMPED_OPENCLAW_GRANT_ALLOWLIST:
                continue
            if not has_clamp:
                offenders.append(
                    f"{module.__name__}.{func}: grant_read_recursive({arg0}) "
                    "establishes the .openclaw read contract but omits "
                    "restrict_group_other=True (the #3198 clamp)")
    assert not offenders, "\n".join(offenders)


def test_check_dir_mode_reads_mode_through_the_seam(tmp_path):
    """_check_dir_mode must consult the seam's effective_mode (the
    Linux backend corrects the ACL-mask display lie) — a raw stat here
    would false-pass on a mask-capped dir. Proven by injecting an
    adapter whose effective_mode disagrees with the on-disk mode."""

    class _MaskCappedPerms(FakePerms):
        def effective_mode(self, path: Path) -> int:
            return 0o750  # what a clobbered mask actually grants

    target = tmp_path / "signals"
    target.mkdir()
    os.chmod(target, 0o755)  # stat says 755 — the displayed lie

    set_perms(_MaskCappedPerms())
    check = deploy._check_dir_mode(target, 0o755)
    assert check.ok is False, (
        "_check_dir_mode believed raw stat over the seam's effective "
        "mode — on Linux this false-passes mask-capped dirs"
    )
    assert "0o750" in check.detail

    set_perms(FakePerms())  # real stat again
    assert deploy._check_dir_mode(target, 0o755).ok is True

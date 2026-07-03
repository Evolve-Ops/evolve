"""Golden tests — `_render_evolve_sudoers` on the platform command table (8.3 L2b).

Design: docs/design-linux-port-2026-06-10.md §5 — ONE writer, TWO command
tables. Three layers:

1. **macOS byte-identity goldens**: `fixtures/sudoers_golden/evolve_macos*.sudoers`
   were captured from the renderer BEFORE it was parameterized by
   `platform_profile` (origin/main as of #2680), so equality here proves the
   refactor changed zero bytes of the production sudoers file. This is the
   kill-risk gate: sudoers grants are exact-argv matches, so a single
   drifted byte is a silently dead grant ("sudo: a password is required" at
   3am, not a test failure). Compared as BYTES, character-for-character.

2. **Linux render snapshot**: `evolve_linux.sudoers` pins the first blessed
   Linux rendering (systemctl/useradd verbs, /home//var-lib roots) the same
   way `test_systemd_jobspec_golden.py` pins the first unit-file rendering.
   Plus leakage asserts: no macOS-only binary/path may appear in it.

3. **One-source-of-truth**: every binary a grant names must come out of
   `get_profile().commands` (or the two documented exceptions: the
   discovered openclaw path and the macOS Homebrew audit interpreter) —
   the §5 invariant that "what evolve may sudo" and "what the code runs"
   share one table.

Regenerating a golden is a CONSCIOUS act: any intentional grant change must
update the fixture in the same PR, and for the macOS goldens that means
explaining why the production sudoers bytes are allowed to change.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, get_profile, set_profile  # noqa: E402

from evolve_admin import setup_wizard  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sudoers_golden"

# Pinned discovery results — the only render input that isn't the profile.
MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
LINUX_OC_PATH = "/usr/lib/node_modules/openclaw/bin/openclaw"


def _render(monkeypatch: pytest.MonkeyPatch, oc_path: "str | None") -> str:
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: oc_path)
    content = setup_wizard._render_evolve_sudoers()
    assert content is not None
    return content


def _assert_bytes_equal(rendered: str, golden_name: str) -> None:
    golden_path = GOLDEN_DIR / golden_name
    golden = golden_path.read_bytes()
    got = rendered.encode("utf-8")
    if got == golden:
        return
    diff = "".join(
        difflib.unified_diff(
            golden.decode("utf-8").splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"golden/{golden_name}",
            tofile="rendered",
            n=2,
        )
    )
    raise AssertionError(
        f"sudoers render drifted from {golden_name} — exact-match grants mean "
        f"every changed byte is a dead grant in production. If the change is "
        f"intentional, regenerate the fixture in the same PR.\n{diff}"
    )


# ── 1. macOS byte-identity (the kill-risk gate) ───────────────────────────────
# The admin conftest pins the MACOS profile autouse; re-pinned here anyway so
# the test is self-evidently macOS even if run outside that conftest.


def test_macos_render_byte_identical_to_pre_refactor(monkeypatch) -> None:
    set_profile(MACOS)
    _assert_bytes_equal(_render(monkeypatch, MAC_OC_PATH), "evolve_macos.sudoers")


def test_macos_render_byte_identical_when_openclaw_missing(monkeypatch) -> None:
    set_profile(MACOS)
    _assert_bytes_equal(
        _render(monkeypatch, None), "evolve_macos_no_openclaw.sudoers"
    )


# ── 2. Linux render snapshot + leakage ───────────────────────────────────────


def test_linux_render_matches_golden(monkeypatch) -> None:
    set_profile(LINUX)
    _assert_bytes_equal(_render(monkeypatch, LINUX_OC_PATH), "evolve_linux.sudoers")


# ── 2b. secure_path carries the deploy-venv bin on Linux (W10 #1) ─────────────
# `evolve-admin` is a console script under {venv_dir}/bin and is NOT on the
# system PATH. Without that dir on secure_path, every `sudo evolve-admin
# deploy <bot>` dies with "command not found" — the W10 pod-break. The fix is
# Linux-only: the macOS secure_path stays byte-identical (its own latent gap
# is deliberately out of scope for this byte-identity-locked wave).


def test_linux_secure_path_leads_with_deploy_venv_bin(monkeypatch) -> None:
    set_profile(LINUX)
    content = _render(monkeypatch, LINUX_OC_PATH)
    expected = f"{LINUX.venv_dir}/bin:/usr/local/bin:/usr/bin:/bin"
    assert f'Defaults secure_path = "{expected}"' in content
    assert f'Defaults:evolve secure_path = "{expected}"' in content
    # The venv bin must LEAD so it wins PATH resolution for `evolve-admin`.
    assert f'secure_path = "{LINUX.venv_dir}/bin:' in content
    # Sanity: the literal Linux deploy-venv bin.
    assert '/var/lib/evolve-venv/bin' in content


def test_macos_secure_path_is_byte_identical_and_omits_venv_bin(monkeypatch) -> None:
    set_profile(MACOS)
    content = _render(monkeypatch, MAC_OC_PATH)
    # The exact pre-W10 macOS string — must not move (byte-identity invariant;
    # see test_macos_render_byte_identical_to_pre_refactor for the whole file).
    expected = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    assert f'Defaults secure_path = "{expected}"' in content
    assert f'Defaults:evolve secure_path = "{expected}"' in content
    # The venv-bin prepend is Linux-only — macOS must NOT carry it this wave.
    assert f"{MACOS.venv_dir}/bin:" not in content


# macOS-only binaries, roots, and concepts that must never appear in a
# Linux-rendered sudoers file — a leak here grants a nonexistent path
# (harmless) or signals the writer consulted the wrong table (not harmless).
_MACOS_ONLY_MARKERS = [
    "/Users/",
    "launchctl",
    "dscl",
    "createhomedir",
    "sysadminctl",
    "/Library/LaunchDaemons",
    "/Library/LaunchAgents",
    "LaunchServices",
    "/opt/homebrew",
    "/usr/sbin/chown",  # Linux chown is /usr/bin
    "/usr/sbin/lsof",  # Linux lsof is /usr/bin
    ".dropbox",  # Dropbox desktop sync app is macOS-only on pods
    "+a ",  # macOS chmod ACL syntax — Linux ACLs are setfacl (W5A)
]


def test_linux_render_has_no_macos_leakage(monkeypatch) -> None:
    set_profile(LINUX)
    content = _render(monkeypatch, LINUX_OC_PATH)
    leaks = {
        marker: [line for line in content.splitlines() if marker in line][:3]
        for marker in _MACOS_ONLY_MARKERS
        if marker in content
    }
    assert not leaks, f"macOS-only markers leaked into the Linux render: {leaks}"


def test_linux_render_when_openclaw_missing_suggests_linux_path(monkeypatch) -> None:
    set_profile(LINUX)
    content = _render(monkeypatch, None)
    assert "# evolve ALL=(ALL) NOPASSWD: SETENV: " + LINUX_OC_PATH in content
    assert "/opt/homebrew" not in content


def test_sudoers_read_grant_present_both_profiles(monkeypatch) -> None:
    """§23: the infra audit's content check reads the two evolve sudoers files
    via `sudo -n <cat>`. The grant must exist on BOTH platforms and use the
    profile's own cat path so it can't drift from the invoked argv (the
    infra_audit._read_sudoers_contents call)."""
    for profile, oc_path in ((MACOS, MAC_OC_PATH), (LINUX, LINUX_OC_PATH)):
        set_profile(profile)
        content = _render(monkeypatch, oc_path)
        cat = profile.cat
        assert (
            f"evolve ALL=(root) NOPASSWD: {cat} /etc/sudoers.d/evolve" in content
        ), f"missing sudoers cat grant for {profile.name}"
        assert (
            f"evolve ALL=(root) NOPASSWD: {cat} /etc/sudoers.d/evolve-admin"
            in content
        ), f"missing evolve-admin sudoers cat grant for {profile.name}"


def test_lsof_port_probe_grant_matches_profile_and_call(monkeypatch) -> None:
    """Blocker-2 invariant: the §17 lsof port-probe grant must use the
    profile's own lsof path on BOTH platforms, and that path must be exactly
    what ``safe_upgrade.gate_port_owners`` invokes — grant and call both
    derive from ``get_profile().lsof``, so they cannot drift.

    The pre-fix bug: the grant used the profile path (``/usr/bin/lsof`` on
    Linux) but the probe hardcoded ``/usr/sbin/lsof`` (macOS). On a Linux pod
    ``sudo -n /usr/sbin/lsof`` did not match the ``/usr/bin/lsof`` grant, so
    sudo demanded a password and the preflight reported a false blocker.
    """
    for profile, oc_path in ((MACOS, MAC_OC_PATH), (LINUX, LINUX_OC_PATH)):
        set_profile(profile)
        content = _render(monkeypatch, oc_path)
        lsof = profile.lsof
        # The grant escapes the colons (macOS visudo rejects bare ':').
        expected_grant = (
            f"evolve ALL=(root) NOPASSWD: {lsof} -nP -iTCP\\:* -sTCP\\:LISTEN -Fpcun"
        )
        assert expected_grant in content, (
            f"missing/!= lsof port-probe grant for {profile.name}; expected:\n"
            f"{expected_grant}"
        )
        # The grant binary is precisely the profile lsof the probe will call.
        assert lsof == get_profile().lsof


# ── 3. One source of truth: grant binaries come from the profile table ───────


def _grant_binaries(content: str) -> set[str]:
    """First absolute path after NOPASSWD: (skipping SETENV:) per grant line."""
    binaries = set()
    for line in content.splitlines():
        if not line.startswith("evolve ALL="):
            continue
        _, _, argv = line.partition("NOPASSWD:")
        tokens = argv.split()
        if tokens and tokens[0] == "SETENV:":
            tokens = tokens[1:]
        if tokens:
            binaries.add(tokens[0])
    return binaries


@pytest.mark.parametrize(
    ("profile", "oc_path", "extra_allowed"),
    [
        # audit_dispatch._kick_runner invokes the Homebrew python3 verbatim
        # on macOS — documented exception until that dispatcher is ported.
        # /usr/bin/touch plants the macOS-only Spotlight-exclusion marker
        # (deploy._plant_never_index_marker); literal because the grant renders
        # only on macOS, where /usr/bin/touch is the canonical path.
        pytest.param(
            MACOS, MAC_OC_PATH,
            {"/opt/homebrew/bin/python3", "/usr/bin/touch"}, id="macos",
        ),
        pytest.param(LINUX, LINUX_OC_PATH, set(), id="linux"),
    ],
)
def test_every_grant_binary_comes_from_the_profile_table(
    monkeypatch, profile, oc_path, extra_allowed
) -> None:
    set_profile(profile)
    content = _render(monkeypatch, oc_path)
    allowed = set(get_profile().commands.values()) | {oc_path} | extra_allowed
    rogue = _grant_binaries(content) - allowed
    assert not rogue, (
        f"grant lines name binaries outside platform_profile.commands: {rogue} "
        f"— add them to the profile table, never inline (design-linux-port §5)"
    )


# ── 3b. Linux grants cover the LinuxPerms adapter's exact argv shapes ─────────
#
# Deferred W4b item, shipped with W5A: the Linux render must grant every
# `sudo setfacl …` invocation the perms adapter (runtime/perms.py) issues
# against the granted path families — grant_read/write_recursive, clear_acl,
# reassert_mask. Drive the REAL adapter with a recording runner and fnmatch
# each recorded argv against the grant lines, the way sudoers itself
# matches (join + glob).


def _grant_arg_patterns(content: str) -> "list[str]":
    """Joined-argv glob pattern per grant line (sudoers escaping removed)."""
    pats = []
    for line in content.splitlines():
        if not line.startswith("evolve ALL="):
            continue
        _, _, argv = line.partition("NOPASSWD:")
        argv = argv.strip()
        if argv.startswith("SETENV:"):
            argv = argv[len("SETENV:"):].strip()
        pats.append(argv.replace("\\", ""))
    return pats


def test_linux_grants_cover_linux_perms_argv_shapes(monkeypatch) -> None:
    import fnmatch
    import subprocess as sp

    from runtime import perms as perms_mod
    from runtime.perms import LinuxPerms

    set_profile(LINUX)
    content = _render(monkeypatch, LINUX_OC_PATH)
    patterns = _grant_arg_patterns(content)

    calls: "list[list[str]]" = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == perms_mod.GETFACL:
            # Canned probe output: single-shot reassert sees an access mask;
            # the recursive form reports one masked path under the tree.
            if "-R" in cmd:
                out = "# file: /var/lib/evolve-plugin/dist\nuser:evolve:rwx\nmask::r--\n\n"
            else:
                out = "user::rwx\nuser:evo:rwx\nmask::r--\n"
            return sp.CompletedProcess(cmd, 0, out, "")
        return sp.CompletedProcess(cmd, 0, "", "")

    # The test paths don't exist on the test host; treat non-.md paths as
    # dirs so the dir-only extras (-k, -d -m) are exercised.
    monkeypatch.setattr(perms_mod, "_is_dir", lambda p: not str(p).endswith(".md"))

    perms = LinuxPerms(runner=runner)
    # Stage 6 family — set_evolve_read_acl's ritual (deploy.py):
    perms.grant_read_recursive(Path("/home/team-bot-a/.openclaw"), "evolve")
    perms.grant(Path("/home/team-bot-a/.openclaw"), "evolve",
                "list,search,readattr,file_inherit,directory_inherit")
    perms.clear_acl(Path("/home/team-bot-a/.openclaw/credentials"))
    perms.clear_acl(Path("/home/team-bot-a/.openclaw/profiles/marcus.md"))
    perms.grant_write_recursive(
        Path("/home/team-bot-a/.openclaw/workspace/evolve"), "evolve", "read,write")
    # §11b family — workspace/manifests:
    perms.grant_write_recursive(
        Path("/home/team-bot-a/.openclaw/workspace/manifests"), "evolve", "read,write")
    # 9b family — evo write ACL on the proposal/signal stores:
    perms.grant_write_recursive(
        Path("/var/lib/evolve/proposals"), "evo", "read,write,delete,append",
        prefixed=True)
    perms.grant_write_recursive(
        Path("/var/lib/evolve/signals"), "evo", "read,write,delete,append",
        prefixed=True)
    # 9b2 family — mask repair, single-shot and recursive:
    perms.reassert_mask(Path("/var/lib/evolve/signals"))
    perms.reassert_mask(Path("/var/lib/evolve-plugin"), recursive=True)

    sudo_argvs = [" ".join(c[1:]) for c in calls if c and c[0] == "sudo"]
    assert sudo_argvs, "the adapter issued no sudo invocations — test is vacuous"
    uncovered = [
        argv for argv in sudo_argvs
        if not any(fnmatch.fnmatchcase(argv, p) for p in patterns)
    ]
    assert not uncovered, (
        "LinuxPerms issued sudo argv with no matching Linux sudoers grant "
        f"(silently dead in the admin-daemon context): {uncovered}"
    )

    # The getfacl probes run unprivileged in the adapter, so there is no
    # `sudo getfacl` consumer anywhere — the old §9b3 probe grants were
    # dormant attack surface and were removed (2026-06-11). Pin the
    # absence: if a privileged-probe escalation ever lands, its grant
    # must return in the SAME PR as the consumer (and this assertion
    # gets updated consciously, not deleted in passing).
    assert "getfacl" not in content, (
        "a getfacl grant reappeared in the Linux sudoers render without "
        "a known `sudo getfacl` consumer — grants without consumers are "
        "pure attack surface (see PR removing §9b3)"
    )


# ── 4. Both renders validate under visudo (strictest-common-denominator) ─────


def _visudo() -> "str | None":
    found = shutil.which("visudo")
    if found:
        return found
    fallback = Path("/usr/sbin/visudo")
    return str(fallback) if fallback.exists() else None


@pytest.mark.parametrize(
    ("profile", "oc_path"),
    [pytest.param(MACOS, MAC_OC_PATH, id="macos"), pytest.param(LINUX, LINUX_OC_PATH, id="linux")],
)
def test_render_passes_visudo_syntax_check(monkeypatch, tmp_path, profile, oc_path) -> None:
    """Whichever host runs this (macOS dev box or Linux CI), BOTH renders
    must parse — that's the design's one-syntax-two-tables bargain."""
    visudo = _visudo()
    if visudo is None:
        pytest.skip("no visudo on this host")
    set_profile(profile)
    f = tmp_path / "rendered.sudoers"
    f.write_text(_render(monkeypatch, oc_path))
    r = subprocess.run([visudo, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"visudo rejected the {profile.name} render: {r.stderr or r.stdout}"

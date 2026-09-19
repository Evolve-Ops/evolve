"""Golden + invariant tests for /etc/sudoers.d/pm-probe.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.

This is the privileged half of the PM's read-only pod probe: it hands the
operator's admin account passwordless root for a fixed set of READS. Three
things can go wrong with a rendered sudoers file, and each has a test here:

1. **A drifted byte is a dead grant.** sudo matches the command line exactly,
   so a changed path silently stops matching and every call it covered fails
   with "a password is required" over a BatchMode ssh — which returns no
   output at all. The goldens pin both renders byte for byte.
2. **A widened pattern is a widened grant.** sudo matches command ARGUMENTS
   as one space-separated string, where ``*`` crosses ``/`` and spaces alike
   (sudoers(5) § Wildcards) — the depth ladder this file used to render was
   never a cap, it was root ``cat`` of anything. So: no wildcard in any
   argument position at all, no interpreter grant, exactly one file-reading
   grant (the fixed-argv reader, which vets its own operand as root), and
   every ``evolve-admin`` form written out in full.
3. **A file visudo rejects installs nothing.** Both renders are syntax-checked
   on whichever host runs the suite.
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

from platform_profile import LINUX, MACOS, set_profile  # noqa: E402

from evolve_admin import pm_probe_sudoers  # noqa: E402
from evolve_admin.pm_probe_install import WRAPPER_PATH  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "sudoers_golden"
ADMIN_USER = "pod-admin"


@pytest.fixture(autouse=True)
def _restore_profile():
    yield
    set_profile(MACOS)


def _render(profile) -> str:
    set_profile(profile)
    return pm_probe_sudoers.render_pm_probe_sudoers(ADMIN_USER)


def _assert_bytes_equal(rendered: str, golden_name: str) -> None:
    golden = (GOLDEN_DIR / golden_name).read_bytes()
    if rendered.encode("utf-8") == golden:
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
        "pm-probe sudoers render drifted from the golden — exact-match grants "
        "mean every changed byte is a dead grant on the pod. If the change is "
        f"intentional, regenerate the fixture in the same PR.\n{diff}"
    )


# ── 1. Byte-identity ─────────────────────────────────────────────────────────


def test_macos_render_matches_golden() -> None:
    _assert_bytes_equal(_render(MACOS), "pm_probe_macos.sudoers")


def test_linux_render_matches_golden() -> None:
    _assert_bytes_equal(_render(LINUX), "pm_probe_linux.sudoers")


def test_render_is_pure_and_stable() -> None:
    assert _render(MACOS) == _render(MACOS)


def test_linux_render_has_no_macos_leakage() -> None:
    content = _render(LINUX)
    leaks = [m for m in ("/Users/", "/Library/LaunchDaemons", "launchctl") if m in content]
    assert not leaks, f"macOS-only markers leaked into the Linux render: {leaks}"


# ── 2. visudo ────────────────────────────────────────────────────────────────


def _visudo() -> str | None:
    found = shutil.which("visudo")
    if found:
        return found
    fallback = Path("/usr/sbin/visudo")
    return str(fallback) if fallback.exists() else None


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_render_passes_visudo_syntax_check(tmp_path, profile) -> None:
    visudo = _visudo()
    if visudo is None:
        pytest.skip("no visudo on this host")
    f = tmp_path / f"pm-probe-{profile.name}.sudoers"
    f.write_text(_render(profile))
    r = subprocess.run([visudo, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"visudo rejected the {profile.name} render: {r.stderr or r.stdout}"


# ── 3. The grant may not widen ───────────────────────────────────────────────


def _grant_lines(content: str) -> list[str]:
    return [ln for ln in content.splitlines() if " NOPASSWD: " in ln]


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_house_sudoers_syntax_rules(profile) -> None:
    """CLAUDE.md § sudoers: full binary paths, no escaped dots, no shell."""
    content = _render(profile)
    for ln in _grant_lines(content):
        cmd = ln.split(" NOPASSWD: ", 1)[1]
        assert cmd.startswith("/"), f"grant does not use a full binary path: {ln}"
        assert "\\." not in ln, f"escaped dot in a sudoers path: {ln}"
        assert not cmd.startswith("*"), f"grant does not pin a binary: {ln}"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_every_grant_is_a_read(profile) -> None:
    """No writer, no service verb, no shell — the whole point of the file."""
    forbidden = (
        "/bin/cp", "/bin/mv", "/bin/rm", "/usr/bin/tee", "/bin/chmod",
        "/usr/sbin/chown", "/bin/mkdir", "/bin/launchctl", "/usr/bin/systemctl",
        "/bin/sh", "/bin/bash", "/usr/bin/sudo", "openclaw", "sqlite3",
    )
    for ln in _grant_lines(_render(profile)):
        # The BINARY only — a grant's argument tail may legitimately name
        # paths and verbs that contain one of these substrings.
        binary = ln.split(" NOPASSWD: ", 1)[1].split()[0]
        for bad in forbidden:
            assert bad not in binary, f"a non-read binary reached the grant: {ln}"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_evolve_admin_grants_are_read_verbs_only(profile) -> None:
    content = _render(profile)
    admin_lines = [ln for ln in _grant_lines(content) if "/bin/evolve-admin " in ln]
    assert admin_lines
    mutating = (
        "deploy", "rollback", "retire-bot", "delete-bot", "refresh-sudoers",
        "board", "promote", "pin", "trip", "reset", "wipe-telemetry", "add-bot",
        "restart-gateways", "pause-all", "resume-all", "provision-bot",
    )
    for ln in admin_lines:
        verb = ln.split("/bin/evolve-admin ", 1)[1].split()[0]
        assert verb not in mutating, f"a mutating evolve-admin verb reached the grant: {ln}"


def test_ensure_pod_perms_is_granted_only_with_check_only() -> None:
    """Without the flag it APPLIES; the grant must never cover the bare form."""
    for ln in _grant_lines(_render(MACOS)):
        if "ensure-pod-perms" not in ln:
            continue
        assert "--check-only" in ln, f"bare ensure-pod-perms would apply changes: {ln}"


def test_board_token_is_not_granted() -> None:
    """`board token` mints a bearer token. It is not a read."""
    for ln in _grant_lines(_render(MACOS)):
        assert "board" not in ln, f"the board group reached the grant: {ln}"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_no_grant_carries_a_wildcard_anywhere(profile) -> None:
    """The finding this file was rewritten for.

    sudoers(5) § Wildcards: ``/`` is excluded from wildcard matching only in
    the file name portion of the COMMAND. Arguments are matched as one
    space-separated string, so ``/bin/cat /var/log/*`` also admits
    ``cat /var/log/x /etc/master.passwd`` — the old depth ladder bounded
    nothing, and ``<verb> *`` handed root an arbitrary argument string. A
    wildcard anywhere below is that same mistake wearing a different shape.
    """
    for ln in _grant_lines(_render(profile)):
        assert "*" not in ln, f"a wildcard reached a grant: {ln}"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_exactly_one_grant_reads_files_and_it_is_the_fixed_argv_reader(profile) -> None:
    """One root reader, and it is the program that vets its own operand."""
    grants = _grant_lines(_render(profile))
    readers = [ln for ln in grants if ln.split(" NOPASSWD: ", 1)[1].startswith(WRAPPER_PATH)]
    assert len(readers) == 1, f"expected exactly one reader grant, got {readers}"
    # ...carrying NO argument specification: sudo cannot express the operand
    # contract, so the reader enforces it. A `*` here would be the old bug.
    assert readers[0].split(" NOPASSWD: ", 1)[1] == WRAPPER_PATH
    assert not [ln for ln in grants if "/cat" in ln.split(" NOPASSWD: ", 1)[1]], (
        "a `cat` grant came back — the reader is the only file-reading grant"
    )


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_no_interpreter_grant(profile) -> None:
    """`python3 <script> *` handed root an arbitrary argument string, and
    context_census.py's own `--json-out <path>` is a root write."""
    for ln in _grant_lines(_render(profile)):
        cmd = ln.split(" NOPASSWD: ", 1)[1]
        assert "python" not in cmd, f"an interpreter reached the grant: {ln}"


def test_write_flags_are_not_granted() -> None:
    """`audit-acls --apply` and `health --fix` were one wildcard away from
    passwordless root repairs; the enumerated forms may not reintroduce them."""
    for ln in _grant_lines(_render(MACOS)):
        cmd = ln.split(" NOPASSWD: ", 1)[1]
        for flag in ("--apply", "--fix", "--json-out", "--out"):
            assert flag not in cmd, f"a write flag reached the grant: {ln}"


# ── 4. The command is reachable, and prints what it would install ────────────


def test_dry_run_through_the_real_cli_prints_the_render() -> None:
    """Drives the click command through `main`, which is also the only thing
    that proves cli.py's one-line registration actually attached it.

    `--dry-run` is the whole read path: it renders and prints, and touches
    neither /etc/sudoers.d nor the reader. The install path is root-only and
    is not exercised here (see the refusal test below)."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    set_profile(MACOS)
    result = CliRunner().invoke(
        main, ["install-pm-probe-sudoers", "--admin-user", ADMIN_USER, "--dry-run"])
    assert result.exit_code == 0, result.output
    # rich wraps at the console width, so compare the grants token-wise.
    for verb in pm_probe_sudoers.PM_PROBE_EVOLVE_ADMIN_READS:
        assert verb.split()[0] in result.output
    assert WRAPPER_PATH in result.output
    # No wildcard in a GRANT (the comment block explains why one would be wrong).
    for line in result.output.splitlines():
        if " NOPASSWD: " in line:
            assert "*" not in line, line


def test_install_refuses_off_root_instead_of_half_installing(monkeypatch) -> None:
    """The install writes a root-owned reader AND a sudoers file; a partial
    run would leave a grant naming a file that is not there."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    monkeypatch.setattr(pm_probe_sudoers.os, "geteuid", lambda: 501)
    result = CliRunner().invoke(main, ["install-pm-probe-sudoers", "--admin-user", ADMIN_USER])
    assert result.exit_code == 1
    assert "must run as root" in result.output

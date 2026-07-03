"""test_perms_seam.py — the Perms seam (Linux port W4a, design §4).

Three layers, mirroring the Scheduler/Isolation seam proofs:

1. **MacOSPerms argv goldens** — every grant/carve-out must emit the
   byte-exact ``sudo /bin/chmod`` argv that ``deploy.py`` open-coded
   before the seam existed. The ACE strings are contract: sudoers
   grants match on the ``chmod +a <ace> <path>`` shapes, and ``chmod
   +a`` idempotence depends on the ACE re-rendering identically.

2. **LinuxPerms argv goldens** — the POSIX translation from design §4:
   access + default ACL pairs (``setfacl -R -m`` / ``-R -d -m``), the
   ``-b``/``-k`` carve-outs, the mask-reassert ritual, and the
   *effective*-perm check that makes a chmod-clobbered mask read as
   drift instead of a false pass.

3. **Swappability** — Protocol conformance + per-verb signature parity
   across all three adapters, the get_perms profile keying, and the
   set_perms override.

Every test runs under the module-level ``_no_subprocess_anywhere``
booby-trap: a passing run IS the zero-real-chmod/setfacl evidence.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

from runtime.perms import (
    GETFACL,
    POD_READ_ACL_PERMS,
    SETFACL,
    FakePerms,
    LinuxPerms,
    MacOSPerms,
    Perms,
    get_perms,
    set_perms,
)

# ── harness ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO real spawns — a real ``sudo chmod +a`` / ``setfacl`` here
    would mutate the host filesystem's ACLs."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"a REAL subprocess spawn was attempted in a perms-seam test. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


@pytest.fixture(autouse=True)
def _reset_perms():
    yield
    set_perms(None)  # never leak an injected adapter into other tests


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(responses=None):
    """Runner stub: records (argv, kwargs); answers from ``responses``
    (argv-prefix tuple → _Result; first match wins; default rc=0)."""
    calls: "list[tuple[list, dict]]" = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        for prefix, result in (responses or {}).items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return _Result(0)

    return run, calls


# The pre-seam deploy.py ACE strings, verbatim (origin/main). These are
# the golden values — if a seam change re-renders any of them, sudoers
# stops matching and chmod +a idempotence dedup breaks.
_READ_ACE = (
    "evolve allow list,search,readattr,readextattr,readsecurity,"
    "file_inherit,directory_inherit"
)
_EVO_WRITE_PERMS = (
    "read,write,delete,append,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)


# ── 1. MacOSPerms argv goldens ────────────────────────────────────────────────


def test_macos_grant_read_recursive_emits_the_two_step_chmod_ritual(tmp_path):
    """set_evolve_read_acl's .openclaw/ contract: unprefixed ACE, +a on
    the dir (inheritance), then -R +a backfill — argv byte-exact vs the
    pre-seam deploy.py, including the 10s/30s timeout split."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    assert perms.grant_read_recursive(tmp_path, "evolve") is True
    assert [c[0] for c in calls] == [
        ["sudo", "/bin/chmod", "+a", _READ_ACE, str(tmp_path)],
        ["sudo", "/bin/chmod", "-R", "+a", _READ_ACE, str(tmp_path)],
    ]
    assert calls[0][1]["timeout"] == 10
    assert calls[1][1]["timeout"] == 30


def test_macos_grant_write_recursive_prefixed_matches_ensure_evo_write_acl(tmp_path):
    """_ensure_evo_write_acl's historical shape: PREFIXED ACE
    (``user:evo allow …``) — the prefixed/unprefixed split is byte
    contract, not style."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    ace = f"user:evo allow {_EVO_WRITE_PERMS}"
    assert perms.grant_write_recursive(
        tmp_path, "evo", _EVO_WRITE_PERMS, prefixed=True) is True
    assert [c[0] for c in calls] == [
        ["sudo", "/bin/chmod", "+a", ace, str(tmp_path)],
        ["sudo", "/bin/chmod", "-R", "+a", ace, str(tmp_path)],
    ]


def test_macos_grant_read_recursive_ignores_restrict_group_other(tmp_path):
    """restrict_group_other is a Linux POSIX-ACL knob; macOS has no default-ACL
    base-entry inheritance to clamp and the +a golden is byte contract — passing
    the flag must NOT perturb the emitted argv."""
    run, calls = _recording_runner()
    assert MacOSPerms(runner=run).grant_read_recursive(
        tmp_path, "evolve", restrict_group_other=True) is True
    assert [c[0] for c in calls] == [
        ["sudo", "/bin/chmod", "+a", _READ_ACE, str(tmp_path)],
        ["sudo", "/bin/chmod", "-R", "+a", _READ_ACE, str(tmp_path)],
    ]


def test_macos_grant_write_recursive_ignores_share_group_other_read(tmp_path):
    """share_group_other_read is likewise Linux-only — the prefixed +a golden is
    unchanged."""
    run, calls = _recording_runner()
    ace = f"user:evo allow {_EVO_WRITE_PERMS}"
    assert MacOSPerms(runner=run).grant_write_recursive(
        tmp_path, "evo", _EVO_WRITE_PERMS, prefixed=True,
        share_group_other_read=True) is True
    assert [c[0] for c in calls] == [
        ["sudo", "/bin/chmod", "+a", ace, str(tmp_path)],
        ["sudo", "/bin/chmod", "-R", "+a", ace, str(tmp_path)],
    ]


def test_macos_single_grant_unprefixed_matches_zshrc_flow(tmp_path):
    """The single-shot grant (no -R): the .zshrc read ACE verbatim."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    target = tmp_path / ".zshrc"
    assert perms.grant(target, "evolve", "read,readattr,readextattr,readsecurity")
    assert [c[0] for c in calls] == [
        ["sudo", "/bin/chmod", "+a",
         "evolve allow read,readattr,readextattr,readsecurity", str(target)],
    ]


def test_macos_single_grant_prefixed_matches_add_acl_repair(tmp_path):
    """_add_acl (the ensure_pod_perms drift repair) used the prefixed
    form with POD_READ_ACL_PERMS and no inherit flags."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    assert perms.grant(tmp_path, "evolve", POD_READ_ACL_PERMS, prefixed=True)
    assert calls[0][0] == [
        "sudo", "/bin/chmod", "+a",
        f"user:evolve allow {POD_READ_ACL_PERMS}", str(tmp_path),
    ]


def test_macos_clear_acl_is_chmod_dash_n(tmp_path):
    """The carve-out primitive — credentials/ + profile-.md strip."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    target = tmp_path / "credentials"
    assert perms.clear_acl(target) is True
    assert calls == [(
        ["sudo", "/bin/chmod", "-N", str(target)],
        {"capture_output": True, "text": True, "timeout": 10},
    )]


def test_macos_clear_acl_recursive_is_chmod_dash_r_dash_n(tmp_path):
    """The build-plugin dist-restore shape: ``chmod -R -N dist/`` (the
    longer timeout matches that call site). Byte-identical to the literal
    it replaced in deploy.build_plugin."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    dist = tmp_path / "dist"
    assert perms.clear_acl(dist, recursive=True) is True
    assert calls == [(
        ["sudo", "/bin/chmod", "-R", "-N", str(dist)],
        {"capture_output": True, "text": True, "timeout": 30},
    )]


def test_macos_duplicate_ace_exists_response_is_success(tmp_path):
    """``chmod +a`` exits 1 with "exists" in stderr when the exact ACE
    is already present — already correct, must read as success (the
    _add_acl semantics, uniform across all grant verbs)."""
    run, _ = _recording_runner({
        ("sudo", "/bin/chmod"): _Result(1, stderr="chmod: Failed to set ACL: entry already exists"),
    })
    perms = MacOSPerms(runner=run)
    assert perms.grant(tmp_path, "evolve", POD_READ_ACL_PERMS) is True
    assert perms.grant_read_recursive(tmp_path, "evolve") is True


def test_macos_real_failure_is_not_success(tmp_path):
    run, _ = _recording_runner({
        ("sudo", "/bin/chmod"): _Result(1, stderr="chmod: Unable to translate 'evolve' to a user/group"),
    })
    perms = MacOSPerms(runner=run)
    assert perms.grant(tmp_path, "evolve", POD_READ_ACL_PERMS) is False


def test_macos_runner_exception_is_false_not_raise(tmp_path):
    def _raises(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    perms = MacOSPerms(runner=_raises)
    assert perms.grant(tmp_path, "evolve", POD_READ_ACL_PERMS) is False
    assert perms.clear_acl(tmp_path) is False


def test_macos_reassert_mask_is_a_noop():
    """No POSIX ACL mask on macOS — zero argv, unconditional True."""
    run, calls = _recording_runner()
    perms = MacOSPerms(runner=run)
    assert perms.reassert_mask(Path("/anything"), recursive=True) is True
    assert calls == []


def test_macos_effective_mode_is_plain_stat(tmp_path):
    f = tmp_path / "auth-profiles.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    run, calls = _recording_runner()
    assert MacOSPerms(runner=run).effective_mode(f) == 0o600
    assert calls == []  # mode bits and ACLs are orthogonal — no probe


def test_macos_acl_masked_owner_only_is_always_false(tmp_path):
    """macOS extended ACLs have no POSIX mask, so a macOS group/other mode
    bit is always real — the mask-artifact suppression is out of scope and
    must never engage there (no getfacl-equivalent probe either)."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    run, calls = _recording_runner()
    assert MacOSPerms(runner=run).acl_masked_owner_only(f) is False
    assert calls == []


# ── 1b. MacOSPerms ls -lde parsing (the check side) ──────────────────────────


def _ls_lde(path: str, entries: "list[str]") -> str:
    header = f"drwx------+ 5 examplebot staff 160 Apr 25 12:00 {path}"
    body = "\n".join(f" {i}: {e}" for i, e in enumerate(entries))
    return header + "\n" + body if body else header


def test_macos_acl_user_effective_parses_ls_lde(tmp_path):
    run, calls = _recording_runner({
        ("/bin/ls",): _Result(0, stdout=_ls_lde(str(tmp_path), [
            f"user:evolve allow {POD_READ_ACL_PERMS},file_inherit,directory_inherit",
            "user:opadmin allow list,search",
        ])),
    })
    perms = MacOSPerms(runner=run)
    assert perms.acl_user_effective(tmp_path, "evolve", POD_READ_ACL_PERMS) is True
    # opadmin's entry is weaker than the contract.
    assert perms.acl_user_effective(tmp_path, "opadmin", POD_READ_ACL_PERMS) is False
    # absent user
    assert perms.acl_user_effective(tmp_path, "secbot", POD_READ_ACL_PERMS) is False
    # the probe is the unprivileged ls -lde, not sudo
    assert calls[0][0] == ["/bin/ls", "-lde", str(tmp_path)]


def test_macos_acl_group_effective_parses_named_group_ace(tmp_path):
    """Group analogue: a named ``group:<g> allow`` ACE satisfies the check;
    the unrelated owning-group / other entries do not. (macOS does not use
    this for the admin socket — bots reach it via ``staff`` group ownership —
    but the seam exposes it for Protocol symmetry with Linux.)"""
    f = tmp_path / "admin-daemon.sock"
    f.write_text("")  # plain file → literal perm names, no dir resolution
    run, calls = _recording_runner({
        ("/bin/ls",): _Result(0, stdout=_ls_lde(str(f), [
            "group:evolve-bots allow read,write",
            "group:other-grp allow read",
        ])),
    })
    perms = MacOSPerms(runner=run)
    assert perms.acl_group_effective(f, "evolve-bots", "read,write") is True
    # weaker named group entry fails a write check
    assert perms.acl_group_effective(f, "other-grp", "read,write") is False
    # absent group
    assert perms.acl_group_effective(f, "staff", "read,write") is False
    # unprivileged probe, not sudo
    assert calls[0][0] == ["/bin/ls", "-lde", str(f)]


def test_macos_acl_check_resolves_dir_form_perm_names(tmp_path):
    """`chmod +a "user:evo allow read,write,append" <dir>` stores the
    directory-resolved names (list/add_file/add_subdirectory) and
    `ls -lde` prints those. Requiring source-form names on a directory
    must resolve before comparing — the 2026-06-06 false-drift class."""
    resolved = (
        "list,add_file,add_subdirectory,delete,readattr,writeattr,"
        "readextattr,writeextattr,readsecurity,file_inherit,directory_inherit"
    )
    run, _ = _recording_runner({
        ("/bin/ls",): _Result(0, stdout=_ls_lde(str(tmp_path), [
            f"user:evo allow {resolved}",
        ])),
    })
    perms = MacOSPerms(runner=run)
    assert perms.acl_user_effective(tmp_path, "evo", _EVO_WRITE_PERMS) is True
    # …but resolution must not mask genuine drift: read-only entry still
    # fails a write-contract check.
    run2, _ = _recording_runner({
        ("/bin/ls",): _Result(0, stdout=_ls_lde(str(tmp_path), [
            "user:evo allow list,readattr",
        ])),
    })
    assert MacOSPerms(runner=run2).acl_user_effective(
        tmp_path, "evo", _EVO_WRITE_PERMS) is False


def test_macos_acl_check_on_file_uses_literal_names(tmp_path):
    """On plain files the kernel stores file-form names as-is — no
    resolution (`read` stays `read`)."""
    f = tmp_path / ".zshrc"
    f.write_text("# shell config\n")
    run, _ = _recording_runner({
        ("/bin/ls",): _Result(0, stdout=_ls_lde(str(f), [
            "user:evolve allow read,readattr,readextattr,readsecurity",
        ])),
    })
    perms = MacOSPerms(runner=run)
    assert perms.acl_user_effective(
        f, "evolve", "read,readattr,readextattr,readsecurity") is True


def test_macos_acl_check_ls_failure_is_false(tmp_path):
    run, _ = _recording_runner({("/bin/ls",): _Result(1, stderr="ls: error")})
    assert MacOSPerms(runner=run).acl_user_effective(
        tmp_path, "evolve", POD_READ_ACL_PERMS) is False


# ── 2. LinuxPerms argv goldens ────────────────────────────────────────────────


def test_linux_grant_read_recursive_emits_access_plus_default_acl_pair(tmp_path):
    """Design §4 row 1: inheritable macOS read ACE → access ACL on the
    existing tree + default ACL for future children. ``rX`` so recursion
    never makes plain files executable."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.grant_read_recursive(tmp_path, "evolve") is True
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", "u:evolve:rX", str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", "u:evolve:rX", str(tmp_path)],
    ]


def test_linux_grant_read_recursive_restrict_group_other_clamps_base_entries(tmp_path):
    """The bot-private .openclaw contract: restrict_group_other clamps the real
    ``group::``/``other::`` to nothing and PINS the mask at ``rX`` — in BOTH the
    access ACL (existing tree) and the default ACL (future children) — so the OC
    gateway stops minting genuinely group/world-readable files. The named
    ``u:evolve:rX`` rides along in the same spec; the explicit ``m::rX`` keeps it
    effective (and self-heals a gateway-0700-clamped ``mask::---``)."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.grant_read_recursive(
        tmp_path, "evolve", restrict_group_other=True) is True
    spec = "u:evolve:rX,g::---,o::---,m::rX"
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", spec, str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", spec, str(tmp_path)],
    ]


def test_linux_grant_read_recursive_default_does_not_clamp(tmp_path):
    """restrict_group_other defaults False — the shared-repo / .claude-projects
    callers keep the bare ``u:evolve:rX`` pair (mask left to recompute). This is
    the regression guard that the clamp is opt-in, never global."""
    run, calls = _recording_runner()
    LinuxPerms(runner=run).grant_read_recursive(tmp_path, "evolve")
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", "u:evolve:rX", str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", "u:evolve:rX", str(tmp_path)],
    ]


def test_linux_grant_write_recursive_collapses_to_rwX(tmp_path):
    """Design §4 row 2: the evo write grant → ``u:evo:rwX`` pair. The
    ``rwX`` on the dir covers create/delete/rename within it — what
    ``os.replace`` in the proposal/signal stores needs."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.grant_write_recursive(
        tmp_path, "evo", _EVO_WRITE_PERMS, prefixed=True) is True
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", "u:evo:rwX", str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", "u:evo:rwX", str(tmp_path)],
    ]


def test_linux_grant_write_recursive_share_group_other_read_rewidens(tmp_path):
    """The workspace shared-channel exception: share_group_other_read appends
    ``g::r-x,o::r-x`` to the write spec (access + default) so the BOT can still
    read evolve-written files it does not own past the .openclaw clamp. The named
    write entry stays; the mask is left to recompute to rwX (group/other remain
    bounded by their own r-x base entries)."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.grant_write_recursive(
        tmp_path, "evolve", _EVO_WRITE_PERMS, share_group_other_read=True) is True
    spec = "u:evolve:rwX,g::r-x,o::r-x"
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", spec, str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", spec, str(tmp_path)],
    ]


def test_linux_grant_write_recursive_default_does_not_rewiden(tmp_path):
    """share_group_other_read defaults False — the shared_dir evo write channel
    keeps the bare named pair (no group/other re-widen)."""
    run, calls = _recording_runner()
    LinuxPerms(runner=run).grant_write_recursive(tmp_path, "evo", _EVO_WRITE_PERMS)
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-m", "u:evo:rwX", str(tmp_path)],
        ["sudo", SETFACL, "-R", "-d", "-m", "u:evo:rwX", str(tmp_path)],
    ]


@pytest.mark.parametrize("verbs,bits", [
    (POD_READ_ACL_PERMS, "rX"),
    ("read,readattr,readextattr,readsecurity", "rX"),
    ("read,write,delete,append", "rwX"),
    ("list,search,add_file,readattr", "rwX"),   # add_file is write-ish
    ("read,writeattr", "rwX"),                   # attr writes are writes
])
def test_linux_verb_collapse_is_honest(tmp_path, verbs, bits):
    """macOS verb sets collapse to rX, or rwX once any write-ish verb
    appears (design §4: "verb collapse is honest")."""
    run, calls = _recording_runner()
    LinuxPerms(runner=run).grant(tmp_path / "f", "evolve", verbs)
    assert calls[0][0][:4] == ["sudo", SETFACL, "-m", f"u:evolve:{bits}"]


def test_linux_single_grant_adds_default_acl_only_for_inheriting_dirs(tmp_path):
    """grant() with inherit flags on a directory adds the -d entry;
    the same verbs on a plain file must not (files can't carry default
    ACLs — setfacl would error)."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    perms.grant(tmp_path, "evolve", f"{POD_READ_ACL_PERMS},file_inherit,directory_inherit")
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-m", "u:evolve:rX", str(tmp_path)],
        ["sudo", SETFACL, "-d", "-m", "u:evolve:rX", str(tmp_path)],
    ]
    f = tmp_path / "leaf.json"
    f.write_text("{}")
    run2, calls2 = _recording_runner()
    LinuxPerms(runner=run2).grant(f, "evolve", f"{POD_READ_ACL_PERMS},file_inherit")
    assert [c[0] for c in calls2] == [
        ["sudo", SETFACL, "-m", "u:evolve:rX", str(f)],
    ]
    # no inherit flags (the _add_acl repair shape) → no -d even on a dir
    run3, calls3 = _recording_runner()
    LinuxPerms(runner=run3).grant(tmp_path, "evolve", POD_READ_ACL_PERMS)
    assert [c[0] for c in calls3] == [
        ["sudo", SETFACL, "-m", "u:evolve:rX", str(tmp_path)],
    ]


def test_linux_clear_acl_strips_access_and_default_acls(tmp_path):
    """The carve-out (credentials/, profile .md): ``setfacl -b`` strips
    the access ACL; on dirs ``-k`` also drops the default ACL so future
    children don't re-inherit the grant being revoked."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    assert perms.clear_acl(tmp_path) is True   # tmp_path is a dir
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-b", str(tmp_path)],
        ["sudo", SETFACL, "-k", str(tmp_path)],
    ]
    f = tmp_path / "profile.md"
    f.write_text("private\n")
    run2, calls2 = _recording_runner()
    assert LinuxPerms(runner=run2).clear_acl(f) is True
    assert [c[0] for c in calls2] == [
        ["sudo", SETFACL, "-b", str(f)],
    ]


def test_linux_clear_acl_recursive_strips_whole_tree(tmp_path):
    """Recursive carve-out (build-plugin dist-restore): ``setfacl -R -b``
    then ``-R -k`` so every directory in the tree drops both its access
    ACL and its default ACL in one walk — no per-path ``_is_dir`` gate."""
    run, calls = _recording_runner()
    perms = LinuxPerms(runner=run)
    dist = tmp_path / "dist"
    dist.mkdir()
    assert perms.clear_acl(dist, recursive=True) is True
    assert [c[0] for c in calls] == [
        ["sudo", SETFACL, "-R", "-b", str(dist)],
        ["sudo", SETFACL, "-R", "-k", str(dist)],
    ]


# ── 2b. LinuxPerms getfacl #effective parsing (the sharp edge) ────────────────

# getfacl output after `setfacl -m u:evo:rwx` followed by a mask-clobbering
# `chmod 750` (group bits become the mask; named ACEs get capped).
_GETFACL_MASKED = """\
# file: /Users/Shared/evolve/proposals
# owner: evolve
# group: wheel
user::rwx
user:evo:rwx\t\t\t#effective:r-x
group::r-x
mask::r-x
other::---
default:user::rwx
default:user:evo:rwx
default:group::r-x
default:mask::rwx
default:other::---
"""

# Healthy state: mask wide, named entry fully effective (no annotation).
_GETFACL_HEALTHY = """\
# file: /Users/Shared/evolve/proposals
# owner: evolve
# group: wheel
user::rwx
user:evo:rwx
group::r-x
mask::rwx
other::---
"""


def test_linux_effective_check_sees_through_a_clobbered_mask(tmp_path):
    """The one place the Linux check must be STRONGER than a literal
    translation (design §4): the evo ACE is present (`user:evo:rwx`) but
    the mask caps it to r-x — a write-contract check must report drift,
    because os.replace really will EACCES."""
    run, calls = _recording_runner({
        (GETFACL,): _Result(0, stdout=_GETFACL_MASKED),
    })
    perms = LinuxPerms(runner=run)
    assert perms.acl_user_effective(tmp_path, "evo", _EVO_WRITE_PERMS) is False
    # unprivileged getfacl, -p (absolute paths, no noise)
    assert calls[0][0] == [GETFACL, "-p", str(tmp_path)]
    # …and the read contract (r+x) is still effectively satisfied.
    assert perms.acl_user_effective(tmp_path, "evo", POD_READ_ACL_PERMS) is True


def test_linux_effective_check_passes_on_healthy_acl(tmp_path):
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=_GETFACL_HEALTHY)})
    assert LinuxPerms(runner=run).acl_user_effective(
        tmp_path, "evo", _EVO_WRITE_PERMS) is True


def test_linux_effective_check_ignores_default_acl_entries(tmp_path):
    """A default-ACL-only grant shapes future children but grants nothing
    on the dir itself — must not satisfy a presence check."""
    default_only = (
        "# file: /Users/Shared/evolve/signals\n"
        "# owner: evolve\n# group: wheel\n"
        "user::rwx\ngroup::r-x\nother::---\n"
        "default:user::rwx\ndefault:user:evo:rwx\n"
        "default:group::r-x\ndefault:mask::rwx\ndefault:other::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=default_only)})
    assert LinuxPerms(runner=run).acl_user_effective(
        tmp_path, "evo", POD_READ_ACL_PERMS) is False


def test_linux_effective_check_getfacl_failure_is_false(tmp_path):
    run, _ = _recording_runner({(GETFACL,): _Result(1, stderr="No such file")})
    assert LinuxPerms(runner=run).acl_user_effective(
        tmp_path, "evolve", POD_READ_ACL_PERMS) is False


# ── 2c. LinuxPerms acl_group_effective (admin-socket bot-group connect ACE) ────
#
# The admin-daemon socket grants the shared bot group (``evolve-bots``) a
# connect(write) ACE: ``setfacl -m g:evolve-bots:rwx``. The drift check is the
# group analogue of acl_user_effective — same getfacl #effective parsing, so a
# mask clamp reads as drift. Owning-group base entry must NOT satisfy a named
# group query.

# Socket getfacl after `setfacl -m g:evolve-bots:rwx` — healthy (mask wide).
_GETFACL_SOCK_GROUP_HEALTHY = """\
# file: /var/lib/evolve/admin-daemon.sock
# owner: evolve
# group: evolve
user::rw-
group::rw-
group:evolve-bots:rwx
mask::rwx
other::r--
"""

# Same socket but a later mask-clobber caps the named group ACE to r-x.
_GETFACL_SOCK_GROUP_MASKED = """\
# file: /var/lib/evolve/admin-daemon.sock
# owner: evolve
# group: evolve
user::rw-
group::r-x
group:evolve-bots:rwx\t\t\t#effective:r-x
mask::r-x
other::r--
"""


def test_linux_group_effective_passes_on_healthy_named_group_ace(tmp_path):
    run, calls = _recording_runner({
        (GETFACL,): _Result(0, stdout=_GETFACL_SOCK_GROUP_HEALTHY),
    })
    perms = LinuxPerms(runner=run)
    # connect needs write — present and effective here.
    assert perms.acl_group_effective(tmp_path, "evolve-bots", "read,write") is True
    # unprivileged getfacl -p, never sudo.
    assert calls[0][0] == [GETFACL, "-p", str(tmp_path)]


def test_linux_group_effective_sees_through_a_clobbered_mask(tmp_path):
    """The mask-gotcha guard: the named ``group:evolve-bots:rwx`` entry is
    present but the mask caps it to r-x, so a connect(write) check must report
    drift (a bot really would EACCES on connect)."""
    run, _ = _recording_runner({
        (GETFACL,): _Result(0, stdout=_GETFACL_SOCK_GROUP_MASKED),
    })
    perms = LinuxPerms(runner=run)
    assert perms.acl_group_effective(tmp_path, "evolve-bots", "read,write") is False
    # read alone is still effectively granted (r-x ⊇ r).
    assert perms.acl_group_effective(tmp_path, "evolve-bots", "read") is True


def test_linux_group_effective_owning_group_does_not_satisfy_named_query(tmp_path):
    """A wide owning-group base entry (``group::rwx``) must NOT be mistaken for
    a named ``group:evolve-bots`` ACE — they are distinct getfacl qualifiers."""
    owning_only = (
        "# file: /var/lib/evolve/admin-daemon.sock\n"
        "# owner: evolve\n# group: evolve\n"
        "user::rw-\ngroup::rwx\nmask::rwx\nother::r--\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=owning_only)})
    assert LinuxPerms(runner=run).acl_group_effective(
        tmp_path, "evolve-bots", "read,write") is False


def test_linux_group_effective_getfacl_failure_is_false(tmp_path):
    run, _ = _recording_runner({(GETFACL,): _Result(1, stderr="No such file")})
    assert LinuxPerms(runner=run).acl_group_effective(
        tmp_path, "evolve-bots", "read,write") is False


def test_linux_effective_mode_substitutes_real_group_bits(tmp_path):
    """Sharp-edge consequence 2: on an ACL'd file stat's group triad
    displays the MASK, not the group entry — naive 0600 assertions
    false-positive. effective_mode() reports the group:: entry's
    effective bits instead."""
    f = tmp_path / "auth-profiles.json"
    f.write_text("{}")
    os.chmod(f, 0o660)  # group triad shows rw- — but that's the mask
    acl = (
        f"# file: {f}\n# owner: examplebot\n# group: staff\n"
        "user::rw-\nuser:evolve:rw-\ngroup::---\nmask::rw-\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).effective_mode(f) == 0o600


def test_linux_effective_mode_is_stat_when_no_acl(tmp_path):
    """No extended ACL → stat tells the truth; no substitution."""
    f = tmp_path / "plain.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    base_only = (
        f"# file: {f}\n# owner: examplebot\n# group: staff\n"
        "user::rw-\ngroup::r--\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=base_only)})
    assert LinuxPerms(runner=run).effective_mode(f) == 0o640


# ── 2b-bis. LinuxPerms.acl_masked_owner_only (audit suppression test) ─────────
# The ACL-grounded predicate behind audit.py's suppression of OpenClaw's
# fs.*.perms_*readable family. Ground truth (verified live on the evo Linux
# pod + reproduced on an Ubuntu runner, 2026-06-23):
#   * clean 0600 file + `setfacl -m u:evolve:rX` → st_mode 0640, group::---,
#     mask::r--, other::---  (PURE mask artifact → suppress)
#   * clean 0700 dir  + the same → st_mode 0750, group::---, mask::r-x
#   * a file whose owning group really has r-x → group::r-x, mask::r-x
#     (a REAL grant, not the mask → must still fire)


def test_linux_acl_masked_owner_only_true_for_pure_mask_artifact_file(tmp_path):
    """Proof artifact (a): 0600 file + evolve-read ACL. st_mode shows 0640
    (the mask in the group triad) but the real group::/other:: are ``---``;
    only ``user:evolve`` (capped by the mask) can read. → suppress."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)  # group triad = the mask r--, NOT a real group grant
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is True


def test_linux_acl_masked_owner_only_true_for_pure_mask_artifact_dir(tmp_path):
    """Proof artifact (a), state-dir variant (fs.state_dir.perms_readable):
    a 0700 dir + evolve ACL stats as 0750 but real group::/other:: are ---."""
    d = tmp_path / ".openclaw"
    d.mkdir()
    os.chmod(d, 0o750)  # group triad = mask r-x
    acl = (
        f"# file: {d}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::---\nmask::r-x\nother::---\n"
        "default:user::rwx\ndefault:group::---\ndefault:other::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(d) is True


def test_linux_acl_masked_owner_only_true_with_root_ace(tmp_path):
    """Regression: the live ``set_evolve_read_acl`` grant plants ``user:root``
    ALONGSIDE ``user:evolve`` (visible on the evo-pod as ``user:root:r-x``).
    ``root`` is inherently privileged (bypasses POSIX perms), so a root ACE is
    never an exposure — yet it was missing from ``EVOLVE_ACL_PRINCIPALS``, which
    made this helper return False for EVERY clamped ``.openclaw`` file and
    silently defeated the entire ACL-mask suppression (readable + writable
    findings fired every audit despite group::/other:: being ``---``). This
    is the exact getfacl the live pod produces."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o650)  # group triad = the mask r-x
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:root:r-x\nuser:evolve:r-x\n"
        "group::---\nmask::r-x\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is True


def test_linux_acl_masked_owner_only_false_for_untrusted_named_user(tmp_path):
    """A named ACE for a NON-service principal that really reaches the file is
    a genuine grant Evolve never makes → the finding stays honest (fire). Root
    being trusted must not blanket-trust every named user."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o650)
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\nuser:mallory:r-x\n"
        "group::---\nmask::r-x\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_restrict_group_other_clamp_makes_3190_suppress_the_real_file(tmp_path):
    """KEYSTONE coupling: the restrict_group_other clamp (set_evolve_read_acl)
    is what makes the merged #3190 audit suppression effective on REAL files.

    BEFORE the clamp, the live evo-pod openclaw.json had a REAL ``group::r-x``
    (born from the over-permissive default ACL) — #3190 correctly does NOT
    suppress that, so the "group-readable" finding RECURS every audit. AFTER the
    clamp, the same file's real ``group::``/``other::`` are ``---`` (the group
    triad is now purely the ACL mask, X-collapsed to ``r--`` on a non-exec
    file), so #3190 proves it a mask artifact and suppresses it — with no real
    exposure. This test pins both halves of that transition in one place."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    # BEFORE: real group::r-x → genuine leak → NOT suppressed (finding fires).
    os.chmod(f, 0o650)
    before = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup::r-x\nmask::r-x\nother::---\n"
    )
    run_before, _ = _recording_runner({(GETFACL,): _Result(0, stdout=before)})
    assert LinuxPerms(runner=run_before).acl_masked_owner_only(f) is False
    # AFTER the restrict_group_other clamp: exact shape the live Ubuntu proof
    # produced for a freshly-minted file — group::---, other::---, mask r--
    # (X-collapsed), evolve still reads via its named entry. → suppressed.
    os.chmod(f, 0o640)
    after = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    run_after, _ = _recording_runner({(GETFACL,): _Result(0, stdout=after)})
    assert LinuxPerms(runner=run_after).acl_masked_owner_only(f) is True


def test_linux_acl_masked_owner_only_false_for_plain_0644(tmp_path):
    """Proof artifact (b): a genuinely world-readable 0644 file (no ACL,
    real other::r). No mask → st_mode is truthful → must still fire. Guards
    against regressing the 2026-06-12 removal of the blanket world-readable
    suppression."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    base_only = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\ngroup::r--\nother::r--\n"  # no mask line → no extended ACL
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=base_only)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_for_real_group_grant(tmp_path):
    """Proof artifact (c): a file whose REAL owning group has r-x (not just
    the mask) — exactly the live evo-pod openclaw.json shape. The mask
    matches group::, so effective group access is real → must still fire."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o650)
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup::r-x\nmask::r-x\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_when_other_is_real_under_mask(tmp_path):
    """The mask caps only the GROUP class, never OTHER. A file with evolve's
    ACL (so a mask exists, group::---) that is ALSO other-readable has a REAL
    other::r — effective_mode leaves the other triad untouched → must fire.
    This is why fs.config.perms_world_readable is excluded from the set, but
    the seam stays correct even if a world-read slips in."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o654)  # group triad = mask r-x; other triad = real r--
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup::---\nmask::r-x\nother::r--\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_when_no_extended_acl(tmp_path):
    """No mask line at all → st_mode is the whole truth, nothing to second-
    guess. (A truly 0600 file wouldn't have been flagged anyway.)"""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    base_only = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\ngroup::---\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=base_only)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_for_foreign_named_group(tmp_path):
    """A NAMED group ACE (``group:staff:r-x``) really can be read by staff
    members — even with ``group::---``, its effective r-x comes through the
    mask. Evolve never plants named-group ACEs, so this is a genuine grant
    and must fire, not be mistaken for the evolve-ACL mask artifact."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o650)  # group triad = mask r-x
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\ngroup:staff:r-x\ngroup::---\n"
        "mask::r-x\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_for_foreign_named_user(tmp_path):
    """A named ACE for a non-service user (``user:mallory:r``) is a real grant
    — not Evolve's evolve-read ACL — so it must fire."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\nuser:mallory:r--\ngroup::---\n"
        "mask::r--\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_true_with_both_service_principals(tmp_path):
    """Both Evolve service principals (``evolve`` read + ``evo`` write) capped
    by the mask, real group::/other:: clean → still a pure artifact → suppress."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o660)  # group triad = mask rw-
    acl = (
        f"# file: {f}\n# owner: evolve\n# group: wheel\n"
        "user::rw-\nuser:evolve:r--\nuser:evo:rw-\ngroup::---\n"
        "mask::rw-\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is True


# ── 2b-flap. TOCTOU mask-flap proof artifacts (the fresh-pod 430/hr storm) ────
# The OC gateway (esp. 2026.6.10) rewrites/chmods the active openclaw.log,
# clamping mask::--- (st_mode group triad → 600); Evolve's evolve-read reassert
# restores mask::r-x (st_mode group triad → 650). The file's real group::/other::
# stay ``---`` throughout (owner-only in fact; only user:evolve:r-x can read).
# OC's non-ACL-aware audit catches a 650 window and emits perms_readable; this
# suppressor re-stats and often catches the file back at 600. The OLD early
# return (raw & 0o077 == 0 → False) un-suppressed the flapped finding there →
# fire→clear→fire storm. The fix: trust the EFFECTIVE (getfacl) ACL, so an
# owner-only st_mode with a clamped mask + only the evolve service ACE suppresses.


def test_linux_acl_masked_owner_only_true_for_flapped_to_600_with_clamped_mask(tmp_path):
    """THE flap regression (case a): the file is momentarily back at st_mode
    0600 (mask clamped to ``---`` by the OC gateway) but its real group::/other::
    are ``---`` and the only named reader is ``user:evolve`` (now showing
    ``#effective:---`` because the mask caps it). This is owner-only IN FACT —
    the STRONGEST proof of non-exposure — so a perms_readable finding flapped in
    from a prior 650 window must be SUPPRESSED, not re-fired. Pre-fix this hit
    the ``raw & 0o077 == 0`` early return and returned False → the storm."""
    f = tmp_path / "openclaw.log"
    f.write_text("log line\n")
    os.chmod(f, 0o600)  # flapped back to owner-only; mask is ---
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\t\t\t#effective:---\n"
        "group::---\nmask::---\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is True


def test_linux_acl_masked_owner_only_true_for_flapped_state_dir_clamped_mask(tmp_path):
    """Flap regression, state-dir variant (fs.state_dir.perms_readable): the
    .openclaw dir momentarily at st_mode 0700 with the mask clamped to ``---``;
    real group::/other:: ---, only the evolve service ACE present → suppress."""
    d = tmp_path / ".openclaw"
    d.mkdir()
    os.chmod(d, 0o700)  # flapped to owner-only; mask ---
    acl = (
        f"# file: {d}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\t\t\t#effective:---\n"
        "group::---\nmask::---\nother::---\n"
        "default:user::rwx\ndefault:group::---\ndefault:other::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(d) is True


def test_linux_acl_masked_owner_only_false_for_real_group_capped_by_clamped_mask(tmp_path):
    """The fix must NOT over-suppress: a file at st_mode 0600 because the mask
    (``---``) is capping a REAL ``group::r-x`` is a GENUINE exposure the moment
    the mask widens — effective_mode restores the real group bits → fire. This
    is the case the owner-only-st_mode shortcut would have wrongly swallowed
    once we stopped early-returning, so pin it explicitly."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o600)  # st_mode owner-only because mask --- caps a real group::r-x
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\t\t\t#effective:---\n"
        "group::r-x\t\t\t#effective:---\nmask::---\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_false_for_foreign_user_under_clamped_mask(tmp_path):
    """Owner-only st_mode (mask ---) but a NON-service named ACE
    (``user:mallory:r``) is present: the moment the mask widens, mallory reads
    it — a real grant Evolve never makes → must fire even at st_mode 0600."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r-x\t\t\t#effective:---\n"
        "user:mallory:r--\t\t\t#effective:---\ngroup::---\nmask::---\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


# ── 2b-ter. write-bit proof artifacts (the m::rwX reassert FP) ────────────────
# The every-~5-min reassert_mask widens the mask to ``m::rwX``; the stat group
# triad then shows the WRITE bit, so OC's st_mode audit fires "group-writable".
# acl_masked_owner_only's ``eff & 0o077`` test covers the write bit identically
# to the read bit (effective_mode substitutes the real group:: bits incl. ``w``,
# and never touches the OTHER triad), so a real group::w / other::w still fires.


def test_linux_acl_masked_owner_only_true_for_write_bit_mask_artifact_dir(tmp_path):
    """Proof artifact (a), WRITE bit: a .openclaw dir at st_mode 0770 whose
    apparent group-write is purely the m::rwX mask (real group::/other:: ---)
    → suppress. This is the live evo-pod shape the writable finding fires on."""
    d = tmp_path / ".openclaw"
    d.mkdir()
    os.chmod(d, 0o770)  # group triad = mask rwx, NOT a real group grant
    acl = (
        f"# file: {d}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::---\nmask::rwx\nother::---\n"
        "default:user::rwx\ndefault:group::---\ndefault:other::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(d) is True


def test_linux_acl_masked_owner_only_false_for_real_group_write(tmp_path):
    """Proof artifact (b), WRITE bit: a dir whose REAL owning group has rwx
    (not just the mask) is a genuine group-write exposure → must still fire."""
    d = tmp_path / ".openclaw"
    d.mkdir()
    os.chmod(d, 0o770)
    acl = (
        f"# file: {d}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::rwx\nmask::rwx\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(d) is False


def test_linux_acl_masked_owner_only_false_for_real_other_write_under_mask(tmp_path):
    """Proof artifact (c), WRITE bit: the mask caps only the GROUP class, never
    OTHER. A dir with evolve's ACL (mask present, group::---) that is ALSO
    other-WRITABLE has a real ``other::w`` — effective_mode leaves the other
    triad untouched → must fire. This is why fs.state_dir.perms_world_writable
    is excluded from the mask-prone set, but the seam stays correct regardless."""
    d = tmp_path / ".openclaw"
    d.mkdir()
    os.chmod(d, 0o772)  # group triad = mask rwx; other triad = real -w-
    acl = (
        f"# file: {d}\n# owner: evo\n# group: evo\n"
        "user::rwx\nuser:evolve:r-x\ngroup::---\nmask::rwx\nother::-w-\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(d) is False


def test_linux_acl_masked_owner_only_runs_getfacl_once(tmp_path):
    """The shared lines-based effective_mode core means a single getfacl
    snapshot (no double-spawn / TOCTOU window)."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    acl = (
        f"# file: {f}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    run, calls = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is True
    assert sum(1 for c in calls if c[0][:1] == [GETFACL]) == 1


def test_linux_acl_masked_owner_only_getfacl_failure_is_false(tmp_path):
    """Fail-closed: a getfacl error must never suppress a security finding."""
    f = tmp_path / "openclaw.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    run, _ = _recording_runner({(GETFACL,): _Result(1, stderr="No such file")})
    assert LinuxPerms(runner=run).acl_masked_owner_only(f) is False


def test_linux_acl_masked_owner_only_stat_failure_is_false(tmp_path):
    """Fail-closed on a missing/unstatable file even when getfacl claims a
    mask (e.g. the path vanished between audit and re-check)."""
    missing = tmp_path / "gone.json"
    acl = (
        f"# file: {missing}\n# owner: evo\n# group: evo\n"
        "user::rw-\nuser:evolve:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    run, _ = _recording_runner({(GETFACL,): _Result(0, stdout=acl)})
    assert LinuxPerms(runner=run).acl_masked_owner_only(missing) is False


# ── 2c. LinuxPerms mask reassert ──────────────────────────────────────────────


def test_linux_reassert_mask_rewidens_when_mask_present(tmp_path):
    run, calls = _recording_runner({
        (GETFACL, "-p"): _Result(0, stdout=_GETFACL_MASKED),
    })
    assert LinuxPerms(runner=run).reassert_mask(tmp_path) is True
    assert calls[-1][0] == ["sudo", SETFACL, "-m", "m::rwX", str(tmp_path)]


def test_linux_reassert_mask_never_creates_an_acl(tmp_path):
    """On a path with no extended ACL (e.g. the credentials/ carve-out)
    the reassert must be a query-only no-op — ``setfacl -m m::`` would
    CREATE an ACL where the carve-out deliberately stripped one."""
    base_only = (
        f"# file: {tmp_path}\n# owner: evolve\n# group: wheel\n"
        "user::rwx\ngroup::r-x\nother::---\n"
    )
    run, calls = _recording_runner({(GETFACL,): _Result(0, stdout=base_only)})
    assert LinuxPerms(runner=run).reassert_mask(tmp_path) is True
    assert [c[0][0:2] for c in calls] == [[GETFACL, "-p"]]  # no setfacl ran


def test_linux_recursive_reassert_repairs_only_masked_paths(tmp_path):
    """The recursive form never runs ``setfacl -R`` (which would mint
    mask entries on every non-ACL'd file in the tree). It enumerates
    ACL'd paths via ``getfacl -R -s -p`` and repairs exactly the ones
    whose ACCESS ACL carries a mask: a default-ACL-only dir is listed
    by -s but must be left alone."""
    root = tmp_path / "plugins"
    enumerated = (
        f"# file: {root}\n# owner: evolve\n# group: staff\n"
        "user::rwx\nuser:evolve:r-x\ngroup::r-x\nmask::r-x\nother::r-x\n"
        "\n"
        f"# file: {root}/defaults-only\n# owner: evolve\n# group: staff\n"
        "user::rwx\ngroup::r-x\nother::r-x\n"
        "default:user::rwx\ndefault:user:evolve:r-x\n"
        "default:group::r-x\ndefault:mask::r-x\ndefault:other::r-x\n"
        "\n"
        f"# file: {root}/lib/hook.py\n# owner: evolve\n# group: staff\n"
        "user::rw-\nuser:evolve:r--\ngroup::r--\nmask::r--\nother::r--\n"
    )
    run, calls = _recording_runner({
        (GETFACL, "-R", "-s", "-p"): _Result(0, stdout=enumerated),
    })
    assert LinuxPerms(runner=run).reassert_mask(root, recursive=True) is True
    assert [c[0] for c in calls] == [
        [GETFACL, "-R", "-s", "-p", str(root)],
        ["sudo", SETFACL, "-m", "m::rwX", str(root)],
        ["sudo", SETFACL, "-m", "m::rwX", f"{root}/lib/hook.py"],
    ]


def test_linux_recursive_reassert_repairs_masked_children_under_unacled_root(tmp_path):
    """Root has no ACL but a child does — the per-path enumeration must
    still find and repair the child (a root-only guard would skip it)."""
    root = tmp_path / "plugins"
    enumerated = (
        f"# file: {root}/nested/state.json\n# owner: evolve\n# group: staff\n"
        "user::rw-\nuser:evo:rw-\ngroup::r--\nmask::r--\nother::---\n"
    )
    run, calls = _recording_runner({
        (GETFACL, "-R", "-s", "-p"): _Result(0, stdout=enumerated),
    })
    assert LinuxPerms(runner=run).reassert_mask(root, recursive=True) is True
    assert calls[-1][0] == ["sudo", SETFACL, "-m", "m::rwX", f"{root}/nested/state.json"]


# ── 3. FakePerms semantics ────────────────────────────────────────────────────


def test_fake_perms_grants_answer_the_effective_check(tmp_path):
    fake = FakePerms()
    assert fake.acl_user_effective(tmp_path, "evolve", POD_READ_ACL_PERMS) is False
    fake.grant_read_recursive(tmp_path, "evolve")
    assert fake.acl_user_effective(tmp_path, "evolve", POD_READ_ACL_PERMS) is True
    # the read grant does not satisfy a write contract
    assert fake.acl_user_effective(tmp_path, "evolve", _EVO_WRITE_PERMS) is False
    fake.grant_write_recursive(tmp_path, "evo", _EVO_WRITE_PERMS)
    assert fake.acl_user_effective(tmp_path, "evo", _EVO_WRITE_PERMS) is True
    # The trailing field is the restrict_group_other flag (default False).
    assert ("grant_read_recursive", str(tmp_path), "evolve", False) in fake.calls


def test_fake_perms_clear_acl_revokes_and_records(tmp_path):
    fake = FakePerms()
    creds = tmp_path / "credentials"
    fake.grant_read_recursive(creds, "evolve")
    fake.clear_acl(creds)
    assert fake.acl_user_effective(creds, "evolve", POD_READ_ACL_PERMS) is False
    assert str(creds) in fake.cleared
    # The recursive flag is recorded in the call log (deploy's dist-restore
    # asserts on it through FakePerms in the both-profile unit tests).
    assert ("clear_acl", str(creds), False) in fake.calls
    fake.clear_acl(creds, recursive=True)
    assert ("clear_acl", str(creds), True) in fake.calls


def test_fake_perms_effective_mode_is_real_stat(tmp_path):
    f = tmp_path / "f.json"
    f.write_text("{}")
    os.chmod(f, 0o640)
    assert FakePerms().effective_mode(f) == 0o640


# ── 4. swappability — Protocol conformance + factory keying ──────────────────

PROTOCOL_VERBS = (
    "grant_read_recursive", "grant_write_recursive", "grant",
    "clear_acl", "acl_user_effective", "effective_mode",
    "acl_masked_owner_only", "reassert_mask",
)


@pytest.mark.parametrize("backend_cls", [MacOSPerms, LinuxPerms, FakePerms])
def test_backends_conform_to_the_perms_protocol(backend_cls):
    backend = backend_cls()
    assert isinstance(backend, Perms)
    for verb in PROTOCOL_VERBS:
        proto_sig = inspect.signature(getattr(Perms, verb))
        impl_sig = inspect.signature(getattr(backend_cls, verb))
        assert impl_sig == proto_sig, (
            f"{backend_cls.__name__}.{verb} signature {impl_sig} != "
            f"protocol {proto_sig} — a swap would break a call site"
        )


def test_get_perms_keys_off_the_platform_profile():
    """Both profiles: the factory selects the matching backend, so a
    pinned profile (tests, the wizard's platform gate) gets the right
    argv dialect without any call-site changes."""
    from platform_profile import LINUX, MACOS, set_profile

    try:
        set_profile(MACOS)
        assert isinstance(get_perms(), MacOSPerms)
        set_profile(LINUX)
        assert isinstance(get_perms(), LinuxPerms)
        # back again — per-profile defaults are stable instances
        set_profile(MACOS)
        assert isinstance(get_perms(), MacOSPerms)
    finally:
        set_profile(None)


def test_set_perms_override_wins_over_both_profiles():
    from platform_profile import LINUX, MACOS, set_profile

    fake = FakePerms()
    set_perms(fake)
    try:
        for profile in (MACOS, LINUX):
            set_profile(profile)
            assert get_perms() is fake
    finally:
        set_profile(None)
        set_perms(None)
    assert get_perms() is not fake

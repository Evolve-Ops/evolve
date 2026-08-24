"""Admin chown call sites are platform-keyed (W7-followup, [META:platform]).

The ``evolve`` admin daemon runs ``sudo chown …`` against a NOPASSWD grant
that ``setup_wizard._render_evolve_sudoers`` renders from the platform
profile. On a Linux pod the chown binary is ``/usr/bin/chown`` (not the macOS
``/usr/sbin/chown``) and the gid-0 group is ``root`` (``wheel`` is not gid 0
on Ubuntu). A hardcoded macOS literal therefore fails to match the grant →
sudo prompts → the daemon has no TTY → the chown fails silently, often
leaving a token file root-owned and unreadable by the bot (the live VPS
"approve a user → gateway EACCES" incident).

These tests pin that the admin call sites swept in this change resolve the
chown BINARY through ``platform_profile.get_profile().chown`` and route the
gid-0 ADMIN group through ``.admin_group`` — while a bot account's PRIMARY
group ``:staff`` stays a literal (it exists on Ubuntu at gid 50). They drive
each site with ``subprocess.run`` mocked, so the host is never touched, and
restore the MACOS profile in teardown to preserve macOS byte-identity for the
~dozens of other suites that import these modules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import platform_profile as pp


CURRENT_USER = subprocess.run(
    ["id", "-un"], capture_output=True, text=True, check=False,
).stdout.strip() or "nobody"


class _R:
    """Minimal stand-in for subprocess.CompletedProcess (success)."""

    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def record_subprocess(monkeypatch):
    """Record every subprocess.run argv and return success without executing.

    Patches the stdlib module object, so it intercepts both top-level and
    function-local ``import subprocess`` in the modules under test.
    """
    calls: list[list[str]] = []

    def _fake(argv, *args, **kwargs):
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake)
    return calls


@pytest.fixture(autouse=True)
def _restore_macos_profile():
    # Conftest pins MACOS process-wide; restore it after any per-test override
    # so byte-identity suites that follow are unaffected.
    yield
    pp.set_profile(pp.MACOS)


def _chown_calls(calls: list[list[str]]) -> list[list[str]]:
    """argvs whose chown binary element is present (basename == 'chown')."""
    return [
        c for c in calls
        if any(isinstance(a, str) and a.rsplit("/", 1)[-1] == "chown" for a in c)
    ]


def _chown_binary(argv: list[str]) -> str:
    for a in argv:
        if isinstance(a, str) and a.rsplit("/", 1)[-1] == "chown":
            return a
    raise AssertionError(f"no chown binary in {argv}")


# ── config.save_network (daemon-critical; gid-0 admin group wheel→root) ───────


@pytest.mark.parametrize(
    "profile,expected_bin,expected_group",
    [(pp.MACOS, "/usr/sbin/chown", "wheel"), (pp.LINUX, "/usr/bin/chown", "root")],
)
def test_save_network_chown_is_platform_keyed(
    record_subprocess, tmp_path, profile, expected_bin, expected_group
):
    from evolve_admin import config

    pp.set_profile(profile)
    dest = tmp_path / "network.json"
    config.save_network({"bots": {}}, dest)

    chowns = _chown_calls(record_subprocess)
    assert chowns, f"{profile.name}: no chown issued by save_network"
    argv = chowns[-1]
    assert _chown_binary(argv) == expected_bin
    # gid-0 admin group: evolve:wheel on macOS, evolve:root on Linux.
    assert f"evolve:{expected_group}" in argv, argv


# ── retire._sudo_cp_tree_fallback (archive copy; gid-0 admin group) ───────────


@pytest.mark.parametrize(
    "profile,expected_bin,expected_group",
    [(pp.MACOS, "/usr/sbin/chown", "wheel"), (pp.LINUX, "/usr/bin/chown", "root")],
)
def test_retire_archive_chown_is_platform_keyed(
    record_subprocess, profile, expected_bin, expected_group
):
    from evolve_admin import retire

    pp.set_profile(profile)
    ok, _detail = retire._sudo_cp_tree_fallback(Path("/src/tree"), Path("/dst/tree"))
    assert ok

    chowns = _chown_calls(record_subprocess)
    assert chowns, f"{profile.name}: no chown issued by archive copy"
    argv = chowns[-1]
    assert _chown_binary(argv) == expected_bin
    assert f"evolve:{expected_group}" in argv, argv
    # The bot-private `:staff` mapping must NOT leak into an admin-group chown.
    assert not any("evolve:staff" in a for a in argv), argv


# ── provisioning._create_openclaw_dir (bot :staff — binary only) ──────────────


@pytest.mark.parametrize(
    "profile,expected_bin", [(pp.MACOS, "/usr/sbin/chown"), (pp.LINUX, "/usr/bin/chown")]
)
def test_provisioning_oc_dir_chown_is_platform_keyed(
    record_subprocess, tmp_path, profile, expected_bin
):
    from evolve_admin import provisioning

    pp.set_profile(profile)
    oc_dir = tmp_path / "Users" / "fakebot" / ".openclaw"  # does not exist → create branch
    created = provisioning._create_openclaw_dir(user="fakebot", oc_dir=oc_dir)
    assert created

    chowns = _chown_calls(record_subprocess)
    assert chowns, f"{profile.name}: no chown issued by _create_openclaw_dir"
    argv = chowns[-1]
    assert _chown_binary(argv) == expected_bin
    # bot PRIMARY group stays literal `:staff` on both platforms.
    assert "fakebot:staff" in argv, argv


# ── backup_keys.ensure_bot_in_sync (bot :staff — binary only) ─────────────────


@pytest.mark.parametrize(
    "profile,expected_bin", [(pp.MACOS, "/usr/sbin/chown"), (pp.LINUX, "/usr/bin/chown")]
)
def test_backup_keys_chown_is_platform_keyed(
    record_subprocess, tmp_path, monkeypatch, profile, expected_bin
):
    from evolve_admin import backup_keys

    # Redirect canonical-path lookups at a tmpdir (mirrors test_backup_keys.env).
    ssh_root = tmp_path / "evolve-ssh"
    ssh_root.mkdir()
    users_root = tmp_path / "Users"
    users_root.mkdir()
    monkeypatch.setenv(backup_keys._ENV_PRIV, str(ssh_root / "evolve-backup-shared"))
    monkeypatch.setenv(backup_keys._ENV_PUB, str(ssh_root / "evolve-backup-shared.pub"))
    monkeypatch.setenv(backup_keys._ENV_USERS_ROOT, str(users_root))
    monkeypatch.setenv(backup_keys._ENV_NO_SUDO, "1")
    (ssh_root / "evolve-backup-shared").write_bytes(b"FAKEPRIV\n")
    (ssh_root / "evolve-backup-shared.pub").write_text("ssh-ed25519 AAAAFAKE evolve\n")
    (users_root / CURRENT_USER).mkdir(parents=True, exist_ok=True)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    net = {"sharedDir": str(shared_dir), "bots": {"bot-a": {"user": CURRENT_USER}}}

    pp.set_profile(profile)
    backup_keys.ensure_bot_in_sync("bot-a", CURRENT_USER, net)

    chowns = _chown_calls(record_subprocess)
    assert chowns, f"{profile.name}: no chown issued by ensure_bot_in_sync"
    for argv in chowns:
        assert _chown_binary(argv) == expected_bin, argv
        # bot PRIMARY group stays literal `:staff` on both platforms.
        assert f"{CURRENT_USER}:staff" in argv, argv

"""Platform-keyed OpenClaw installs-registry paths (2026-06-23 Linux fold-in).

``safe_upgrade._installs_sqlite_path`` / ``_installs_json_path`` /
``_installs_json_migrated_path`` build a bot user's plugin-registry paths. They
hardcoded ``/Users/<user>``, so on Linux every installs read resolved to a
nonexistent ``/Users`` path and returned ``None`` (masked by the preflight
gate's fail-safe). They now route through ``config.user_home`` (pwd-first,
profile-keyed fallback): ``/home/<user>`` on Linux, ``/Users/<user>`` on macOS.

A deliberately-nonexistent account is used so ``user_home`` falls through pwd to
the profile-keyed construction — the platform behaviour under test.
"""
from __future__ import annotations

from pathlib import Path

from evolve_admin import safe_upgrade as su

_FAKE_USER = "evolve_nonexistent_acct_zzz"


def test_installs_paths_are_linux_correct_under_linux_profile():
    from platform_profile import LINUX, set_profile

    set_profile(LINUX)  # the autouse conftest fixture restores MACOS on teardown
    assert su._installs_sqlite_path(_FAKE_USER) == Path(
        f"/home/{_FAKE_USER}/.openclaw/state/openclaw.sqlite")
    assert su._installs_json_path(_FAKE_USER) == Path(
        f"/home/{_FAKE_USER}/.openclaw/plugins/installs.json")
    assert su._installs_json_migrated_path(_FAKE_USER) == Path(
        f"/home/{_FAKE_USER}/.openclaw/plugins/installs.json.migrated")


def test_installs_paths_macos_byte_identical():
    # MACOS profile is the conftest default; the resolved paths must be exactly
    # what the old hardcoded /Users/<user> literals produced (mini unchanged).
    assert su._installs_sqlite_path(_FAKE_USER) == Path(
        f"/Users/{_FAKE_USER}/.openclaw/state/openclaw.sqlite")
    assert su._installs_json_path(_FAKE_USER) == Path(
        f"/Users/{_FAKE_USER}/.openclaw/plugins/installs.json")
    assert su._installs_json_migrated_path(_FAKE_USER) == Path(
        f"/Users/{_FAKE_USER}/.openclaw/plugins/installs.json.migrated")

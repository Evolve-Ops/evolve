"""Tests for platform_profile — the blessed home for platform-divergent paths.

Design: docs/design-linux-port-2026-06-10.md §6. The macOS profile is a
BEHAVIOR-PRESERVATION pin: its values must equal the strings the codebase
hardcoded before the 8.3 sweep (and the CLAUDE.md "macOS Paths" table) —
a drift here silently rewrites sudoers grants and deploy paths.
"""

from __future__ import annotations

import dataclasses

import pytest

from platform_profile import LINUX, MACOS, PlatformProfile, get_profile, is_within


# ── is_within / nested_deploy_checkout (2026-06-23 Linux freeze) ──────────────
#
# The freeze: on Linux the deploy checkout is a CHILD of shared_dir
# (/var/lib/evolve/repo under /var/lib/evolve), so a recursive perms pass over
# shared_dir descended into the git tree and flipped tracked files'
# exec bits → `git pull --ff-only` refused → fleet frozen on stale code. On
# macOS the checkout is a SIBLING (/Users/Shared/evolve-repo next to
# /Users/Shared/evolve), so the same pass never touches it. is_within() is the
# structural seam that tells the two layouts apart (no sys.platform branch).


def test_is_within_linux_nested_layout():
    assert is_within("/var/lib/evolve/repo", "/var/lib/evolve") is True
    assert is_within("/var/lib/evolve/repo/packages/admin/x.py", "/var/lib/evolve") is True


def test_is_within_macos_sibling_layout_is_not_contained():
    # The crux: a sibling that shares a string prefix is NOT within. A naive
    # str.startswith would wrongly call this contained and break macOS.
    assert is_within("/Users/Shared/evolve-repo", "/Users/Shared/evolve") is False


def test_is_within_equal_paths_not_within():
    assert is_within("/var/lib/evolve", "/var/lib/evolve") is False


def test_is_within_normalizes_trailing_slash_and_dotsegments():
    assert is_within("/var/lib/evolve/repo/", "/var/lib/evolve/") is True
    assert is_within("/var/lib/evolve/./repo", "/var/lib/evolve") is True


def test_nested_deploy_checkout_linux_returns_repo_path():
    from pathlib import Path

    got = LINUX.nested_deploy_checkout("/var/lib/evolve")
    assert got == Path("/var/lib/evolve/repo")


def test_nested_deploy_checkout_macos_returns_none():
    # Sibling layout → None → recursive passes safely recurse the whole tree,
    # byte-identical to the pre-fix blanket -R (the hard macOS invariant).
    assert MACOS.nested_deploy_checkout("/Users/Shared/evolve") is None


def test_nested_deploy_checkout_honors_overridden_shared_dir():
    # A pod with a non-default sharedDir whose repo is still nested under it.
    prof = dataclasses.replace(
        LINUX, shared_dir_default="/srv/evolve", deploy_checkout_default="/srv/evolve/repo"
    )
    from pathlib import Path

    assert prof.nested_deploy_checkout("/srv/evolve") == Path("/srv/evolve/repo")
    # ...and None when the passed shared_dir does not actually contain it.
    assert prof.nested_deploy_checkout("/some/other/dir") is None


# ── get_profile() keying ─────────────────────────────────────────────────────


def test_get_profile_darwin_is_macos():
    assert get_profile("darwin") is MACOS


@pytest.mark.parametrize("platform", ["linux", "linux2"])
def test_get_profile_linux(platform):
    assert get_profile(platform) is LINUX


def test_get_profile_unknown_defaults_to_macos():
    # Every pod that exists today is macOS — unknowns must preserve
    # current behavior, not crash or guess Linux.
    assert get_profile("freebsd14") is MACOS
    assert get_profile("") is MACOS


def test_get_profile_no_arg_uses_sys_platform():
    import sys

    expected = LINUX if sys.platform.startswith("linux") else MACOS
    assert get_profile() is expected


# ── macOS profile: behavior-preservation pins ────────────────────────────────


def test_macos_paths_match_pre_sweep_literals():
    assert MACOS.user_home_root == "/Users"
    assert MACOS.shared_dir_default == "/Users/Shared/evolve"
    assert MACOS.deploy_checkout_default == "/Users/Shared/evolve-repo"
    assert MACOS.daemon_dir == "/Library/LaunchDaemons"
    # scratch_dir is the world-traversable cwd for sudo -u <bot> subprocesses;
    # on macOS it stays /Users/Shared (the pre-scratch_dir run_cmd default).
    assert MACOS.scratch_dir == "/Users/Shared"


def test_macos_command_table_matches_claude_md():
    # The CLAUDE.md "macOS Paths" table, verbatim. These feed subprocess
    # calls and the W4 sudoers writer; a wrong path is a dead grant.
    assert MACOS.cat == "/bin/cat"
    assert MACOS.service_manager == "/bin/launchctl"
    assert MACOS.chown == "/usr/sbin/chown"  # /bin/chown doesn't exist on macOS
    assert MACOS.chmod == "/bin/chmod"
    assert MACOS.mkdir == "/bin/mkdir"
    assert MACOS.cp == "/bin/cp"
    assert MACOS.rm == "/bin/rm"


def test_macos_deploy_layer_paths_and_group():
    # Deploy-layer canonical singletons (Linux deploy-port W7). macOS values
    # are behavior-preservation pins — they must equal the literals deploy.py
    # / installer.py hardcoded before the W7 sweep.
    assert MACOS.venv_dir == "/Users/Shared/evolve-venv"
    assert MACOS.venv_python == "/Users/Shared/evolve-venv/bin/python3"
    assert MACOS.venv_evolve_admin == "/Users/Shared/evolve-venv/bin/evolve-admin"
    assert MACOS.plugin_install_dir == "/Users/Shared/evolve-plugin"
    # The admin/root group on macOS is `wheel` (gid 0) — the installer's
    # `chown root:wheel` shared-dir owner and the plugin-dir chowns.
    assert MACOS.admin_group == "wheel"


def test_macos_openclaw_pkg_json_candidates():
    # macOS Homebrew (Apple Silicon) + /usr/local (Intel / manual npm -g).
    from pathlib import Path
    assert MACOS.node_global_modules_dirs == (
        "/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules",
    )
    assert MACOS.openclaw_pkg_json_candidates == (
        Path("/opt/homebrew/lib/node_modules/openclaw/package.json"),
        Path("/usr/local/lib/node_modules/openclaw/package.json"),
    )


def test_macos_has_no_linux_user_management():
    # macOS account management is dscl/sysadminctl (MacOSIsolation
    # adapter), not useradd/userdel/getent.
    assert MACOS.useradd is None
    assert MACOS.userdel is None
    assert MACOS.getent is None
    for name in ("useradd", "userdel", "getent"):
        assert name not in MACOS.commands


def test_evolve_config_shared_dir_default_unchanged_on_macos():
    # The wired consumer: evolve_config's canonical default must resolve
    # to the exact pre-sweep string on macOS.
    import evolve_config

    if get_profile() is MACOS:
        assert str(evolve_config.CANONICAL_SHARED_DIR) == "/Users/Shared/evolve"


# ── Linux profile: spec §6 values ────────────────────────────────────────────


def test_linux_paths_match_design_doc():
    assert LINUX.user_home_root == "/home"
    assert LINUX.shared_dir_default == "/var/lib/evolve"
    assert LINUX.deploy_checkout_default == "/var/lib/evolve/repo"
    assert LINUX.daemon_dir == "/etc/systemd/system"
    # /Users/Shared does not exist on Linux — the scratch cwd is /tmp (1777).
    assert LINUX.scratch_dir == "/tmp"
    assert LINUX.service_manager == "/usr/bin/systemctl"
    assert LINUX.chown == "/usr/bin/chown"  # NOT the macOS /usr/sbin quirk
    assert LINUX.useradd == "/usr/sbin/useradd"
    assert LINUX.userdel == "/usr/sbin/userdel"
    assert LINUX.getent == "/usr/bin/getent"


def test_linux_deploy_layer_paths_and_group():
    # Deploy-layer canonical singletons on Linux (W7). FHS-correct siblings
    # of the shared dir, mirroring the macOS /Users/Shared layout.
    assert LINUX.venv_dir == "/var/lib/evolve-venv"
    assert LINUX.venv_python == "/var/lib/evolve-venv/bin/python3"
    assert LINUX.venv_evolve_admin == "/var/lib/evolve-venv/bin/evolve-admin"
    assert LINUX.plugin_install_dir == "/var/lib/evolve-plugin"
    # The admin/root group on Linux is `root` (gid 0). `wheel` exists on
    # Linux only as a sudo-membership group (not gid 0), so the pre-W7
    # `chown root:wheel` failed on a fresh Ubuntu box — the bug this fixes.
    assert LINUX.admin_group == "root"


def test_linux_openclaw_pkg_json_candidates_include_nodesource_path():
    # The bug: the macOS-only candidate list missed /usr/lib/node_modules —
    # `npm root -g` on the NodeSource/apt VPS (evolve-vsp-pod) — so OpenClaw's
    # version read as None on Linux and the cve-scan applicability filter went
    # inert. /usr/lib must be first (the verified live location).
    from pathlib import Path
    assert LINUX.node_global_modules_dirs == (
        "/usr/lib/node_modules", "/usr/local/lib/node_modules",
    )
    assert LINUX.openclaw_pkg_json_candidates == (
        Path("/usr/lib/node_modules/openclaw/package.json"),
        Path("/usr/local/lib/node_modules/openclaw/package.json"),
    )
    # No macOS Homebrew path leaks into the Linux candidate list.
    assert all(
        "homebrew" not in str(p) for p in LINUX.openclaw_pkg_json_candidates
    )


def test_openclaw_pkg_json_candidates_derive_from_node_modules_dirs():
    # The property is derived, so it can never drift from the field — each
    # candidate is "<root>/openclaw/package.json".
    import dataclasses
    from pathlib import Path
    prof = dataclasses.replace(MACOS, node_global_modules_dirs=("/x/lib", "/y/lib"))
    assert prof.openclaw_pkg_json_candidates == (
        Path("/x/lib/openclaw/package.json"),
        Path("/y/lib/openclaw/package.json"),
    )


def test_macos_npm_global_prefix_byte_identical():
    # #3194 follow-up: ocadmin's OPENCLAW_NPM_PREFIX (the `npm install -g`
    # --prefix target) flows through this. macOS MUST resolve to the exact
    # prior hardcode "/opt/homebrew" — when openclaw isn't installed (fallback
    # path) AND when it is (first-existing path), both are /opt/homebrew.
    assert MACOS.npm_global_prefix == "/opt/homebrew"


def test_linux_npm_global_prefix_is_usr():
    # On the Linux VPS pod openclaw lives at /usr/lib/node_modules/openclaw
    # (npm root -g = /usr/lib/node_modules, prefix -g = /usr). The upgrade MUST
    # target --prefix=/usr so it lands where the gateway/systemd unit loads
    # from — NOT the macOS Homebrew prefix the Linux gateway never reads.
    assert LINUX.npm_global_prefix == "/usr"
    assert "homebrew" not in LINUX.npm_global_prefix


def test_npm_global_prefix_derives_from_node_modules_dirs():
    # Derived from node_global_modules_dirs (<root>.parent.parent), so it can
    # never drift from the openclaw_pkg_json_candidates / install location.
    # Fallback branch (openclaw not installed) is deterministic, no FS probe.
    import dataclasses
    prof = dataclasses.replace(MACOS, node_global_modules_dirs=("/x/lib/node_modules",))
    assert prof.npm_global_prefix == "/x"


def test_macos_npx_bin_byte_identical():
    # ocadmin's OPENCLAW_NPX (Google Workspace `npx @googleworkspace/cli auth`)
    # flows through this. macOS MUST equal the prior hardcode.
    assert MACOS.npx_bin == "/opt/homebrew/bin/npx"


def test_linux_npx_bin_is_usr_bin():
    # NodeSource/apt bundles npx beside node in /usr/bin; no Homebrew on Linux.
    assert LINUX.npx_bin == "/usr/bin/npx"
    assert "homebrew" not in LINUX.npx_bin


def test_venv_python_and_evolve_admin_derive_from_venv_dir():
    # venv_evolve_admin is a derived property — it can never drift from
    # venv_dir; venv_python is a sibling under the same bin/.
    for profile in (MACOS, LINUX):
        assert profile.venv_python == f"{profile.venv_dir}/bin/python3"
        assert profile.venv_evolve_admin == f"{profile.venv_dir}/bin/evolve-admin"


def test_admin_group_differs_by_platform():
    # The whole point of platform-keying the group: `wheel` (macOS gid 0)
    # is NOT gid 0 on Linux, so the two profiles must diverge.
    assert MACOS.admin_group != LINUX.admin_group


def test_bot_shared_group_is_platform_keyed():
    # The shared group every bot joins — the admin-daemon socket's connect
    # channel. macOS: `staff` (every account's primary group); Linux:
    # `evolve-bots` (the secondary group `useradd -G evolve-bots` adds bots to,
    # since Linux bots are NOT in the socket-owning `evolve` group).
    assert MACOS.bot_shared_group == "staff"
    assert LINUX.bot_shared_group == "evolve-bots"
    # Must equal the canonical creator literal so the chgrp target / connect ACE
    # and `groupadd -f evolve-bots` can't drift apart.
    from runtime.isolation import EVOLVE_BOTS_GROUP
    assert LINUX.bot_shared_group == EVOLVE_BOTS_GROUP
    # Distinct from the ROOT-owned-artifact group (admin_group).
    assert LINUX.bot_shared_group != LINUX.admin_group
    # The default (unknown platforms resolve to MACOS) carries the macOS value.
    assert PlatformProfile.__dataclass_fields__["bot_shared_group"].default == "staff"


# ── commands table (the W4 sudoers-writer surface) ───────────────────────────


def test_commands_table_is_complete_and_absolute():
    for profile in (MACOS, LINUX):
        for name in ("cat", "chmod", "chown", "mkdir", "cp", "rm", "service_manager"):
            assert name in profile.commands, f"{profile.name} missing {name}"
        for name, path in profile.commands.items():
            assert path.startswith("/"), (
                f"{profile.name}.{name} = {path!r} is not an absolute path"
            )


def test_linux_commands_include_user_management():
    assert LINUX.commands["useradd"] == "/usr/sbin/useradd"
    assert LINUX.commands["userdel"] == "/usr/sbin/userdel"
    assert LINUX.commands["getent"] == "/usr/bin/getent"


# ── immutability ─────────────────────────────────────────────────────────────


def test_profiles_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        MACOS.user_home_root = "/home"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        LINUX.daemon_dir = "/tmp"  # type: ignore[misc]


def test_profile_is_a_leaf_module():
    # platform_profile must import nothing from Evolve — evolve_config
    # imports it, so any Evolve import here is a cycle waiting to happen.
    import platform_profile

    source = open(platform_profile.__file__, encoding="utf-8").read()
    assert "import evolve_config" not in source
    assert "from evolve_config" not in source
    assert "import evolve_admin" not in source


# ── blessed user_home helper (evolve_config) ─────────────────────────────────


def test_user_home_resolves_existing_account_via_pwd():
    # root exists everywhere; on macOS its home is /var/root — proof the
    # helper resolves via pwd rather than doing path construction.
    import pwd

    from evolve_config import user_home

    assert user_home("root") == __import__("pathlib").Path(pwd.getpwnam("root").pw_dir)


def test_user_home_falls_back_to_profile_construction():
    from pathlib import Path

    from evolve_config import user_home

    home = user_home("no-such-account-xyz")
    assert home == Path(get_profile().user_home_root) / "no-such-account-xyz"


def test_bot_home_delegates_to_user_home():
    from pathlib import Path

    from evolve_config import bot_home

    # bot_id with a user override in config: resolution must go through
    # get_bot_user first (bot_id ≠ account), then user_home.
    config = {"bots": {"logical-bot": {"user": "no-such-account-xyz"}}}
    assert bot_home("logical-bot", config) == (
        Path(get_profile().user_home_root) / "no-such-account-xyz"
    )


def test_profile_dataclass_has_no_mutable_defaults():
    # Frozen + only str/None fields: hashable and safely shareable.
    assert hash(MACOS) != hash(LINUX)
    assert isinstance(MACOS, PlatformProfile)

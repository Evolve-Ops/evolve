"""#3194 follow-up — ocadmin's OpenClaw npm-upgrade wrapper paths are
platform-keyed via ``platform_profile``, not hardcoded macOS Homebrew paths.

The bug: ``evolve-admin menu upgrade`` read the installed version from, and ran
``npm install -g --prefix=…`` against, ``/opt/homebrew/...`` on every platform.
On the Linux VPS pod OpenClaw lives at ``/usr/lib/node_modules/openclaw`` (npm
``root -g`` = /usr/lib/node_modules, ``prefix -g`` = /usr), so the version read
errored and the install would land in a macOS prefix the Linux gateway never
loads from — a silently-ineffective upgrade.

These tests reload ``ocadmin`` under a forced profile (the module-level
constants resolve at import) and assert:

* macOS: byte-identical to the prior ``/opt/homebrew`` hardcodes.
* Linux: ``--prefix=/usr``, package.json under /usr/lib/node_modules, npx on
  /usr/bin — where the systemd-unit gateway actually loads from.
"""
from __future__ import annotations

import importlib

import platform_profile


def _reload_ocadmin_with_profile(monkeypatch, profile):
    """Force ``platform_profile.get_profile`` to *profile*, then reimport
    ocadmin so its import-time constants resolve against that profile."""
    monkeypatch.setattr(platform_profile, "get_profile", lambda: profile)
    import evolve_admin.ocadmin as ocadmin

    return importlib.reload(ocadmin)


def _restore_ocadmin(monkeypatch):
    """Undo the forced profile and reload ocadmin so its module-level constants
    resolve against the genuine platform again — a leaked Linux-pinned ocadmin
    would poison later tests that import its constants."""
    monkeypatch.undo()
    import evolve_admin.ocadmin as ocadmin

    importlib.reload(ocadmin)


def test_macos_upgrade_paths_byte_identical(monkeypatch):
    oc = _reload_ocadmin_with_profile(monkeypatch, platform_profile.MACOS)
    try:
        # npm_global_prefix fallback (openclaw not installed locally) is
        # deterministic /opt/homebrew regardless of FS state.
        assert oc.OPENCLAW_NPM_PREFIX == "/opt/homebrew"
        assert oc.OPENCLAW_NPX == "/opt/homebrew/bin/npx"
        # The package.json default resolves to the first-existing candidate, or
        # the first candidate — both macOS node_modules roots, never Linux.
        assert "homebrew" in str(oc.OPENCLAW_PACKAGE_JSON) or \
            "/usr/local/lib/node_modules" in str(oc.OPENCLAW_PACKAGE_JSON)
        assert str(oc.OPENCLAW_PACKAGE_JSON).endswith(
            "/node_modules/openclaw/package.json"
        )
        # The install argv the upgrade builds (ocadmin.py ~line 664).
        target = "openclaw@2026.5.20"
        cmd = ["npm", "install", "-g", f"--prefix={oc.OPENCLAW_NPM_PREFIX}", target]
        assert cmd == [
            "npm", "install", "-g", "--prefix=/opt/homebrew", "openclaw@2026.5.20",
        ]
        assert oc._npm_node_modules_dir().as_posix() == \
            "/opt/homebrew/lib/node_modules"
    finally:
        _restore_ocadmin(monkeypatch)


def test_linux_upgrade_install_argv_targets_usr_prefix(monkeypatch):
    oc = _reload_ocadmin_with_profile(monkeypatch, platform_profile.LINUX)
    try:
        assert oc.OPENCLAW_NPM_PREFIX == "/usr"
        assert oc.OPENCLAW_NPX == "/usr/bin/npx"
        assert str(oc.OPENCLAW_PACKAGE_JSON) == \
            "/usr/lib/node_modules/openclaw/package.json"
        assert "homebrew" not in str(oc.OPENCLAW_PACKAGE_JSON)

        target = "openclaw@2026.6.8"
        cmd = ["npm", "install", "-g", f"--prefix={oc.OPENCLAW_NPM_PREFIX}", target]
        assert cmd == [
            "npm", "install", "-g", "--prefix=/usr", "openclaw@2026.6.8",
        ]
        # Staging dir for `.openclaw-XXX` rename dirs lands under the Linux root.
        assert oc._npm_node_modules_dir().as_posix() == "/usr/lib/node_modules"
    finally:
        _restore_ocadmin(monkeypatch)


# ── #3227 follow-up: the per-bot `doctor` menu action (choice == "d") ─────────
#
# The doctor call was the one wrapper-path #3227 left hardcoded (it carried an
# explicit TODO): ``cd /Users/{user} && /opt/homebrew/bin/openclaw doctor`` —
# both the bot home and the openclaw binary are macOS-only, so on the Linux VPS
# pod the cd lands nowhere (home is /home/{user}) and the binary path doesn't
# exist (openclaw is /usr/bin). These assert the two resolvers now used:
# ``user_home`` (home) and ``_openclaw_cli_path`` (binary).


def _force_present(monkeypatch, present_path):
    """Make ``Path.exists`` return True only for *present_path* so the CLI
    resolver is deterministic regardless of the test host's filesystem (the
    laptop may or may not have a real ``openclaw`` on PATH)."""
    import pathlib

    monkeypatch.setattr(
        pathlib.Path, "exists", lambda self: str(self) == present_path
    )


def test_macos_doctor_paths_byte_identical(monkeypatch):
    """macOS: the resolved binary + home reproduce the prior hardcode exactly —
    ``/opt/homebrew/bin/openclaw`` and ``/Users/{user}``."""
    import evolve_config

    oc = _reload_ocadmin_with_profile(monkeypatch, platform_profile.MACOS)
    try:
        _force_present(monkeypatch, "/opt/homebrew/bin/openclaw")
        assert oc._openclaw_cli_path() == "/opt/homebrew/bin/openclaw"

        # A not-yet-created account name → profile-keyed construction.
        monkeypatch.setattr(evolve_config, "get_profile", lambda: platform_profile.MACOS)
        assert str(oc.user_home("svc-doctor-test")) == "/Users/svc-doctor-test"

        # The full argv the doctor action builds, byte-identical to the old line.
        home = oc.user_home("svc-doctor-test")
        ocbin = oc._openclaw_cli_path()
        assert f"cd {home} && {ocbin} doctor" == \
            "cd /Users/svc-doctor-test && /opt/homebrew/bin/openclaw doctor"
    finally:
        _restore_ocadmin(monkeypatch)


def test_linux_doctor_paths_target_usr_and_home(monkeypatch):
    """Linux: the resolver finds openclaw on /usr/bin and the home is under
    /home — where the systemd-unit pod actually lives."""
    import evolve_config

    oc = _reload_ocadmin_with_profile(monkeypatch, platform_profile.LINUX)
    try:
        _force_present(monkeypatch, "/usr/bin/openclaw")
        ocbin = oc._openclaw_cli_path()
        assert ocbin == "/usr/bin/openclaw"
        assert "homebrew" not in ocbin

        monkeypatch.setattr(evolve_config, "get_profile", lambda: platform_profile.LINUX)
        home = oc.user_home("svc-doctor-test")
        assert str(home) == "/home/svc-doctor-test"

        assert f"cd {home} && {ocbin} doctor" == \
            "cd /home/svc-doctor-test && /usr/bin/openclaw doctor"
    finally:
        _restore_ocadmin(monkeypatch)

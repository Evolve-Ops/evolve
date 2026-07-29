"""Per-bot / pod-perms chown sites are platform-keyed (W7-followup).

Design: docs/design-linux-port-2026-06-10.md §6, the "W7-followup bite".
W7 ported the venv/plugin canonical paths + installer.setup_shared's group,
but DEFERRED the ~two dozen inline ``subprocess.run(["sudo",
"/usr/sbin/chown", ...])`` literals in deploy.py's per-bot-config +
``ensure_pod_perms`` / ``ensure_bot_perms`` paths. Those hardcoded the macOS
chown BINARY (``/usr/sbin/chown``) and, on the root-owned shared dirs, the
macOS gid-0 group ``wheel`` — both HARD failures on a fresh Ubuntu box
(``/usr/sbin/chown`` doesn't exist; ``wheel`` is neither gid 0 nor present).

These tests pin that the routed sites now resolve the binary from
``_PROFILE.chown`` and the admin group from ``_PROFILE.admin_group``, with the
macOS values byte-identical to the pre-followup literals (the hard
macOS-byte-identity invariant). ``deploy._PROFILE`` is captured at import
(module constant), so the host-profile lever here is monkeypatching that
attribute — not ``set_profile`` (which only reaches the call-time
``get_profile()`` / ``get_perms()`` sites).

The ``:staff`` (bot PRIMARY group) sites are deliberately NOT keyed — ``staff``
exists on Ubuntu (gid 50) so they're benign there; see the module-level note in
deploy.py. We assert those keep ``:staff`` while still routing the binary.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[2] / "analyzer", _HERE.parents[1]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import platform_profile as pp  # noqa: E402
from evolve_admin import deploy  # noqa: E402
from evolve_admin.runtime import FakePerms, set_perms  # noqa: E402

# (profile, expected chown binary, expected admin group) — the macOS row is
# the byte-identity pin (must equal the pre-followup literals).
_CASES = [
    (pp.MACOS, "/usr/sbin/chown", "wheel"),
    (pp.LINUX, "/usr/bin/chown", "root"),
]


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


@pytest.fixture
def recorded(monkeypatch):
    """Record deploy.py's direct subprocess.run argv; inject FakePerms so the
    seam tail (reassert_mask / grant_read_recursive) spawns nothing. A
    ``sudo /bin/cat <path>`` is served from the real file so read-modify-write
    flows (strip_agents_main) reach their chown instead of bailing on an empty
    read."""
    calls: list[list[str]] = []

    def _run(cmd, *a, **kw):
        cmd = list(cmd)
        calls.append(cmd)
        if len(cmd) >= 2 and os.path.basename(str(cmd[-2])) == "cat":
            try:
                return _Result(stdout=Path(cmd[-1]).read_text())
            except OSError:
                return _Result()
        return _Result()

    monkeypatch.setattr(deploy.subprocess, "run", _run)
    fake = FakePerms()
    set_perms(fake)
    yield calls, fake
    set_perms(None)


def _chowns(calls: list[list[str]]) -> list[list[str]]:
    """Just the chown argvs (binary is a full path, so match on basename)."""
    return [c for c in calls if c and os.path.basename(c[1]) == "chown"]


@pytest.mark.parametrize("profile,exp_bin,exp_group", _CASES)
def test_ensure_evolve_owned_dir_perms_is_platform_keyed(
    monkeypatch, recorded, tmp_path, profile, exp_bin, exp_group
):
    calls, fake = recorded
    monkeypatch.setattr(deploy, "_PROFILE", profile)
    target = tmp_path / "config_intents"
    target.mkdir()

    assert deploy._ensure_evolve_owned_dir_perms(target) is True

    chowns = _chowns(calls)
    assert chowns, f"{profile.name}: no chown issued"
    # The marquee site: recursive chown to evolve:<admin_group>.
    owner = next((c for c in chowns if "-R" in c), None)
    assert owner is not None, f"{profile.name}: no recursive chown in {chowns}"
    assert owner[1] == exp_bin, f"{profile.name}: chown binary {owner[1]!r}"
    assert f"evolve:{exp_group}" in owner, f"{profile.name}: group in {owner}"
    # The mask re-widen ran through the seam (no raw setfacl/chmod spawn).
    assert any(c[0] == "reassert_mask" for c in fake.calls)


@pytest.mark.parametrize("profile,exp_bin,exp_group", _CASES)
def test_set_dir_owner_is_platform_keyed(
    monkeypatch, recorded, tmp_path, profile, exp_bin, exp_group
):
    calls, _ = recorded
    monkeypatch.setattr(deploy, "_PROFILE", profile)

    assert deploy._set_dir_owner(tmp_path / "alerts", "evolve") is True

    chowns = _chowns(calls)
    assert len(chowns) == 1, chowns
    assert chowns[0][1] == exp_bin
    assert f"evolve:{exp_group}" in chowns[0]


@pytest.mark.parametrize("profile,exp_bin,exp_group", _CASES)
def test_fix_shared_dir_subdir_chown_is_platform_keyed(
    monkeypatch, recorded, tmp_path, profile, exp_bin, exp_group
):
    # The per-bot ensure_bot_perms site: applications/<bot> chowned to
    # <bot>:<admin_group> (bot, NOT staff — it's a pod-shared subdir).
    calls, _ = recorded
    monkeypatch.setattr(deploy, "_PROFILE", profile)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")
    shared = tmp_path / "shared"
    (shared / "applications" / "examplebot").mkdir(parents=True)

    deploy.fix_shared_dir_permissions("examplebot", shared)

    chowns = _chowns(calls)
    app_chown = next(
        (c for c in chowns if any("applications/examplebot" in a for a in c)), None
    )
    assert app_chown is not None, f"{profile.name}: no applications chown in {chowns}"
    assert app_chown[1] == exp_bin
    assert f"examplebot:{exp_group}" in app_chown


@pytest.mark.parametrize("profile,exp_bin", [(p, b) for p, b, _ in _CASES])
def test_staff_sites_route_binary_but_keep_staff_group(
    monkeypatch, recorded, tmp_path, profile, exp_bin
):
    # A representative bot-PRIMARY-group (:staff) site — strip_agents_main
    # writes the bot's openclaw.json then chowns it <bot>:staff. The binary
    # is routed; the group stays literal `staff` (gid 50 on Ubuntu).
    calls, _ = recorded
    monkeypatch.setattr(deploy, "_PROFILE", profile)
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "examplebot")

    home = tmp_path / "examplebot"
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    oc_json = oc / "openclaw.json"
    oc_json.write_text('{"agents":{"main":{}}}\n')
    monkeypatch.setattr(deploy, "_user_home", lambda user: home)
    # chmod_secret_config would sudo-chmod; stub it (not under test here).
    monkeypatch.setattr(deploy._secret_perms, "chmod_secret_config", lambda p: None)
    # The post-repair gateway restart goes through the scheduler seam (its own
    # subprocess) — stub it so the test stays a pure chown-argv assertion.
    monkeypatch.setattr(
        deploy, "get_scheduler", lambda: type("_S", (), {"restart": lambda self, label: (True, "")})()
    )

    deploy.strip_agents_main("examplebot")

    chowns = _chowns(calls)
    staff = next((c for c in chowns if any(a.endswith(":staff") for a in c)), None)
    assert staff is not None, f"{profile.name}: no :staff chown in {chowns}"
    assert staff[1] == exp_bin, f"{profile.name}: binary not routed: {staff}"
    assert any(a == "examplebot:staff" for a in staff)


def test_cellar_check_skipped_on_non_macos(monkeypatch):
    # The Homebrew Cellar check is macOS-only — gated OUT on Linux explicitly
    # (no /opt/homebrew there), never reaching the macOS-only chmod apply.
    monkeypatch.setattr(deploy, "_PROFILE", pp.LINUX)
    res = deploy._check_cellar_perms()
    assert res.ok is True
    assert "non-macos" in res.detail.lower()


def test_cellar_check_not_short_circuited_on_macos(monkeypatch):
    # On macOS the explicit non-macOS gate must NOT fire — the check proceeds
    # to its real sampling (here, the host-layout skip when no Cellar exists).
    monkeypatch.setattr(deploy, "_PROFILE", pp.MACOS)
    res = deploy._check_cellar_perms()
    assert "non-macos" not in res.detail.lower()


def test_node_install_hint_is_platform_appropriate(monkeypatch):
    monkeypatch.setattr(deploy, "_PROFILE", pp.MACOS)
    macos_hint = deploy._node_install_hint()
    assert "brew install node" in macos_hint
    assert "nodesource" not in macos_hint.lower()

    monkeypatch.setattr(deploy, "_PROFILE", pp.LINUX)
    linux_hint = deploy._node_install_hint()
    assert "nodesource" in linux_hint.lower()
    assert "brew" not in linux_hint.lower()

"""tests/test_evolve_app_required_tools_target.py — the first-party Evolve apps
(procedures + crons + the required_tools openclaw.json patch) must land on the
pod's PRIMARY bot, never the brain-less ``evolve`` SERVICE account.

EVOLVE-ACCT-OCJSON. ``install_evolve_app`` used to hardcode the logical bot id
``"evolve"``. On a fresh pod the primary bot is ``"evo"`` (network["primary"]
== "evo", with NO ``bots.evolve`` entry), so ``get_bot_user("evolve")`` fell
back to the literal string ``"evolve"`` → resolved to the ``evolve`` service
account home. Post evo-account-separation that account runs only the admin
daemon and carries NO openclaw.json, so on Linux:

  * the required_tools patch deferred FOREVER (the live VPS GA log), and
  * the procedure + cron landed under /home/evolve where the evo gateway
    (HOME=/home/evo) never reads them.

The fix resolves the PRIMARY bot via ``_resolve_evolve_app_target``
(primary_bot_id → get_bot_user → user_home). These tests DRIVE THE REAL
resolution (no mock of the resolver / get_bot_user) so they fail if the
hardcode ever returns — the lesson from the sibling install_evolve_bot_docs
test, which mocked ``_bot_user_for`` to "evo" and so never exercised the real
fresh-pod fallback.

macOS byte-identity is asserted explicitly: every current macOS network shape
must resolve to /Users/evolve (the legacy hardcode target), unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin import deploy  # noqa: E402


# ── Profile pinning ───────────────────────────────────────────────────────────
# The resolver runs the REAL user_home(); on this dev/CI box neither "evo" nor
# "evolve" is a real OS account, so user_home falls back to
# get_profile().user_home_root / user — /Users/<u> under MACOS, /home/<u> under
# LINUX. Pinning the profile therefore drives a deterministic, fully-real chain.

@pytest.fixture
def macos_profile():
    from platform_profile import MACOS, set_profile, get_profile
    prev = get_profile()
    set_profile(MACOS)
    yield
    set_profile(prev)


@pytest.fixture
def linux_profile():
    from platform_profile import LINUX, set_profile, get_profile
    prev = get_profile()
    set_profile(LINUX)
    yield
    set_profile(prev)


# Current network.json shapes in the wild.
FRESH_MACOS_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evolve"}}}
FRESH_LINUX_NET = {"primary": "evo", "bots": {"evo": {"role": "primary", "user": "evo"}}}
LEGACY_PRIMARY_FIELD_NET = {"primary": "evolve", "bots": {"evolve": {"role": "primary"}}}
LEGACY_NO_PRIMARY_FIELD_NET = {"bots": {"evolve": {"role": "primary"}}}
DEGENERATE_NET: dict = {"bots": {}}


# ── _resolve_evolve_app_target — the real resolution ──────────────────────────


def test_linux_fresh_resolves_to_evo_home(linux_profile):
    """The bug: on a fresh Linux pod the primary is 'evo' on account 'evo'.
    Artifacts + the required_tools patch must land under /home/evo, where the
    evo gateway actually reads — NOT the brain-less /home/evolve service acct."""
    bot_id, account, oc = deploy._resolve_evolve_app_target(FRESH_LINUX_NET)
    assert bot_id == "evo"
    assert account == "evo"
    assert oc == Path("/home/evo/.openclaw")
    # The smoking gun: never the evolve service account.
    assert "/home/evolve" not in str(oc)


def test_macos_fresh_byte_identical_to_legacy_hardcode(macos_profile):
    """HARD INVARIANT: on a fresh macOS pod the primary is 'evo' but its account
    is 'evolve' (pre-cutover), so this resolves to /Users/evolve — byte-identical
    to the old ``_PROFILE.name == "macos"`` hardcode it replaced."""
    bot_id, account, oc = deploy._resolve_evolve_app_target(FRESH_MACOS_NET)
    assert bot_id == "evo"
    assert account == "evolve"
    assert oc == Path("/Users/evolve/.openclaw")  # == the legacy hardcode


@pytest.mark.parametrize("net", [
    FRESH_MACOS_NET,
    LEGACY_PRIMARY_FIELD_NET,
    LEGACY_NO_PRIMARY_FIELD_NET,
    DEGENERATE_NET,
])
def test_macos_all_current_shapes_target_users_evolve(macos_profile, net):
    """Every macOS network shape — fresh, legacy-with-primary-field,
    legacy-without, and the empty-network degenerate fallback — must resolve to
    the SAME /Users/evolve target the hardcode produced. This is the proof that
    macOS behaviour is provably unchanged by the switch to the resolver."""
    _bot_id, account, oc = deploy._resolve_evolve_app_target(net)
    assert account == "evolve"
    assert oc == Path("/Users/evolve/.openclaw")


def test_degenerate_network_falls_back_to_evolve(linux_profile):
    """An empty/unparseable network (primary unresolvable) falls back to the
    literal 'evolve' — same as the old code's bot id, never a crash."""
    bot_id, account, oc = deploy._resolve_evolve_app_target(DEGENERATE_NET)
    assert bot_id == "evolve"
    assert account == "evolve"


def test_bot_literally_named_evolve_is_honored(linux_profile):
    """Adversarial: a pod whose primary IS a bot literally named 'evolve' with a
    remapped account resolves THAT account (no special-casing of the string)."""
    net = {"primary": "evolve", "bots": {"evolve": {"role": "primary", "user": "evo"}}}
    bot_id, account, oc = deploy._resolve_evolve_app_target(net)
    assert bot_id == "evolve"
    assert account == "evo"
    assert oc == Path("/home/evo/.openclaw")


# ── install_evolve_app — drive the REAL patch path ───────────────────────────


def _run_install_evolve_app(net, profile_fixture_name, request, tmp_path):
    """Drive the real install_evolve_app("security-cve-scan", ...) with only the
    privileged I/O boundary stubbed. Returns
    (result, cp_dests, chmod_paths, deferred)."""
    # NOTE: `sudo_dest_refusal` (the D-2 dest gate) is patched off here. It
    # lstats for real, and these tests assert on a byte-exact /home/evo or
    # /Users/evolve that does not exist on the test host — so the gate
    # correctly refuses, and re-pointing those paths would destroy what the
    # test is FOR. The gate itself is pinned against a real filesystem in
    # test_deploy_sudo_dest_gate.py::TestRequiredToolsPatch.
    request.getfixturevalue(profile_fixture_name)  # pin profile

    sudo_calls: list[list[str]] = []
    chmod_paths: list[str] = []

    def _fake_run_sudo(cmd, result, check=True):
        sudo_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_cat(cmd, *a, **k):
        # The only direct subprocess.run in install_evolve_app is the
        # `sudo /bin/cat <path>` read fallback. Return a minimal openclaw.json
        # ONLY for the openclaw.json read (so the patch path runs); everything
        # else (cron jobs.json) reads as absent → starts fresh.
        path = cmd[-1] if isinstance(cmd, (list, tuple)) else ""
        if str(path).endswith("openclaw.json"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"tools": {}}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "absent")

    def _fake_chmod_secret(path):
        chmod_paths.append(str(path))
        return True

    with patch.object(deploy, "load_network", return_value=net), \
         patch.object(deploy, "sudo_dest_refusal", return_value=""), \
         patch.object(deploy, "_run_sudo", _fake_run_sudo), \
         patch.object(deploy.subprocess, "run", _fake_cat), \
         patch("evolve_admin.secret_config_perms.chmod_secret_config", _fake_chmod_secret):
        result = deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)

    cp_dests = [c[-1] for c in sudo_calls if c and c[0] == "cp"]
    deferred = any("cannot enable required_tools" in s for s in result.steps)
    return result, cp_dests, chmod_paths, deferred


def test_install_evolve_app_patches_evo_openclaw_on_linux(linux_profile, request, tmp_path):
    """End-to-end on Linux: the required_tools patch + procedure + cron all cp to
    /home/evo, and the openclaw.json gets the granted chmod 600 — NEVER the
    /home/evolve service account, and the deferral does NOT fire."""
    result, cp_dests, chmod_paths, deferred = _run_install_evolve_app(
        FRESH_LINUX_NET, "linux_profile", request, tmp_path
    )
    assert result.success, result.errors
    # The openclaw.json patch landed on the evo primary.
    assert "/home/evo/.openclaw/openclaw.json" in cp_dests, cp_dests
    assert any(p.endswith("/home/evo/.openclaw/openclaw.json") for p in chmod_paths), chmod_paths
    # Procedure + cron co-located on the same primary home.
    assert any(d.endswith("/home/evo/.openclaw/workspace/procedures/security-cve-scan.md")
               for d in cp_dests), cp_dests
    # The bug must be gone: nothing touches the brain-less service account, and
    # the permanent-deferral path is not taken.
    assert all("/home/evolve/" not in d for d in cp_dests), cp_dests
    assert not deferred, result.steps


def test_install_evolve_app_macos_unchanged(macos_profile, request, tmp_path):
    """macOS byte-identity through the real install path: the openclaw.json
    patch still targets /Users/evolve/.openclaw/openclaw.json (the legacy
    target), and no chown is emitted (cp preserves owner; §4 grants chmod only)."""
    result, cp_dests, chmod_paths, deferred = _run_install_evolve_app(
        FRESH_MACOS_NET, "macos_profile", request, tmp_path
    )
    assert result.success, result.errors
    assert "/Users/evolve/.openclaw/openclaw.json" in cp_dests, cp_dests
    assert any(p.endswith("/Users/evolve/.openclaw/openclaw.json") for p in chmod_paths), chmod_paths
    assert not deferred, result.steps


def test_install_evolve_app_emits_no_chown_on_openclaw(linux_profile, request, tmp_path):
    """Secret-config contract: the patched openclaw.json always pre-exists (we
    only patch what we just read), so `cp` (no -p) preserves its owner and the
    old chown is redundant; 0600 is enforced via chmod_secret_config. The patch
    must emit NO `chown` against openclaw.json."""
    result, _cp, _chmod, _deferred = _run_install_evolve_app(
        FRESH_LINUX_NET, "linux_profile", request, tmp_path
    )
    # Re-capture all sudo calls to assert on chown specifically.
    sudo_calls: list[list[str]] = []

    def _cap(cmd, res, check=True):
        sudo_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_cat(cmd, *a, **k):
        path = cmd[-1] if isinstance(cmd, (list, tuple)) else ""
        if str(path).endswith("openclaw.json"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"tools": {}}), "")
        return subprocess.CompletedProcess(cmd, 1, "", "absent")

    with patch.object(deploy, "load_network", return_value=FRESH_LINUX_NET), \
         patch.object(deploy, "_run_sudo", _cap), \
         patch.object(deploy.subprocess, "run", _fake_cat), \
         patch("evolve_admin.secret_config_perms.chmod_secret_config", lambda p: True):
        deploy.install_evolve_app("security-cve-scan", shared_dir=tmp_path)

    chown_on_oc = [c for c in sudo_calls
                   if c and c[0] == "chown" and str(c[-1]).endswith("openclaw.json")]
    assert chown_on_oc == [], f"ungranted chown on openclaw.json: {chown_on_oc}"

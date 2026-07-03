"""tests/test_setup_status.py — First-time-setup detection.

Covers `evolve_admin.status.setup_status()` plus the /api/setup-status
endpoint. The detection is load-bearing for the no-primary banner and
install-evo wizard flow — every false case in the detection chain has
to independently flag setup as incomplete so the operator never lands
in the "silently broken pod" state described in the PR follow-up to
#1890.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.status import setup_status  # noqa: E402


def _home_with_openclaw(home: Path, body: str = "{}") -> Path:
    """Create ``<home>/.openclaw/openclaw.json`` and return *home*.

    Pair with ``patch("evolve_admin.status.user_home", return_value=home)``:
    the gate reads ``user_home(user) / ".openclaw" / "openclaw.json"``, so the
    test home stands in for the (platform-resolved) primary home.
    """
    oc = home / ".openclaw" / "openclaw.json"
    oc.parent.mkdir(parents=True, exist_ok=True)
    oc.write_text(body)
    return home


def _write_network(path: Path, **overrides) -> Path:
    body = {
        "networkId": "test",
        "sharedDir": str(path.parent / "shared"),
        "primary": "evo",
        "members": ["evo"],
        "bots": {"evo": {"role": "primary", "port": 19000, "multiUser": False}},
    }
    body.update(overrides)
    path.write_text(json.dumps(body))
    return path


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    return _write_network(tmp_path / "network.json")


# ── Happy path ──────────────────────────────────────────────────────────────


def test_setup_complete_when_all_four_checks_pass(network_path: Path, tmp_path: Path):
    """All four detection booleans true → setup_complete=true."""
    home = _home_with_openclaw(tmp_path / "home" / "evo")
    with patch("evolve_admin.status._macos_account_exists", return_value=True), \
         patch("evolve_admin.status.user_home", return_value=home):
        r = setup_status(network_path)
    assert r["setup_complete"] is True
    assert r["has_primary"] is True
    assert r["primary_bot_id"] == "evo"
    assert r["primary_registered"] is True
    assert r["primary_macos_account_ok"] is True
    assert r["primary_openclaw_json_ok"] is True
    assert r["evo_install_target"] == "evo"


# ── Each false case independently triggers setup_complete=false ─────────────


def test_unset_pointer_resolves_via_role_primary_bot(tmp_path: Path):
    """network.primary unset but the default bot carries role:"primary" → the
    shared resolver finds it, so has_primary=True and the bot is registered.

    This previously asserted has_primary=False — that encoded the split-brain
    bug (gate read only network.primary while the engine resolved the role).
    The genuinely-no-primary case is covered by
    test_genuine_no_primary_still_fires_banner.
    """
    network_path = _write_network(tmp_path / "network.json", primary=None)
    with patch("evolve_admin.status._macos_account_exists", return_value=False):
        r = setup_status(network_path)
    assert r["has_primary"] is True
    assert r["primary_bot_id"] == "evo"
    assert r["primary_registered"] is True
    # account stubbed missing → still not setup_complete
    assert r["setup_complete"] is False


def test_primary_points_at_missing_bot(tmp_path: Path):
    """network.primary set but the bot isn't in network.bots."""
    network_path = _write_network(
        tmp_path / "network.json",
        primary="evo",
        bots={},  # bot ledger empty
        members=[],
    )
    r = setup_status(network_path)
    assert r["has_primary"] is True
    assert r["primary_registered"] is False
    assert r["primary_macos_account_ok"] is False
    assert r["setup_complete"] is False


def test_macos_account_missing(network_path: Path):
    """Bot registered but dscl can't find the macOS account."""
    with patch("evolve_admin.status._macos_account_exists", return_value=False):
        r = setup_status(network_path)
    assert r["primary_registered"] is True
    assert r["primary_macos_account_ok"] is False
    assert r["primary_openclaw_json_ok"] is False
    assert r["setup_complete"] is False


def test_openclaw_json_missing(network_path: Path, tmp_path: Path):
    """macOS account exists but openclaw.json never landed."""
    # Point the read at a path that doesn't exist; the sudo cat fallback
    # should also fail (no sudoers entry in test env).
    missing_home = tmp_path / "home" / "evo"  # no .openclaw/openclaw.json created
    with patch("evolve_admin.status._macos_account_exists", return_value=True), \
         patch("evolve_admin.status.user_home", return_value=missing_home), \
         patch("evolve_admin.status.subprocess.run") as mock_run:
        # sudo -n cat should fail (no passwordless sudo in test env)
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        r = setup_status(network_path)
    assert r["primary_macos_account_ok"] is True
    assert r["primary_openclaw_json_ok"] is False
    assert r["setup_complete"] is False


def test_openclaw_json_corrupt(network_path: Path, tmp_path: Path):
    """openclaw.json exists but is not valid JSON."""
    home = _home_with_openclaw(tmp_path / "home" / "evo", body="{not valid json")
    with patch("evolve_admin.status._macos_account_exists", return_value=True), \
         patch("evolve_admin.status.user_home", return_value=home), \
         patch("evolve_admin.status.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        r = setup_status(network_path)
    assert r["primary_openclaw_json_ok"] is False
    assert r["setup_complete"] is False


# ── Resolver-based primary detection (split-brain bug fix) ───────────────────
#
# setup_status() routes primary detection through primary_bot.primary_bot_id —
# the same resolver the engine uses — so the banner gate can't disagree with
# the engine. The resolver's order: (1) top-level network.primary, (2) the
# first bots{} entry with role:"primary", (3) legacy "evolve" fallback.


def test_role_primary_without_top_level_pointer(tmp_path: Path):
    """Regression: a working role:primary bot with NO top-level `primary`
    field must resolve → has_primary=True, setup_complete=True.

    Before the resolver fix this returned has_primary=False (the gate read
    only network.primary) and fired the false install-evo banner over a
    working primary.
    """
    network_path = _write_network(
        tmp_path / "network.json",
        primary=None,  # older provisioner never wrote the pointer
        members=["evolve"],
        bots={"evolve": {"role": "primary", "port": 19030, "multiUser": False}},
    )
    home = _home_with_openclaw(tmp_path / "home" / "evolve")
    with patch("evolve_admin.status._macos_account_exists", return_value=True), \
         patch("evolve_admin.status.user_home", return_value=home):
        r = setup_status(network_path)
    assert r["has_primary"] is True
    assert r["primary_bot_id"] == "evolve"
    assert r["primary_registered"] is True
    assert r["setup_complete"] is True


def test_role_primary_under_non_evo_name(tmp_path: Path):
    """Any bot can be the admin bot: a role:primary bot named something other
    than evo/evolve resolves and is not nagged."""
    network_path = _write_network(
        tmp_path / "network.json",
        primary=None,
        members=["team-bot-a"],
        bots={"team-bot-a": {"role": "primary", "port": 19030, "multiUser": False}},
    )
    home = _home_with_openclaw(tmp_path / "home" / "team-bot-a")
    with patch("evolve_admin.status._macos_account_exists", return_value=True), \
         patch("evolve_admin.status.user_home", return_value=home):
        r = setup_status(network_path)
    assert r["has_primary"] is True
    assert r["primary_bot_id"] == "team-bot-a"
    assert r["primary_registered"] is True
    assert r["setup_complete"] is True


def test_genuine_no_primary_still_fires_banner(tmp_path: Path):
    """No top-level pointer, no role:primary bot, no legacy evolve → genuinely
    no primary: setup_complete=False and the install-evo target is present."""
    network_path = _write_network(
        tmp_path / "network.json",
        primary=None,
        members=[],
        bots={},  # nothing resolves
    )
    r = setup_status(network_path)
    assert r["has_primary"] is False
    assert r["primary_bot_id"] is None
    assert r["primary_registered"] is False
    assert r["setup_complete"] is False
    assert r["evo_install_target"] == "evo"


def test_misconfigured_pointer_still_fails(tmp_path: Path):
    """Safety case preserved: top-level primary names a bot that isn't in
    bots{} → the resolver trusts the pointer, but primary_registered must
    still fail so a real misconfiguration keeps firing the banner."""
    network_path = _write_network(
        tmp_path / "network.json",
        primary="ghost",
        members=[],
        bots={},  # "ghost" is not registered
    )
    r = setup_status(network_path)
    assert r["has_primary"] is True
    assert r["primary_bot_id"] == "ghost"
    assert r["primary_registered"] is False
    assert r["setup_complete"] is False


# ── Cross-platform home resolution (META:platform S3) ───────────────────────


def test_linux_primary_home_not_under_users(tmp_path: Path):
    """Regression: on a Linux pod the primary's home is /home/<user>, NOT
    /Users/<user>. setup_status must resolve the openclaw.json read through
    user_home() (pwd-first, profile fallback) and the account check through a
    POSIX-portable pwd lookup — both platform-correct in the admin-ui daemon,
    which never runs the platform gate that swaps in the Linux isolation
    adapter.

    Before this fix, status.py hardcoded ``Path(f"/Users/{user}/...")`` and
    routed account-existence through ``get_isolation().user_exists()`` (a dscl
    probe that no-ops on Linux), so a healthy evo-primary on Ubuntu reported
    ``setup_complete=False`` and fired the false install-evo banner.

    Drives the real ``user_home`` / ``pwd.getpwnam`` code under a pinned LINUX
    profile (``user_home_root="/home"``), with a faked passwd entry whose home
    is a tmp dir explicitly NOT under ``/Users``.
    """
    from types import SimpleNamespace

    from platform_profile import LINUX, set_profile

    network_path = _write_network(tmp_path / "network.json")  # primary "evo"
    linux_home = _home_with_openclaw(tmp_path / "home" / "evo")
    assert "/Users" not in str(linux_home)  # the invariant under test

    def _fake_pw(name: str):
        return SimpleNamespace(
            pw_name=name, pw_passwd="x", pw_uid=502, pw_gid=502,
            pw_gecos="", pw_dir=str(linux_home), pw_shell="/bin/bash",
        )

    set_profile(LINUX)
    try:
        with patch("evolve_config.pwd.getpwnam", side_effect=_fake_pw), \
             patch("evolve_admin.status.pwd.getpwnam", side_effect=_fake_pw):
            r = setup_status(network_path)
    finally:
        set_profile(None)  # reset to sys.platform default for other tests

    assert r["primary_macos_account_ok"] is True
    assert r["primary_openclaw_json_ok"] is True
    assert r["setup_complete"] is True


def test_account_missing_is_keyerror_not_dscl(tmp_path: Path):
    """_macos_account_exists is pwd-based: an unknown account (pwd KeyError)
    reads as missing, independent of any isolation adapter / dscl."""
    from evolve_admin.status import _macos_account_exists

    with patch("evolve_admin.status.pwd.getpwnam", side_effect=KeyError("nope")):
        assert _macos_account_exists("ghost-user") is False


# ── API endpoint surface ─────────────────────────────────────────────────────


def test_endpoint_returns_setup_status_shape(network_path: Path):
    """GET /api/setup-status returns the same shape setup_status() produces."""
    from flask import Flask
    from evolve_admin.web.server import setup_status as _imported_setup_status

    app = Flask(__name__)

    @app.get("/api/setup-status")
    def _ep():
        from flask import jsonify
        return jsonify(_imported_setup_status(network_path))

    with patch("evolve_admin.status._macos_account_exists", return_value=False):
        client = app.test_client()
        r = client.get("/api/setup-status")
    assert r.status_code == 200
    body = r.get_json()
    expected_keys = {
        "setup_complete", "has_primary", "primary_bot_id",
        "primary_registered", "primary_macos_account_ok",
        "primary_openclaw_json_ok", "evo_install_target",
    }
    assert expected_keys.issubset(body.keys())
    assert body["evo_install_target"] == "evo"
    # macOS account check stubbed to False → not setup_complete
    assert body["setup_complete"] is False

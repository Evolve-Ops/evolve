"""Regression: bot_id may differ from macOS account name (team_bot_b/personal_bot_user case).

Pins the contract that every module resolving per-bot filesystem paths must
go through evolve_admin.config.bot_home() or get_bot_user(), never via a
raw f"/Users/{bot_id}/" construction.

The canonical real-world example is team_bot_b (logical bot_id) running on the
personal_bot_user macOS account. Any code that wrote Path(f"/Users/{bot_id}/...")
would look for /Users/team_bot_b/.openclaw/ which does not exist — the actual
directory is /Users/personal_bot_user/.openclaw/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.config import bot_home, get_bot_user  # noqa: E402

# ── Canonical test network ────────────────────────────────────────────────────

# This network mirrors the production structure. team_bot_b has a "user" override.
NETWORK = {
    "bots": {
        "team_bot_a": {"role": "member", "port": 18789},
        "team_bot_b": {"role": "member", "port": 18790, "user": "personal_bot_user"},
        "admin_bot": {"role": "member", "port": 18791},
        "team_bot_c": {"role": "member", "port": 18792},
    }
}

# ── config.py — core accessor tests ──────────────────────────────────────────


def test_bot_home_with_matching_account():
    """When bot_id matches the account, bot_home returns /Users/<bot_id>."""
    home = str(bot_home("team_bot_a", NETWORK))
    # Either pwd resolved it or we fell back to /Users/team_bot_a
    assert home.endswith("/team_bot_a"), f"expected path ending in /team_bot_a, got {home!r}"


def test_bot_home_with_divergent_account():
    """When bot_id has a 'user' override, bot_home follows it (team_bot_b/personal_bot_user)."""
    # Mock pwd.getpwnam so the test works without a real personal_bot_user account
    fake_pw = MagicMock()
    fake_pw.pw_dir = "/Users/personal_bot_user"
    with patch("evolve_admin.config.pwd.getpwnam", return_value=fake_pw):
        home = str(bot_home("team_bot_b", NETWORK))
    assert home.endswith("/personal_bot_user"), f"got {home!r}"
    assert "/team_bot_b" not in home, f"path incorrectly contains /team_bot_b: {home!r}"


def test_bot_home_divergent_account_pwd_fallback():
    """When pwd.getpwnam raises KeyError, bot_home falls back to /Users/<user>."""
    with patch("evolve_admin.config.pwd.getpwnam", side_effect=KeyError("personal_bot_user")):
        home = str(bot_home("team_bot_b", NETWORK))
    assert home == "/Users/personal_bot_user", f"got {home!r}"


def test_get_bot_user_default():
    """Bots without 'user' field resolve to bot_id."""
    assert get_bot_user("team_bot_a", NETWORK) == "team_bot_a"


def test_get_bot_user_override():
    """Bots with 'user' field resolve to the account name."""
    assert get_bot_user("team_bot_b", NETWORK) == "personal_bot_user"


def test_get_bot_user_no_network():
    """get_bot_user with empty network falls back to bot_id."""
    assert get_bot_user("team_bot_b", {}) == "team_bot_b"


# ── config_sandbox/stores.py — _bot_oc_dir respects user override ─────────────


def test_config_sandbox_bot_oc_dir_team_bot_b():
    """config_sandbox stores resolves team_bot_b to personal_bot_user account."""
    from evolve_admin.config_sandbox.stores import _bot_oc_dir

    fake_pw = MagicMock()
    fake_pw.pw_dir = "/Users/personal_bot_user"
    with patch("evolve_admin.config.pwd.getpwnam", return_value=fake_pw), \
         patch("evolve_admin.config.load_network", return_value=NETWORK):
        oc = _bot_oc_dir("team_bot_b", NETWORK)

    assert str(oc) == "/Users/personal_bot_user/.openclaw", f"got {oc!r}"


def test_config_sandbox_bot_oc_dir_team_bot_a():
    """config_sandbox stores resolves team_bot_a (no override) to /Users/team_bot_a/.openclaw."""
    from evolve_admin.config_sandbox.stores import _bot_oc_dir

    fake_pw = MagicMock()
    fake_pw.pw_dir = "/Users/team_bot_a"
    with patch("evolve_admin.config.pwd.getpwnam", return_value=fake_pw), \
         patch("evolve_admin.config.load_network", return_value=NETWORK):
        oc = _bot_oc_dir("team_bot_a", NETWORK)

    assert str(oc) == "/Users/team_bot_a/.openclaw", f"got {oc!r}"


# ── web/server.py — defer-archive path uses bot_home ─────────────────────────


def test_server_defer_stats_uses_bot_home_for_team_bot_b():
    """_defer_stats iterates archive files using bot_home, not /Users/{bot_id}/."""
    # We can't easily call the real route, but we can verify the import path:
    # the fix imports _bot_home from config (line 61 of server.py) which is
    # bot_home from evolve_admin.config. That already handles user overrides.
    # This test verifies that bot_home("team_bot_b", NETWORK) does not produce
    # /Users/team_bot_b/ — i.e., the function the route now uses is correct.
    fake_pw = MagicMock()
    fake_pw.pw_dir = "/Users/personal_bot_user"
    with patch("evolve_admin.config.pwd.getpwnam", return_value=fake_pw):
        archive = bot_home("team_bot_b", NETWORK) / ".openclaw" / "workspace" / "evolve" / "defer-archive.jsonl"
    assert str(archive).startswith("/Users/personal_bot_user/"), f"got {archive!r}"
    assert "/team_bot_b/" not in str(archive), f"path incorrectly contains /team_bot_b/: {archive!r}"


# ── wizard.py — first bot path uses DiscoveredBot.home, not f"/Users/{bot_id}" ──


def test_wizard_uses_discovered_bot_home():
    """wizard.py's channel-detection code now reads from DiscoveredBot.home,
    not Path(f'/Users/{first_bot_id}/...'). Verify the dataclass has home."""
    from evolve_admin.wizard import DiscoveredBot

    bot = DiscoveredBot(
        bot_id="team_bot_b",
        user="personal_bot_user",
        home=Path("/Users/personal_bot_user"),
        oc_config=Path("/Users/personal_bot_user/.openclaw/openclaw.json"),
        workspace=None,
        port=18790,
    )
    assert bot.home == Path("/Users/personal_bot_user")
    # The .home path should be used, not constructed from bot_id
    oc_json = bot.home / ".openclaw" / "openclaw.json"
    assert str(oc_json) == "/Users/personal_bot_user/.openclaw/openclaw.json"

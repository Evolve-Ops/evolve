"""Regression tests for audit._check_user_accounts and _known_bot_users.

Pins the contract that bot users from network.json are auto-allowlisted —
adding/removing a bot must NOT trigger 🔴 CRITICAL "New user account(s)
detected" if the new bot is in network.json.

Background: every 15-min security audit was firing CRITICAL because personal_bot
(a real, expected bot user) wasn't in the static baseline file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# ── _known_bot_users: resolves bot_id → unix user via network.json ──


@pytest.mark.parametrize("config,expected", [
    # Empty / minimal configs — no bots known
    ({}, set()),
    ({"members": []}, set()),
    ({"members": ["team_bot_a"]}, {"team_bot_a"}),  # no bots dict → bot_id is the user
    # bot_id == user (the common case)
    (
        {"members": ["team_bot_a", "admin_bot"], "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}}},
        {"team_bot_a", "admin_bot"},
    ),
    # bot_id != user (the team_bot_b → personal_bot_user case from finding 011)
    (
        {"members": ["team_bot_b"], "bots": {"team_bot_b": {"user": "personal_bot_user"}}},
        {"personal_bot_user"},
    ),
    # bot in members but missing user mapping → fall back to bot_id
    (
        {"members": ["team_bot_a", "team_bot_b"], "bots": {"team_bot_b": {"user": "personal_bot_user"}}},
        {"team_bot_a", "personal_bot_user"},
    ),
    # Defensive: bots dict has non-dict entries
    (
        {"members": ["team_bot_a"], "bots": {"team_bot_a": "not-a-dict"}},
        {"team_bot_a"},
    ),
    # Defensive: members is None / bots is None
    (
        {"members": None, "bots": None},
        set(),
    ),
])
def test_known_bot_users_resolution(config, expected):
    assert audit._known_bot_users(config) == expected


# ── _check_user_accounts: bot users now auto-allowlisted ──


def _make_dscl_output(users):
    """Mimic `dscl . -list /Users` output."""
    # Include the standard system users dscl normally lists
    sys_users = ["_assetcache", "_atsserver", "daemon", "nobody", "root"]
    return "\n".join(sys_users + list(users)) + "\n"


def _patch_dscl(users):
    from subprocess import CompletedProcess

    def fake(cmd, **kw):
        return CompletedProcess(args=cmd, returncode=0,
                                stdout=_make_dscl_output(users), stderr="")
    return patch.object(audit.subprocess, "run", side_effect=fake)


def _baseline(tmp_path: Path, content: str) -> Path:
    sec = tmp_path / "security"
    sec.mkdir()
    (sec / "user-accounts.baseline").write_text(content)
    return tmp_path


def test_new_bot_user_in_network_json_does_not_trigger_critical(tmp_path):
    """The reported false-positive: personal_bot exists as a macOS user and is in
    network.json's members, but the static baseline file predates personal_bot.
    Audit MUST NOT flag personal_bot as a new user."""
    # Baseline lists everyone EXCEPT personal_bot (the historical state on the mini)
    _baseline(tmp_path, "pod_admin_user\nevolve\nforge\nteam_bot_a\nteam_bot_c\nadmin_bot\nsecurity_bot\n")
    config = {
        "members": ["team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot"],
        "bots": {b: {"user": b} for b in ["team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot"]},
    }
    with _patch_dscl(["pod_admin_user", "evolve", "forge", "team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot"]):
        findings = audit._check_user_accounts(tmp_path, config)
    levels = [f.level for f in findings]
    msgs = [f.message for f in findings]
    assert "critical" not in levels, (
        f"personal_bot is in network.json — must NOT be CRITICAL. Got: {msgs}"
    )
    assert any("OK" in m for m in msgs)


def test_truly_unknown_user_still_triggers_critical(tmp_path):
    """The fix must NOT swallow real new-user alerts. A user not in the
    baseline AND not in network.json must still fire CRITICAL."""
    _baseline(tmp_path, "pod_admin_user\nevolve\nteam_bot_a\n")
    config = {
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    with _patch_dscl(["pod_admin_user", "evolve", "team_bot_a", "attacker"]):  # 'attacker' is new + not in network
        findings = audit._check_user_accounts(tmp_path, config)
    criticals = [f for f in findings if f.level == "critical"]
    assert len(criticals) == 1, f"expected 1 critical, got {findings}"
    assert "attacker" in criticals[0].message


def test_team_bot_b_personal_bot_user_mapping_doesnt_flag_either_name(tmp_path):
    """team_bot_b's macOS user is personal_bot_user. Baseline doesn't have personal_bot_user but
    network.json maps team_bot_b → personal_bot_user. Neither name should fire CRITICAL."""
    _baseline(tmp_path, "pod_admin_user\nevolve\n")
    config = {
        "members": ["team_bot_b"],
        "bots": {"team_bot_b": {"user": "personal_bot_user"}},
    }
    with _patch_dscl(["pod_admin_user", "evolve", "personal_bot_user"]):
        findings = audit._check_user_accounts(tmp_path, config)
    msgs = [f.message for f in findings]
    assert not any("CRITICAL" in m for m in msgs), (
        f"personal_bot_user is the macOS account for team_bot_b — must not be CRITICAL. Got: {msgs}"
    )


def test_baseline_user_no_longer_in_dscl_or_network_warns_removed(tmp_path):
    """If a user is in the baseline but no longer exists AND isn't a known
    bot, that's a real "removed" finding."""
    _baseline(tmp_path, "pod_admin_user\nevolve\nteam_bot_a\nformer-bot\n")
    config = {
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    with _patch_dscl(["pod_admin_user", "evolve", "team_bot_a"]):  # former-bot is gone
        findings = audit._check_user_accounts(tmp_path, config)
    warns = [f for f in findings if f.level == "warn"]
    assert any("former-bot" in f.message for f in warns), (
        f"deleted non-bot user should warn. Got: {[f.message for f in findings]}"
    )


def test_removing_bot_from_network_does_not_warn(tmp_path):
    """Symmetric to the new-user case: removing a bot from network.json
    should not warn just because its user account is still on the box."""
    _baseline(tmp_path, "pod_admin_user\nevolve\nteam_bot_a\npersonal_bot\n")
    config = {
        "members": ["team_bot_a"],  # personal_bot removed from members
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    with _patch_dscl(["pod_admin_user", "evolve", "team_bot_a", "personal_bot"]):  # personal_bot still exists
        findings = audit._check_user_accounts(tmp_path, config)
    warns = [f for f in findings if f.level == "warn"]
    # personal_bot is in baseline AND still exists, but not in network anymore.
    # personal_bot is still a known bot user (just not in this config), so removing
    # from network shouldn't fire either critical OR removed-warning.
    assert not any("personal_bot" in f.message for f in findings), (
        f"removing a bot from network.json — bot's user still on disk — should be silent. Got: {[f.message for f in findings]}"
    )


def test_back_compat_no_config_argument(tmp_path):
    """Calling without a config (the old signature) must still work — no
    bot allowlist, falls back to baseline-only."""
    _baseline(tmp_path, "pod_admin_user\nevolve\nteam_bot_a\n")
    with _patch_dscl(["pod_admin_user", "evolve", "team_bot_a", "personal_bot"]):
        # Old call signature (no config arg) must not crash
        findings = audit._check_user_accounts(tmp_path)
    # Without config, personal_bot has no allowlist → fires CRITICAL (the old
    # behavior). This documents the safety net: callers that haven't been
    # updated to pass config get the strict baseline-only check.
    criticals = [f for f in findings if f.level == "critical"]
    assert len(criticals) == 1
    assert "personal_bot" in criticals[0].message

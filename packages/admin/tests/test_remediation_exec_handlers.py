"""Tests for the Phase 5 exec-config remediation handlers.

Pins:
  - set_exec_allowlist writes the allowlist into tools.exec.allowlist on
    the bot's openclaw.json, preserves other tools/exec keys, and rejects
    bad input shapes before hitting safe_write_bot_config.
  - set_exec_security writes tools.exec.security to one of the allowed
    levels, is idempotent on no-op, and rejects bad levels with a clear error.
  - Both handlers route writes through safe_write_bot_config so the
    OpenClaw schema validation runs before the live config is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.remediation.handlers import (  # noqa: E402
    HANDLERS,
    handle_set_exec_allowlist,
    handle_set_exec_security,
)


# ── Registry ────────────────────────────────────────────────────────────────


def test_registry_now_has_five_kinds():
    """After Phase 5, the handler registry adds the two exec-config kinds."""
    assert "set_exec_allowlist" in HANDLERS
    assert "set_exec_security" in HANDLERS
    # The original three are still there
    assert "install_infra_jobs" in HANDLERS
    assert "reset_baseline" in HANDLERS
    assert "flip_cron_session_target" in HANDLERS


# ── set_exec_allowlist ──────────────────────────────────────────────────────


@pytest.fixture
def fake_cfg(monkeypatch):
    """Stub _bot_user_for + _load_bot_oc_config + _safe_write_bot_config so
    handler tests run without touching real /Users paths or invoking sudo."""
    state = {
        "user": "security_bot",
        "cfg": {
            "agents": {"defaults": {"workspace": "/Users/security_bot/.openclaw/workspace"}},
            "tools": {"exec": {"security": "full", "ask": "on-miss"}},
        },
        "writes": [],
    }
    monkeypatch.setattr(
        "evolve_admin.remediation.handlers._bot_user_for",
        lambda bot_id: state["user"],
    )
    monkeypatch.setattr(
        "evolve_admin.remediation.handlers._load_bot_oc_config",
        lambda user: json.loads(json.dumps(state["cfg"])),  # deep copy
    )

    def capture_write(bot_id, cfg, reason):
        state["writes"].append({"bot_id": bot_id, "cfg": cfg, "reason": reason})

    monkeypatch.setattr(
        "evolve_admin.remediation.handlers._safe_write_bot_config",
        capture_write,
    )
    return state


def test_set_exec_allowlist_writes_list(tmp_path: Path, fake_cfg):
    out = handle_set_exec_allowlist(
        {"bot_id": "security_bot", "commands": ["ls", "cat", "git"]}, tmp_path,
    )
    assert out["bot_id"] == "security_bot"
    assert out["count"] == 3
    assert out["previous_allowlist"] == []
    assert out["new_allowlist"] == ["ls", "cat", "git"]
    # Verify the write was attempted with the expected config shape
    assert len(fake_cfg["writes"]) == 1
    written = fake_cfg["writes"][0]["cfg"]
    assert written["tools"]["exec"]["allowlist"] == ["ls", "cat", "git"]
    # Pre-existing exec keys preserved (security stays "full" — paired
    # with set_exec_security when the operator wants to downgrade too)
    assert written["tools"]["exec"]["security"] == "full"
    assert written["tools"]["exec"]["ask"] == "on-miss"


def test_set_exec_allowlist_with_empty_list_clears(tmp_path: Path, fake_cfg):
    """An explicit empty list clears the allowlist — distinct from None,
    which would leave it untouched. Operator passes [] deliberately."""
    # Seed an existing allowlist so the test can confirm it's overwritten.
    fake_cfg["cfg"]["tools"]["exec"]["allowlist"] = ["old"]
    out = handle_set_exec_allowlist(
        {"bot_id": "security_bot", "commands": []}, tmp_path,
    )
    assert out["previous_allowlist"] == ["old"]
    assert out["new_allowlist"] == []
    written = fake_cfg["writes"][0]["cfg"]
    assert written["tools"]["exec"]["allowlist"] == []


def test_set_exec_allowlist_missing_bot_id(tmp_path: Path):
    with pytest.raises(ValueError, match="bot_id"):
        handle_set_exec_allowlist({"commands": ["ls"]}, tmp_path)


def test_set_exec_allowlist_missing_commands(tmp_path: Path):
    with pytest.raises(ValueError, match="commands"):
        handle_set_exec_allowlist({"bot_id": "security_bot"}, tmp_path)


def test_set_exec_allowlist_commands_not_list(tmp_path: Path):
    with pytest.raises(ValueError, match="commands"):
        handle_set_exec_allowlist(
            {"bot_id": "security_bot", "commands": "ls,cat"}, tmp_path,
        )


def test_set_exec_allowlist_rejects_non_string_entries(tmp_path: Path, fake_cfg):
    """Reject early with a clear error rather than letting OpenClaw's
    validator complain about a typed schema mismatch."""
    with pytest.raises(ValueError, match=r"commands\[1\]"):
        handle_set_exec_allowlist(
            {"bot_id": "security_bot", "commands": ["ls", 42]}, tmp_path,
        )
    # No write attempted on validation failure
    assert fake_cfg["writes"] == []


# ── set_exec_security ───────────────────────────────────────────────────────


def test_set_exec_security_downgrade_to_allowlist(tmp_path: Path, fake_cfg):
    out = handle_set_exec_security(
        {"bot_id": "security_bot", "level": "allowlist"}, tmp_path,
    )
    assert out["previous"] == "full"
    assert out["new"] == "allowlist"
    written = fake_cfg["writes"][0]["cfg"]
    assert written["tools"]["exec"]["security"] == "allowlist"


def test_set_exec_security_idempotent_when_already_at_level(tmp_path: Path, fake_cfg):
    out = handle_set_exec_security(
        {"bot_id": "security_bot", "level": "full"}, tmp_path,
    )
    # cfg is already at "full" — handler returns without writing
    assert out["level"] == "full"
    assert "no write" in out["note"].lower()
    assert fake_cfg["writes"] == []


def test_set_exec_security_invalid_level(tmp_path: Path):
    with pytest.raises(ValueError, match="level"):
        handle_set_exec_security(
            {"bot_id": "security_bot", "level": "permissive"}, tmp_path,
        )


def test_set_exec_security_missing_bot_id(tmp_path: Path):
    with pytest.raises(ValueError, match="bot_id"):
        handle_set_exec_security({"level": "allowlist"}, tmp_path)


def test_set_exec_security_deny_locks_down(tmp_path: Path, fake_cfg):
    """The 'deny' level disables exec entirely. Useful when a bot
    doesn't actually need shell access."""
    out = handle_set_exec_security(
        {"bot_id": "security_bot", "level": "deny"}, tmp_path,
    )
    assert out["new"] == "deny"
    written = fake_cfg["writes"][0]["cfg"]
    assert written["tools"]["exec"]["security"] == "deny"

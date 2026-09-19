"""Tests for permissions.inventory — the three-source per-bot reader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from permissions import inventory as inv


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    """Synthetic ~/<bot>/.openclaw/ tree."""
    home = tmp_path / "bot"
    (home / ".openclaw" / "cron").mkdir(parents=True)
    return home


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


# ── Permission config ────────────────────────────────────────────────────────

def test_permission_config_reads_full_field_set(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {
        "tools": {
            "exec": {"security": "full", "ask": "on-miss"},
            "fs": {"workspaceOnly": True},
            "web": {"search": {"enabled": True}, "fetch": {"enabled": False}},
        },
        "commands": {"native": "auto", "ownerAllowFrom": ["telegram:123"]},
        # NB: no top-level "sandbox" — OC schema rejects it; valid path is
        # agents.defaults.sandbox.mode, which the inventory does not track yet.
    })

    pi = inv.read_inventory("bot", home_override=bot_home)

    assert pi.permission_config.read_error is None
    assert pi.permission_config.fields["tools.exec.security"] == "full"
    assert pi.permission_config.fields["tools.exec.ask"] == "on-miss"
    assert pi.permission_config.fields["tools.fs.workspaceOnly"] is True
    assert pi.permission_config.fields["tools.web.search.enabled"] is True
    assert pi.permission_config.fields["tools.web.fetch.enabled"] is False
    assert pi.permission_config.fields["commands.ownerAllowFrom"] == ["telegram:123"]
    # sandbox not in PERMISSION_CONFIG_FIELDS — see inventory.py for rationale.
    assert "sandbox.enabled" not in pi.permission_config.fields
    assert pi.permission_config.field_signature  # non-empty stable hash


def test_permission_config_missing_file_returns_error(bot_home: Path):
    pi = inv.read_inventory("bot", home_override=bot_home)
    assert pi.permission_config.read_error == "not_found"
    assert all(v is None for v in pi.permission_config.fields.values()) or not pi.permission_config.fields


def test_permission_config_malformed_json_returns_error(bot_home: Path):
    (bot_home / ".openclaw" / "openclaw.json").write_text("{not valid json")
    pi = inv.read_inventory("bot", home_override=bot_home)
    assert pi.permission_config.read_error
    assert pi.permission_config.read_error.startswith("json_decode")


def test_field_signature_stable_across_key_order(bot_home: Path, tmp_path: Path):
    cfg_a = {"tools": {"exec": {"security": "full", "ask": "on-miss"}}}
    cfg_b = {"tools": {"exec": {"ask": "on-miss", "security": "full"}}}
    _write(bot_home / ".openclaw" / "openclaw.json", cfg_a)
    sig_a = inv.read_inventory("bot", home_override=bot_home).permission_config.field_signature

    home_b = tmp_path / "bot_b"
    (home_b / ".openclaw").mkdir(parents=True)
    _write(home_b / ".openclaw" / "openclaw.json", cfg_b)
    sig_b = inv.read_inventory("bot", home_override=home_b).permission_config.field_signature

    assert sig_a == sig_b


# ── Exec approvals ───────────────────────────────────────────────────────────

def test_exec_approvals_absent_is_clean(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    pi = inv.read_inventory("bot", home_override=bot_home)
    assert pi.exec_approvals.present is False
    assert pi.exec_approvals.defaults_count == 0
    assert pi.exec_approvals.agents == []


def test_exec_approvals_dict_shape(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    _write(bot_home / ".openclaw" / "exec-approvals.json", {
        "defaults": {"git status": {}, "git log": {}},
        "agents": {
            "main": {
                "approvals": {
                    "python3 scripts/tasks.py list": {},
                    "gh pr view 123": {},
                    "ls /Users/pod_admin_user/foo": {},
                }
            }
        }
    })

    pi = inv.read_inventory("bot", home_override=bot_home)

    assert pi.exec_approvals.present
    assert pi.exec_approvals.defaults_count == 2
    assert len(pi.exec_approvals.agents) == 1
    main = pi.exec_approvals.agents[0]
    assert main.agent_id == "main"
    assert main.count == 3
    # Canonical: subcommands kept; paths and numeric args masked
    assert "python3 <path> list" in main.patterns
    assert "gh pr view <arg>" in main.patterns
    assert "ls <path>" in main.patterns


def test_canonicalize_pattern_handles_flags_and_paths():
    assert inv._canonicalize_pattern("git status") == "git status"
    assert inv._canonicalize_pattern("rm -rf /tmp/x") == "rm -rf <path>"
    assert inv._canonicalize_pattern("curl https://example.com/api") == "curl <url>"
    assert inv._canonicalize_pattern("gh pr view 123") == "gh pr view <arg>"
    assert inv._canonicalize_pattern("") == ""


def test_exec_approvals_list_shape(bot_home: Path):
    """Some implementations might use a list rather than a dict."""
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    _write(bot_home / ".openclaw" / "exec-approvals.json", {
        "defaults": [],
        "agents": {"main": {"approvals": ["git status", "ls"]}}
    })

    pi = inv.read_inventory("bot", home_override=bot_home)

    assert pi.exec_approvals.defaults_count == 0
    main = pi.exec_approvals.agents[0]
    assert main.count == 2


# ── Cron jobs ────────────────────────────────────────────────────────────────

def test_cron_jobs_capped_agent_turn(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    _write(bot_home / ".openclaw" / "cron" / "jobs.json", {
        "jobs": [
            {
                "id": "j1",
                "name": "daily-review",
                "enabled": True,
                "schedule": {"kind": "every"},
                "payload": {"kind": "agentTurn", "message": "review", "maxTurns": 20, "maxBudgetUsd": 5.0},
            }
        ]
    })

    pi = inv.read_inventory("bot", home_override=bot_home)

    assert pi.scheduled_invocations.present
    assert len(pi.scheduled_invocations.jobs) == 1
    j = pi.scheduled_invocations.jobs[0]
    assert j.payload_kind == "agentTurn"
    assert j.has_turn_cap and j.has_budget_cap
    assert "review" in j.payload_summary


def test_cron_jobs_uncapped_agent_turn(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    _write(bot_home / ".openclaw" / "cron" / "jobs.json", {
        "jobs": [
            {
                "id": "j1",
                "name": "admin_bot-check",
                "enabled": True,
                "schedule": {"kind": "every"},
                "payload": {"kind": "agentTurn", "message": "scan"},
            }
        ]
    })

    pi = inv.read_inventory("bot", home_override=bot_home)
    j = pi.scheduled_invocations.jobs[0]
    assert not j.has_turn_cap
    assert not j.has_budget_cap


def test_cron_jobs_system_event_no_caps_needed(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    _write(bot_home / ".openclaw" / "cron" / "jobs.json", {
        "jobs": [
            {
                "id": "j1",
                "name": "macos-update",
                "schedule": {"kind": "cron"},
                "payload": {"kind": "systemEvent", "command": "softwareupdate -i -a"},
            }
        ]
    })

    pi = inv.read_inventory("bot", home_override=bot_home)
    j = pi.scheduled_invocations.jobs[0]
    assert j.payload_kind == "systemEvent"
    assert j.payload_summary.startswith("event:")


def test_cron_jobs_absent(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {})
    pi = inv.read_inventory("bot", home_override=bot_home)
    assert pi.scheduled_invocations.present is False
    assert pi.scheduled_invocations.jobs == []


# ── Composite ────────────────────────────────────────────────────────────────

def test_read_inventory_to_dict_roundtrip(bot_home: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {"tools": {"exec": {"security": "full"}}})
    pi = inv.read_inventory("bot", home_override=bot_home)
    d = pi.to_dict()
    assert d["bot_id"] == "bot"
    assert "permission_config" in d
    assert "exec_approvals" in d
    assert "scheduled_invocations" in d
    # round-trip through JSON
    assert json.loads(json.dumps(d))


def test_cache_write_load_roundtrip(bot_home: Path, tmp_path: Path):
    _write(bot_home / ".openclaw" / "openclaw.json", {"tools": {"exec": {"security": "allowlist"}}})
    pi = inv.read_inventory("bot", home_override=bot_home)

    shared = tmp_path / "shared"
    inv.write_inventory(pi, shared)

    loaded = inv.load_inventory(shared, "bot")
    assert loaded is not None
    assert loaded["bot_id"] == "bot"
    assert loaded["permission_config"]["fields"]["tools.exec.security"] == "allowlist"


def test_load_inventory_missing_returns_none(tmp_path: Path):
    assert inv.load_inventory(tmp_path, "nobody") is None

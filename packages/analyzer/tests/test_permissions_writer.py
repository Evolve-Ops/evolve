"""Tests for permissions.writer — openclaw.json field mutator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from permissions import writer


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    return home


def _write_oc(home: Path, obj: dict) -> Path:
    p = home / ".openclaw" / "openclaw.json"
    p.write_text(json.dumps(obj))
    return p


def test_set_single_field(bot_home: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full"}}})
    ok, _ = writer.write_openclaw_fields(
        "bot", {"tools.exec.ask": "always"}, home_override=bot_home,
    )
    assert ok
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["ask"] == "always"
    # Existing field preserved
    assert saved["tools"]["exec"]["security"] == "full"


def test_set_creates_intermediate_dicts(bot_home: Path):
    # Uses a real OC schema path (agents.defaults.sandbox.mode) so the test
    # documents a valid mutation, not just dotpath mechanics.
    _write_oc(bot_home, {})
    ok, _ = writer.write_openclaw_fields(
        "bot", {"agents.defaults.sandbox.mode": "off"}, home_override=bot_home,
    )
    assert ok
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["agents"]["defaults"]["sandbox"]["mode"] == "off"


def test_set_multiple_fields(bot_home: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full"}}})
    ok, _ = writer.write_openclaw_fields(
        "bot", {
            "tools.exec.security": "allowlist",
            "agents.defaults.sandbox.mode": "non-main",
            "tools.fs.workspaceOnly": True,
        }, home_override=bot_home,
    )
    assert ok
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["security"] == "allowlist"
    assert saved["agents"]["defaults"]["sandbox"]["mode"] == "non-main"
    assert saved["tools"]["fs"]["workspaceOnly"] is True


def test_unset_removes_leaf(bot_home: Path):
    _write_oc(bot_home, {
        "tools": {"exec": {"security": "full", "ask": "on-miss"}},
    })
    ok, _ = writer.write_openclaw_fields(
        "bot", {}, field_unsets=["tools.exec.ask"], home_override=bot_home,
    )
    assert ok
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert "ask" not in saved["tools"]["exec"]
    assert saved["tools"]["exec"]["security"] == "full"


def test_unset_missing_is_noop(bot_home: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full"}}})
    ok, _ = writer.write_openclaw_fields(
        "bot", {}, field_unsets=["a.b.c"], home_override=bot_home,
    )
    assert ok


def test_missing_openclaw_returns_error(bot_home: Path):
    ok, msg = writer.write_openclaw_fields(
        "bot", {"tools.exec.security": "deny"}, home_override=bot_home,
    )
    assert not ok
    assert "unreadable" in msg


def test_malformed_openclaw_returns_error(bot_home: Path):
    (bot_home / ".openclaw" / "openclaw.json").write_text("{not json")
    ok, msg = writer.write_openclaw_fields(
        "bot", {"tools.exec.security": "deny"}, home_override=bot_home,
    )
    assert not ok
    assert "unreadable" in msg


def test_read_openclaw_json_parses(bot_home: Path):
    _write_oc(bot_home, {"a": 1})
    obj = writer.read_openclaw_json(bot_home / ".openclaw" / "openclaw.json")
    assert obj == {"a": 1}


def test_read_openclaw_json_missing_returns_none(tmp_path: Path):
    assert writer.read_openclaw_json(tmp_path / "nope.json") is None


def test_read_openclaw_json_malformed_returns_none(bot_home: Path):
    (bot_home / ".openclaw" / "openclaw.json").write_text("not json")
    assert writer.read_openclaw_json(bot_home / ".openclaw" / "openclaw.json") is None

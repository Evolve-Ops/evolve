"""Tests for permissions.baseline + UpdatePermissionBaseline applier."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbiter.appliers import permissions as _perms_app  # noqa: F401
from arbiter.appliers.base import get_applier
from permissions import baseline as _bl
from schema.proposal import UpdatePermissionBaseline


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _applier(shared: Path):
    a = get_applier("UpdatePermissionBaseline")
    a.shared_override = shared  # type: ignore[attr-defined]
    return a


# ── baseline module ────────────────────────────────────────────────────────

def test_load_returns_default_when_missing(shared_dir: Path):
    b = _bl.load(shared_dir)
    assert b["version"] == 1
    assert "pod_default" in b


def test_write_then_load_roundtrip(shared_dir: Path):
    b = _bl.load(shared_dir)
    b["per_bot_overrides"]["team_bot_c"] = {"permission_config": {"tools.fs.workspaceOnly": True}}
    _bl.write(b, shared_dir)
    loaded = _bl.load(shared_dir)
    assert loaded["per_bot_overrides"]["team_bot_c"]["permission_config"]["tools.fs.workspaceOnly"] is True


def test_write_default_if_missing_only_seeds_once(shared_dir: Path):
    assert _bl.write_default_if_missing(shared_dir) is True
    assert _bl.write_default_if_missing(shared_dir) is False


def test_resolve_merges_overrides(shared_dir: Path):
    b = _bl.load(shared_dir)
    b["per_bot_overrides"]["team_bot_c"] = {
        "permission_config": {"tools.fs.workspaceOnly": True}
    }
    merged = _bl.resolve(b, "team_bot_c")
    # From override
    assert merged["tools.fs.workspaceOnly"] is True
    # From pod_default (modal posture for member bots — full + ask on-miss
    # since the 2026-05-25 pivot, internal/spec-app-derived-permissions-2026-05-24.md).
    assert merged["tools.exec.security"] == "full"


def test_denylist_for_returns_lists(shared_dir: Path):
    b = _bl.load(shared_dir)
    approvals = _bl.denylist_for(b, "approvals")
    cron = _bl.denylist_for(b, "cron")
    assert isinstance(approvals, list) and len(approvals) > 0
    assert isinstance(cron, list) and len(cron) > 0
    assert _bl.denylist_for(b, "bogus") == []


# ── UpdatePermissionBaseline applier ────────────────────────────────────────

def test_set_pod_default_updates_field(shared_dir: Path):
    a = _applier(shared_dir)
    action = UpdatePermissionBaseline(
        operation="set_pod_default",
        fields={"tools.exec.security": "allowlist"},
    )

    result = a.apply(action, "<pod>")

    assert result.ok, result.message
    b = _bl.load(shared_dir)
    assert b["pod_default"]["permission_config"]["tools.exec.security"] == "allowlist"
    # Other fields preserved (ask remains "on-miss" — the new member-bot
    # default modal value, kept through the applier so a partial update
    # doesn't blow away unrelated baseline fields).
    assert b["pod_default"]["permission_config"]["tools.exec.ask"] == "on-miss"


def test_set_bot_override_adds_then_removes(shared_dir: Path):
    a = _applier(shared_dir)
    add = UpdatePermissionBaseline(
        operation="set_bot_override", bot_id="team_bot_c",
        fields={"tools.fs.workspaceOnly": True},
    )
    result = a.apply(add, "team_bot_c")
    assert result.ok
    assert _bl.load(shared_dir)["per_bot_overrides"]["team_bot_c"][
        "permission_config"]["tools.fs.workspaceOnly"] is True

    # Empty fields → remove override
    rm = UpdatePermissionBaseline(
        operation="set_bot_override", bot_id="team_bot_c", fields={},
    )
    result = a.apply(rm, "team_bot_c")
    assert result.ok
    assert "team_bot_c" not in _bl.load(shared_dir)["per_bot_overrides"]


def test_set_bot_override_requires_bot_id(shared_dir: Path):
    a = _applier(shared_dir)
    action = UpdatePermissionBaseline(
        operation="set_bot_override", bot_id="",
        fields={"tools.fs.workspaceOnly": True},
    )
    result = a.apply(action, "bot")
    assert not result.ok


def test_set_denylist_patterns_accepts_additions(shared_dir: Path):
    a = _applier(shared_dir)
    current = _bl.denylist_for(_bl.load(shared_dir), "approvals")
    new_list = current + [r"^evil-command$"]
    action = UpdatePermissionBaseline(
        operation="set_denylist_patterns",
        fields={"surface": "approvals", "patterns": new_list},
    )
    result = a.apply(action, "<pod>")
    assert result.ok
    assert r"^evil-command$" in _bl.denylist_for(_bl.load(shared_dir), "approvals")


def test_set_denylist_patterns_refuses_removals(shared_dir: Path):
    """Spec §5.4: v1 only allows additions to the denylist."""
    a = _applier(shared_dir)
    current = _bl.denylist_for(_bl.load(shared_dir), "approvals")
    shortened = current[:-1]  # drop the last rule
    action = UpdatePermissionBaseline(
        operation="set_denylist_patterns",
        fields={"surface": "approvals", "patterns": shortened},
    )
    result = a.apply(action, "<pod>")
    assert not result.ok
    assert "denylist" in result.message.lower()


def test_revert_restores_prior_baseline(shared_dir: Path):
    a = _applier(shared_dir)
    initial = _bl.load(shared_dir)
    action = UpdatePermissionBaseline(
        operation="set_pod_default",
        fields={"tools.exec.security": "deny"},
    )
    snap = a.capture_snapshot(action, "<pod>")
    a.apply(action, "<pod>")
    a.revert(snap, "<pod>")

    restored = _bl.load(shared_dir)
    # The default seed runs at baseline_path() check; compare a key field
    assert restored["pod_default"]["permission_config"]["tools.exec.security"] == \
        initial["pod_default"]["permission_config"]["tools.exec.security"]

"""Regression tests for the post-Phase-E.2.b backup-status fallback lie.

Bug shape (observed in production 2026-05-26): after the evo account
cutover, ``pod_state.backup_status`` reported
``workspace_exists: false`` for every member bot when the admin daemon
socket was unreachable. The in-process fallback called
``Path("/Users/<bot>/.openclaw/workspace").exists()`` as the
unprivileged ``evo`` user, which returns False on PermissionError —
indistinguishable from "genuinely missing". Evo confidently reported
"the workspaces don't exist" for healthy bots whose workspaces had
recent backup commits.

Fix: the fallback now uses ``_stat_status`` to distinguish "exists" /
"absent" / "indeterminate", and returns ``{ok: false, fallback_blocked:
true, error: ...}`` when the workspace can't be statted.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.tools.pod_state_backup import (
    _backup_status_handler,
    _stat_status,
)


# ── _stat_status helper ──────────────────────────────────────────────────────


def test_stat_status_exists(tmp_path):
    target = tmp_path / "real_dir"
    target.mkdir()
    assert _stat_status(target) == "exists"


def test_stat_status_absent(tmp_path):
    assert _stat_status(tmp_path / "missing") == "absent"


def test_stat_status_inaccessible_on_permission_error(tmp_path):
    """PermissionError on stat → 'indeterminate' (the core bug fix)."""
    target = tmp_path / "denied"

    def fake_stat(*a, **kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    with patch.object(Path, "stat", fake_stat):
        assert _stat_status(target) == "indeterminate"


def test_stat_status_inaccessible_on_generic_oserror(tmp_path):
    """Any other OSError (e.g. EIO) → 'indeterminate'. Conservative — we
    can't tell from outside whether the path exists, so don't claim it."""
    target = tmp_path / "io_err"

    def fake_stat(*a, **kw):
        raise OSError(errno.EIO, "I/O error")

    with patch.object(Path, "stat", fake_stat):
        assert _stat_status(target) == "indeterminate"


# ── Fallback path returns ok:false instead of lying ──────────────────────────


def test_fallback_returns_ok_false_when_workspace_inaccessible(tmp_path, monkeypatch):
    """The bug repro: daemon unreachable + can't stat /Users/<bot>/.openclaw/workspace.

    Pre-fix: silently returns ``workspace_exists: false``.
    Post-fix: returns ``ok: false, fallback_blocked: true``.
    """
    # Force the daemon path to fail with "unavailable"
    from evolve_admin.evo import admin_client

    def raise_unavailable(*a, **kw):
        raise admin_client.AdminDaemonUnavailable("simulated socket dead")

    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup.get_json",
        raise_unavailable,
        raising=False,
    )
    # Easier: import path the handler uses
    monkeypatch.setattr(admin_client, "get_json", raise_unavailable)

    # And the workspace stat raises PermissionError (the post-cutover
    # cross-bot case).
    real_stat = Path.stat

    def fake_stat(self, *a, **kw):
        s = str(self)
        if "/.openclaw/workspace" in s:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(self, *a, **kw)

    with patch.object(Path, "stat", fake_stat):
        result = _backup_status_handler(
            tmp_path / "network.json", bot_id="team_bot_c",
        )

    # The crucial assertion: the response is shaped as an error, NOT as
    # data that says workspace_exists=False.
    assert result.get("ok") is False, (
        f"expected ok:false; got {result}"
    )
    assert result.get("fallback_blocked") is True
    assert "workspace_exists" not in result, (
        "post-fix response must NOT carry a workspace_exists field "
        "(would be confused for data); error shape only"
    )
    assert "unknown" in result.get("error", "").lower()
    # Recovery hint should point at the kickstart command
    assert "kickstart" in result.get("error", "")


def test_fallback_returns_normal_payload_when_workspace_exists(tmp_path, monkeypatch):
    """When the workspace IS statable (e.g. running as evolve, pre-cutover,
    or as evo for evo's own workspace), the existing happy-path data is
    returned. We're not breaking the legitimate case.
    """
    from evolve_admin.evo import admin_client

    def raise_unavailable(*a, **kw):
        raise admin_client.AdminDaemonUnavailable("daemon down")

    monkeypatch.setattr(admin_client, "get_json", raise_unavailable)

    # Build a fake bot home with a real workspace dir
    bot_home = tmp_path / "Users" / "evo"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)

    # Point _workspace_path + _backup_plist_path at our fake paths
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._workspace_path",
        lambda bid: workspace,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._backup_plist_path",
        lambda bid: tmp_path / "no-plist.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._configured_remote",
        lambda np, bid: None,
    )

    result = _backup_status_handler(
        tmp_path / "network.json", bot_id="evo",
    )
    assert result.get("ok") is not False
    assert result.get("workspace_exists") is True
    assert result.get("bot_id") == "evo"


def test_fallback_returns_normal_payload_when_workspace_genuinely_absent(
    tmp_path, monkeypatch,
):
    """When the workspace genuinely doesn't exist (not a permission issue),
    the response still says workspace_exists=false — that's CORRECT data,
    not a lie. The new fallback_blocked path only fires for stat failures
    that aren't FileNotFoundError.
    """
    from evolve_admin.evo import admin_client

    def raise_unavailable(*a, **kw):
        raise admin_client.AdminDaemonUnavailable("daemon down")

    monkeypatch.setattr(admin_client, "get_json", raise_unavailable)

    bot_home = tmp_path / "Users" / "newbie"
    bot_home.mkdir(parents=True)
    # workspace deliberately NOT created
    workspace = bot_home / ".openclaw" / "workspace"

    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._workspace_path",
        lambda bid: workspace,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._backup_plist_path",
        lambda bid: tmp_path / "no-plist.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._configured_remote",
        lambda np, bid: None,
    )

    result = _backup_status_handler(
        tmp_path / "network.json", bot_id="newbie",
    )
    # Genuinely absent → workspace_exists:false is correct data, NOT
    # an error response.
    assert result.get("ok") is not False
    assert result.get("workspace_exists") is False
    assert result.get("fallback_blocked") is None or "fallback_blocked" not in result

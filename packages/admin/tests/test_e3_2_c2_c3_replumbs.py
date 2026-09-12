"""Tests for E.3.2 (C2 backup-status) + E.3.3 (C3 pod_state.bots).

Spec: internal/spec-evo-account-separation-2026-05-25.md Phase E.3.

Both re-plumbs follow the same pattern as E.3.1's config.bot:
  - Primary path: admin daemon HTTP endpoint over unix socket
  - Fallback path: legacy in-process / direct-fs read

Tests use mocking against admin_client.get_json + the in-process
helpers so we exercise both paths without standing up a real socket.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.admin_client import AdminDaemonUnavailable
from evolve_admin.evo.tools.pod_state_backup import _backup_status_handler
from evolve_admin.evo.tools.pod_state_bots import (
    _fetch_pod_state_via_daemon,
    _handler as pod_state_bots_handler,
    _shape_response_from_status_data,
)


# ─────────────────────────────────────────────────────────────────────────────
# C2 — pod_state.backup_status
# ─────────────────────────────────────────────────────────────────────────────


def test_backup_status_uses_daemon_when_200(monkeypatch):
    """Daemon returns 200 → use the payload; don't run direct-fs fallback."""
    expected = {
        "bot_id": "team_bot_a",
        "backup_configured": True,
        "configured_remote": "git@github.com:org/team_bot_a-backup.git",
        "schedule": {"hour": 2, "minute": 0, "human": "daily at 02:00"},
        "workspace_path": "/Users/team_bot_a/.openclaw/workspace",
        "workspace_exists": True,
        "git_remote": "git@github.com:org/team_bot_a-backup.git",
        "last_commit_at": "2026-05-26T02:00:01Z",
        "last_commit_sha": "abc123def456",
        "last_commit_subject": "[backup] daily snapshot",
        "never_run": False,
        "remote_drift": False,
    }

    def fail_fallback(*args, **kw):
        pytest.fail("fallback should NOT run when daemon returns 200")

    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._backup_plist_path",
        fail_fallback,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, expected),
    )

    result = _backup_status_handler(Path("/tmp/unused"), "team_bot_a")
    assert result == expected


def test_backup_status_falls_back_when_daemon_unavailable(monkeypatch, tmp_path):
    """Daemon unreachable → fall back to direct-fs path."""
    def raising_get_json(path, **kw):
        raise AdminDaemonUnavailable("socket not bound (test)")

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json", raising_get_json,
    )
    # Stub out the path helpers to simulate "bot doesn't have a backup plist"
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._backup_plist_path",
        lambda bot_id: tmp_path / f"ai.evolve.{bot_id}.backup.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._workspace_path",
        lambda bot_id: tmp_path / f"{bot_id}_workspace",
    )

    result = _backup_status_handler(Path("/tmp/unused"), "team_bot_a")
    # Fallback path completed and produced the canonical shape
    assert result["bot_id"] == "team_bot_a"
    assert result["backup_configured"] is False
    assert result["workspace_exists"] is False


def test_backup_status_falls_back_on_403(monkeypatch, tmp_path):
    """Daemon returns 403 (no body) → fall back to direct-fs."""
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (403, None),
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._backup_plist_path",
        lambda bot_id: tmp_path / f"{bot_id}.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.pod_state_backup._workspace_path",
        lambda bot_id: tmp_path / "fallback_workspace",
    )

    result = _backup_status_handler(Path("/tmp/unused"), "team_bot_a")
    # Fallback ran successfully (shape filled in by the in-process code)
    assert result["bot_id"] == "team_bot_a"
    assert "backup_configured" in result


def test_backup_status_empty_bot_id_rejected():
    result = _backup_status_handler(Path("/tmp/unused"), "")
    assert "error" in result


# ─────────────────────────────────────────────────────────────────────────────
# C3 — pod_state.bots
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_state_bots_uses_daemon_when_200(monkeypatch):
    """Daemon /api/status returns 200 → use it; don't fall back to
    in-process network_status."""
    daemon_status = {
        "bots": {
            "team_bot_a": {
                "role": "member",
                "port": 18789,
                "live": True,
                "last_metric_date": "2026-05-26",
                "evolve_version": "2026.5.26",
                "tile": {
                    "health_chips": [
                        {"id": "scan_needed", "severity": "info",
                         "label": "Scan needed", "detail": "no recent scan",
                         "nav": "/apps"},  # nav should be stripped
                    ],
                },
            },
        },
        "primary": "team_bot_a",
        "network_id": "test-net",
    }

    def fail_fallback(*args, **kw):
        pytest.fail("network_status fallback should NOT run when daemon returns 200")

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, daemon_status),
    )
    monkeypatch.setattr(
        "evolve_admin.status.network_status", fail_fallback,
    )

    result = pod_state_bots_handler(Path("/tmp/unused"))
    assert result["count"] == 1
    assert result["primary"] == "team_bot_a"
    assert result["network_id"] == "test-net"
    bot = result["bots"][0]
    assert bot["bot_id"] == "team_bot_a"
    assert bot["status"] == "online"
    # tile_chips present, nav stripped
    chips = bot["tile_chips"]
    assert len(chips) == 1
    assert "nav" not in chips[0]
    assert chips[0]["id"] == "scan_needed"


def test_pod_state_bots_filters_by_bot_id_from_daemon(monkeypatch):
    daemon_status = {
        "bots": {
            "team_bot_a": {"role": "member", "port": 18789, "live": True,
                    "last_metric_date": "2026-05-26"},
            "admin_bot": {"role": "member", "port": 18790, "live": True,
                      "last_metric_date": "2026-05-26"},
        },
        "primary": "evolve",
    }
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, daemon_status),
    )

    result = pod_state_bots_handler(Path("/tmp/unused"), bot_id="admin_bot")
    assert result["count"] == 1
    assert result["bots"][0]["bot_id"] == "admin_bot"


def test_pod_state_bots_bot_not_found_in_daemon(monkeypatch):
    daemon_status = {"bots": {"team_bot_a": {"role": "member"}}}
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, daemon_status),
    )

    result = pod_state_bots_handler(Path("/tmp/unused"), bot_id="ghost")
    assert result["count"] == 0
    assert "not found" in result["error"]


def test_pod_state_bots_falls_back_when_daemon_unavailable(monkeypatch):
    """Daemon socket unreachable → use the legacy in-process path."""
    fallback_called = {"count": 0}

    def raising_get_json(path, **kw):
        raise AdminDaemonUnavailable("socket not bound")

    def stub_network_status(_path):
        fallback_called["count"] += 1
        return {
            "bots": {"team_bot_a": {"role": "member", "port": 18789, "live": True,
                             "last_metric_date": "2026-05-26"}},
            "primary": "team_bot_a",
            "network_id": "fallback-net",
        }

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json", raising_get_json,
    )
    # network_status lives in evolve_admin.status; the tool imports it lazily.
    import evolve_admin.status
    monkeypatch.setattr(
        evolve_admin.status, "network_status", stub_network_status,
    )

    result = pod_state_bots_handler(Path("/tmp/unused"))
    assert fallback_called["count"] == 1
    # Either count is what we'd expect from the fallback (1 bot)
    assert result["count"] == 1
    assert result["bots"][0]["bot_id"] == "team_bot_a"


def test_pod_state_bots_falls_back_on_500(monkeypatch):
    """Daemon returns 500 → fall back."""
    fallback_called = {"count": 0}

    def stub_network_status(_path):
        fallback_called["count"] += 1
        return {"bots": {}, "primary": None}

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (500, None),
    )
    import evolve_admin.status
    monkeypatch.setattr(
        evolve_admin.status, "network_status", stub_network_status,
    )

    pod_state_bots_handler(Path("/tmp/unused"))
    assert fallback_called["count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# _shape_response_from_status_data — pure-function tests
# ─────────────────────────────────────────────────────────────────────────────


def test_shape_response_includes_primary_and_network_id():
    data = {
        "bots": {
            "team_bot_a": {"role": "member", "port": 18789, "live": True,
                    "last_metric_date": "2026-05-26"},
        },
        "primary": "team_bot_a",
        "network_id": "test",
    }
    result = _shape_response_from_status_data(data, bot_id=None)
    assert result["count"] == 1
    assert result["primary"] == "team_bot_a"
    assert result["network_id"] == "test"


def test_shape_response_omits_tile_chips_when_no_tile_key():
    """No daemon-side tile compute → no tile_chips field."""
    data = {
        "bots": {
            "team_bot_a": {"role": "member", "port": 18789, "live": True,
                    "last_metric_date": "2026-05-26"},  # no "tile" key
        },
    }
    result = _shape_response_from_status_data(data, bot_id=None)
    bot = result["bots"][0]
    # tile_chips absent (project_bot's "set field only when not None" rule)
    assert "tile_chips" not in bot


def test_shape_response_status_offline_when_gateway_unreachable():
    """gateway_status_fresh + gateway_reachable=False → status='offline'."""
    data = {
        "bots": {
            "team_bot_a": {
                "role": "member", "port": 18789, "live": True,
                "last_metric_date": "2026-05-26",
                "gateway_status_fresh": True,
                "gateway_reachable": False,
            },
        },
    }
    result = _shape_response_from_status_data(data, bot_id="team_bot_a")
    assert result["bots"][0]["status"] == "offline"


# ─────────────────────────────────────────────────────────────────────────────
# _fetch_pod_state_via_daemon — helper unit test
# ─────────────────────────────────────────────────────────────────────────────


def test_fetch_pod_state_returns_none_on_socket_failure(monkeypatch):
    def raising_get_json(path, **kw):
        raise AdminDaemonUnavailable("socket gone")

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json", raising_get_json,
    )
    result = _fetch_pod_state_via_daemon()
    assert result is None


def test_fetch_pod_state_returns_none_on_non_200(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (500, None),
    )
    result = _fetch_pod_state_via_daemon()
    assert result is None


def test_fetch_pod_state_returns_dict_on_success(monkeypatch):
    expected = {"bots": {"x": {}}, "primary": "x"}
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, expected),
    )
    result = _fetch_pod_state_via_daemon()
    assert result == expected

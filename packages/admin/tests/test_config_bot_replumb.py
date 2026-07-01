"""Tests for the config.bot tool's re-plumbing onto the admin daemon's unix socket.

Spec: docs/spec-evo-account-separation-2026-05-25.md Phase E.3.1.

The tool tries the admin daemon's HTTP path first; on AdminDaemonUnavailable
(socket not bound, daemon not restarted, etc.) it falls back to the legacy
direct-fs path. Both paths return the same shape.

These tests:
  * Verify the admin daemon path is preferred when it works
  * Verify the fallback runs cleanly when the daemon is unavailable
  * Verify the fallback doesn't run when the daemon returns a non-200
    result that's not a connection error (e.g. 404 — bot not found)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.admin_client import AdminDaemonUnavailable
from evolve_admin.evo.tools.config_bot import _handler
from evolve_admin.evo.tools.fs_tristate import ReadResult, ReadState


# ── Admin daemon path: 200 response ──────────────────────────────────────────


def test_handler_uses_admin_daemon_when_200(monkeypatch):
    """When the daemon returns 200, the handler should return that payload
    verbatim and NOT fall back to the direct-fs path."""
    expected = {
        "bot_id": "team_bot_a",
        "available": True,
        "config": {"gateway": {"port": 18789}},
    }
    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        lambda *args, **kw: pytest.fail(
            "fallback should NOT run when admin daemon returns 200"
        ),
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, expected),
    )

    result = _handler(Path("/tmp/unused"), "team_bot_a")
    assert result == expected


def test_handler_uses_admin_daemon_when_404(monkeypatch):
    """404 is a structured response too — bot not found / no read access.
    The handler returns the 404 body without falling back to direct-fs,
    because the daemon has the authoritative answer."""
    not_found = {
        "bot_id": "ghost",
        "available": False,
        "error": "could not read openclaw.json (bot not found or no read access)",
    }
    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        lambda *args, **kw: pytest.fail(
            "fallback should NOT run for a structured 404 from the daemon"
        ),
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (404, not_found),
    )

    result = _handler(Path("/tmp/unused"), "ghost")
    assert result == not_found


# ── Fallback to direct-fs when daemon unavailable ────────────────────────────


def test_handler_falls_back_when_daemon_unavailable(monkeypatch):
    """AdminDaemonUnavailable → run the legacy direct-fs read."""
    fallback_called = {"count": 0}

    def fake_read_oc_json(bot_id, network_path):
        fallback_called["count"] += 1
        return ReadResult(ReadState.EXISTS, "{}"), {"tools": {"exec": {"security": "full"}}}

    def fake_get_json(path, **kw):
        raise AdminDaemonUnavailable("socket not bound (test)")

    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        fake_read_oc_json,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        fake_get_json,
    )

    result = _handler(Path("/tmp/unused"), "team_bot_a")
    assert fallback_called["count"] == 1
    assert result["bot_id"] == "team_bot_a"
    assert result["available"] is True
    assert result["config"]["tools"]["exec_security"] == "full"


def test_handler_falls_back_when_daemon_returns_403(monkeypatch):
    """403 is a daemon-says-no answer — but the structured body is None
    so the handler can't trust it as a final answer. Falls back to
    direct-fs to maintain the migration-window invariant: 'pre-cutover,
    direct-fs reads always work as a safety net'."""
    fallback_called = {"count": 0}

    def fake_read_oc_json(bot_id, network_path):
        fallback_called["count"] += 1
        return ReadResult(ReadState.EXISTS, "{}"), {"tools": {"exec": {"security": "deny"}}}

    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        fake_read_oc_json,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (403, None),  # daemon rejected; body is non-JSON
    )

    result = _handler(Path("/tmp/unused"), "team_bot_a")
    assert fallback_called["count"] == 1
    assert result["available"] is True


def test_handler_falls_back_when_get_json_raises_unexpected(monkeypatch):
    """Any unexpected exception in the admin path → fall back to fs.
    Defensive: never let an admin-client bug break the tool."""
    fallback_called = {"count": 0}

    def fake_read_oc_json(bot_id, network_path):
        fallback_called["count"] += 1
        return ReadResult(ReadState.EXISTS, "{}"), {"gateway": {"port": 18789}}

    def fake_get_json(path, **kw):
        raise ValueError("simulated unexpected error")

    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        fake_read_oc_json,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        fake_get_json,
    )

    result = _handler(Path("/tmp/unused"), "team_bot_a")
    assert fallback_called["count"] == 1


# ── Fallback handles missing-bot scenario ────────────────────────────────────


def test_handler_returns_unavailable_when_both_paths_fail(monkeypatch):
    """If the daemon is unreachable AND the direct-fs read can't determine
    the config (permission wall), the handler returns available=False with
    an indeterminate error — NOT a false 'not found'."""
    monkeypatch.setattr(
        "evolve_admin.evo.tools.config_bot._read_openclaw_json",
        lambda *args, **kw: (ReadResult(ReadState.INDETERMINATE, None, "denied"), None),
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (_ for _ in ()).throw(
            AdminDaemonUnavailable("unavailable")
        ),
    )

    result = _handler(Path("/tmp/unused"), "ghost")
    assert result["bot_id"] == "ghost"
    assert result["available"] is False
    assert result["read_state"] == ReadState.INDETERMINATE.value
    assert "could not determine" in result["error"].lower()


# ── Empty bot_id ─────────────────────────────────────────────────────────────


def test_handler_rejects_empty_bot_id():
    result = _handler(Path("/tmp/unused"), "")
    assert "error" in result
    assert "bot_id" in result["error"].lower()

"""Tests for E.3.5 — breakers + redeploy + remove re-plumbs.

Spec: internal/spec-evo-account-separation-2026-05-25.md Phase E.3.5.

Six tools re-plumbed onto the admin daemon socket:
  * action.bot.trip_breaker         → POST /api/admin/breakers/trip (new)
  * action.bot.reset_breaker        → POST /api/admin/breakers/reset (new)
  * action.pod.trip_breaker         → POST /api/admin/breakers/trip (scope=pod)
  * action.pod.reset_breaker        → POST /api/admin/breakers/reset (scope=pod)
  * action.bot.redeploy             → POST /api/deploy
  * action.bot.remove               → POST /api/lifecycle/retire
                                       (was /api/remove pre-2026-05-31;
                                       see retire.delete_bot docstring
                                       for the menu-rewrite story)

All follow the same daemon-first/fallback pattern as E.3.4.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


# ── action.bot.trip_breaker ──────────────────────────────────────────────────


def test_trip_bot_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_breakers import _trip_bot_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_breakers._bot_in_network",
        lambda np, bid: True,
    )
    captured = {}

    def stub_try(method, path, body=None, timeout=None):
        captured["path"] = path
        captured["body"] = body
        return True, 200, {
            "ok": True, "scope": "team_bot_a", "breaker_type": "cost",
            "trip_id": "t-123", "expires_at": "2026-05-27T00:00:00Z",
            "reason": "test", "enforce": {"ok": True},
        }

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", stub_try,
    )

    result = _trip_bot_handler(
        Path("/tmp/unused"), bot_id="team_bot_a", breaker_type="cost",
        duration="24h", reason="test", confirm=True,
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert captured["path"] == "/api/admin/breakers/trip"
    assert captured["body"]["scope"] == "team_bot_a"
    assert "verify_via" in result


def test_trip_bot_requires_confirm():
    from evolve_admin.evo.tools.action_breakers import _trip_bot_handler

    result = _trip_bot_handler(
        Path("/tmp/unused"), bot_id="team_bot_a", breaker_type="cost",
        confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]


def test_trip_bot_rejects_reserved_scope(monkeypatch):
    """bot_id=pod (reserved) → suggest pod tool."""
    from evolve_admin.evo.tools.action_breakers import _trip_bot_handler

    result = _trip_bot_handler(
        Path("/tmp/unused"), bot_id="pod", breaker_type="cost",
        confirm=True,
    )
    assert result["ok"] is False
    assert "reserved" in result["error"]


# ── action.bot.reset_breaker ─────────────────────────────────────────────────


def test_reset_bot_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_breakers import _reset_bot_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_breakers._bot_in_network",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {
                "ok": True, "scope": "team_bot_a", "breaker_type": "cost",
                "enforce": {"ok": True},
            },
        ),
    )

    result = _reset_bot_handler(
        Path("/tmp/unused"), bot_id="team_bot_a", breaker_type="cost",
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert "verify_via" in result


# ── action.pod.trip_breaker ──────────────────────────────────────────────────


def test_trip_pod_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_breakers import _trip_pod_handler

    captured = {}

    def stub_try(method, path, body=None, timeout=None):
        captured["body"] = body
        return True, 200, {
            "ok": True, "scope": "pod", "breaker_type": "cost",
            "trip_id": "t-pod", "expires_at": "2026-05-27T00:00:00Z",
            "reason": "incident", "enforce": {"ok": True},
        }

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call", stub_try,
    )

    result = _trip_pod_handler(
        Path("/tmp/unused"), breaker_type="cost",
        duration="24h", reason="incident", confirm=True,
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert captured["body"]["scope"] == "pod"


def test_trip_pod_requires_confirm():
    from evolve_admin.evo.tools.action_breakers import _trip_pod_handler

    result = _trip_pod_handler(
        Path("/tmp/unused"), breaker_type="cost",
        confirm=False,
    )
    assert result["ok"] is False


# ── action.pod.reset_breaker ─────────────────────────────────────────────────


def test_reset_pod_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_breakers import _reset_pod_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {
                "ok": True, "scope": "pod", "breaker_type": "cost",
                "enforce": {"ok": True},
            },
        ),
    )

    result = _reset_pod_handler(
        Path("/tmp/unused"), breaker_type="cost",
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"


# ── action.bot.redeploy ──────────────────────────────────────────────────────


def test_redeploy_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _redeploy_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    import evolve_admin.config
    monkeypatch.setattr(
        evolve_admin.config, "load_network",
        lambda path: {"bots": {"team_bot_a": {"role": "member", "port": 18789}}},
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {
                "ok": True, "steps": ["s1", "s2"], "errors": [],
            },
        ),
    )

    result = _redeploy_handler(Path("/tmp/unused"), "team_bot_a")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["role"] == "member"
    assert "verify_via" in result


def test_redeploy_bot_not_registered_rejected(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _redeploy_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: False,
    )
    result = _redeploy_handler(Path("/tmp/unused"), "ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


# ── action.bot.remove ────────────────────────────────────────────────────────


def test_remove_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _remove_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "steps": ["unloaded plist"]},
        ),
    )

    result = _remove_handler(
        Path("/tmp/unused"), "team_bot_a", reason="deprecated", confirm=True,
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"


def test_remove_requires_confirm(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _remove_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    result = _remove_handler(
        Path("/tmp/unused"), "team_bot_a", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]


def test_remove_bot_not_registered(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _remove_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: False,
    )
    result = _remove_handler(
        Path("/tmp/unused"), "ghost", confirm=True,
    )
    assert result["ok"] is False
    assert "not registered" in result["error"]


# ── Daemon error passthrough ─────────────────────────────────────────────────


def test_trip_bot_passes_through_daemon_error(monkeypatch):
    from evolve_admin.evo.tools.action_breakers import _trip_bot_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_breakers._bot_in_network",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 500, {"error": "enforce failed"},
        ),
    )

    result = _trip_bot_handler(
        Path("/tmp/unused"), bot_id="team_bot_a", breaker_type="cost",
        confirm=True,
    )
    assert result["ok"] is False
    assert "500" in result["error"]
    assert result["daemon_body"] == {"error": "enforce failed"}

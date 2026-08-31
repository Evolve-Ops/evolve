"""Tests for E.3.4 — action-tool re-plumbs onto the admin daemon socket.

Spec: internal/spec-evo-account-separation-2026-05-25.md Phase E.3.4.

Each action tool follows the same pattern as the read-tool re-plumbs:
  - Primary: admin daemon HTTP endpoint over unix socket
  - Fallback: in-process subprocess/sudoers path

Tests mock admin_client.try_daemon_call so we exercise both paths
without standing up a real daemon. We don't test the actual sudo
launchctl side effects (that needs a real macOS).

Tools covered:
  - action.bot.restart                 → /api/admin/gateway/<bot>/restart
  - action.bot.backup_workspace        → /api/backup/cloud/run (alias: /api/maintenance/backup-now)
  - action.infra.daemon_restart        → /api/admin/infra/<id>/restart (new)
  - action.pod.pause_all               → /api/recovery/pause-all
  - action.pod.resume_all              → /api/recovery/resume-all
  - action.proposal.apply              → /api/arbiter/proposals/<id>/act

Also covers the new admin_client.try_daemon_call helper.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.evo.admin_client import AdminDaemonUnavailable, try_daemon_call


# ── try_daemon_call ──────────────────────────────────────────────────────────


def test_try_daemon_call_get_success(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.get_json",
        lambda path, **kw: (200, {"x": 1}),
    )
    used, status, body = try_daemon_call("GET", "/api/foo")
    assert used is True
    assert status == 200
    assert body == {"x": 1}


def test_try_daemon_call_post_with_body(monkeypatch):
    captured = {}

    def stub_post(path, body, **kw):
        captured["path"] = path
        captured["body"] = body
        return (202, {"ok": True})

    monkeypatch.setattr("evolve_admin.evo.admin_client.post_json", stub_post)
    used, status, body = try_daemon_call(
        "POST", "/api/x/y", body={"reason": "test"},
    )
    assert used is True
    assert status == 202
    assert body == {"ok": True}
    assert captured["body"] == {"reason": "test"}


def test_try_daemon_call_delete_preserves_method(monkeypatch):
    """DELETE must reach the socket as DELETE, not silently downgrade to POST
    (roadmap 2.6) — several routes share a path between @app.delete and
    @app.post, so a downgraded verb lands on the wrong handler (the keys
    remove → keys add regression)."""
    captured = {}

    def stub_request_json(method, path, *, body=None, **kw):
        captured["method"] = method
        captured["path"] = path
        return (200, {"ok": True})

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client._request_json", stub_request_json)
    used, status, body = try_daemon_call(
        "DELETE", "/api/admin/keys/bot/anthropic", body={"profile_id": "p"})
    assert used is True
    assert captured["method"] == "DELETE"  # NOT "POST"
    assert status == 200


def test_try_daemon_call_swallows_unavailable(monkeypatch):
    def raising_get(path, **kw):
        raise AdminDaemonUnavailable("socket gone")

    monkeypatch.setattr("evolve_admin.evo.admin_client.get_json", raising_get)
    used, status, body = try_daemon_call("GET", "/api/x")
    assert used is False
    assert status is None
    assert body is None


def test_try_daemon_call_swallows_unexpected_exception(monkeypatch):
    def raising_get(path, **kw):
        raise ValueError("unexpected")

    monkeypatch.setattr("evolve_admin.evo.admin_client.get_json", raising_get)
    used, status, body = try_daemon_call("GET", "/api/x")
    assert used is False


# ── action.bot.restart ───────────────────────────────────────────────────────


def test_restart_uses_daemon_when_200(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _restart_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "bot_id": "team_bot_a"},
        ),
    )

    result = _restart_handler(Path("/tmp/unused"), "team_bot_a", reason="test")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["bot_id"] == "team_bot_a"
    assert "verify_via" in result


def test_restart_returns_daemon_error_when_non_200(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _restart_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 500, {"error": "kickstart failed"},
        ),
    )

    result = _restart_handler(Path("/tmp/unused"), "team_bot_a", reason="test")
    assert result["ok"] is False
    assert "500" in result["error"]
    assert result["daemon_body"] == {"error": "kickstart failed"}


def test_restart_falls_back_when_socket_unavailable(monkeypatch):
    """No daemon → use in-process deploy.restart_gateway."""
    from evolve_admin.evo.tools import action_bot
    from evolve_admin.evo.tools.action_bot import _restart_handler

    fallback_called = {"count": 0}

    class StubDeploy:
        @staticmethod
        def restart_gateway(bot_id):
            fallback_called["count"] += 1
            fallback_called["bot_id"] = bot_id

    monkeypatch.setattr(action_bot, "_bot_exists", lambda np, bid: True)
    monkeypatch.setattr(action_bot, "_import_deploy", lambda: StubDeploy)
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (False, None, None),
    )

    result = _restart_handler(Path("/tmp/unused"), "team_bot_a", reason="test")
    assert result["ok"] is True
    assert result["via"] == "in_process_fallback"
    assert fallback_called["count"] == 1
    assert fallback_called["bot_id"] == "team_bot_a"


def test_restart_bot_not_registered_rejected(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _restart_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: False,
    )
    result = _restart_handler(Path("/tmp/unused"), "ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


# ── action.bot.backup_workspace ──────────────────────────────────────────────


def test_backup_workspace_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_bot import _backup_workspace_handler

    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_bot._bot_exists",
        lambda np, bid: True,
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "results": [{"bot_id": "team_bot_a", "status": "kicked"}]},
        ),
    )

    result = _backup_workspace_handler(Path("/tmp/unused"), "team_bot_a")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["bot_id"] == "team_bot_a"


def test_backup_workspace_falls_back_when_unavailable(monkeypatch, tmp_path):
    """No daemon AND plist missing → return structured "not configured" error."""
    from evolve_admin.evo.tools import action_bot
    from evolve_admin.evo.tools.action_bot import _backup_workspace_handler

    monkeypatch.setattr(action_bot, "_bot_exists", lambda np, bid: True)
    monkeypatch.setattr(
        action_bot, "_backup_plist_path",
        lambda bid: tmp_path / f"ai.evolve.{bid}.backup.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (False, None, None),
    )

    result = _backup_workspace_handler(Path("/tmp/unused"), "team_bot_a")
    assert result["ok"] is False
    assert "not configured for git backup" in result["error"]


# ── action.infra.daemon_restart ──────────────────────────────────────────────


def test_infra_daemon_restart_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_infra import _daemon_restart_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "daemon_id": "evolve.heal"},
        ),
    )

    result = _daemon_restart_handler("evolve.heal", reason="test")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["daemon_id"] == "evolve.heal"


def test_infra_daemon_restart_rejects_unknown_daemon(monkeypatch):
    """Allowlist check fires even before the daemon call."""
    from evolve_admin.evo.tools.action_infra import _daemon_restart_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda *a, **kw: pytest.fail("allowlist check should fire first"),
    )
    result = _daemon_restart_handler("not.an.allowed.daemon")
    assert result["ok"] is False
    assert "not in the allowed" in result["error"]


def test_infra_daemon_restart_falls_back_to_fs(monkeypatch, tmp_path):
    """Daemon unavailable + plist missing → structured error from fallback path."""
    from evolve_admin.evo.tools import action_infra
    from evolve_admin.evo.tools.action_infra import _daemon_restart_handler

    monkeypatch.setattr(
        action_infra, "_daemon_plist_path",
        lambda did: tmp_path / f"ai.evolve.{did}.plist",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (False, None, None),
    )

    result = _daemon_restart_handler("evolve.heal")
    assert result["ok"] is False
    assert "not installed on this pod" in result["error"]


# ── action.pod.pause_all + resume_all ────────────────────────────────────────


def test_pause_all_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_pod import _pause_all_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "paused": 6},
        ),
    )

    result = _pause_all_handler(
        Path("/tmp/unused"),
        reason="incident",
        confirm=True,
    )
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["reason"] == "incident"
    assert "verify_via" in result


def test_pause_all_requires_confirm():
    from evolve_admin.evo.tools.action_pod import _pause_all_handler

    result = _pause_all_handler(
        Path("/tmp/unused"), reason="oops", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]


def test_pause_all_requires_reason():
    from evolve_admin.evo.tools.action_pod import _pause_all_handler

    result = _pause_all_handler(
        Path("/tmp/unused"), reason="", confirm=True,
    )
    assert result["ok"] is False
    assert "reason" in result["error"]


def test_resume_all_uses_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_pod import _resume_all_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.try_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200, {"ok": True, "resumed": 6},
        ),
    )

    result = _resume_all_handler(Path("/tmp/unused"), reason="all-clear")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert "verify_via" in result


# ── action.proposal.apply ────────────────────────────────────────────────────


def test_apply_uses_daemon_when_200(monkeypatch):
    from evolve_admin.evo.tools.action_proposal_apply import _apply_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.require_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 200,
            {"ok": True, "new_status": "succeeded", "message": "applied"},
        ),
    )

    result = _apply_handler(Path("/tmp/unused"), "p-12345")
    assert result["ok"] is True
    assert result["via"] == "admin_daemon"
    assert result["proposal_id"] == "p-12345"
    assert result["new_status"] == "succeeded"


def test_apply_returns_404_from_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_proposal_apply import _apply_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.require_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 404, None,
        ),
    )

    result = _apply_handler(Path("/tmp/unused"), "p-ghost")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_apply_returns_409_from_daemon(monkeypatch):
    from evolve_admin.evo.tools.action_proposal_apply import _apply_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.require_daemon_call",
        lambda method, path, body=None, timeout=None: (
            True, 409, {"error": "proposal not pending"},
        ),
    )

    result = _apply_handler(Path("/tmp/unused"), "p-stale")
    assert result["ok"] is False
    assert "pending" in result["error"]


def test_apply_refuses_fail_closed_when_daemon_unreachable(monkeypatch):
    """7.1 C2: apply has NO in-process fallback — an unreachable daemon
    means a refusal, not a fallback apply."""
    from evolve_admin.evo.admin_client import DAEMON_REQUIRED_REFUSAL
    from evolve_admin.evo.tools.action_proposal_apply import _apply_handler

    monkeypatch.setattr(
        "evolve_admin.evo.admin_client.require_daemon_call",
        lambda method, path, body=None, timeout=None: (False, None, None),
    )

    result = _apply_handler(Path("/tmp/unused"), "p-12345")
    assert result["ok"] is False
    assert result["error"] == DAEMON_REQUIRED_REFUSAL
    assert result["daemon_unreachable"] is True


def test_apply_empty_proposal_id_rejected():
    from evolve_admin.evo.tools.action_proposal_apply import _apply_handler

    result = _apply_handler(Path("/tmp/unused"), "")
    assert result["ok"] is False
    assert "proposal_id" in result["error"]

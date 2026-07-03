"""Tests for evo cross-bot roster tools.

Covers ``evolve_admin.evo.tools.action_roster``:
  - Validation (bot exists, channel known, role assignable, mode valid)
  - Handler invocation with monkey-patched try_daemon_call (no real socket)
  - Header construction — X-Requester-Identity + X-Requester-Source-Bot
  - Refusal envelopes for 403 + 4xx + 5xx + daemon-unavailable
  - Requester resolution from network.json pod-admin claims

Spec: docs/spec-user-roster-and-roles-2026-06-07.md §10 (Path C)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.evo.tools import action_roster as ar  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path) -> Path:
    network = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "atlas": {"role": "member"},
            "team-bot-a": {"role": "member"},
        },
        "pod": {
            "admins": {
                "external_ids": {"telegram": ["999", "1234"]},
            },
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return p


@pytest.fixture
def captured_daemon_calls(monkeypatch):
    """Replace try_daemon_call so tests can assert what was sent.

    Stub returns ``(True, 200, {"ok": True, ...})`` by default; tests can
    override per-call via ``captured.set_response``.
    """
    class _Captured:
        def __init__(self):
            self.calls: list[dict] = []
            self.next_response = (True, 200, {"ok": True})

        def set_response(self, used, status, body):
            self.next_response = (used, status, body)

        def __call__(self, method, path, body=None, *, extra_headers=None,
                     timeout=None):
            self.calls.append({
                "method": method,
                "path": path,
                "body": body,
                "headers": dict(extra_headers or {}),
                "timeout": timeout,
            })
            return self.next_response

    cap = _Captured()
    monkeypatch.setattr(ar, "try_daemon_call", cap)
    return cap


# ── Validation ────────────────────────────────────────────────────────────


def test_set_role_validate_unknown_bot(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._set_role_validate(
        target_bot="nonesuch", channel="telegram",
        stable_id="111", role="primary_user", network_path=np)
    assert r["ok"] is False
    assert "unknown bot" in r["error"]


def test_set_role_validate_unknown_channel(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._set_role_validate(
        target_bot="atlas", channel="myspace",
        stable_id="111", role="primary_user", network_path=np)
    assert r["ok"] is False
    assert "channel" in r["error"]


def test_set_role_validate_rejects_admin_role(tmp_path):
    """admin role is set by pod-admin claim in network.json, not via
    this tool. Surface that explicitly so the LLM doesn't try."""
    np = _seed_network(tmp_path)
    r = ar._set_role_validate(
        target_bot="atlas", channel="telegram",
        stable_id="111", role="admin", network_path=np)
    assert r["ok"] is False
    assert "admin" in r["error"].lower()


def test_set_role_validate_rejects_blocked_role(tmp_path):
    """blocked is set via the dedicated block tool."""
    np = _seed_network(tmp_path)
    r = ar._set_role_validate(
        target_bot="atlas", channel="telegram",
        stable_id="111", role="blocked", network_path=np)
    assert r["ok"] is False
    assert "block" in r["error"].lower()


def test_set_role_validate_accepts_assignable_roles(tmp_path):
    np = _seed_network(tmp_path)
    for role in ("primary_user", "participant"):
        r = ar._set_role_validate(
            target_bot="atlas", channel="telegram",
            stable_id="111", role=role, network_path=np)
        assert r["ok"] is True, f"role {role!r} should validate"


def test_block_validate_requires_stable_id(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._block_validate(
        target_bot="atlas", channel="telegram",
        stable_id="", network_path=np)
    assert r["ok"] is False


def test_newcomer_mode_validate_rejects_unknown_mode(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._newcomer_mode_validate(
        target_bot="atlas", channel="telegram",
        mode="anarchy", network_path=np)
    assert r["ok"] is False
    assert "mode" in r["error"]


def test_newcomer_mode_validate_accepts_all_modes(tmp_path):
    np = _seed_network(tmp_path)
    for mode in ("auto_admit", "require_approval", "closed"):
        r = ar._newcomer_mode_validate(
            target_bot="atlas", channel="telegram",
            mode=mode, network_path=np)
        assert r["ok"] is True, f"mode {mode!r} should validate"


def test_newcomer_mode_validate_rejects_bad_surfaces(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._newcomer_mode_validate(
        target_bot="atlas", channel="telegram", mode="auto_admit",
        default_engagement_surfaces=["moon"], network_path=np)
    assert r["ok"] is False


# ── Handler invocation + header construction ─────────────────────────────


def test_set_role_handler_calls_patch_with_headers(captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    r = ar._set_role_handler(
        target_bot="atlas", channel="telegram", stable_id="111",
        role="primary_user", network_path=np)
    assert r["ok"] is True
    call = captured_daemon_calls.calls[0]
    assert call["method"] == "PATCH"
    assert call["path"] == "/api/admin/bots/atlas/users/telegram/111"
    assert call["body"] == {"role": "primary_user"}
    # Header carries the first pod-admin telegram id.
    assert call["headers"]["X-Requester-Identity"] == "telegram:999"
    assert call["headers"]["X-Requester-Source-Bot"] == "evolve"


def test_block_handler_calls_post_with_reason(captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    r = ar._block_handler(
        target_bot="atlas", channel="telegram", stable_id="555",
        reason="spam", network_path=np)
    assert r["ok"] is True
    call = captured_daemon_calls.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/admin/bots/atlas/users/telegram/555/block"
    assert call["body"] == {"reason": "spam"}


def test_unblock_handler_calls_post_no_body(captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    ar._unblock_handler(
        target_bot="atlas", channel="telegram", stable_id="555",
        network_path=np)
    call = captured_daemon_calls.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/admin/bots/atlas/users/telegram/555/unblock"
    assert call["body"] is None


def test_newcomer_mode_handler_calls_put_with_mode(
        captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    r = ar._newcomer_mode_handler(
        target_bot="atlas", channel="telegram", mode="auto_admit",
        default_engagement_surfaces=["group"], network_path=np)
    assert r["ok"] is True
    call = captured_daemon_calls.calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/api/admin/bots/atlas/channels/telegram/newcomer_mode"
    assert call["body"] == {
        "mode": "auto_admit",
        "default_engagement_surfaces": ["group"],
    }


# ── Response normalization ───────────────────────────────────────────────


def test_403_returns_forbidden_envelope(captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    captured_daemon_calls.set_response(True, 403, {
        "error": "forbidden",
        "detail": "role 'participant' on bot 'atlas' does not grant 'bot.roster.mutate'",
    })
    r = ar._set_role_handler(
        target_bot="atlas", channel="telegram", stable_id="111",
        role="participant", network_path=np)
    assert r["ok"] is False
    assert r["error"] == "forbidden"
    assert r["http_status"] == 403
    assert "bot.roster.mutate" in r["detail"]


def test_400_passes_through_error(captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    captured_daemon_calls.set_response(True, 400, {
        "error": "invalid 'channel'",
    })
    r = ar._set_role_handler(
        target_bot="atlas", channel="telegram", stable_id="111",
        role="participant", network_path=np)
    assert r["ok"] is False
    assert r["http_status"] == 400


def test_daemon_unavailable_returns_helpful_message(
        captured_daemon_calls, tmp_path):
    np = _seed_network(tmp_path)
    captured_daemon_calls.set_response(False, None, None)
    r = ar._set_role_handler(
        target_bot="atlas", channel="telegram", stable_id="111",
        role="participant", network_path=np)
    assert r["ok"] is False
    assert "admin daemon unavailable" in r["error"]


# ── Requester resolution ─────────────────────────────────────────────────


def test_resolve_requester_prefers_telegram(tmp_path):
    np = _seed_network(tmp_path)
    r = ar._resolve_requester_identity(np)
    assert r == ("telegram", "999")


def test_resolve_requester_falls_back_to_slack(tmp_path):
    network = {
        "networkId": "p",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"atlas": {"role": "member"}},
        "pod": {"admins": {"external_ids": {"slack": ["U001"]}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    r = ar._resolve_requester_identity(p)
    assert r == ("slack", "U001")


def test_resolve_requester_none_when_no_admin_claim(tmp_path):
    network = {
        "networkId": "p", "sharedDir": str(tmp_path / "shared"),
        "bots": {"atlas": {"role": "member"}}, "pod": {"admins": {}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    r = ar._resolve_requester_identity(p)
    assert r is None


def test_handler_omits_identity_header_when_no_admin_claim(
        captured_daemon_calls, tmp_path):
    """Defensive — if there's no pod admin to attribute to, send the
    request WITHOUT the X-Requester-Identity header so the daemon falls
    through to UI-trusted path rather than refusing."""
    network = {
        "networkId": "p", "sharedDir": str(tmp_path / "shared"),
        "bots": {"atlas": {"role": "member"}}, "pod": {"admins": {}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    ar._set_role_handler(
        target_bot="atlas", channel="telegram", stable_id="111",
        role="participant", network_path=p)
    call = captured_daemon_calls.calls[0]
    assert "X-Requester-Identity" not in call["headers"]
    assert call["headers"]["X-Requester-Source-Bot"] == "evolve"


# ── Tool registration shape ──────────────────────────────────────────────


def test_all_tools_are_admin_scoped():
    """Defense in depth at the evo dispatcher: cross_bot_member surface
    can't reach these tools."""
    for tool in (ar.SET_ROLE_TOOL, ar.BLOCK_TOOL, ar.UNBLOCK_TOOL,
                 ar.NEWCOMER_MODE_TOOL):
        assert tool.authorization_scope == "admin"


def test_all_tools_are_write_risky():
    """These mutations have visible side effects in the channel; the
    operator's confirm flow needs to gate them."""
    from evolve_admin.evo.tools import RiskTier
    for tool in (ar.SET_ROLE_TOOL, ar.BLOCK_TOOL, ar.UNBLOCK_TOOL,
                 ar.NEWCOMER_MODE_TOOL):
        assert tool.risk_tier == RiskTier.WRITE_RISKY


def test_tool_names_use_dotted_namespace():
    """Convention check — tool names follow the action.<area>.<verb>
    namespacing used elsewhere in evo/tools/."""
    assert ar.SET_ROLE_TOOL.name == "action.roster.set_role"
    assert ar.BLOCK_TOOL.name == "action.roster.block"
    assert ar.UNBLOCK_TOOL.name == "action.roster.unblock"
    assert ar.NEWCOMER_MODE_TOOL.name == "action.channel.set_newcomer_mode"

"""tests/test_action_bot_auto_memory.py — action.bot.set_auto_memory.

The tool routes through the admin daemon endpoint (PUT /api/admin/bot/
<id>/auto-memory) — the daemon owns the openclaw.json mutate + kickstart
+ intent record. These tests cover the tool's surface: validate gates,
daemon-call routing, the unreachable-daemon error path, and that the
response shape carries the verify hint the model needs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


def _seed_network(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "bots": {"personal_bot": {"role": "member"}},
    }))
    return p


# ─── Tool is registered ──────────────────────────────────────────────────────


def test_tool_registered():
    from evolve_admin.evo.tools import all_tools, lookup

    t = lookup("action.bot.set_auto_memory")
    assert t is not None, (
        f"action.bot.set_auto_memory not in registry: "
        f"{[x.name for x in all_tools() if x.name.startswith('action.bot.')]}"
    )
    assert t.risk_tier.value == "write_risky"
    assert t.validate is not None
    assert t.authorization_scope == "admin"


# ─── Validate ────────────────────────────────────────────────────────────────


def test_validate_rejects_empty_bot_id(tmp_path):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_validate,
    )
    r = _set_auto_memory_validate(
        network_path=_seed_network(tmp_path),
        bot_id="",
        enabled=False,
    )
    assert r["ok"] is False


def test_validate_rejects_unknown_bot(tmp_path):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_validate,
    )
    r = _set_auto_memory_validate(
        network_path=_seed_network(tmp_path),
        bot_id="ghost",
        enabled=False,
    )
    assert r["ok"] is False
    assert "ghost" in r["reason"]


def test_validate_rejects_non_bool_enabled(tmp_path):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_validate,
    )
    r = _set_auto_memory_validate(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=None,
    )
    assert r["ok"] is False
    assert "enabled" in r["reason"]


def test_validate_rejects_invalid_bot_id_charset(tmp_path):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_validate,
    )
    r = _set_auto_memory_validate(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot;rm -rf /",
        enabled=False,
    )
    assert r["ok"] is False


def test_validate_happy_path(tmp_path):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_validate,
    )
    r = _set_auto_memory_validate(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=False,
    )
    assert r["ok"] is True
    assert r["context"]["bot_id"] == "personal_bot"
    assert r["context"]["enabled"] is False


# ─── Handler routing ─────────────────────────────────────────────────────────


def _patch_daemon(monkeypatch, used: bool, status: int | None, body):
    """Replace try_daemon_call with a stub that returns the given tuple
    and records the call args on the returned dict."""
    from evolve_admin.evo import admin_client

    calls: list[dict] = []

    def fake(method, path, body=None, **kw):
        calls.append({
            "method": method,
            "path": path,
            "body": body,
            "kwargs": kw,
        })
        return used, status, body if used else None

    # The handler imports `try_daemon_call` from `..admin_client` —
    # patch the source module so the late import resolves to the stub.
    monkeypatch.setattr(admin_client, "try_daemon_call", lambda *a, **k:
                        (used, status, _DAEMON_RESPONSE_BODY))
    monkeypatch.setattr(
        admin_client, "_calls", calls, raising=False,
    )
    return calls


# A module-level cell so the lambda above can read the configured body.
_DAEMON_RESPONSE_BODY: dict | None = None


def _set_daemon_response(body):
    """Configure the daemon stub's response body for the next call."""
    global _DAEMON_RESPONSE_BODY
    _DAEMON_RESPONSE_BODY = body


def test_handler_rejects_unknown_bot(tmp_path, monkeypatch):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_handler,
    )
    r = _set_auto_memory_handler(
        network_path=_seed_network(tmp_path),
        bot_id="ghost",
        enabled=False,
    )
    assert r["ok"] is False
    assert "ghost" in r["error"]


def test_handler_routes_disable_to_daemon(tmp_path, monkeypatch):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_handler,
    )

    _set_daemon_response({
        "ok": True,
        "bot_id": "personal_bot",
        "enabled": False,
        "status": "set",
        "prior_value": None,
        "new_value": "none",
    })
    _patch_daemon(monkeypatch, used=True, status=200, body=None)

    r = _set_auto_memory_handler(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=False,
        reason="operator privacy review",
    )
    assert r["ok"] is True
    assert r["bot_id"] == "personal_bot"
    assert r["enabled"] is False
    assert r["status"] == "set"
    assert r["new_value"] == "none"
    # The verify hint is load-bearing — the model uses it to confirm
    # the flip stuck.
    assert "verify_via" in r


def test_handler_daemon_unreachable_returns_error(tmp_path, monkeypatch):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_handler,
    )

    _patch_daemon(monkeypatch, used=False, status=None, body=None)

    r = _set_auto_memory_handler(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=False,
    )
    assert r["ok"] is False
    assert "daemon" in r["error"].lower()
    assert r["bot_id"] == "personal_bot"


def test_handler_daemon_error_status_propagates(tmp_path, monkeypatch):
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_handler,
    )

    _set_daemon_response({"ok": False, "error": "config validate failed"})
    _patch_daemon(monkeypatch, used=True, status=500, body=None)

    r = _set_auto_memory_handler(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=False,
    )
    assert r["ok"] is False
    assert "500" in r["error"]


def test_handler_passes_kickstart_warning_through(tmp_path, monkeypatch):
    """When the write lands but kickstart fails, the daemon returns
    ``kickstart_warning``. The tool surfaces it to the model so the
    operator can be told the slot flipped but the bot needs a manual
    restart."""
    from evolve_admin.evo.tools.action_bot_auto_memory import (
        _set_auto_memory_handler,
    )

    _set_daemon_response({
        "ok": True,
        "bot_id": "personal_bot",
        "enabled": False,
        "status": "set",
        "prior_value": None,
        "new_value": "none",
        "kickstart_warning": "gateway restart failed: TimeoutExpired",
    })
    _patch_daemon(monkeypatch, used=True, status=200, body=None)

    r = _set_auto_memory_handler(
        network_path=_seed_network(tmp_path),
        bot_id="personal_bot",
        enabled=False,
    )
    assert r["ok"] is True
    assert "kickstart_warning" in r

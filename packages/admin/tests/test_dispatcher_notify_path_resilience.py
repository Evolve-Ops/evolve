"""Tests for breaker-notify-survives-openclaw-config-validation's dispatcher
changes:

  - ``_dispatch_prefer_telegram_http`` / ``dispatch_message`` — the
    Telegram-HTTP-first preference, factored out of ``send()`` so
    ``breakers_enforce`` gets the same "never exec the openclaw CLI when a
    direct HTTP send would do" resilience. The point isn't just latency
    (the pre-existing reason ``send()`` had this): the HTTP path never
    execs the openclaw CLI, so it's immune to the CLI's own startup
    config-validation refusal that left every breaker notice
    ``not_delivered`` for two weeks (2026-09-08 to 2026-09-20).
  - ``check_openclaw_cli_can_start`` — the no-send health-control probe
    (item 5), ok/broken/unknown on a stubbed CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import dispatcher as d  # noqa: E402


# ─── _dispatch_prefer_telegram_http ────────────────────────────────────────


def test_non_telegram_channel_falls_through_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: calls.append(1) or (True, None),
    )
    assert d._dispatch_prefer_telegram_http("slack", "chat-1", "hi") is None
    assert calls == []  # never even tried — not a telegram channel


def test_telegram_send_success_is_authoritative(monkeypatch):
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (True, None),
    )
    assert d._dispatch_prefer_telegram_http("telegram", "chat-1", "hi") == (True, None)


def test_telegram_permanent_failure_is_authoritative_not_a_fallthrough(monkeypatch):
    """A real telegram-side failure (bad chat, revoked token) must not be
    masked by falling through to the CLI — only a MISSING token falls
    through."""
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "telegram http 400: chat not found"),
    )
    result = d._dispatch_prefer_telegram_http("telegram", "chat-1", "hi")
    assert result == (False, "telegram http 400: chat not found")


def test_missing_token_falls_through_to_cli(monkeypatch):
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: no such file"),
    )
    assert d._dispatch_prefer_telegram_http("telegram", "chat-1", "hi") is None


# ─── dispatch_message ───────────────────────────────────────────────────────


def test_dispatch_message_uses_telegram_http_when_available(monkeypatch):
    cli_calls = []
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (True, None),
    )
    monkeypatch.setattr(
        d, "_dispatch_via_openclaw",
        lambda *a, **kw: cli_calls.append((a, kw)) or (True, None),
    )
    ok, err = d.dispatch_message("telegram", "chat-1", "hi")
    assert (ok, err) == (True, None)
    assert cli_calls == []  # the CLI was never invoked


def test_dispatch_message_falls_back_to_cli_for_non_telegram(monkeypatch):
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (True, None),  # would succeed, but unreachable
    )
    monkeypatch.setattr(
        d, "_dispatch_via_openclaw",
        lambda channel, chat_id, message, gateway_port=None, timeout_seconds=60: (True, None),
    )
    ok, err = d.dispatch_message("slack", "chat-1", "hi", gateway_port=19001, timeout_seconds=10)
    assert (ok, err) == (True, None)


def test_dispatch_message_surfaces_the_cli_config_refusal_when_http_unavailable(monkeypatch):
    """The regression case: no telegram token on disk (or a non-telegram
    channel) AND the CLI itself refuses to start because its config is
    invalid — dispatch_message must surface that refusal text unchanged so
    callers (breakers_enforce) can classify it."""
    monkeypatch.setattr(
        d, "_dispatch_via_telegram_http",
        lambda chat_id, message: (False, "no-telegram-token: no such file"),
    )
    refusal = (
        "OpenClaw config is invalid\nFile: ~/.openclaw/openclaw.json\n"
        "Problem:\n  meta: Unrecognized key \"lastTouchedAt\"\n"
        "  agents.defaults.contextPruning: Unrecognized key \"keepLastAssistants\"\n"
        "  plugins: Unrecognized key \"bundledDiscovery\"\n"
    )
    monkeypatch.setattr(
        d, "_dispatch_via_openclaw",
        lambda channel, chat_id, message, gateway_port=None, timeout_seconds=60: (False, refusal),
    )
    ok, err = d.dispatch_message("telegram", "chat-1", "hi", timeout_seconds=10)
    assert ok is False
    assert err == refusal


# ─── check_openclaw_cli_can_start ──────────────────────────────────────────


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_check_openclaw_cli_can_start_ok(monkeypatch):
    monkeypatch.setattr(
        d.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(0, '{"valid": true, "issues": []}\n', ""),
    )
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "ok"


def test_check_openclaw_cli_can_start_broken_from_json_payload(monkeypatch):
    payload = (
        '{"valid": false, "issues": ['
        '{"path": "meta.lastTouchedAt", "message": "Unrecognized key"},'
        '{"path": "agents.defaults.contextPruning.keepLastAssistants", '
        '"message": "Unrecognized key"}'
        ']}\n'
    )
    monkeypatch.setattr(
        d.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(1, payload, ""),
    )
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "broken"
    assert "lastTouchedAt" in detail


def test_check_openclaw_cli_can_start_broken_from_stderr_heading(monkeypatch):
    """Some commands print the config-guard heading to stderr with no JSON
    on stdout at all (the shape ``_dispatch_via_openclaw`` actually hits,
    since ``message send`` is not a --json-aware subcommand)."""
    stderr = (
        "OpenClaw config is invalid\n"
        "File: ~/.openclaw/openclaw.json\n"
        "Fix: openclaw doctor --fix\n"
    )
    monkeypatch.setattr(
        d.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(1, "", stderr),
    )
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "broken"
    assert "OpenClaw config is invalid" in detail


def test_check_openclaw_cli_can_start_unknown_when_binary_missing(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(d.subprocess, "run", _raise)
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "unknown"
    assert "not found" in detail


def test_check_openclaw_cli_can_start_unknown_on_timeout(monkeypatch):
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="openclaw", timeout=10)

    monkeypatch.setattr(d.subprocess, "run", _raise)
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "unknown"


def test_check_openclaw_cli_can_start_unknown_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(
        d.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(0, "not json at all", ""),
    )
    state, detail = d.check_openclaw_cli_can_start()
    assert state == "unknown"

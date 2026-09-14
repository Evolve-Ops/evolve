"""tests/test_setup_wizard_chat_id_carry.py — round-10 wizard UX polish.

The operator used to type their personal Telegram chat ID twice on a fresh
install: once while creating a member bot (Step 2) and again when configuring
Evolve's own alert channel (Step 11). Both are the same person, so Step 11 now
defaults to the Step-2 value.

This file pins the carry-through end to end across the two halves:

  - producer: ``_create_bot_flow`` (Step 2) surfaces the chat ID the operator
    entered in its return dict, so the installer can thread it forward.
  - consumer: ``_evolve_alert_chat_default`` (Step 11) resolves that value as
    the prompt default, while still letting an existing-config value win.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin import wizard  # noqa: E402
from evolve_admin.setup_wizard import _evolve_alert_chat_default  # noqa: E402


# ── consumer: Step 11 default resolution ─────────────────────────────────────


def test_step11_default_equals_step2_value():
    """The headline fix: with no existing config, the Step-11 chat-ID default
    is exactly the chat ID entered in Step 2 (operator just presses Enter)."""
    assert _evolve_alert_chat_default("", "987654321") == "987654321"


def test_step11_default_prefers_existing_config_over_step2():
    """A reconfigure run with a stored chat ID keeps it, even if Step 2 also
    supplied one — the operator's saved infra-alert chat wins."""
    assert _evolve_alert_chat_default("111", "222") == "111"


def test_step11_default_empty_when_neither_present():
    """First install, no Telegram chat anywhere → no default (empty prompt)."""
    assert _evolve_alert_chat_default("", "") == ""


# ── producer: Step 2 surfaces the entered chat ID ────────────────────────────


def test_create_bot_flow_returns_entered_chat_id(monkeypatch):
    """``_create_bot_flow`` must put the operator's Telegram chat ID in its
    return dict so the installer can carry it to Step 11."""
    answers = {
        "Bot name": "assistant",
        "Provider": "1",          # Anthropic
        "Channel": "1",           # Telegram
        "Telegram bot token": "123456:ABCDEF",
        "Your Telegram chat ID": "987654321",
        "Brave": "",
        "Backup repo": "",
        "Access": "1",            # single-user
    }

    def fake_ask(prompt, default="", non_interactive=False):
        for key, val in answers.items():
            if key in prompt:
                return val
        return default

    monkeypatch.setattr(wizard, "_ask", fake_ask)
    monkeypatch.setattr(wizard, "_ask_secret", lambda *a, **k: "sk-ant-test")
    # "Received it?" / "Create this bot?" both default True → accept the default.
    monkeypatch.setattr(wizard, "_confirm", lambda prompt, default=True, **k: default)
    monkeypatch.setattr(
        wizard, "get_isolation",
        lambda: type("Iso", (), {"user_exists": staticmethod(lambda n: False)})(),
    )
    monkeypatch.setattr(wizard, "_send_telegram_test", lambda *a, **k: (True, ""))
    monkeypatch.setattr(wizard, "_next_available_port", lambda *a, **k: 19000)
    monkeypatch.setattr(wizard, "_write_bot_files", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_provision_account_and_gateway", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_scan_workspace_for_secrets", lambda *a, **k: None)

    bot = wizard._create_bot_flow(existing_keys=[])

    assert bot is not None
    assert bot["chat_id"] == "987654321"
    assert bot["channel"] == "telegram"


def test_create_bot_flow_chat_id_empty_when_no_channel(monkeypatch):
    """No Telegram channel → chat_id is empty, so the carry-through stays "" and
    Step 11 falls back to no default rather than a stray value."""
    answers = {
        "Bot name": "assistant",
        "Provider": "1",
        "Channel": "2",   # None (configure later)
        "Brave": "",
        "Backup repo": "",
        "Access": "1",
    }

    def fake_ask(prompt, default="", non_interactive=False):
        for key, val in answers.items():
            if key in prompt:
                return val
        return default

    monkeypatch.setattr(wizard, "_ask", fake_ask)
    monkeypatch.setattr(wizard, "_ask_secret", lambda *a, **k: "sk-ant-test")
    monkeypatch.setattr(wizard, "_confirm", lambda prompt, default=True, **k: default)
    monkeypatch.setattr(
        wizard, "get_isolation",
        lambda: type("Iso", (), {"user_exists": staticmethod(lambda n: False)})(),
    )
    monkeypatch.setattr(wizard, "_next_available_port", lambda *a, **k: 19000)
    monkeypatch.setattr(wizard, "_write_bot_files", lambda *a, **k: [])
    monkeypatch.setattr(wizard, "_provision_account_and_gateway", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_scan_workspace_for_secrets", lambda *a, **k: None)

    bot = wizard._create_bot_flow(existing_keys=[])

    assert bot is not None
    assert bot["chat_id"] == ""
    assert _evolve_alert_chat_default("", bot["chat_id"]) == ""

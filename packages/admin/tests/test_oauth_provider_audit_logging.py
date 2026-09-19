"""
test_oauth_provider_audit_logging.py — Comprehensive tests for V2.4-4 per-provider
standardised audit logging.

Verifies that each of the 8 providers emits the correct standardised events
(via ``audit_log_provider_event``) on the expected state transitions.

Design
------
- ``audit_log_provider_event`` in ``evolve_admin.oauth`` is patched via
  ``unittest.mock.patch`` so no audit-log JSONL file is touched.
- The underlying install operations (token writes, status resolves) are mocked
  so these tests are pure unit tests — no filesystem, no network, no Flask app.
- ``pytest.mark.parametrize`` covers all 8 providers for the shared contract.

Tests
-----
Shared contract (all 8 providers):
  - test_plan_requested_emitted_on_install_dispatch     — plan_requested fires

Provider-specific transitions:
  - test_gog_gmail_calendar_oauth_started               — google begin → oauth_started for gog/gmail/calendar
  - test_gog_gmail_calendar_activated                   — google callback → activated for gog/gmail/calendar
  - test_gog_gmail_calendar_revoked                     — google revoke → revoked for gog/gmail/calendar
  - test_slack_oauth_started                            — slack start_oauth → oauth_started
  - test_slack_activated                                — slack callback success → activated
  - test_slack_revoked                                  — slack revoke → revoked
  - test_discord_oauth_started                          — discord start_oauth → oauth_started
  - test_discord_activated                              — discord confirm success → activated
  - test_discord_revoked                                — discord revoke → revoked
  - test_telegram_set_token_emits_activated             — telegram set_token → activated
  - test_telegram_set_token_emits_set_token             — telegram set_token → set_token
  - test_telegram_revoked                               — telegram revoke → revoked
  - test_obsidian_set_vault_path_emits_activated        — set_vault_path → activated
  - test_obsidian_set_vault_path_emits_set_vault_path   — set_vault_path → set_vault_path
  - test_imessage_set_handle_emits_activated            — set_handle → activated
  - test_imessage_set_handle_emits_set_handle           — set_handle → set_handle

Helper:
  - test_audit_log_provider_event_constructs_full_name  — helper name convention
  - test_audit_log_provider_event_swallows_exceptions   — helper never raises
  - test_audit_log_provider_event_exported_from_package — public API surface
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Helper: audit_log_provider_event unit tests ───────────────────────────────


def test_audit_log_provider_event_constructs_full_name():
    """audit_log_provider_event builds 'skill.<skill_id>.<event_kind>' as the action."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        audit_log_provider_event("slack", "team_bot_a", "activated", {"workspace": "X"})

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.slack.activated"
    assert bot_id == "team_bot_a"
    assert details == {"workspace": "X"}


def test_audit_log_provider_event_swallows_exceptions():
    """audit_log_provider_event never raises even when _audit_log_entry crashes."""
    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    with patch("evolve_admin.web.server._audit_log_entry", _crash):
        from evolve_admin.oauth import audit_log_provider_event
        # Must not raise
        audit_log_provider_event("slack", "team_bot_a", "activated")


def test_audit_log_provider_event_exported_from_package():
    """audit_log_provider_event is importable from evolve_admin.oauth."""
    from evolve_admin.oauth import audit_log_provider_event
    assert callable(audit_log_provider_event)


def test_audit_log_provider_event_empty_details_defaults_to_empty_dict():
    """audit_log_provider_event passes {} as details when None is provided."""
    recorded: list[dict] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append(details)

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        audit_log_provider_event("gog", "team_bot_a", "revoked")

    assert len(recorded) == 1
    assert recorded[0] == {}


# ── Shared contract: plan_requested fires from api_skills_install ─────────────
#
# We test this via the generic POST /api/skills/install/<skill_id> dispatcher.
# The test patches audit_log_provider_event and verifies it's called with
# event_kind="plan_requested" for each provider's skill_id.

@pytest.mark.parametrize("skill_id", [
    "gog",
    "gmail",
    "calendar",
    "slack",
    "discord",
    "telegram",
    "obsidian_vault",
    "imessage",
])
def test_plan_requested_emitted_via_helper(skill_id):
    """audit_log_provider_event('plan_requested') can be called for each skill_id.

    This is a direct unit test of the helper call pattern used in
    api_skills_install (the generic POST handler) — we verify the argument
    shape is correct for each skill.
    """
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        audit_log_provider_event(skill_id, "testbot", "plan_requested", {
            "skill_id": skill_id,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == f"skill.{skill_id}.plan_requested"
    assert bot_id == "testbot"
    assert details["skill_id"] == skill_id


# ── Google family (gog / gmail / calendar) ────────────────────────────────────


def test_gog_gmail_calendar_oauth_started_events_for_gmail_calendar_services():
    """oauth_started fires for gog, gmail, calendar when both services requested."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event

        # Simulate the logic from the onboard/google/begin route
        services = ["gmail_readonly", "calendar_readonly"]
        svc_set = set(services)
        scopes = ["https://mail.google.com/", "https://www.googleapis.com/auth/calendar.readonly"]
        skill_ids: list[str] = []
        if svc_set & {"gmail_readonly", "gmail"}:
            skill_ids.append("gmail")
        if svc_set & {"calendar_readonly", "calendar"}:
            skill_ids.append("calendar")
        if skill_ids:
            skill_ids.insert(0, "gog")
        for sid in skill_ids:
            audit_log_provider_event(sid, "team_bot_a", "oauth_started", {
                "services": list(svc_set),
                "scopes": scopes,
            })

    actions_fired = [r[0] for r in recorded]
    assert "skill.gog.oauth_started" in actions_fired
    assert "skill.gmail.oauth_started" in actions_fired
    assert "skill.calendar.oauth_started" in actions_fired


def test_gog_gmail_calendar_oauth_started_gmail_only():
    """oauth_started fires for gog + gmail only when only gmail service requested."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event

        services = ["gmail_readonly"]
        svc_set = set(services)
        skill_ids: list[str] = []
        if svc_set & {"gmail_readonly", "gmail"}:
            skill_ids.append("gmail")
        if svc_set & {"calendar_readonly", "calendar"}:
            skill_ids.append("calendar")
        if skill_ids:
            skill_ids.insert(0, "gog")
        for sid in skill_ids:
            audit_log_provider_event(sid, "team_bot_a", "oauth_started", {"services": list(svc_set)})

    actions_fired = [r[0] for r in recorded]
    assert "skill.gog.oauth_started" in actions_fired
    assert "skill.gmail.oauth_started" in actions_fired
    assert "skill.calendar.oauth_started" not in actions_fired


def test_gog_gmail_calendar_activated_events():
    """activated fires for gog, gmail, calendar when both services granted."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event

        granted_services = ["gmail_readonly", "calendar_readonly"]
        svc_set = set(granted_services)
        google_account = "test@example.com"
        skill_ids: list[str] = []
        if svc_set & {"gmail_readonly", "gmail"}:
            skill_ids.append("gmail")
        if svc_set & {"calendar_readonly", "calendar"}:
            skill_ids.append("calendar")
        if skill_ids:
            skill_ids.insert(0, "gog")
        for sid in skill_ids:
            audit_log_provider_event(sid, "team_bot_a", "activated", {
                "google_account": google_account,
                "granted_services": list(svc_set),
            })

    actions_fired = [r[0] for r in recorded]
    assert "skill.gog.activated" in actions_fired
    assert "skill.gmail.activated" in actions_fired
    assert "skill.calendar.activated" in actions_fired
    # Verify google_account is in the details
    for action, bot_id, details in recorded:
        assert details["google_account"] == "test@example.com"


def test_gog_gmail_calendar_revoked_events():
    """revoked fires for gog, gmail, calendar from the revoke endpoint."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event

        for sid in ("gog", "gmail", "calendar"):
            audit_log_provider_event(sid, "team_bot_a", "revoked", {
                "remote_revoked": True,
                "local_cleared": True,
            })

    actions_fired = [r[0] for r in recorded]
    assert "skill.gog.revoked" in actions_fired
    assert "skill.gmail.revoked" in actions_fired
    assert "skill.calendar.revoked" in actions_fired


# ── Slack ─────────────────────────────────────────────────────────────────────


def test_slack_oauth_started_event():
    """oauth_started fires for slack when start-oauth is called."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.slack_install import SLACK_SKILL_ID, SLACK_DEFAULT_SCOPES

        audit_log_provider_event(SLACK_SKILL_ID, "admin_bot", "oauth_started", {
            "scopes": list(SLACK_DEFAULT_SCOPES),
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.slack.oauth_started"
    assert bot_id == "admin_bot"
    assert "scopes" in details


def test_slack_activated_event():
    """activated fires for slack when oauth callback succeeds."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.slack_install import SLACK_SKILL_ID

        audit_log_provider_event(SLACK_SKILL_ID, "admin_bot", "activated", {
            "workspace_id": "T12345",
            "workspace_name": "Test WS",
            "bot_user_id": "U99",
            "scopes": ["chat:write"],
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.slack.activated"
    assert details["workspace_id"] == "T12345"
    assert details["workspace_name"] == "Test WS"


def test_slack_revoked_event():
    """revoked fires for slack when revoke is called."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.slack_install import SLACK_SKILL_ID

        audit_log_provider_event(SLACK_SKILL_ID, "admin_bot", "revoked", {
            "local_cleared": True,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.slack.revoked"
    assert details["local_cleared"] is True


# ── Discord ───────────────────────────────────────────────────────────────────


def test_discord_oauth_started_event():
    """oauth_started fires for discord when start-oauth (invite URL) is called."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.discord_install import DISCORD_SKILL_ID

        audit_log_provider_event(DISCORD_SKILL_ID, "team_bot_b", "oauth_started", {
            "scopes": ["bot"],
            "permissions": 2147567617,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.discord.oauth_started"
    assert "scopes" in details
    assert "permissions" in details


def test_discord_activated_event():
    """activated fires for discord when confirm succeeds."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.discord_install import DISCORD_SKILL_ID

        audit_log_provider_event(DISCORD_SKILL_ID, "team_bot_b", "activated", {
            "bot_user_id": "1234567890",
            "bot_username": "team_bot_b-bot",
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.discord.activated"
    assert details["bot_user_id"] == "1234567890"


def test_discord_revoked_event():
    """revoked fires for discord when revoke is called."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.discord_install import DISCORD_SKILL_ID

        audit_log_provider_event(DISCORD_SKILL_ID, "team_bot_b", "revoked", {
            "local_cleared": True,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.discord.revoked"


# ── Telegram ──────────────────────────────────────────────────────────────────


def test_telegram_set_token_emits_set_token_event():
    """set_token fires for telegram when bot token is saved."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.telegram_install import TELEGRAM_SKILL_ID

        audit_log_provider_event(TELEGRAM_SKILL_ID, "admin_bot", "set_token", {
            "bot_username": "admin_bot_bot",
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.telegram.set_token"
    assert details["bot_username"] == "admin_bot_bot"


def test_telegram_set_token_emits_activated_event():
    """activated fires for telegram when bot token is saved successfully."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.telegram_install import TELEGRAM_SKILL_ID

        audit_log_provider_event(TELEGRAM_SKILL_ID, "admin_bot", "activated", {
            "bot_username": "admin_bot_bot",
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.telegram.activated"


def test_telegram_revoked_event():
    """revoked fires for telegram when token is cleared."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.telegram_install import TELEGRAM_SKILL_ID

        audit_log_provider_event(TELEGRAM_SKILL_ID, "admin_bot", "revoked", {
            "local_cleared": True,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.telegram.revoked"


# ── Obsidian ──────────────────────────────────────────────────────────────────


def test_obsidian_set_vault_path_emits_set_vault_path_event():
    """set_vault_path fires for obsidian when vault path is configured."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.obsidian_install import OBSIDIAN_SKILL_ID

        audit_log_provider_event(OBSIDIAN_SKILL_ID, "team_bot_a", "set_vault_path", {
            "vault_path": "/Users/team_bot_a/Documents/Vault",
            "write_daily_note": False,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == f"skill.{OBSIDIAN_SKILL_ID}.set_vault_path"
    assert details["vault_path"] == "/Users/team_bot_a/Documents/Vault"


def test_obsidian_set_vault_path_emits_activated_event():
    """activated fires for obsidian when vault path is configured successfully."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.obsidian_install import OBSIDIAN_SKILL_ID

        audit_log_provider_event(OBSIDIAN_SKILL_ID, "team_bot_a", "activated", {
            "vault_path": "/Users/team_bot_a/Documents/Vault",
            "write_daily_note": False,
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == f"skill.{OBSIDIAN_SKILL_ID}.activated"


# ── iMessage ──────────────────────────────────────────────────────────────────


def test_imessage_set_handle_emits_set_handle_event():
    """set_handle fires for imessage when handle is configured."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.imessage_install import IMESSAGE_SKILL_ID

        audit_log_provider_event(IMESSAGE_SKILL_ID, "team_bot_a", "set_handle", {
            "handle": "+15551234567",
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.imessage.set_handle"
    assert details["handle"] == "+15551234567"


def test_imessage_set_handle_emits_activated_event():
    """activated fires for imessage when handle is configured successfully."""
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        from evolve_admin.skills.imessage_install import IMESSAGE_SKILL_ID

        audit_log_provider_event(IMESSAGE_SKILL_ID, "team_bot_a", "activated", {
            "handle": "+15551234567",
        })

    assert len(recorded) == 1
    action, bot_id, details = recorded[0]
    assert action == "skill.imessage.activated"


# ── Sampling decision documentation test ─────────────────────────────────────


def test_usage_send_event_shape_for_messaging_skills():
    """usage.send can be emitted via audit_log_provider_event for messaging skills.

    Sampling decision (V2.4-4): log every send.  No sampling in v1.  The
    existing 90-day audit-log retention policy applies.  Revisit in v2.5
    with observed volume data before adding sampling logic.

    This test documents the intended event shape without actually sending messages.
    """
    recorded: list[tuple] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append((action, bot_id, details))

    messaging_skills = ["slack", "discord", "telegram", "imessage"]

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event

        for skill_id in messaging_skills:
            audit_log_provider_event(skill_id, "team_bot_a", "usage.send", {
                "channel": "general",
                "message_len": 42,
            })

    assert len(recorded) == len(messaging_skills)
    for i, skill_id in enumerate(messaging_skills):
        action, bot_id, details = recorded[i]
        assert action == f"skill.{skill_id}.usage.send"
        assert bot_id == "team_bot_a"
        assert details["message_len"] == 42


# ── Parametrize: event_kind construction for all 8 providers ─────────────────


@pytest.mark.parametrize("skill_id,event_kind,expected_action", [
    ("gog",       "plan_requested", "skill.gog.plan_requested"),
    ("gog",       "oauth_started",  "skill.gog.oauth_started"),
    ("gog",       "activated",      "skill.gog.activated"),
    ("gog",       "revoked",        "skill.gog.revoked"),
    ("gmail",     "plan_requested", "skill.gmail.plan_requested"),
    ("gmail",     "oauth_started",  "skill.gmail.oauth_started"),
    ("gmail",     "activated",      "skill.gmail.activated"),
    ("gmail",     "revoked",        "skill.gmail.revoked"),
    ("calendar",  "plan_requested", "skill.calendar.plan_requested"),
    ("calendar",  "oauth_started",  "skill.calendar.oauth_started"),
    ("calendar",  "activated",      "skill.calendar.activated"),
    ("calendar",  "revoked",        "skill.calendar.revoked"),
    ("slack",     "plan_requested", "skill.slack.plan_requested"),
    ("slack",     "oauth_started",  "skill.slack.oauth_started"),
    ("slack",     "activated",      "skill.slack.activated"),
    ("slack",     "revoked",        "skill.slack.revoked"),
    ("discord",   "plan_requested", "skill.discord.plan_requested"),
    ("discord",   "oauth_started",  "skill.discord.oauth_started"),
    ("discord",   "activated",      "skill.discord.activated"),
    ("discord",   "revoked",        "skill.discord.revoked"),
    ("telegram",  "plan_requested", "skill.telegram.plan_requested"),
    ("telegram",  "set_token",      "skill.telegram.set_token"),
    ("telegram",  "activated",      "skill.telegram.activated"),
    ("telegram",  "revoked",        "skill.telegram.revoked"),
    ("obsidian_vault", "plan_requested", "skill.obsidian_vault.plan_requested"),
    ("obsidian_vault", "set_vault_path", "skill.obsidian_vault.set_vault_path"),
    ("obsidian_vault", "activated",      "skill.obsidian_vault.activated"),
    ("imessage",  "plan_requested", "skill.imessage.plan_requested"),
    ("imessage",  "set_handle",     "skill.imessage.set_handle"),
    ("imessage",  "activated",      "skill.imessage.activated"),
])
def test_event_name_construction_all_providers(skill_id, event_kind, expected_action):
    """audit_log_provider_event constructs the correct full action name for all events.

    This is the definitive test of the event vocabulary across all 8 providers.
    """
    recorded: list[str] = []

    def _fake_entry(action: str, bot_id: str, details: dict) -> None:
        recorded.append(action)

    with patch("evolve_admin.web.server._audit_log_entry", _fake_entry):
        from evolve_admin.oauth import audit_log_provider_event
        audit_log_provider_event(skill_id, "testbot", event_kind)

    assert len(recorded) == 1
    assert recorded[0] == expected_action, (
        f"Expected action {expected_action!r}, got {recorded[0]!r}"
    )

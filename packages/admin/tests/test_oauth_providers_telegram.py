"""
test_oauth_providers_telegram.py — Tests for the Telegram provider implementation.

Verifies that the Telegram provider correctly wraps telegram_install.py and
satisfies the Provider interface contract defined in V2.1-1.

Tests
-----
- test_telegram_provider_satisfies_telegram_integration_id — integration_ids includes "telegram"
- test_telegram_provider_registered_at_import — provider appears in PROVIDER_REGISTRY
- test_telegram_provider_not_slack — Telegram and Slack are distinct providers
- test_telegram_provider_is_satisfied_when_token_valid — mirrors resolve_status
- test_telegram_provider_is_satisfied_false_when_token_missing — no token → False
- test_telegram_provider_action_label_is_plex_friendly — human label, no jargon
- test_telegram_provider_action_url_returns_telegram_route — correct endpoint
- test_telegram_provider_build_missing_item_when_valid — returns None (satisfied)
- test_telegram_provider_build_missing_item_when_missing — returns missing-item dict
- test_telegram_provider_registration_guard — importing twice doesn't double-register
- test_telegram_provider_build_missing_item_display_name_from_req — uses req display_name
- test_telegram_provider_handles_resolve_status_exception — safe fallback on error
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Helper: fake resolve_status results ──────────────────────────────────────

from evolve_admin.skills.telegram_install import InstallStatus  # noqa: E402


def _status_valid(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        bot_token_state="valid",
        bot_username="myevolvebot",
        bot_first_name="My Evolve Bot",
        can_join_groups=True,
        can_read_all_group_messages=False,
    )


def _status_missing(bot_id, **kw):
    return InstallStatus(bot_id=bot_id, bot_token_state="missing")


def _status_revoked(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        bot_token_state="revoked",
        bot_username="myevolvebot",
        error="Unauthorized",
    )


def _status_invalid(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        bot_token_state="invalid",
        error="stored_token_format_invalid",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_telegram_provider_satisfies_telegram_integration_id():
    """The Telegram provider's integration_ids includes 'telegram'."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    telegram_providers = [p for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids]
    assert len(telegram_providers) == 1
    provider = telegram_providers[0]
    assert "telegram" in provider.integration_ids
    assert isinstance(provider.integration_ids, frozenset)


def test_telegram_provider_registered_at_import():
    """Telegram provider appears exactly once in PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    telegram_entries = [p for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids]
    assert len(telegram_entries) == 1

    provider = telegram_entries[0]
    assert provider.skill_id == "telegram"
    assert callable(provider.is_satisfied)
    assert callable(provider.build_missing_item)
    assert callable(provider.action_url)
    assert isinstance(provider.action_label, str)


def test_telegram_provider_not_slack():
    """Telegram and Slack providers are distinct — no shared integration_ids."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    slack = next(p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)
    telegram = next(p for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids)

    assert slack is not telegram
    assert not slack.integration_ids.intersection(telegram.integration_ids), (
        "Slack and Telegram share integration_ids — they should be distinct"
    )


def test_telegram_provider_is_satisfied_when_token_valid():
    """is_satisfied returns True when resolve_status returns bot_token_state=valid."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_is_satisfied
    from evolve_admin.skills import telegram_install

    with patch.object(telegram_install, "resolve_status", side_effect=_status_valid):
        result = _telegram_is_satisfied("admin_bot")
    assert result is True


def test_telegram_provider_is_satisfied_false_when_token_missing():
    """is_satisfied returns False when token is missing."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_is_satisfied
    from evolve_admin.skills import telegram_install

    with patch.object(telegram_install, "resolve_status", side_effect=_status_missing):
        result = _telegram_is_satisfied("admin_bot")
    assert result is False


def test_telegram_provider_is_satisfied_false_when_token_revoked():
    """is_satisfied returns False when token is revoked."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_is_satisfied
    from evolve_admin.skills import telegram_install

    with patch.object(telegram_install, "resolve_status", side_effect=_status_revoked):
        result = _telegram_is_satisfied("admin_bot")
    assert result is False


def test_telegram_provider_is_satisfied_false_when_token_invalid():
    """is_satisfied returns False when token has invalid format."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_is_satisfied
    from evolve_admin.skills import telegram_install

    with patch.object(telegram_install, "resolve_status", side_effect=_status_invalid):
        result = _telegram_is_satisfied("admin_bot")
    assert result is False


def test_telegram_provider_action_label_is_plex_friendly():
    """The action_label is human-readable without OAuth/scope/token jargon."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids)
    label = provider.action_label

    for jargon in ("oauth", "scope", "configure", "client"):
        assert jargon.lower() not in label.lower(), (
            f"action_label contains jargon word '{jargon}': {label!r}"
        )
    assert "Telegram" in label, f"action_label should mention Telegram: {label!r}"


def test_telegram_provider_action_url_returns_telegram_route():
    """action_url(bot_id) returns the Telegram install route."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids)
    assert provider.action_url("admin_bot") == "/api/skills/install/telegram"
    assert provider.action_url("team_bot_a") == "/api/skills/install/telegram"


def test_telegram_provider_build_missing_item_when_valid():
    """build_missing_item returns None when Telegram is satisfied (token valid)."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_build_missing_item
    from evolve_admin.skills import telegram_install

    req = {"id": "telegram", "display_name": "Telegram", "reason": "test reason"}

    with patch.object(telegram_install, "resolve_status", side_effect=_status_valid):
        result = _telegram_build_missing_item("admin_bot", req)
    assert result is None


def test_telegram_provider_build_missing_item_when_missing():
    """build_missing_item returns a missing-item dict when token is missing."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_build_missing_item
    from evolve_admin.skills import telegram_install

    req = {"id": "telegram", "display_name": "Telegram", "reason": "test reason"}

    with patch.object(telegram_install, "resolve_status", side_effect=_status_missing):
        result = _telegram_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["integration_id"] == "telegram"
    assert result["skill_id"] == "telegram"
    assert result["status"] == "missing"
    assert result["action_url"] == "/api/skills/install/telegram"
    assert result["action_label"] == "Set up Telegram bot"
    assert isinstance(result["install_plan_steps"], list)
    assert len(result["install_plan_steps"]) > 0


def test_telegram_provider_build_missing_item_when_revoked():
    """build_missing_item returns revoked status when token is revoked."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_build_missing_item
    from evolve_admin.skills import telegram_install

    req = {"id": "telegram", "display_name": "Telegram", "reason": "test reason"}

    with patch.object(telegram_install, "resolve_status", side_effect=_status_revoked):
        result = _telegram_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "revoked"
    steps = result["install_plan_steps"]
    assert len(steps) > 0
    # Should have a set_token step since user needs to re-enter the key
    step_ids = [s["id"] for s in steps]
    assert "set_token" in step_ids


def test_telegram_provider_registration_guard():
    """Importing telegram_provider twice doesn't add a second entry to PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY
    import importlib
    import evolve_admin.oauth.providers.telegram_provider as _mod

    count_before = sum(1 for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids)

    # Re-running the registration guard logic
    importlib.reload(_mod)

    count_after = sum(1 for p in PROVIDER_REGISTRY if "telegram" in p.integration_ids)
    # Should not exceed 1 (the guard prevents double-registration)
    assert count_after <= 1, (
        f"Double registration detected: {count_after} Telegram providers in registry"
    )


def test_telegram_provider_build_missing_item_display_name_from_req():
    """build_missing_item uses display_name from the manifest req dict."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_build_missing_item
    from evolve_admin.skills import telegram_install

    req = {
        "id": "telegram",
        "display_name": "Telegram (Custom Label)",
        "reason": "messaging required",
    }

    with patch.object(telegram_install, "resolve_status", side_effect=_status_missing):
        result = _telegram_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["display_name"] == "Telegram (Custom Label)"
    assert result["reason"] == "messaging required"


def test_telegram_provider_handles_resolve_status_exception():
    """build_missing_item returns a safe fallback when resolve_status raises."""
    from evolve_admin.oauth.providers.telegram_provider import _telegram_build_missing_item
    from evolve_admin.skills import telegram_install

    def _raise(bot_id, **kw):
        raise RuntimeError("simulated crash")

    req = {"id": "telegram", "display_name": "Telegram", "reason": "messaging"}

    with patch.object(telegram_install, "resolve_status", side_effect=_raise):
        result = _telegram_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "unknown"
    assert result["skill_id"] == "telegram"

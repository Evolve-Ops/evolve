"""
test_oauth_providers_discord.py — Tests for the Discord provider implementation.

Verifies that the Discord provider correctly wraps discord_install.py and
satisfies the Provider interface contract defined in V2.1-1.

Tests
-----
- test_discord_provider_satisfies_discord_integration_id — integration_ids includes "discord"
- test_discord_provider_registered_at_import — provider appears in PROVIDER_REGISTRY
- test_discord_provider_not_slack — Discord and Slack are distinct providers
- test_discord_provider_not_gog — Discord and GOG are distinct providers
- test_discord_provider_is_satisfied_when_token_valid — mirrors resolve_status
- test_discord_provider_is_satisfied_false_when_missing — no config → False
- test_discord_provider_is_satisfied_false_when_credentials_missing — no creds → False
- test_discord_provider_action_label_is_plex_friendly — human label, no jargon
- test_discord_provider_action_url_returns_discord_route — correct endpoint
- test_discord_provider_build_missing_item_when_valid — returns None (satisfied)
- test_discord_provider_build_missing_item_when_missing — returns missing-item dict
- test_discord_provider_build_missing_item_when_credentials_missing — configure step
- test_discord_provider_registration_guard — importing twice doesn't double-register
- test_discord_provider_handles_resolve_status_exception — safe fallback on crash
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

from evolve_admin.skills.discord_install import InstallStatus  # noqa: E402


def _status_valid(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        token_state="valid",
        bot_user_id="123456789012345678",
        bot_username="EvolveBot#1234",
        invite_url="https://discord.com/oauth2/authorize?client_id=C123&...",
        invited_guilds=["987654321098765432"],
    )


def _status_missing(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        token_state="missing",
        invite_url="https://discord.com/oauth2/authorize?client_id=C123&...",
    )


def _status_credentials_missing(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        token_state="credentials_missing",
        error="Pod admin needs to register a Discord app. See docs/skills/discord-setup.md.",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_discord_provider_satisfies_discord_integration_id():
    """The Discord provider's integration_ids includes 'discord'."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    discord_providers = [p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids]
    assert len(discord_providers) == 1
    provider = discord_providers[0]
    assert "discord" in provider.integration_ids
    assert isinstance(provider.integration_ids, frozenset)


def test_discord_provider_registered_at_import():
    """Discord provider appears exactly once in PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    discord_entries = [p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids]
    assert len(discord_entries) == 1

    provider = discord_entries[0]
    assert provider.skill_id == "discord"
    assert callable(provider.is_satisfied)
    assert callable(provider.build_missing_item)
    assert callable(provider.action_url)
    assert isinstance(provider.action_label, str)


def test_discord_provider_not_slack():
    """Discord and Slack providers are distinct — no shared integration_ids."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    slack = next(p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)
    discord = next(p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)

    assert slack is not discord
    assert not slack.integration_ids.intersection(discord.integration_ids), (
        "Slack and Discord share integration_ids — they should be distinct"
    )


def test_discord_provider_not_gog():
    """Discord and GOG providers are distinct — no shared integration_ids."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    gog = next(p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)
    discord = next(p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)

    assert gog is not discord
    assert not gog.integration_ids.intersection(discord.integration_ids), (
        "GOG and Discord share integration_ids — they should be distinct"
    )


def test_discord_provider_is_satisfied_when_token_valid():
    """is_satisfied returns True when resolve_status returns token_state=valid."""
    from evolve_admin.oauth.providers.discord_provider import _discord_is_satisfied
    from evolve_admin.skills import discord_install

    with patch.object(discord_install, "resolve_status", side_effect=_status_valid):
        result = _discord_is_satisfied("admin_bot")
    assert result is True


def test_discord_provider_is_satisfied_false_when_missing():
    """is_satisfied returns False when no per-bot config."""
    from evolve_admin.oauth.providers.discord_provider import _discord_is_satisfied
    from evolve_admin.skills import discord_install

    with patch.object(discord_install, "resolve_status", side_effect=_status_missing):
        result = _discord_is_satisfied("admin_bot")
    assert result is False


def test_discord_provider_is_satisfied_false_when_credentials_missing():
    """is_satisfied returns False when pod credentials missing."""
    from evolve_admin.oauth.providers.discord_provider import _discord_is_satisfied
    from evolve_admin.skills import discord_install

    with patch.object(discord_install, "resolve_status", side_effect=_status_credentials_missing):
        result = _discord_is_satisfied("admin_bot")
    assert result is False


def test_discord_provider_action_label_is_plex_friendly():
    """The action_label is human-readable without OAuth/scope/token jargon."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)
    label = provider.action_label

    for jargon in ("oauth", "scope", "token", "configure", "client"):
        assert jargon.lower() not in label.lower(), (
            f"action_label contains jargon word '{jargon}': {label!r}"
        )
    assert "Discord" in label, f"action_label should mention Discord: {label!r}"


def test_discord_provider_action_url_returns_discord_route():
    """action_url(bot_id) returns the Discord install route."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)
    assert provider.action_url("admin_bot") == "/api/skills/install/discord"
    assert provider.action_url("team_bot_a") == "/api/skills/install/discord"


def test_discord_provider_build_missing_item_when_valid():
    """build_missing_item returns None when Discord is satisfied (token valid)."""
    from evolve_admin.oauth.providers.discord_provider import _discord_build_missing_item
    from evolve_admin.skills import discord_install

    req = {"id": "discord", "display_name": "Discord", "reason": "test reason"}

    with patch.object(discord_install, "resolve_status", side_effect=_status_valid):
        result = _discord_build_missing_item("admin_bot", req)
    assert result is None


def test_discord_provider_build_missing_item_when_missing():
    """build_missing_item returns a missing-item dict when config is missing."""
    from evolve_admin.oauth.providers.discord_provider import _discord_build_missing_item
    from evolve_admin.skills import discord_install

    req = {"id": "discord", "display_name": "Discord", "reason": "test reason"}

    with patch.object(discord_install, "resolve_status", side_effect=_status_missing):
        result = _discord_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["integration_id"] == "discord"
    assert result["skill_id"] == "discord"
    assert result["status"] == "missing"
    assert result["action_url"] == "/api/skills/install/discord"
    assert result["action_label"] == "Set up Discord server"
    assert isinstance(result["install_plan_steps"], list)
    assert len(result["install_plan_steps"]) > 0


def test_discord_provider_build_missing_item_when_credentials_missing():
    """build_missing_item returns configure_credentials step when credentials missing."""
    from evolve_admin.oauth.providers.discord_provider import _discord_build_missing_item
    from evolve_admin.skills import discord_install

    req = {"id": "discord", "display_name": "Discord", "reason": "test reason"}

    with patch.object(discord_install, "resolve_status", side_effect=_status_credentials_missing):
        result = _discord_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "credentials_missing"
    steps = result["install_plan_steps"]
    assert len(steps) == 1
    assert steps[0]["id"] == "configure_credentials"


def test_discord_provider_registration_guard():
    """Importing discord_provider twice doesn't add a second entry to PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY
    import importlib
    import evolve_admin.oauth.providers.discord_provider as _mod

    count_before = sum(1 for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)

    # Re-running the registration guard logic
    importlib.reload(_mod)

    count_after = sum(1 for p in PROVIDER_REGISTRY if "discord" in p.integration_ids)
    # Should not exceed 1 (the guard prevents double-registration)
    assert count_after <= 1, (
        f"Double registration detected: {count_after} Discord providers in registry"
    )


def test_discord_provider_build_missing_item_display_name_from_req():
    """build_missing_item uses display_name from the manifest req dict."""
    from evolve_admin.oauth.providers.discord_provider import _discord_build_missing_item
    from evolve_admin.skills import discord_install

    req = {
        "id": "discord",
        "display_name": "Discord (Custom Label)",
        "reason": "messaging required",
    }

    with patch.object(discord_install, "resolve_status", side_effect=_status_missing):
        result = _discord_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["display_name"] == "Discord (Custom Label)"
    assert result["reason"] == "messaging required"


def test_discord_provider_handles_resolve_status_exception():
    """build_missing_item returns a safe fallback when resolve_status raises."""
    from evolve_admin.oauth.providers.discord_provider import _discord_build_missing_item
    from evolve_admin.skills import discord_install

    def _raise(bot_id, **kw):
        raise RuntimeError("simulated crash")

    req = {"id": "discord", "display_name": "Discord", "reason": "messaging"}

    with patch.object(discord_install, "resolve_status", side_effect=_raise):
        result = _discord_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "unknown"
    assert result["skill_id"] == "discord"

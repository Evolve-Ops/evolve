"""
test_oauth_providers_slack.py — Tests for the Slack provider implementation.

Verifies that the Slack provider correctly wraps slack_install.py and satisfies
the Provider interface contract defined in V2.1-1.

Tests
-----
- test_slack_provider_satisfies_slack_integration_id — integration_ids includes "slack"
- test_slack_provider_registered_at_import — provider appears in PROVIDER_REGISTRY
- test_slack_provider_not_gog — Slack and GOG are distinct providers
- test_slack_provider_is_satisfied_when_token_valid — mirrors resolve_status
- test_slack_provider_is_satisfied_false_when_token_missing — no token → False
- test_slack_provider_action_label_is_plex_friendly — human label, no jargon
- test_slack_provider_action_url_returns_slack_route — correct endpoint
- test_slack_provider_build_missing_item_when_valid — returns None (satisfied)
- test_slack_provider_build_missing_item_when_missing — returns missing-item dict
- test_slack_provider_registration_guard — importing twice doesn't double-register
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

from evolve_admin.skills.slack_install import InstallStatus  # noqa: E402


def _status_valid(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        token_state="valid",
        workspace_id="T12345",
        workspace_name="Test Workspace",
        bot_user_id="U99999",
        scopes_granted=["chat:write", "channels:read"],
    )


def _status_missing(bot_id, **kw):
    return InstallStatus(bot_id=bot_id, token_state="missing")


def _status_credentials_missing(bot_id, **kw):
    return InstallStatus(
        bot_id=bot_id,
        token_state="credentials_missing",
        error="Pod admin needs to register a Slack app. See docs/skills/slack-setup.md.",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_slack_provider_satisfies_slack_integration_id():
    """The Slack provider's integration_ids includes 'slack'."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    slack_providers = [p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids]
    assert len(slack_providers) == 1
    provider = slack_providers[0]
    assert "slack" in provider.integration_ids
    assert isinstance(provider.integration_ids, frozenset)


def test_slack_provider_registered_at_import():
    """Slack provider appears exactly once in PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    slack_entries = [p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids]
    assert len(slack_entries) == 1

    provider = slack_entries[0]
    assert provider.skill_id == "slack"
    assert callable(provider.is_satisfied)
    assert callable(provider.build_missing_item)
    assert callable(provider.action_url)
    assert isinstance(provider.action_label, str)


def test_slack_provider_not_gog():
    """Slack and GOG providers are distinct — no shared integration_ids."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    gog = next(p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)
    slack = next(p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)

    assert gog is not slack
    assert not gog.integration_ids.intersection(slack.integration_ids), (
        "GOG and Slack share integration_ids — they should be distinct"
    )


def test_slack_provider_is_satisfied_when_token_valid():
    """is_satisfied returns True when resolve_status returns token_state=valid."""
    from evolve_admin.oauth.providers.slack_provider import _slack_is_satisfied
    from evolve_admin.skills import slack_install

    with patch.object(slack_install, "resolve_status", side_effect=_status_valid):
        result = _slack_is_satisfied("admin_bot")
    assert result is True


def test_slack_provider_is_satisfied_false_when_token_missing():
    """is_satisfied returns False when token is missing."""
    from evolve_admin.oauth.providers.slack_provider import _slack_is_satisfied
    from evolve_admin.skills import slack_install

    with patch.object(slack_install, "resolve_status", side_effect=_status_missing):
        result = _slack_is_satisfied("admin_bot")
    assert result is False


def test_slack_provider_is_satisfied_false_when_credentials_missing():
    """is_satisfied returns False when pod credentials missing."""
    from evolve_admin.oauth.providers.slack_provider import _slack_is_satisfied
    from evolve_admin.skills import slack_install

    with patch.object(slack_install, "resolve_status", side_effect=_status_credentials_missing):
        result = _slack_is_satisfied("admin_bot")
    assert result is False


def test_slack_provider_action_label_is_plex_friendly():
    """The action_label is human-readable without OAuth/scope/token jargon."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)
    label = provider.action_label

    for jargon in ("oauth", "scope", "token", "configure", "client"):
        assert jargon.lower() not in label.lower(), (
            f"action_label contains jargon word '{jargon}': {label!r}"
        )
    assert "Slack" in label, f"action_label should mention Slack: {label!r}"


def test_slack_provider_action_url_returns_slack_route():
    """action_url(bot_id) returns the Slack install route."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)
    assert provider.action_url("admin_bot") == "/api/skills/install/slack"
    assert provider.action_url("team_bot_a") == "/api/skills/install/slack"


def test_slack_provider_build_missing_item_when_valid():
    """build_missing_item returns None when Slack is satisfied (token valid)."""
    from evolve_admin.oauth.providers.slack_provider import _slack_build_missing_item
    from evolve_admin.skills import slack_install

    req = {"id": "slack", "display_name": "Slack", "reason": "test reason"}

    with patch.object(slack_install, "resolve_status", side_effect=_status_valid):
        result = _slack_build_missing_item("admin_bot", req)
    assert result is None


def test_slack_provider_build_missing_item_when_missing():
    """build_missing_item returns a missing-item dict when token is missing."""
    from evolve_admin.oauth.providers.slack_provider import _slack_build_missing_item
    from evolve_admin.skills import slack_install

    req = {"id": "slack", "display_name": "Slack", "reason": "test reason"}

    with patch.object(slack_install, "resolve_status", side_effect=_status_missing):
        result = _slack_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["integration_id"] == "slack"
    assert result["skill_id"] == "slack"
    assert result["status"] == "missing"
    assert result["action_url"] == "/api/skills/install/slack"
    assert result["action_label"] == "Set up Slack workspace"
    assert isinstance(result["install_plan_steps"], list)
    assert len(result["install_plan_steps"]) > 0


def test_slack_provider_build_missing_item_when_credentials_missing():
    """build_missing_item returns configure_credentials step when credentials missing."""
    from evolve_admin.oauth.providers.slack_provider import _slack_build_missing_item
    from evolve_admin.skills import slack_install

    req = {"id": "slack", "display_name": "Slack", "reason": "test reason"}

    with patch.object(slack_install, "resolve_status", side_effect=_status_credentials_missing):
        result = _slack_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "credentials_missing"
    steps = result["install_plan_steps"]
    assert len(steps) == 1
    assert steps[0]["id"] == "configure_credentials"


def test_slack_provider_registration_guard():
    """Importing slack_provider twice doesn't add a second entry to PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY
    import importlib
    import evolve_admin.oauth.providers.slack_provider as _mod

    count_before = sum(1 for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)

    # Re-running the registration guard logic
    importlib.reload(_mod)

    count_after = sum(1 for p in PROVIDER_REGISTRY if "slack" in p.integration_ids)
    # Should not exceed 1 (the guard prevents double-registration)
    assert count_after <= 1, (
        f"Double registration detected: {count_after} Slack providers in registry"
    )


def test_slack_provider_build_missing_item_display_name_from_req():
    """build_missing_item uses display_name from the manifest req dict."""
    from evolve_admin.oauth.providers.slack_provider import _slack_build_missing_item
    from evolve_admin.skills import slack_install

    req = {
        "id": "slack",
        "display_name": "Slack (Custom Label)",
        "reason": "messaging required",
    }

    with patch.object(slack_install, "resolve_status", side_effect=_status_missing):
        result = _slack_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["display_name"] == "Slack (Custom Label)"
    assert result["reason"] == "messaging required"


def test_slack_provider_handles_resolve_status_exception():
    """build_missing_item returns a safe fallback when resolve_status raises."""
    from evolve_admin.oauth.providers.slack_provider import _slack_build_missing_item
    from evolve_admin.skills import slack_install

    def _raise(bot_id, **kw):
        raise RuntimeError("simulated crash")

    req = {"id": "slack", "display_name": "Slack", "reason": "messaging"}

    with patch.object(slack_install, "resolve_status", side_effect=_raise):
        result = _slack_build_missing_item("admin_bot", req)

    assert result is not None
    assert result["status"] == "unknown"
    assert result["skill_id"] == "slack"

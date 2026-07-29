"""
test_oauth_providers_gog.py — Tests for the gog_provider implementation.

Verifies that the GOG provider correctly wraps gog_install.py and satisfies
the Provider interface contract.

Tests
-----
- test_gog_provider_satisfies_gmail_and_calendar — integration_ids includes "gog"
- test_gog_provider_is_satisfied_mirrors_gog_install — same logic as today
- test_gog_provider_action_label_is_plex_friendly — human-readable label, no jargon
- test_gog_provider_action_url_returns_gog_route — correct endpoint
- test_gog_provider_build_missing_item_when_active — returns None (satisfied)
- test_gog_provider_build_missing_item_when_oauth_pending — returns missing item
- test_gog_provider_registered_in_registry — provider appears in PROVIDER_REGISTRY
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Reader helpers ─────────────────────────────────────────────────────────────


def _active_readers():
    return (
        lambda bot_id: True,
        lambda bot_id: {"status": "active", "google_account": "test@example.com",
                        "services": ["gmail_readonly", "calendar_readonly"]},
        lambda: True,
    )


def _missing_readers():
    return (
        lambda bot_id: True,   # plugin enabled
        lambda bot_id: None,   # no OAuth profile → oauth_pending
        lambda: True,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_gog_provider_satisfies_gmail_and_calendar():
    """The GOG provider's integration_ids includes 'gog'."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    gog_providers = [p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids]
    assert len(gog_providers) == 1
    provider = gog_providers[0]
    assert "gog" in provider.integration_ids
    # The integration_ids frozenset maps to the manifest's requirements.integrations[].id value
    # which is "gog" for Gmail+Calendar — both services are handled by this one provider.
    assert isinstance(provider.integration_ids, frozenset)


def test_gog_provider_is_satisfied_mirrors_gog_install(tmp_path):
    """gog_provider._gog_is_satisfied returns the same result as gog_install.resolve_status."""
    from evolve_admin.oauth.providers.gog_provider import _gog_is_satisfied
    from evolve_admin.skills.gog_install import resolve_status

    plugin_fn, profile_fn, client_fn = _active_readers()

    # Provider says satisfied
    provider_result = _gog_is_satisfied(
        "admin_bot",
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )

    # gog_install.resolve_status says active
    gog_status = resolve_status(
        "admin_bot",
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )

    assert provider_result is True
    assert gog_status.status == "active"
    assert provider_result == (gog_status.status == "active")


def test_gog_provider_is_satisfied_false_when_missing(tmp_path):
    """_gog_is_satisfied returns False when OAuth profile missing."""
    from evolve_admin.oauth.providers.gog_provider import _gog_is_satisfied

    plugin_fn, profile_fn, client_fn = _missing_readers()
    result = _gog_is_satisfied(
        "admin_bot",
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result is False


def test_gog_provider_action_label_is_plex_friendly():
    """The action_label is human-readable without OAuth/scope jargon."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)

    label = provider.action_label
    # Must be friendly: no jargon words
    for jargon in ("oauth", "scope", "configure", "client", "token"):
        assert jargon.lower() not in label.lower(), (
            f"action_label contains jargon word '{jargon}': {label!r}"
        )
    # Must mention Gmail or Calendar (concrete, recognisable)
    assert any(word in label for word in ("Gmail", "Calendar", "Google")), (
        f"action_label should mention Gmail, Calendar, or Google: {label!r}"
    )


def test_gog_provider_action_url_returns_gog_route():
    """action_url(bot_id) returns the GOG install route."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    provider = next(p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids)
    assert provider.action_url("admin_bot") == "/api/skills/install/gog"
    assert provider.action_url("team_bot_a") == "/api/skills/install/gog"


def test_gog_provider_build_missing_item_when_active():
    """build_missing_item returns None when GOG is active (satisfied)."""
    from evolve_admin.oauth.providers.gog_provider import _gog_build_missing_item

    plugin_fn, profile_fn, client_fn = _active_readers()
    req = {"id": "gog", "display_name": "Google (GOG)", "reason": "test reason"}

    result = _gog_build_missing_item(
        "admin_bot", req,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result is None


def test_gog_provider_build_missing_item_when_oauth_pending():
    """build_missing_item returns a missing-item dict when GOG is oauth_pending."""
    from evolve_admin.oauth.providers.gog_provider import _gog_build_missing_item

    plugin_fn, profile_fn, client_fn = _missing_readers()
    req = {"id": "gog", "display_name": "Google (GOG)", "reason": "test reason"}

    result = _gog_build_missing_item(
        "admin_bot", req,
        read_plugin_enabled=plugin_fn,
        read_oauth_profile=profile_fn,
        read_oauth_client_configured=client_fn,
    )
    assert result is not None
    assert result["integration_id"] == "gog"
    assert result["skill_id"] == "gog"
    assert result["status"] == "oauth_pending"
    assert result["action_url"] == "/api/skills/install/gog"
    assert result["action_label"] == "Set up Gmail & Calendar"
    assert isinstance(result["install_plan_steps"], list)
    assert len(result["install_plan_steps"]) > 0


def test_gog_provider_registered_in_registry():
    """GOG provider appears exactly once in PROVIDER_REGISTRY."""
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY

    gog_entries = [p for p in PROVIDER_REGISTRY if "gog" in p.integration_ids]
    assert len(gog_entries) == 1

    provider = gog_entries[0]
    assert provider.skill_id == "gog"
    assert callable(provider.is_satisfied)
    assert callable(provider.build_missing_item)
    assert callable(provider.action_url)
    assert isinstance(provider.action_label, str)

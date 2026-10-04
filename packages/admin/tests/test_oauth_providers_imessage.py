"""tests/test_oauth_providers_imessage.py — iMessage provider registry (V2.1-6 Task 4).

Tests:
  - imessage_provider is registered in PROVIDER_REGISTRY at import time.
  - find_provider("imessage") returns the iMessage provider.
  - Provider.is_satisfied delegates to imessage_install.resolve_status.
  - Provider.build_missing_item returns None when satisfied; returns a
    missing-item dict with the correct shape when not.
  - Provider.action_url returns the correct endpoint.
  - No double-registration if the module is re-imported.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for _path in (str(_ADMIN_DIR),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.oauth.providers import PROVIDER_REGISTRY, find_provider  # noqa: E402
from evolve_admin.skills.imessage_install import (  # noqa: E402
    IMESSAGE_SKILL_ID,
    InstallStatus,
)


# ── Registry inclusion ────────────────────────────────────────────────────────


class TestRegistryInclusion:
    def test_imessage_in_provider_registry(self):
        """imessage_provider must have registered itself at import time."""
        ids = [
            integration_id
            for p in PROVIDER_REGISTRY
            for integration_id in p.integration_ids
        ]
        assert "imessage" in ids

    def test_find_provider_returns_imessage(self):
        provider = find_provider("imessage")
        assert provider is not None
        assert provider.skill_id == IMESSAGE_SKILL_ID

    def test_no_double_registration(self):
        """Re-importing imessage_provider must not duplicate the registry entry."""
        count_before = sum(1 for p in PROVIDER_REGISTRY if "imessage" in p.integration_ids)
        # Force re-import
        import importlib
        import evolve_admin.oauth.providers.imessage_provider as _mod
        importlib.reload(_mod)
        count_after = sum(1 for p in PROVIDER_REGISTRY if "imessage" in p.integration_ids)
        assert count_after == count_before


# ── Provider.is_satisfied ─────────────────────────────────────────────────────


class TestProviderIsSatisfied:
    def _get_provider(self):
        return find_provider("imessage")

    def test_returns_true_when_active(self):
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "active"
        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            result = provider.is_satisfied("testbot")
        assert result is True

    def test_returns_false_when_not_active(self):
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "no_tcc_fda"
        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            result = provider.is_satisfied("testbot")
        assert result is False

    def test_returns_false_on_exception(self):
        provider = self._get_provider()
        with patch(
            "evolve_admin.skills.imessage_install.resolve_status",
            side_effect=RuntimeError("unexpected"),
        ):
            result = provider.is_satisfied("testbot")
        assert result is False


# ── Provider.build_missing_item ───────────────────────────────────────────────


class TestBuildMissingItem:
    def _get_provider(self):
        return find_provider("imessage")

    def test_returns_none_when_active(self):
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "active"

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch("evolve_admin.skills.imessage_install.build_install_plan", return_value=[]):
                item = provider.build_missing_item("testbot", {"id": "imessage"})

        assert item is None

    def test_returns_dict_when_not_satisfied(self):
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "no_tcc_fda"

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch("evolve_admin.skills.imessage_install.build_install_plan", return_value=[]):
                item = provider.build_missing_item("testbot", {
                    "id": "imessage",
                    "display_name": "iMessage",
                    "reason": "Required for messaging",
                })

        assert item is not None
        assert item["integration_id"] == IMESSAGE_SKILL_ID
        assert item["skill_id"] == IMESSAGE_SKILL_ID
        assert item["status"] == "no_tcc_fda"
        assert item["action_url"] == f"/api/skills/install/{IMESSAGE_SKILL_ID}"
        assert "install_plan_steps" in item

    def test_missing_item_uses_req_display_name(self):
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "handle_not_configured"

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch("evolve_admin.skills.imessage_install.build_install_plan", return_value=[]):
                item = provider.build_missing_item("testbot", {
                    "id": "imessage",
                    "display_name": "iMessage Messaging",
                    "reason": "Needed for client communication",
                })

        assert item["display_name"] == "iMessage Messaging"
        assert item["reason"] == "Needed for client communication"

    def test_returns_unknown_on_exception(self):
        provider = self._get_provider()
        with patch(
            "evolve_admin.skills.imessage_install.resolve_status",
            side_effect=RuntimeError("crash"),
        ):
            item = provider.build_missing_item("testbot", {"id": "imessage"})

        assert item is not None
        assert item["status"] == "unknown"
        assert item["action_url"] is not None

    def test_install_plan_steps_in_item(self):
        """install_plan_steps must be present (list, possibly empty)."""
        provider = self._get_provider()
        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "no_tcc_fda"

        mock_step = MagicMock()
        mock_step.to_dict.return_value = {"id": "grant_fda", "label": "Grant FDA"}

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch(
                "evolve_admin.skills.imessage_install.build_install_plan",
                return_value=[mock_step],
            ):
                item = provider.build_missing_item("testbot", {"id": "imessage"})

        assert isinstance(item["install_plan_steps"], list)
        assert len(item["install_plan_steps"]) == 1
        assert item["install_plan_steps"][0]["id"] == "grant_fda"


# ── Provider.action_url ───────────────────────────────────────────────────────


class TestActionUrl:
    def test_action_url_format(self):
        provider = find_provider("imessage")
        url = provider.action_url("testbot")
        assert url == f"/api/skills/install/{IMESSAGE_SKILL_ID}"

    def test_action_url_ignores_bot_id(self):
        """action_url is bot-agnostic (same URL for all bots)."""
        provider = find_provider("imessage")
        assert provider.action_url("team_bot_a") == provider.action_url("admin_bot")


# ── Orchestrator integration ──────────────────────────────────────────────────


class TestOrchestratorIntegration:
    """Ensure iMessage integrates with evaluate_install_prerequisites correctly."""

    def test_unsatisfied_imessage_in_prerequisites(self):
        from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "no_tcc_fda"

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch("evolve_admin.skills.imessage_install.build_install_plan", return_value=[]):
                result = evaluate_install_prerequisites(
                    "testbot",
                    {"integrations": [{"id": "imessage", "display_name": "iMessage"}]},
                    shared_dir=Path("/tmp"),
                )

        assert result["satisfied"] is False
        assert len(result["missing"]) == 1
        assert result["missing"][0]["integration_id"] == IMESSAGE_SKILL_ID

    def test_satisfied_imessage_returns_satisfied(self):
        from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites

        mock_status = MagicMock(spec=InstallStatus)
        mock_status.status = "active"

        with patch("evolve_admin.skills.imessage_install.resolve_status", return_value=mock_status):
            with patch("evolve_admin.skills.imessage_install.build_install_plan", return_value=[]):
                result = evaluate_install_prerequisites(
                    "testbot",
                    {"integrations": [{"id": "imessage"}]},
                    shared_dir=Path("/tmp"),
                )

        assert result["satisfied"] is True
        assert result["missing"] == []

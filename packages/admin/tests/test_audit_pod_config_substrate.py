"""Tests for substrate audit slices in audit_pod_config (Workstream B-skills)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


def test_default_pod_config_includes_skill_audit_block() -> None:
    from evolve_admin.applications.audit_pod_config import default_pod_config
    cfg = default_pod_config()
    assert "skill_audit" in cfg
    assert cfg["skill_audit"]["default_cadence"] == "weekly"
    assert cfg["skill_audit"]["calibration_mode"] is True


def test_default_pod_config_includes_provider_audit_block() -> None:
    from evolve_admin.applications.audit_pod_config import default_pod_config
    cfg = default_pod_config()
    assert "provider_audit" in cfg
    assert cfg["provider_audit"]["default_cadence"] == "weekly"
    assert cfg["provider_audit"]["calibration_mode"] is True


def test_render_pod_config_propagates_skill_audit_settings() -> None:
    from evolve_admin.applications.audit_pod_config import render_pod_config
    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "skill_audit": {
            "default_cadence": "daily",
            "calibration_mode": False,
            "ceilings": {"max_proposals_per_run": 10},
        },
    }
    rendered = render_pod_config(network, "team_bot_a")
    assert rendered["skill_audit"]["default_cadence"] == "daily"
    assert rendered["skill_audit"]["calibration_mode"] is False
    assert rendered["skill_audit"]["ceilings"]["max_proposals_per_run"] == 10


def test_render_pod_config_per_bot_skill_cadence_override() -> None:
    from evolve_admin.applications.audit_pod_config import render_pod_config
    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot"}},
        "skill_audit": {
            "default_cadence": "weekly",
            "bot_cadence": {"team_bot_a": "daily"},
        },
    }
    team_bot_a_cfg = render_pod_config(network, "team_bot_a")
    admin_bot_cfg = render_pod_config(network, "admin_bot")
    assert team_bot_a_cfg["skill_audit"]["default_cadence"] == "daily"
    assert admin_bot_cfg["skill_audit"]["default_cadence"] == "weekly"


def test_render_pod_config_invalid_cadence_falls_back_to_weekly() -> None:
    from evolve_admin.applications.audit_pod_config import render_pod_config
    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "skill_audit": {"default_cadence": "yearly"},   # invalid
    }
    rendered = render_pod_config(network, "team_bot_a")
    assert rendered["skill_audit"]["default_cadence"] == "weekly"


def test_render_pod_config_provider_audit_block_present() -> None:
    from evolve_admin.applications.audit_pod_config import render_pod_config
    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "provider_audit": {"default_cadence": "monthly"},
    }
    rendered = render_pod_config(network, "team_bot_a")
    assert rendered["provider_audit"]["default_cadence"] == "monthly"

"""Tests for audit_pod_config — admin-side helper that syncs network.json's
app_audit slice into each bot's /Users/<bot>/.openclaw/workspace/evolve/pod_config.json.

We don't test the sudo /tmp staging path here — that needs real sudoers.
We test the rendering + idempotency + per-bot cadence resolution paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import audit_pod_config  # noqa: E402


# ── render_pod_config ───────────────────────────────────────────────────────


def test_render_uses_pod_default_when_bot_has_no_override() -> None:
    network = {
        "app_audit": {
            "default_cadence": "weekly",
            "bot_cadence": {},
            "calibration_mode": True,
            "audit_on_critical_structural": True,
            "tier3_tier": "tier2",
            "ceilings": {
                "max_auto_fix_per_run": 3,
                "max_proposals_per_run": 5,
                "max_tokens_per_audit": 100_000,
            },
        },
    }
    cfg = audit_pod_config.render_pod_config(network, "team_bot_a")
    assert cfg["audit"]["cadence"] == "weekly"


def test_render_honors_per_bot_override() -> None:
    network = {
        "app_audit": {
            "default_cadence": "monthly",
            "bot_cadence": {"team_bot_a": "daily", "admin_bot": "weekly"},
        },
    }
    assert audit_pod_config.render_pod_config(network, "team_bot_a")["audit"]["cadence"] == "daily"
    assert audit_pod_config.render_pod_config(network, "admin_bot")["audit"]["cadence"] == "weekly"
    assert audit_pod_config.render_pod_config(network, "other")["audit"]["cadence"] == "monthly"


def test_render_falls_back_to_monthly_when_unconfigured() -> None:
    """Empty / missing app_audit block uses the built-in default."""
    cfg = audit_pod_config.render_pod_config({}, "team_bot_a")
    assert cfg["audit"]["cadence"] == "monthly"
    assert cfg["audit"]["calibration_mode"] is True
    assert cfg["audit"]["ceilings"]["max_tokens_per_audit"] == 100_000


def test_render_carries_calibration_and_ceilings() -> None:
    network = {
        "app_audit": {
            "default_cadence": "monthly",
            "calibration_mode": False,
            "audit_on_critical_structural": False,
            "tier3_tier": "tier1",
            "ceilings": {
                "max_auto_fix_per_run": 1,
                "max_proposals_per_run": 2,
                "max_tokens_per_audit": 50_000,
            },
        },
    }
    cfg = audit_pod_config.render_pod_config(network, "team_bot_a")
    assert cfg["audit"]["calibration_mode"] is False
    assert cfg["audit"]["audit_on_critical_structural"] is False
    assert cfg["audit"]["tier3_tier"] == "tier1"
    assert cfg["audit"]["ceilings"]["max_tokens_per_audit"] == 50_000


def test_default_pod_config_matches_render_with_empty_network() -> None:
    """The default fallback the runner uses when pod_config.json is missing
    matches what an empty-config render produces (modulo bot-id resolution)."""
    default = audit_pod_config.default_pod_config()
    rendered = audit_pod_config.render_pod_config({}, "team_bot_a")
    assert default["audit"]["cadence"] == rendered["audit"]["cadence"]
    assert default["audit"]["calibration_mode"] == rendered["audit"]["calibration_mode"]
    assert default["audit"]["ceilings"] == rendered["audit"]["ceilings"]


# ── write_pod_config — happy path + idempotency ────────────────────────────


def test_write_pod_config_creates_file(tmp_path: Path, monkeypatch) -> None:
    """Direct write succeeds when the target dir is writable; idempotency
    re-skips the write."""
    monkeypatch.setattr(
        audit_pod_config, "pod_config_path",
        lambda bot_user: tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
                          / "evolve" / "pod_config.json",
    )
    network = {"app_audit": {"default_cadence": "weekly"}}

    ok = audit_pod_config.write_pod_config(network, "team_bot_a", "team_bot_a")
    assert ok is True
    path = (tmp_path / "Users/team_bot_a/.openclaw/workspace/evolve/pod_config.json")
    data = json.loads(path.read_text())
    assert data["audit"]["cadence"] == "weekly"


def test_write_pod_config_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Re-writing the same content shouldn't change mtime."""
    monkeypatch.setattr(
        audit_pod_config, "pod_config_path",
        lambda bot_user: tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
                          / "evolve" / "pod_config.json",
    )
    network = {"app_audit": {"default_cadence": "weekly"}}
    audit_pod_config.write_pod_config(network, "team_bot_a", "team_bot_a")
    path = (tmp_path / "Users/team_bot_a/.openclaw/workspace/evolve/pod_config.json")
    first_mtime = path.stat().st_mtime_ns

    # Sleep tiny bit then re-write with same content
    import time
    time.sleep(0.01)
    audit_pod_config.write_pod_config(network, "team_bot_a", "team_bot_a")
    second_mtime = path.stat().st_mtime_ns
    assert first_mtime == second_mtime, "idempotent write should not bump mtime"


# ── sync_all_pods ───────────────────────────────────────────────────────────


# ── Infra-audit block (Workstream B-infra) ──────────────────────────────────


def test_render_includes_infra_audit_block() -> None:
    """A default network ships with the infra_audit block in pod_config."""
    cfg = audit_pod_config.render_pod_config({}, "team_bot_a")
    assert "infra_audit" in cfg
    assert cfg["infra_audit"]["default_cadence"] == "daily"
    assert cfg["infra_audit"]["calibration_mode"] is True
    assert "ceilings" in cfg["infra_audit"]


def test_render_carries_infra_audit_overrides() -> None:
    """Operator-set infra_audit values flow through to pod_config."""
    network = {
        "infra_audit": {
            "default_cadence": "weekly",
            "calibration_mode": False,
            "element_overrides": {"acls": "off"},
            "ceilings": {"max_proposals_per_run": 2},
        },
    }
    cfg = audit_pod_config.render_pod_config(network, "team_bot_a")
    assert cfg["infra_audit"]["default_cadence"] == "weekly"
    assert cfg["infra_audit"]["calibration_mode"] is False
    assert cfg["infra_audit"]["element_overrides"] == {"acls": "off"}
    assert cfg["infra_audit"]["ceilings"]["max_proposals_per_run"] == 2


def test_render_infra_cadence_validation() -> None:
    """Garbage cadence falls back to daily."""
    network = {"infra_audit": {"default_cadence": "yearly"}}
    cfg = audit_pod_config.render_pod_config(network, "team_bot_a")
    assert cfg["infra_audit"]["default_cadence"] == "daily"


def test_default_pod_config_includes_infra_audit() -> None:
    """The runner's fallback default carries the infra_audit block."""
    default = audit_pod_config.default_pod_config()
    assert "infra_audit" in default
    assert default["infra_audit"]["default_cadence"] == "daily"


# ── Sync-all-pods ──────────────────────────────────────────────────────────


def test_sync_all_pods_visits_every_bot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        audit_pod_config, "pod_config_path",
        lambda bot_user: tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
                          / "evolve" / "pod_config.json",
    )
    network = {
        "app_audit": {"default_cadence": "monthly", "bot_cadence": {"team_bot_a": "weekly"}},
        "bots": {
            "team_bot_a":   {"user": "team_bot_a"},
            "admin_bot": {"user": "admin_bot"},
            "team_bot_b":  {"user": "personal_bot_user"},   # logical bot vs macOS user
        },
    }
    results = audit_pod_config.sync_all_pods(network)
    assert results == {"team_bot_a": True, "admin_bot": True, "team_bot_b": True}
    # team_bot_b's file should live under the personal_bot_user macOS user
    p_team_bot_b = tmp_path / "Users/personal_bot_user/.openclaw/workspace/evolve/pod_config.json"
    assert p_team_bot_b.exists()
    # And carry the pod default cadence (not the team_bot_a-specific weekly)
    data = json.loads(p_team_bot_b.read_text())
    assert data["audit"]["cadence"] == "monthly"
    # Team_bot_a's per-bot override is applied
    p_team_bot_a = tmp_path / "Users/team_bot_a/.openclaw/workspace/evolve/pod_config.json"
    data_team_bot_a = json.loads(p_team_bot_a.read_text())
    assert data_team_bot_a["audit"]["cadence"] == "weekly"


def test_sync_all_pods_skips_planned_non_member_bots(
    tmp_path: Path, monkeypatch
) -> None:
    """A bots entry without pod membership (e.g. an `evo add-bot`
    purpose{} block for a planned, not-yet-created bot) must not trigger
    a pod_config write — there's no home directory to write into, and
    attempting one sudo-mkdirs a phantom /Users/<bot>."""
    monkeypatch.setattr(
        audit_pod_config, "pod_config_path",
        lambda bot_user: tmp_path / "Users" / bot_user / ".openclaw" / "workspace"
                          / "evolve" / "pod_config.json",
    )
    network = {
        "members": ["team_bot_a"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "nova": {"purpose": {"archetype": "research-analyst",
                                 "mission": "Watch things.",
                                 "captured": "declared",
                                 "confidence": 1.0}},
        },
    }
    results = audit_pod_config.sync_all_pods(network)
    assert results == {"team_bot_a": True}
    assert not (tmp_path / "Users/nova").exists()

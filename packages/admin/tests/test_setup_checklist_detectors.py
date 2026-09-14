"""Unit tests for each Setup-checklist detector.

Each detector is tested in isolation against a controlled fake of its
signal source (filesystem path, network dict, or openclaw_cfg dict).
Filesystem-touching detectors use ``monkeypatch`` to redirect
``bot_home`` so the test stays hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.setup_checklist import (
    _detect_ai_optim_tiers,
    _detect_cost_profile,
    _detect_gallery_app,
    _detect_github,
    _detect_pairing,
    _detect_purpose,
    _detect_search,
    _detect_secondary_llm,
    _detect_spend_cap,
)


# ── purpose ───────────────────────────────────────────────────────────────
#
# Purpose lives in network.json (bots[<id>].purpose) — no filesystem read.
# The detector delegates to bot_purpose.has_purpose, so these tests
# construct the network dict directly both with and without a purpose.


def test_purpose_false_when_no_purpose_field():
    """Fresh bot — the 9 live pods today — has no purpose → pending."""
    network = {"bots": {"team_bot_a": {}}}
    assert _detect_purpose("team_bot_a", network, None) is False


def test_purpose_false_when_bot_absent():
    """Detector tolerates a bot that isn't a network member yet."""
    assert _detect_purpose("ghost", {"bots": {}}, None) is False


def test_purpose_false_when_archetype_blank():
    """A purpose dict with an empty archetype isn't a real declaration —
    has_purpose() gates on a non-empty archetype, so this stays pending."""
    network = {"bots": {"team_bot_a": {"purpose": {"archetype": "", "mission": "x"}}}}
    assert _detect_purpose("team_bot_a", network, None) is False


def test_purpose_true_when_archetype_declared():
    """Operator declared an archetype (via wizard or the Purpose card) → done."""
    network = {
        "bots": {
            "team_bot_a": {
                "purpose": {
                    "archetype": "personal-assistant",
                    "mission": "Keep my schedule and triage email",
                    "captured": "declared",
                }
            }
        }
    }
    assert _detect_purpose("team_bot_a", network, None) is True


def test_purpose_ignores_openclaw_cfg():
    """The data lives in network.json; a populated openclaw_cfg must not
    flip the result either way."""
    network = {"bots": {"team_bot_a": {}}}
    assert _detect_purpose("team_bot_a", network, openclaw_cfg={"purpose": "x"}) is False


# ── pairing ───────────────────────────────────────────────────────────────


def _stub_bot_home(tmp_path: Path):
    """Return a bot_home stub that pretends every bot lives under tmp_path."""

    def stub(bot_id, network=None):
        return tmp_path

    return stub


def test_pairing_false_when_no_credentials_dir(monkeypatch, tmp_path):
    # Detectors that need bot_home() resolve it via lazy import — patch at
    # the source module, not at setup_checklist (where it doesn't exist
    # at module scope).
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    assert _detect_pairing("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_pairing_false_when_allowfrom_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    creds = tmp_path / ".openclaw" / "credentials"
    creds.mkdir(parents=True)
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []})
    )
    assert _detect_pairing("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_pairing_true_when_any_channel_has_approved_user(monkeypatch, tmp_path):
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    creds = tmp_path / ".openclaw" / "credentials"
    creds.mkdir(parents=True)
    (creds / "slack-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["U123"]})
    )
    assert _detect_pairing("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is True


# ── github ────────────────────────────────────────────────────────────────


def test_github_false_when_url_missing():
    assert _detect_github("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_github_false_when_url_empty_string():
    network = {"bots": {"team_bot_a": {"backupRepoUrl": ""}}}
    assert _detect_github("team_bot_a", network, None) is False


def test_github_false_when_url_whitespace_only():
    network = {"bots": {"team_bot_a": {"backupRepoUrl": "   "}}}
    assert _detect_github("team_bot_a", network, None) is False


def test_github_false_when_url_set_but_no_state_file(monkeypatch, tmp_path):
    """Wiring exists but the daemon hasn't run yet → still pending. Catches
    the "backupRepoUrl set on day 0, first push hasn't happened" case."""
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id: tmp_path / bot_id / "workspace",
    )
    network = {"bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:ops/team_bot_a-backup"}}}
    assert _detect_github("team_bot_a", network, None) is False


def test_github_true_when_url_set_and_recent_success(monkeypatch, tmp_path):
    """The happy path: URL wired AND state.json shows a recent successful push."""
    import json as _json
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id: tmp_path / bot_id / "workspace",
    )
    state_dir = tmp_path / "team_bot_a" / "workspace" / "evolve-backup"
    state_dir.mkdir(parents=True)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (state_dir / "state.json").write_text(_json.dumps({"last_success_at": recent}))
    network = {"bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:ops/team_bot_a-backup"}}}
    assert _detect_github("team_bot_a", network, None) is True


def test_github_false_when_last_success_older_than_24h(monkeypatch, tmp_path):
    """Pushed once, then broke. Daemon last succeeded > 24h ago → flag it
    as pending so the operator sees something's wrong."""
    import json as _json
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id: tmp_path / bot_id / "workspace",
    )
    state_dir = tmp_path / "team_bot_a" / "workspace" / "evolve-backup"
    state_dir.mkdir(parents=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    (state_dir / "state.json").write_text(_json.dumps({"last_success_at": stale}))
    network = {"bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:ops/team_bot_a-backup"}}}
    assert _detect_github("team_bot_a", network, None) is False


def test_github_accepts_iso_format_with_z_suffix(monkeypatch, tmp_path):
    """backup.py may write timestamps with trailing 'Z' instead of '+00:00'."""
    import json as _json
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(
        "evolve_admin.config.get_bot_workspace",
        lambda bot_id: tmp_path / bot_id / "workspace",
    )
    state_dir = tmp_path / "team_bot_a" / "workspace" / "evolve-backup"
    state_dir.mkdir(parents=True)
    recent_z = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    (state_dir / "state.json").write_text(_json.dumps({"last_success_at": recent_z}))
    network = {"bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:ops/team_bot_a-backup"}}}
    assert _detect_github("team_bot_a", network, None) is True


# ── search ────────────────────────────────────────────────────────────────


def test_search_false_when_no_openclaw_cfg():
    assert _detect_search("team_bot_a", {}, None) is False


def test_search_false_when_no_plugins_block():
    assert _detect_search("team_bot_a", {}, openclaw_cfg={}) is False


def test_search_false_when_no_search_plugins_present():
    cfg = {"plugins": {"entries": {"github": {"enabled": True}}}}
    assert _detect_search("team_bot_a", {}, openclaw_cfg=cfg) is False


def test_search_true_when_brave_enabled():
    cfg = {"plugins": {"entries": {"brave": {"enabled": True}}}}
    assert _detect_search("team_bot_a", {}, openclaw_cfg=cfg) is True


def test_search_true_when_brave_present_with_default_enabled():
    """A plugin entry without an explicit `enabled` field is treated as enabled."""
    cfg = {"plugins": {"entries": {"brave": {"config": {"key": "..."}}}}}
    assert _detect_search("team_bot_a", {}, openclaw_cfg=cfg) is True


def test_search_false_when_brave_explicitly_disabled():
    cfg = {"plugins": {"entries": {"brave": {"enabled": False}}}}
    assert _detect_search("team_bot_a", {}, openclaw_cfg=cfg) is False


def test_search_true_when_tavily_enabled():
    cfg = {"plugins": {"entries": {"tavily": {"enabled": True}}}}
    assert _detect_search("team_bot_a", {}, openclaw_cfg=cfg) is True


# ── ai_optim_tiers ────────────────────────────────────────────────────────
#
# Tier config lives in ~/.openclaw/evolve-tiers.json, NOT openclaw.json.
# Tests redirect bot_home into tmp_path so the detector reads a controlled
# fixture file.


def _write_tiers_file(tmp_path: Path, bot_id: str, tiers: dict) -> None:
    p = tmp_path / "Users" / bot_id / ".openclaw"
    p.mkdir(parents=True, exist_ok=True)
    (p / "evolve-tiers.json").write_text(json.dumps({"tiers": tiers}))


def _stub_bot_home_for_tiers(tmp_path: Path):
    def stub(bot_id, network=None):
        return tmp_path / "Users" / bot_id
    return stub


def test_ai_optim_tiers_false_when_no_file(monkeypatch, tmp_path):
    """Fresh bot with no evolve-tiers.json → not done."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is False


def test_ai_optim_tiers_true_when_workhorse_only(monkeypatch, tmp_path):
    """Workhorse (tier2) alone is enough — judge (tier0) defaults to the
    workhorse model at runtime via models.resolve_tier, so the operator
    isn't blocked from finishing setup just for not having picked a
    distinct cross-provider judge."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    _write_tiers_file(tmp_path, "team_bot_a", {
        "tier2": {"models": ["anthropic/claude-sonnet-4.5"]},
    })
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is True


def test_ai_optim_tiers_true_via_defaults_when_judge_only(monkeypatch, tmp_path):
    """Judge picked, workhorse (tier2) OMITTED → configured-via-defaults.

    Post-Addendum-2 (#2561): the DEFAULT_MODEL_CATALOG ships in code as the
    base layer of the keyed merge, so an absent tier2 resolves to the blessed
    sonnet-class rung at runtime — the bot CAN take a human turn. A fresh pod
    works out of the box; the checklist must not flag a tier the defaults
    already satisfy. Only an EXPLICIT-but-empty tier2 flags (next two tests);
    an omitted one does not."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    _write_tiers_file(tmp_path, "team_bot_a", {
        "tier0": {"models": ["openai/gpt-4o"]},
    })
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is True


def test_ai_optim_tiers_true_when_workhorse_and_judge_both_set(monkeypatch, tmp_path):
    """Both tiers assigned → done."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    _write_tiers_file(tmp_path, "team_bot_a", {
        "tier2": {"models": ["anthropic/claude-sonnet-4.5"]},
        "tier0": {"models": ["openai/gpt-4o"]},
    })
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is True


def test_ai_optim_tiers_false_when_workhorse_models_empty(monkeypatch, tmp_path):
    """tier2 entry EXPLICITLY present but its models list is empty → broken
    config, still flagged. Distinct from an OMITTED tier2 (defaulted → True):
    an empty override is the operator typing nothing useful, which the keyed
    merge no-ops back to defaults at runtime but the checklist must still
    surface so they fix it (#2561 onboarding semantics)."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    _write_tiers_file(tmp_path, "team_bot_a", {
        "tier2": {"models": []},
        "tier0": {"models": ["openai/gpt-4o"]},
    })
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is False


def test_ai_optim_tiers_false_when_workhorse_model_is_whitespace(monkeypatch, tmp_path):
    """Robustness: tier2 EXPLICITLY present with only whitespace 'models' isn't
    really configured → broken, still flagged (same broken-vs-absent
    distinction as the empty-models case; #2561 onboarding semantics)."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    _write_tiers_file(tmp_path, "team_bot_a", {
        "tier2": {"models": ["  "]},
        "tier0": {"models": ["openai/gpt-4o"]},
    })
    assert _detect_ai_optim_tiers("team_bot_a", {}, None) is False


def test_ai_optim_tiers_ignores_openclaw_cfg(monkeypatch, tmp_path):
    """The function signature accepts openclaw_cfg for uniformity with the
    other detectors, but tier data ISN'T in openclaw.json — passing a
    populated openclaw_cfg with phantom 'tiers' must NOT flip the result."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home_for_tiers(tmp_path))
    bogus = {
        "tiers": {
            "tier2": {"models": ["anthropic/claude-sonnet-4.5"]},
            "tier0": {"models": ["openai/gpt-4o"]},
        },
    }
    # No evolve-tiers.json on disk → False even though openclaw_cfg looks complete.
    assert _detect_ai_optim_tiers("team_bot_a", {}, openclaw_cfg=bogus) is False


# ── secondary_llm ─────────────────────────────────────────────────────────


def _write_auth_profiles(tmp_path: Path, profiles: dict) -> None:
    p = tmp_path / ".openclaw" / "agents" / "main" / "agent"
    p.mkdir(parents=True)
    (p / "auth-profiles.json").write_text(json.dumps({"profiles": profiles}))


def test_secondary_llm_false_when_no_auth_file(monkeypatch, tmp_path):
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    assert _detect_secondary_llm("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_secondary_llm_false_when_only_one_provider(monkeypatch, tmp_path):
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    _write_auth_profiles(tmp_path, {
        "anthropic:default": {"provider": "anthropic", "type": "api_key"},
    })
    assert _detect_secondary_llm("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_secondary_llm_true_when_two_distinct_providers(monkeypatch, tmp_path):
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    _write_auth_profiles(tmp_path, {
        "anthropic:default": {"provider": "anthropic", "type": "api_key"},
        "openai:default":    {"provider": "openai",    "type": "api_key"},
    })
    assert _detect_secondary_llm("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is True


def test_secondary_llm_ignores_duplicate_provider(monkeypatch, tmp_path):
    """Two profiles for the same provider don't count as 'secondary'."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    _write_auth_profiles(tmp_path, {
        "anthropic:default": {"provider": "anthropic", "type": "api_key"},
        "anthropic:fallback": {"provider": "anthropic", "type": "auth_token"},
    })
    assert _detect_secondary_llm("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_secondary_llm_ignores_unknown_provider(monkeypatch, tmp_path):
    """A provider not in the LLM allowlist (e.g. an internal credential slot) doesn't count."""
    monkeypatch.setattr("evolve_admin.config.bot_home", _stub_bot_home(tmp_path))
    _write_auth_profiles(tmp_path, {
        "anthropic:default": {"provider": "anthropic", "type": "api_key"},
        "internal:default":  {"provider": "internal",  "type": "api_key"},
    })
    assert _detect_secondary_llm("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


# ── cost_profile ──────────────────────────────────────────────────────────


def test_cost_profile_false_when_no_openclaw_cfg():
    assert _detect_cost_profile("team_bot_a", {}, None) is False


def test_cost_profile_false_when_no_cost_fields_set():
    cfg = {"agents": {"defaults": {"model": {"primary": "claude-sonnet-4.5"}}}}
    assert _detect_cost_profile("team_bot_a", {}, openclaw_cfg=cfg) is False


def test_cost_profile_true_when_heartbeat_set():
    cfg = {"agents": {"defaults": {"heartbeat": {"enabled": True, "interval": 600}}}}
    assert _detect_cost_profile("team_bot_a", {}, openclaw_cfg=cfg) is True


def test_cost_profile_true_when_context_pruning_set():
    cfg = {"agents": {"defaults": {"contextPruning": {"enabled": True}}}}
    assert _detect_cost_profile("team_bot_a", {}, openclaw_cfg=cfg) is True


def test_cost_profile_true_when_session_at_root_set():
    """``session`` lives at root, not under agents.defaults (OC schema rule)."""
    cfg = {"session": {"reset": "on_quiet"}}
    assert _detect_cost_profile("team_bot_a", {}, openclaw_cfg=cfg) is True


# ── gallery_app ───────────────────────────────────────────────────────────


def test_gallery_app_false_when_no_manifests(monkeypatch):
    monkeypatch.setattr(
        "evolve_admin.applications.manifest.list_manifests",
        lambda shared_dir, bot_id: [],
    )
    assert _detect_gallery_app("team_bot_a", {"sharedDir": "/x"}, None) is False


def test_gallery_app_false_when_only_non_gallery_manifests(monkeypatch):
    class FakeManifest:
        def is_gallery_app(self):
            return False

    monkeypatch.setattr(
        "evolve_admin.applications.manifest.list_manifests",
        lambda shared_dir, bot_id: [FakeManifest()],
    )
    assert _detect_gallery_app("team_bot_a", {"sharedDir": "/x"}, None) is False


def test_gallery_app_true_when_at_least_one_gallery_manifest(monkeypatch):
    class FakeManifest:
        def __init__(self, gallery):
            self._gallery = gallery

        def is_gallery_app(self):
            return self._gallery

    monkeypatch.setattr(
        "evolve_admin.applications.manifest.list_manifests",
        lambda shared_dir, bot_id: [FakeManifest(False), FakeManifest(True)],
    )
    assert _detect_gallery_app("team_bot_a", {"sharedDir": "/x"}, None) is True


# ── spend_cap ─────────────────────────────────────────────────────────────


def test_spend_cap_false_when_field_absent():
    assert _detect_spend_cap("team_bot_a", {"bots": {"team_bot_a": {}}}, None) is False


def test_spend_cap_false_when_cap_is_zero():
    network = {"bots": {"team_bot_a": {"daily_cap_usd": 0}}}
    assert _detect_spend_cap("team_bot_a", network, None) is False


def test_spend_cap_false_when_cap_is_negative():
    network = {"bots": {"team_bot_a": {"daily_cap_usd": -1.0}}}
    assert _detect_spend_cap("team_bot_a", network, None) is False


def test_spend_cap_false_when_cap_is_garbage():
    network = {"bots": {"team_bot_a": {"daily_cap_usd": "not a number"}}}
    assert _detect_spend_cap("team_bot_a", network, None) is False


def test_spend_cap_true_when_cap_positive():
    network = {"bots": {"team_bot_a": {"daily_cap_usd": 5.0}}}
    assert _detect_spend_cap("team_bot_a", network, None) is True


def test_spend_cap_true_when_cap_numeric_string():
    """JSON sometimes round-trips numerics as strings — be permissive."""
    network = {"bots": {"team_bot_a": {"daily_cap_usd": "5.0"}}}
    assert _detect_spend_cap("team_bot_a", network, None) is True

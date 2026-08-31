"""tests/test_rsi_config.py — better_engine_config resolution rules."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from better_engine_config import (  # noqa: E402
    BetterEngineConfig,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_CONFIG_MODE,
    load,
    save,
)


def test_default_config_has_everything_enabled():
    cfg = BetterEngineConfig.default()
    assert cfg.is_better_engine_enabled("anybot")
    assert cfg.is_rsi_enabled("anybot")


def test_pod_default_applies_when_no_override():
    cfg = BetterEngineConfig.from_dict(
        {
            "schema_version": 1,
            "pod_defaults": {"rsi": {"enabled": False}},
            "bots": {},
        }
    )
    assert cfg.is_better_engine_enabled("bot1")  # BE default true
    assert not cfg.is_rsi_enabled("bot1")  # RSI set false at pod level


def test_bot_override_wins_over_pod_default():
    cfg = BetterEngineConfig.from_dict(
        {
            "schema_version": 1,
            "pod_defaults": {"rsi": {"enabled": True}},
            "bots": {"bot1": {"rsi": {"enabled": False}}},
        }
    )
    assert not cfg.is_rsi_enabled("bot1")
    assert cfg.is_rsi_enabled("other")  # inherits pod default


def test_better_engine_off_implies_rsi_off():
    cfg = BetterEngineConfig.from_dict(
        {
            "schema_version": 1,
            "pod_defaults": {
                "better_engine": {"enabled": False},
                "rsi": {"enabled": True},
            },
            "bots": {},
        }
    )
    assert not cfg.is_better_engine_enabled("bot1")
    assert not cfg.is_rsi_enabled("bot1")  # BE off trumps


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load(tmp_path)
    assert cfg.is_better_engine_enabled("anybot")


def test_load_valid_file(tmp_path):
    path = tmp_path / DEFAULT_CONFIG_FILENAME
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pod_defaults": {"rsi": {"enabled": False}},
                "bots": {"team": {"better_engine": {"enabled": False}}},
            }
        )
    )

    cfg = load(tmp_path)
    assert not cfg.is_rsi_enabled("other")
    assert not cfg.is_better_engine_enabled("team")


def test_load_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / DEFAULT_CONFIG_FILENAME
    path.write_text("{not valid json")
    cfg = load(tmp_path)
    assert cfg.is_better_engine_enabled("anybot")  # fell back to defaults


def test_roundtrip_to_dict_and_back():
    cfg = BetterEngineConfig.from_dict(
        {
            "schema_version": 1,
            "pod_defaults": {"rsi": {"enabled": True}},
            "bots": {"team_bot_a": {"rsi": {"enabled": False}}},
        }
    )
    restored = BetterEngineConfig.from_dict(cfg.to_dict())
    assert restored.is_rsi_enabled("other") == cfg.is_rsi_enabled("other")
    assert restored.is_rsi_enabled("team_bot_a") == cfg.is_rsi_enabled("team_bot_a")


def test_unknown_key_raises_keyerror():
    cfg = BetterEngineConfig.default()
    with pytest.raises(KeyError):
        cfg.resolve("bot", "made", "up", "key")


# ── Per-session cost cap + cache TTL (cost-cap normalization) ────────────────


def test_session_cost_cap_defaults_to_none():
    cfg = BetterEngineConfig.default()
    assert cfg.per_bot_session_cost_cap_usd("anybot") is None


def test_set_and_get_session_cost_cap():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_session_cost_cap_usd("team_bot_a", 5.50)
    assert cfg.per_bot_session_cost_cap_usd("team_bot_a") == 5.50
    assert cfg.per_bot_session_cost_cap_usd("other") is None


def test_clear_session_cost_cap_with_none():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_session_cost_cap_usd("team_bot_a", 5.50)
    cfg.set_per_bot_session_cost_cap_usd("team_bot_a", None)
    assert cfg.per_bot_session_cost_cap_usd("team_bot_a") is None


def test_session_cost_cap_survives_roundtrip():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_session_cost_cap_usd("team_bot_a", 7.25)
    restored = BetterEngineConfig.from_dict(cfg.to_dict())
    assert restored.per_bot_session_cost_cap_usd("team_bot_a") == 7.25


def test_cache_retention_defaults_to_none():
    cfg = BetterEngineConfig.default()
    assert cfg.per_bot_cache_retention("anybot") is None


def test_set_and_get_cache_retention():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_cache_retention("team_bot_a", "long")
    assert cfg.per_bot_cache_retention("team_bot_a") == "long"
    cfg.set_per_bot_cache_retention("team_bot_a", "short")
    assert cfg.per_bot_cache_retention("team_bot_a") == "short"


def test_clear_cache_retention_with_none():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_cache_retention("team_bot_a", "long")
    cfg.set_per_bot_cache_retention("team_bot_a", None)
    assert cfg.per_bot_cache_retention("team_bot_a") is None


def test_cache_retention_rejects_invalid_value():
    cfg = BetterEngineConfig.default()
    with pytest.raises(ValueError):
        cfg.set_per_bot_cache_retention("team_bot_a", "forever")


def test_cache_retention_survives_roundtrip():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_cache_retention("team_bot_a", "long")
    restored = BetterEngineConfig.from_dict(cfg.to_dict())
    assert restored.per_bot_cache_retention("team_bot_a") == "long"


# ── Phase 5 — graduated remediation ladder (spec: internal/spec-cost-caps-2026-06-05.md)


def test_tier_downgrade_defaults_to_none():
    cfg = BetterEngineConfig.default()
    assert cfg.per_bot_tier_downgrade_usd("anybot") is None


def test_set_and_get_tier_downgrade():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 7.50)
    assert cfg.per_bot_tier_downgrade_usd("team_bot_a") == 7.50
    assert cfg.per_bot_tier_downgrade_usd("other") is None


def test_clear_tier_downgrade_with_none():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 7.50)
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", None)
    assert cfg.per_bot_tier_downgrade_usd("team_bot_a") is None


def test_tier_downgrade_zero_or_negative_reads_as_none():
    """Opt-in convention: zero/negative means 'no enforcement at this tier'."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 0.0)
    assert cfg.per_bot_tier_downgrade_usd("team_bot_a") is None


def test_l1_breaker_aliases_existing_daily_hard_storage():
    """l1_breaker_usd is a spec-renamed view of the existing
    per_bot_daily_hard_usd; the storage is unchanged until Phase 8."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_daily_hard_usd("team_bot_a", 12.00)
    assert cfg.per_bot_l1_breaker_usd("team_bot_a") == 12.00
    # And the setter alias writes through to the same storage slot.
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 15.00)
    raw = cfg.bots["team_bot_a"]["budget"]["per_bot_daily_hard_usd"]
    assert raw == 15.00


def test_set_and_get_l2_breaker():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_l2_breaker_usd("team_bot_a", 25.00)
    assert cfg.per_bot_l2_breaker_usd("team_bot_a") == 25.00


def test_set_and_get_weekly_warn():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_weekly_warn_usd("team_bot_a", 10.00)
    assert cfg.per_bot_weekly_warn_usd("team_bot_a") == 10.00


def test_pod_weekly_warn_defaults_to_none():
    cfg = BetterEngineConfig.default()
    assert cfg.pod_weekly_warn_usd() is None


def test_set_and_get_pod_weekly_warn():
    cfg = BetterEngineConfig.default()
    cfg.set_pod_weekly_warn_usd(20.00)
    assert cfg.pod_weekly_warn_usd() == 20.00


def test_new_ladder_fields_survive_roundtrip():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 7.50)
    cfg.set_per_bot_l2_breaker_usd("team_bot_a", 25.00)
    cfg.set_per_bot_weekly_warn_usd("team_bot_a", 10.00)
    cfg.set_pod_weekly_warn_usd(20.00)
    restored = BetterEngineConfig.from_dict(cfg.to_dict())
    assert restored.per_bot_tier_downgrade_usd("team_bot_a") == 7.50
    assert restored.per_bot_l2_breaker_usd("team_bot_a") == 25.00
    assert restored.per_bot_weekly_warn_usd("team_bot_a") == 10.00
    assert restored.pod_weekly_warn_usd() == 20.00


# ── Validation: well-ordered remediation ladder ────────────────────────────


def test_validate_ladder_passes_when_all_unset():
    """Empty ladder is valid — nothing to enforce, nothing to invert."""
    cfg = BetterEngineConfig.default()
    assert cfg.validate_remediation_ladder("team_bot_a") == []


def test_validate_ladder_passes_when_well_ordered():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_daily_warn_usd("team_bot_a", 5.00)
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 8.00)
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 12.00)
    cfg.set_per_bot_l2_breaker_usd("team_bot_a", 25.00)
    assert cfg.validate_remediation_ladder("team_bot_a") == []


def test_validate_ladder_rejects_l2_below_l1():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 50.00)
    cfg.set_per_bot_l2_breaker_usd("team_bot_a", 25.00)  # below L1
    errs = cfg.validate_remediation_ladder("team_bot_a")
    assert errs
    assert any("l2_breaker" in e and "l1_breaker" in e for e in errs)


def test_validate_ladder_rejects_tier_above_l1():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 20.00)
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 10.00)  # below tier
    errs = cfg.validate_remediation_ladder("team_bot_a")
    assert errs


def test_validate_ladder_rejects_warn_above_tier():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_daily_warn_usd("team_bot_a", 10.00)
    cfg.set_per_bot_tier_downgrade_usd("team_bot_a", 5.00)
    errs = cfg.validate_remediation_ladder("team_bot_a")
    assert errs


def test_validate_ladder_partial_set_is_valid():
    """Skipping a tier is fine — missing rung means 'no enforcement
    at that tier'. As long as the set rungs are ordered, no error."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_daily_warn_usd("team_bot_a", 5.00)
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 12.00)
    # No tier_downgrade, no l2. Ladder is valid: warn=5 < l1=12.
    assert cfg.validate_remediation_ladder("team_bot_a") == []


def test_validate_ladder_rejects_equal_thresholds():
    """Strict greater-than: equal values aren't a valid ladder."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_l1_breaker_usd("team_bot_a", 10.00)
    cfg.set_per_bot_l2_breaker_usd("team_bot_a", 10.00)
    assert cfg.validate_remediation_ladder("team_bot_a") != []


# ─── save() file mode ──────────────────────────────────────────────────────
# Found 2026-07-31 on the mini: better-engine-config.json went
# ``-rw-r--r--`` → ``-rw-------`` after one programmatic save. ``save()``
# writes via tempfile+rename, and ``os.replace`` carries the TEMP file's mode
# onto the destination — ``mkstemp`` mints 0600. That locks out
# ``evolve_admin/evo/tools/pod_state_cost_caps.py``, which reads this config
# from the evo gateway subprocess running as the separate ``evo`` macOS user.
# The file holds budgets and toggles, never tokens, so 0644 is correct — the
# CLAUDE.md 0600 secret-config contract does not cover it.


def test_save_writes_0644_on_a_fresh_file(tmp_path):
    save(BetterEngineConfig.default(), tmp_path)
    mode = (tmp_path / DEFAULT_CONFIG_FILENAME).stat().st_mode & 0o777
    assert mode == 0o644
    assert mode == DEFAULT_CONFIG_MODE


def test_save_does_not_tighten_an_existing_0644_file(tmp_path):
    """The regression itself: save an existing world-readable config and it
    must still be world-readable afterwards."""
    path = tmp_path / DEFAULT_CONFIG_FILENAME
    save(BetterEngineConfig.default(), tmp_path)
    os.chmod(path, 0o644)

    cfg = load(tmp_path)
    cfg.set_per_bot_daily_hard_usd("team_bot_a", 20.0)
    save(cfg, tmp_path)

    assert path.stat().st_mode & 0o777 == 0o644


def test_save_reheals_a_config_already_tightened_to_0600(tmp_path):
    """A pod whose config was tightened by an earlier save gets it back on the
    next write — no manual chmod needed."""
    path = tmp_path / DEFAULT_CONFIG_FILENAME
    save(BetterEngineConfig.default(), tmp_path)
    os.chmod(path, 0o600)

    save(load(tmp_path), tmp_path)

    assert path.stat().st_mode & 0o777 == 0o644


def test_save_leaves_no_temp_files_behind(tmp_path):
    save(BetterEngineConfig.default(), tmp_path)
    save(load(tmp_path), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [DEFAULT_CONFIG_FILENAME]

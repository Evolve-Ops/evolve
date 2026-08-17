"""tests/test_rsi_bot_setup.py — bot setup primitives.

Covers the three concrete settings introduced for the Bot setup surface:
- archetype (existing field, now explicitly settable)
- monthly_cap_usd (new BetterEngineConfig field, derives daily warn/hard)
- surfacing_cadence (new ProfileFrontmatter field)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from better_engine_config import (  # noqa: E402
    BetterEngineConfig,
    DEFAULT_CONFIG_FILENAME,
    load,
    save,
)
from profile import (  # noqa: E402
    ALL_CADENCES,
    ARCHETYPE_PRIMARY,
    ARCHETYPE_SINGLE_USER_MEMBER,
    CADENCE_AS_IT_ARISES,
    CADENCE_DAILY,
    CADENCE_URGENT_ONLY,
    CADENCE_WEEKLY,
    Profile,
    ProfileFrontmatter,
    create_default_profile,
    load_profile,
    save_profile,
)


# ─────────────────────────────────────────────────────────────────────────────
# ProfileFrontmatter.surfacing_cadence
# ─────────────────────────────────────────────────────────────────────────────


def test_frontmatter_accepts_known_cadence():
    fm = ProfileFrontmatter(bot_id="team_bot_a", surfacing_cadence=CADENCE_WEEKLY)
    assert fm.surfacing_cadence == CADENCE_WEEKLY


def test_frontmatter_rejects_unknown_cadence_at_construction():
    with pytest.raises(ValueError, match="surfacing_cadence"):
        ProfileFrontmatter(bot_id="team_bot_a", surfacing_cadence="every_other_tuesday")


def test_frontmatter_default_cadence_is_none():
    fm = ProfileFrontmatter(bot_id="team_bot_a")
    assert fm.surfacing_cadence is None


def test_cadence_round_trips_through_disk(tmp_path):
    p = create_default_profile(
        shared_dir=tmp_path, bot_id="team_bot_a", archetype=ARCHETYPE_PRIMARY
    )
    p.frontmatter.surfacing_cadence = CADENCE_WEEKLY
    save_profile(p, tmp_path)

    reloaded = load_profile(tmp_path, "team_bot_a")
    assert reloaded is not None
    assert reloaded.frontmatter.surfacing_cadence == CADENCE_WEEKLY


def test_cadence_serialization_only_when_set(tmp_path):
    """When unset, surfacing_cadence is omitted from frontmatter so we
    don't litter old profiles with empty fields."""
    p = create_default_profile(
        shared_dir=tmp_path, bot_id="team_bot_a", archetype=ARCHETYPE_PRIMARY
    )
    text = (tmp_path / "profiles" / "team_bot_a.md").read_text(encoding="utf-8")
    assert "surfacing_cadence" not in text


def test_cadence_unknown_value_in_file_drops_to_none(tmp_path):
    """If a user hand-edits the file with a typo, we tolerate it on read
    rather than refusing to load the profile."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "team_bot_a.md").write_text(
        "---\n"
        "bot_id: team_bot_a\n"
        "schema_version: 1\n"
        "created_at: 2026-04-15T10:23:45+00:00\n"
        "updated_at: 2026-04-15T10:23:45+00:00\n"
        "surfacing_cadence: typo_value\n"
        "---\n\n"
        "# User Profile — team_bot_a\n",
        encoding="utf-8",
    )
    p = load_profile(tmp_path, "team_bot_a")
    assert p is not None
    assert p.frontmatter.surfacing_cadence is None  # silently dropped


def test_all_cadences_round_trip(tmp_path):
    for c in ALL_CADENCES:
        p = create_default_profile(
            shared_dir=tmp_path,
            bot_id=f"bot_{c}",
            archetype=ARCHETYPE_PRIMARY,
        )
        p.frontmatter.surfacing_cadence = c
        save_profile(p, tmp_path)
        reloaded = load_profile(tmp_path, f"bot_{c}")
        assert reloaded.frontmatter.surfacing_cadence == c


# ─────────────────────────────────────────────────────────────────────────────
# BetterEngineConfig — per-bot monthly cap
# ─────────────────────────────────────────────────────────────────────────────


def test_per_bot_monthly_cap_unset_returns_none():
    cfg = BetterEngineConfig.default()
    assert cfg.per_bot_monthly_cap_usd("team_bot_a") is None


def test_set_and_get_per_bot_monthly_cap():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 30.0)
    assert cfg.per_bot_monthly_cap_usd("team_bot_a") == 30.0


def test_clear_per_bot_monthly_cap():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 30.0)
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", None)
    assert cfg.per_bot_monthly_cap_usd("team_bot_a") is None


def test_monthly_cap_does_not_leak_to_other_bots():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 30.0)
    assert cfg.per_bot_monthly_cap_usd("paq") is None


def test_warn_cap_uses_monthly_when_set():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 60.0)
    # warn = 60/30 * 1.5 = 3.0
    assert cfg.budget_warn_cap_usd("team_bot_a") == pytest.approx(3.0)


def test_hard_cap_uses_monthly_when_set():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 60.0)
    # hard = 60/30 * 2.5 = 5.0
    assert cfg.budget_hard_cap_usd("team_bot_a") == pytest.approx(5.0)


def test_warn_cap_falls_back_to_explicit_default_when_monthly_unset():
    cfg = BetterEngineConfig.default()
    # Compiled default is 2.00
    assert cfg.budget_warn_cap_usd("team_bot_a") == 2.00


def test_hard_cap_falls_back_to_explicit_default_when_monthly_unset():
    cfg = BetterEngineConfig.default()
    assert cfg.budget_hard_cap_usd("team_bot_a") == 5.00


def test_monthly_cap_only_affects_target_bot():
    """A monthly cap on bot 'team_bot_a' must not change the daily caps for 'paq'."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 90.0)  # would imply warn=4.5, hard=7.5
    # paq still gets the compiled defaults
    assert cfg.budget_warn_cap_usd("paq") == 2.00
    assert cfg.budget_hard_cap_usd("paq") == 5.00
    # team_bot_a gets the derived values
    assert cfg.budget_warn_cap_usd("team_bot_a") == pytest.approx(4.5)
    assert cfg.budget_hard_cap_usd("team_bot_a") == pytest.approx(7.5)


def test_save_and_load_round_trip_with_monthly_cap(tmp_path):
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 45.0)
    save(cfg, tmp_path)

    reloaded = load(tmp_path)
    assert reloaded.per_bot_monthly_cap_usd("team_bot_a") == 45.0
    # And derived caps are stable across save/load
    assert reloaded.budget_warn_cap_usd("team_bot_a") == pytest.approx(45.0 / 30 * 1.5)


def test_save_writes_to_canonical_path(tmp_path):
    cfg = BetterEngineConfig.default()
    save(cfg, tmp_path)
    assert (tmp_path / DEFAULT_CONFIG_FILENAME).exists()


def test_save_creates_shared_dir_if_missing(tmp_path):
    target = tmp_path / "new_dir"
    cfg = BetterEngineConfig.default()
    save(cfg, target)
    assert target.exists()
    assert (target / DEFAULT_CONFIG_FILENAME).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: existing budget_hawk-style code path still works
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_hawk_style_resolution_with_monthly_set(tmp_path):
    """End-to-end: write config, load it, check the caps that budget_hawk reads."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_monthly_cap_usd("team_bot_a", 30.0)
    save(cfg, tmp_path)

    loaded = load(tmp_path)
    # Same values that budget_hawk's context factory will read
    warn = loaded.budget_warn_cap_usd("team_bot_a")
    hard = loaded.budget_hard_cap_usd("team_bot_a")
    assert warn < hard  # invariant: hard > warn
    assert warn == pytest.approx(30.0 / 30 * 1.5)
    assert hard == pytest.approx(30.0 / 30 * 2.5)

"""Tests for evolve_admin.release_notes."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evolve_admin.release_notes import (
    ReleaseEntry,
    TIER_FEATURE,
    TIER_MAINTENANCE,
    TIER_SECURITY,
    load_releases,
    min_deployed_version,
    parse_version,
    resolve_latest_release,
    tier_rank,
)


def test_parse_version_roundtrip():
    assert parse_version("2026.0516.0") == (2026, 516, 0)
    assert parse_version("2026.0516.1234") == (2026, 516, 1234)
    assert parse_version("2025.1231.99") == (2025, 1231, 99)


def test_parse_version_rejects_garbage():
    for bad in ["", "v2026.0516.0", "2026.05.0", "abc", None, "2026.0516"]:
        assert parse_version(bad) is None  # type: ignore[arg-type]


def test_tier_rank_orders_correctly():
    assert tier_rank(TIER_SECURITY) > tier_rank(TIER_FEATURE) > tier_rank(TIER_MAINTENANCE)
    assert tier_rank("nonsense") == 0


def _write(p: Path, body: str) -> Path:
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return p


def test_load_releases_missing_file_returns_empty(tmp_path):
    assert load_releases(tmp_path / "nope.yaml") == []


def test_load_releases_parses_valid_yaml(tmp_path):
    f = _write(tmp_path / "RELEASES.yaml", """
        - version: "2026.0516.0"
          tier: feature
          headline: "Tier-aware update banner"
          details: "Updates are now classified by significance."
          link: internal/spec-release-tiers-2026-05-16.md
        - version: "2026.0515.1173"
          tier: maintenance
    """)
    entries = load_releases(f)
    assert len(entries) == 2
    assert entries[0].version == "2026.0516.0"
    assert entries[0].tier == TIER_FEATURE
    assert entries[0].headline == "Tier-aware update banner"
    assert entries[1].tier == TIER_MAINTENANCE
    assert entries[1].headline is None


def test_load_releases_skips_malformed_entries(tmp_path):
    f = _write(tmp_path / "RELEASES.yaml", """
        - version: "2026.0516.0"
          tier: feature
        - version: "not-a-version"
          tier: feature
        - tier: security  # missing version
        - "this is a string not a dict"
    """)
    entries = load_releases(f)
    assert len(entries) == 1
    assert entries[0].version == "2026.0516.0"


def test_load_releases_unknown_tier_demoted_to_maintenance(tmp_path):
    f = _write(tmp_path / "RELEASES.yaml", """
        - version: "2026.0516.0"
          tier: extraordinary
          headline: "Mystery"
    """)
    entries = load_releases(f)
    assert len(entries) == 1
    assert entries[0].tier == TIER_MAINTENANCE


def test_load_releases_handles_invalid_yaml(tmp_path):
    # PyYAML rejects this; loader should return [].
    f = _write(tmp_path / "RELEASES.yaml", "::: not valid yaml :::\n  - foo: [")
    entries = load_releases(f)
    assert entries == []


def test_load_releases_handles_top_level_dict(tmp_path):
    # Top-level must be a list.
    f = _write(tmp_path / "RELEASES.yaml", """
        version: "2026.0516.0"
        tier: feature
    """)
    assert load_releases(f) == []


def _entries(rows):
    return [
        ReleaseEntry(
            version=r["version"],
            tier=r.get("tier", TIER_MAINTENANCE),
            headline=r.get("headline"),
        )
        for r in rows
    ]


def test_resolve_returns_none_when_min_deployed_missing():
    assert resolve_latest_release(
        min_deployed_version=None,
        current_version="2026.0516.0",
        entries=[],
    ) is None


def test_resolve_returns_none_when_caught_up():
    assert resolve_latest_release(
        min_deployed_version="2026.0516.0",
        current_version="2026.0516.0",
        entries=[],
    ) is None


def test_resolve_returns_maintenance_default_when_no_entries_in_range():
    r = resolve_latest_release(
        min_deployed_version="2026.0515.1170",
        current_version="2026.0516.0",
        entries=[],
    )
    assert r is not None
    assert r.entry.tier == TIER_MAINTENANCE
    assert r.entry.version == "2026.0516.0"
    assert r.from_version == "2026.0515.1170"
    assert r.to_version == "2026.0516.0"


def test_resolve_picks_max_tier_in_range():
    entries = _entries([
        {"version": "2026.0516.0", "tier": TIER_FEATURE, "headline": "Banner work"},
        {"version": "2026.0515.1200", "tier": TIER_SECURITY, "headline": "Auth fix"},
        {"version": "2026.0515.1180", "tier": TIER_MAINTENANCE},
    ])
    r = resolve_latest_release(
        min_deployed_version="2026.0515.1170",
        current_version="2026.0516.0",
        entries=entries,
    )
    assert r is not None
    assert r.entry.tier == TIER_SECURITY
    assert r.entry.headline == "Auth fix"


def test_resolve_excludes_entries_at_or_below_min_deployed():
    # Entry at 2026.0515.1170 should NOT be selected — deployed is already there.
    entries = _entries([
        {"version": "2026.0515.1170", "tier": TIER_SECURITY, "headline": "Already applied"},
        {"version": "2026.0516.0", "tier": TIER_FEATURE, "headline": "New thing"},
    ])
    r = resolve_latest_release(
        min_deployed_version="2026.0515.1170",
        current_version="2026.0516.0",
        entries=entries,
    )
    assert r is not None
    assert r.entry.tier == TIER_FEATURE


def test_resolve_excludes_entries_above_current():
    # If RELEASES has a future-dated entry, ignore it.
    entries = _entries([
        {"version": "2026.0517.0", "tier": TIER_SECURITY, "headline": "Future"},
        {"version": "2026.0516.0", "tier": TIER_FEATURE, "headline": "Current"},
    ])
    r = resolve_latest_release(
        min_deployed_version="2026.0515.0",
        current_version="2026.0516.0",
        entries=entries,
    )
    assert r is not None
    assert r.entry.tier == TIER_FEATURE


def test_resolve_tiebreaks_by_newest_version_within_same_tier():
    entries = _entries([
        {"version": "2026.0515.1100", "tier": TIER_FEATURE, "headline": "Older"},
        {"version": "2026.0515.1200", "tier": TIER_FEATURE, "headline": "Newer"},
    ])
    r = resolve_latest_release(
        min_deployed_version="2026.0515.1000",
        current_version="2026.0516.0",
        entries=entries,
    )
    assert r is not None
    assert r.entry.headline == "Newer"


def test_resolve_to_dict_shape_matches_spec():
    entries = _entries([
        {"version": "2026.0516.0", "tier": TIER_FEATURE, "headline": "X"},
    ])
    r = resolve_latest_release(
        min_deployed_version="2026.0515.1170",
        current_version="2026.0516.0",
        entries=entries,
    )
    assert r is not None
    d = r.to_dict()
    assert d["tier"] == TIER_FEATURE
    assert d["version"] == "2026.0516.0"
    assert d["headline"] == "X"
    assert d["range"]["from_version"] == "2026.0515.1170"
    assert d["range"]["to_version"] == "2026.0516.0"
    assert d["range"]["count"] >= 1


def test_min_deployed_version_picks_lowest():
    sync = {
        "team_bot_a":   {"deployed_version": "2026.0515.1200", "synced": False},
        "admin_bot": {"deployed_version": "2026.0515.1100", "synced": False},
        "team_bot_b":  {"deployed_version": "2026.0516.0",    "synced": True},
    }
    assert min_deployed_version(sync) == "2026.0515.1100"


def test_min_deployed_version_skips_never_deployed():
    sync = {
        "team_bot_a":  {"deployed_version": None, "synced": False},
        "admin_bot": {"deployed_version": "2026.0515.1100", "synced": False},
    }
    assert min_deployed_version(sync) == "2026.0515.1100"


def test_min_deployed_version_returns_none_when_no_deploys():
    assert min_deployed_version({}) is None
    assert min_deployed_version({"team_bot_a": {"deployed_version": None}}) is None


def test_min_deployed_version_ignores_unparseable():
    sync = {
        "team_bot_a":  {"deployed_version": "dev-build", "synced": False},
        "admin_bot": {"deployed_version": "2026.0515.1100", "synced": False},
    }
    assert min_deployed_version(sync) == "2026.0515.1100"

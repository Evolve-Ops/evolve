"""Tests for ``install_profile`` — the feature-gating helper.

Resolution rules under test:

  1. Explicit ``features.<name>.enabled`` always wins.
  2. Profile default (``feature_profile`` → ``PROFILE_DEFAULTS``) when no
     explicit override.
  3. Missing / malformed ``install.json`` falls through to safe defaults
     (profile=standard, all gated features off).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from install_profile import (
    DEFAULT_PROFILE,
    PROFILE_DEFAULTS,
    VALID_PROFILES,
    get_feature_config,
    get_feature_profile,
    is_feature_enabled,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def install_json(tmp_path: Path):
    """Yield a writer that creates an install.json at ``tmp_path/install.json``."""
    path = tmp_path / "install.json"

    def write(data: dict) -> Path:
        path.write_text(json.dumps(data))
        return path

    return write


# ── Catalog sanity ──────────────────────────────────────────────────────────


def test_default_profile_is_standard():
    assert DEFAULT_PROFILE == "standard"
    assert DEFAULT_PROFILE in VALID_PROFILES


def test_developer_profile_enables_upstream_issues_watcher_by_default():
    # The motivating power feature — if this default changes, the spec needs
    # updating too.
    assert "upstream_issues_watcher" in PROFILE_DEFAULTS["developer"]


def test_standard_profile_has_no_defaults_enabled():
    # Standard is the safe default for household installs. Anything we ever
    # add to this set is a Plex-test failure waiting to happen.
    assert PROFILE_DEFAULTS["standard"] == frozenset()


def test_minimal_profile_has_no_defaults_enabled():
    assert PROFILE_DEFAULTS["minimal"] == frozenset()


# ── get_feature_profile ─────────────────────────────────────────────────────


def test_missing_install_json_returns_default_profile(tmp_path: Path):
    assert get_feature_profile(tmp_path / "nope.json") == "standard"


def test_explicit_standard_profile(install_json):
    p = install_json({"feature_profile": "standard"})
    assert get_feature_profile(p) == "standard"


def test_explicit_developer_profile(install_json):
    p = install_json({"feature_profile": "developer"})
    assert get_feature_profile(p) == "developer"


def test_explicit_minimal_profile(install_json):
    p = install_json({"feature_profile": "minimal"})
    assert get_feature_profile(p) == "minimal"


def test_unknown_profile_falls_through_to_default(install_json):
    # Typo / future-version-of-this-file / hostile input → safe default.
    p = install_json({"feature_profile": "ultra-dev"})
    assert get_feature_profile(p) == "standard"


def test_non_string_profile_falls_through_to_default(install_json):
    p = install_json({"feature_profile": True})
    assert get_feature_profile(p) == "standard"


def test_malformed_install_json_falls_through_to_default(tmp_path: Path):
    p = tmp_path / "install.json"
    p.write_text("not valid json {{{")
    assert get_feature_profile(p) == "standard"


# ── is_feature_enabled ──────────────────────────────────────────────────────


def test_missing_install_json_disables_gated_features(tmp_path: Path):
    # With no install.json at all, the install is treated as a fresh /
    # standard household — nothing in the gated catalog should run.
    p = tmp_path / "nope.json"
    assert is_feature_enabled("upstream_issues_watcher", p) is False


def test_standard_profile_disables_gated_features(install_json):
    p = install_json({"feature_profile": "standard"})
    assert is_feature_enabled("upstream_issues_watcher", p) is False


def test_developer_profile_enables_gated_features(install_json):
    p = install_json({"feature_profile": "developer"})
    assert is_feature_enabled("upstream_issues_watcher", p) is True


def test_explicit_off_overrides_developer_default(install_json):
    p = install_json({
        "feature_profile": "developer",
        "features": {"upstream_issues_watcher": {"enabled": False}},
    })
    assert is_feature_enabled("upstream_issues_watcher", p) is False


def test_explicit_on_overrides_standard_default(install_json):
    p = install_json({
        "feature_profile": "standard",
        "features": {"upstream_issues_watcher": {"enabled": True}},
    })
    assert is_feature_enabled("upstream_issues_watcher", p) is True


def test_unknown_feature_is_disabled_under_standard(install_json):
    p = install_json({"feature_profile": "standard"})
    assert is_feature_enabled("nonexistent_feature", p) is False


def test_unknown_feature_is_disabled_under_developer(install_json):
    # The developer profile enables only things explicitly catalogued —
    # not arbitrary names.
    p = install_json({"feature_profile": "developer"})
    assert is_feature_enabled("nonexistent_feature", p) is False


def test_features_block_can_enable_uncatalogued_feature(install_json):
    # An explicit per-feature flag is the source of truth — it does NOT
    # require the feature to appear in PROFILE_DEFAULTS first. This lets
    # operators opt into experimental features that haven't been promoted
    # to the developer profile yet.
    p = install_json({
        "feature_profile": "standard",
        "features": {"experimental_thing": {"enabled": True}},
    })
    assert is_feature_enabled("experimental_thing", p) is True


def test_non_dict_features_block_is_ignored(install_json):
    p = install_json({
        "feature_profile": "developer",
        "features": ["this", "is", "not", "a", "dict"],
    })
    # Falls through to profile default for catalogued names…
    assert is_feature_enabled("upstream_issues_watcher", p) is True
    # …and to False for everything else.
    assert is_feature_enabled("random_thing", p) is False


def test_non_dict_feature_entry_is_ignored(install_json):
    p = install_json({
        "feature_profile": "standard",
        "features": {"upstream_issues_watcher": "yes please"},
    })
    # Malformed entry → fall through to profile default (False under standard).
    assert is_feature_enabled("upstream_issues_watcher", p) is False


def test_feature_entry_without_enabled_field_falls_through(install_json):
    # An entry with config but no "enabled" key should defer to the profile
    # default, not silently disable.
    p = install_json({
        "feature_profile": "developer",
        "features": {"upstream_issues_watcher": {"poll_interval_minutes": 5}},
    })
    assert is_feature_enabled("upstream_issues_watcher", p) is True


# ── get_feature_config ──────────────────────────────────────────────────────


def test_get_feature_config_returns_payload(install_json):
    p = install_json({
        "feature_profile": "developer",
        "features": {
            "upstream_issues_watcher": {
                "enabled": True,
                "poll_interval_minutes": 5,
                "repos": [{"repo": "openclaw/openclaw", "author": "cjalden"}],
            },
        },
    })
    cfg = get_feature_config("upstream_issues_watcher", p)
    assert cfg["poll_interval_minutes"] == 5
    assert cfg["repos"][0]["repo"] == "openclaw/openclaw"


def test_get_feature_config_returns_empty_when_missing(install_json):
    p = install_json({"feature_profile": "developer"})
    assert get_feature_config("upstream_issues_watcher", p) == {}


def test_get_feature_config_returns_empty_when_install_json_absent(tmp_path: Path):
    assert get_feature_config("anything", tmp_path / "nope.json") == {}


def test_get_feature_config_returns_empty_for_malformed_install_json(tmp_path: Path):
    p = tmp_path / "install.json"
    p.write_text("{{{ not json")
    assert get_feature_config("anything", p) == {}


# ── Permissiveness guarantee ────────────────────────────────────────────────


def test_no_exception_paths_raise_on_garbage_input(tmp_path: Path):
    """Gating decisions must never crash the calling monitor.

    Any read of a malformed/missing/permission-denied install.json
    should return safe defaults, never propagate the underlying error.
    """
    cases = [
        tmp_path / "nope.json",                          # missing
        tmp_path / "permission-denied",                  # would-be-noisy
    ]
    # Add a corrupt file
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("\x00\x01garbage")
    cases.append(corrupt)
    # Add an empty file
    empty = tmp_path / "empty.json"
    empty.write_text("")
    cases.append(empty)

    for p in cases:
        # None of these should raise.
        assert get_feature_profile(p) == "standard"
        assert is_feature_enabled("upstream_issues_watcher", p) is False
        assert get_feature_config("upstream_issues_watcher", p) == {}

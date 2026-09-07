"""tests/test_rsi_profile.py — user profile model, storage, query, init.

Note: dimension_weights were removed in the weights deletion pass. The
profile is now archetype + sections + audit log only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from profile import (  # noqa: E402
    ARCHETYPE_MULTI_USER_MEMBER,
    ARCHETYPE_PRIMARY,
    ARCHETYPE_SINGLE_USER_MEMBER,
    PROFILE_SECTIONS,
    Profile,
    ProfileFrontmatter,
    create_default_profile,
    ensure_profile,
    load_profile,
    save_profile,
    section_text,
)


# ─────────────────────────────────────────────────────────────────────────────
# ProfileFrontmatter
# ─────────────────────────────────────────────────────────────────────────────


def test_frontmatter_rejects_empty_bot_id():
    with pytest.raises(ValueError):
        ProfileFrontmatter(bot_id="")


def test_frontmatter_roundtrip_preserves_archetype():
    fm = ProfileFrontmatter(bot_id="team_bot_a", archetype=ARCHETYPE_PRIMARY)
    restored = ProfileFrontmatter.from_dict(fm.to_dict())
    assert restored.bot_id == "team_bot_a"
    assert restored.archetype == ARCHETYPE_PRIMARY


def test_frontmatter_roundtrip_without_archetype():
    fm = ProfileFrontmatter(bot_id="team_bot_a")
    restored = ProfileFrontmatter.from_dict(fm.to_dict())
    assert restored.bot_id == "team_bot_a"
    assert restored.archetype is None


# ─────────────────────────────────────────────────────────────────────────────
# Storage (create, load, save, roundtrip)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_default_profile_writes_file(tmp_path):
    p = create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    assert (tmp_path / "profiles" / "team_bot_a.md").exists()
    assert p.bot_id == "team_bot_a"
    assert p.frontmatter.archetype == ARCHETYPE_SINGLE_USER_MEMBER


def test_create_default_profile_records_archetype(tmp_path):
    for arch in (
        ARCHETYPE_PRIMARY,
        ARCHETYPE_SINGLE_USER_MEMBER,
        ARCHETYPE_MULTI_USER_MEMBER,
    ):
        bot = f"bot_{arch}"
        p = create_default_profile(shared_dir=tmp_path, bot_id=bot, archetype=arch)
        assert p.frontmatter.archetype == arch
        loaded = load_profile(tmp_path, bot)
        assert loaded.frontmatter.archetype == arch


def test_load_profile_missing_returns_none(tmp_path):
    assert load_profile(tmp_path, "team_bot_a") is None


def test_save_profile_updates_updated_at(tmp_path):
    p = create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    before = p.frontmatter.updated_at
    p.sections["Interests"] = "running"
    save_profile(p, tmp_path)
    reloaded = load_profile(tmp_path, "team_bot_a")
    assert reloaded is not None
    assert reloaded.sections.get("Interests", "").strip() == "running"
    assert reloaded.frontmatter.updated_at >= before


def test_create_default_profile_preserves_existing_content(tmp_path):
    p = create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    p.sections["Interests"] = "woodworking"
    save_profile(p, tmp_path)

    again = create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    assert "woodworking" in again.sections.get("Interests", "")


def test_rendered_profile_contains_all_sections(tmp_path):
    create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    text = (tmp_path / "profiles" / "team_bot_a.md").read_text(encoding="utf-8")
    for section in PROFILE_SECTIONS:
        assert f"## {section}" in text
    assert text.startswith("---\n")
    assert "---" in text.split("\n\n", 1)[0]


def test_rendered_frontmatter_no_dimension_weights(tmp_path):
    """Regression: frontmatter must NOT carry dimension_weights anymore."""
    create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    text = (tmp_path / "profiles" / "team_bot_a.md").read_text(encoding="utf-8")
    assert "dimension_weights" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Legacy frontmatter tolerance — old files with dimension_weights still load
# ─────────────────────────────────────────────────────────────────────────────


def test_legacy_profile_with_dimension_weights_still_loads(tmp_path):
    """Profiles written by older revisions carried a dimension_weights block.
    The reader must tolerate it (ignore the block) without raising."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    legacy = """---
bot_id: legacy_bot
schema_version: 1
created_at: 2026-04-15T10:23:45+00:00
updated_at: 2026-05-01T14:30:12+00:00
dimension_weights:
  utility: 1.2
  hygiene: 0.5
---

# User Profile — legacy_bot

## Interests
running
"""
    (profiles_dir / "legacy_bot.md").write_text(legacy, encoding="utf-8")
    p = load_profile(tmp_path, "legacy_bot")
    assert p is not None
    assert p.bot_id == "legacy_bot"
    assert "running" in p.sections.get("Interests", "")


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_section_text_returns_empty_when_missing(tmp_path):
    assert section_text(tmp_path, "team_bot_a", "Interests") == ""


def test_ensure_profile_creates_when_missing(tmp_path):
    p = ensure_profile(tmp_path, "team_bot_a", archetype=ARCHETYPE_SINGLE_USER_MEMBER)
    assert p.bot_id == "team_bot_a"
    assert (tmp_path / "profiles" / "team_bot_a.md").exists()


def test_ensure_profile_returns_existing(tmp_path):
    create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    p = ensure_profile(tmp_path, "team_bot_a")
    assert p.bot_id == "team_bot_a"


# ─────────────────────────────────────────────────────────────────────────────
# Preserving user-added sections
# ─────────────────────────────────────────────────────────────────────────────


def test_user_edited_unknown_section_preserved(tmp_path):
    create_default_profile(
        shared_dir=tmp_path,
        bot_id="team_bot_a",
        archetype=ARCHETYPE_SINGLE_USER_MEMBER,
    )
    path = tmp_path / "profiles" / "team_bot_a.md"
    text = path.read_text(encoding="utf-8")
    text += "\n\n## Custom Notes\n\nSomething personal.\n"
    path.write_text(text, encoding="utf-8")

    loaded = load_profile(tmp_path, "team_bot_a")
    assert "Something personal" in loaded.sections.get("Custom Notes", "")

    save_profile(loaded, tmp_path)
    text2 = path.read_text(encoding="utf-8")
    assert "## Custom Notes" in text2
    assert "Something personal" in text2

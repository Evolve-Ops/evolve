"""Tests for user_profile (per-user, bot-side profile, v2).

Spec: docs/spec-user-profile-2026-05-07.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from user_profile import (  # noqa: E402
    AUDIT_LOG_SECTION,
    USER_PROFILE_SCHEMA_VERSION,
    USER_PROFILE_SECTIONS,
    UserProfile,
    UserProfileFrontmatter,
    load_profile,
    profile_path,
    save_profile,
    status_path,
    update_status,
    wipe_for_dnt,
)


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter
# ─────────────────────────────────────────────────────────────────────────────


def test_frontmatter_requires_user_key_and_bot_id():
    with pytest.raises(ValueError):
        UserProfileFrontmatter(user_key="", bot_id="team_bot_a")
    with pytest.raises(ValueError):
        UserProfileFrontmatter(user_key="alice", bot_id="")


def test_frontmatter_defaults():
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    assert fm.schema_version == USER_PROFILE_SCHEMA_VERSION
    assert fm.tracking_enabled is True
    assert fm.created_at  # auto-populated
    assert fm.updated_at


def test_frontmatter_round_trip_dict():
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a", tracking_enabled=False)
    d = fm.to_dict()
    fm2 = UserProfileFrontmatter.from_dict(d)
    assert fm2.user_key == "alice"
    assert fm2.bot_id == "team_bot_a"
    assert fm2.tracking_enabled is False


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def test_profile_path_simple_user_key(tmp_path):
    p = profile_path(tmp_path, "alice")
    assert p == tmp_path / ".openclaw/profiles/alice.md"


def test_profile_path_escapes_unsafe_chars(tmp_path):
    p = profile_path(tmp_path, "slack:U0123/foo")
    assert p == tmp_path / ".openclaw/profiles/slack__U0123_foo.md"


def test_profile_path_rewrites_leading_dot(tmp_path):
    """A user_key starting with ``.`` would produce a dotfile-shaped
    profile that the status aggregator silently skips. ``safe_user_key``
    rewrites the leading dot to an underscore."""
    p = profile_path(tmp_path, ".sneaky")
    assert p == tmp_path / ".openclaw/profiles/_sneaky.md"


def test_status_path(tmp_path):
    assert status_path(tmp_path) == tmp_path / ".openclaw/profiles/.status.json"


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_save_and_load_round_trip(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(
        frontmatter=fm,
        sections={
            "Goals": "Ship the book by year-end.",
            "Vocation": "Software engineer at a small startup.",
        },
    )
    save_profile(profile, tmp_path)

    loaded = load_profile(tmp_path, "alice")
    assert loaded is not None
    assert loaded.user_key == "alice"
    assert loaded.bot_id == "team_bot_a"
    assert loaded.tracking_enabled is True
    assert loaded.sections["Goals"] == "Ship the book by year-end."
    assert loaded.sections["Vocation"] == "Software engineer at a small startup."


def test_load_returns_none_for_missing_file(tmp_path):
    assert load_profile(tmp_path, "nobody") is None


def test_save_creates_parent_directories(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm)
    save_profile(profile, tmp_path)
    assert (tmp_path / ".openclaw/profiles").is_dir()


def test_save_writes_with_mode_600(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm)
    path = save_profile(profile, tmp_path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_save_updates_updated_at(tmp_path):
    fm = UserProfileFrontmatter(
        user_key="alice", bot_id="team_bot_a", updated_at="2020-01-01T00:00:00+00:00"
    )
    profile = UserProfile(frontmatter=fm)
    save_profile(profile, tmp_path)
    loaded = load_profile(tmp_path, "alice")
    assert loaded.frontmatter.updated_at != "2020-01-01T00:00:00+00:00"


def test_unknown_sections_are_preserved(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(
        frontmatter=fm,
        sections={"Goals": "x", "User Custom Section": "user added this manually"},
    )
    save_profile(profile, tmp_path)
    loaded = load_profile(tmp_path, "alice")
    assert loaded.sections["User Custom Section"] == "user added this manually"


def test_empty_sections_render_as_just_heading(tmp_path):
    """v2 deliberately drops the ``<!-- no entries yet -->`` placeholder."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm)
    path = save_profile(profile, tmp_path)
    text = path.read_text()
    assert "<!-- no entries yet -->" not in text


# ─────────────────────────────────────────────────────────────────────────────
# has_content
# ─────────────────────────────────────────────────────────────────────────────


def test_has_content_false_for_empty_profile(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm)
    assert profile.has_content() is False


def test_has_content_true_when_section_populated(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm, sections={"Vocation": "engineer"})
    assert profile.has_content() is True


def test_has_content_ignores_audit_log_alone():
    """Audit Log entries can describe past facts, but the operational
    state of "what the bot knows" is the section bodies. A profile with
    only Audit Log entries reports has_content=False.
    """
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(
        frontmatter=fm,
        sections={AUDIT_LOG_SECTION: "- 2026-01-01: removed Vocation entry"},
    )
    assert profile.has_content() is False


def test_has_content_ignores_prelude():
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm, sections={"_prelude": "intro paragraph"})
    assert profile.has_content() is False


# ─────────────────────────────────────────────────────────────────────────────
# Status file
# ─────────────────────────────────────────────────────────────────────────────


def test_update_status_no_profiles_yet(tmp_path):
    update_status(tmp_path)
    s = json.loads(status_path(tmp_path).read_text())
    assert s == {"any_has_content": False}


def test_update_status_with_empty_profile(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)
    s = json.loads(status_path(tmp_path).read_text())
    assert s["any_has_content"] is False


def test_update_status_with_populated_profile(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(
        UserProfile(frontmatter=fm, sections={"Goals": "ship"}), tmp_path
    )
    s = json.loads(status_path(tmp_path).read_text())
    assert s["any_has_content"] is True


def test_update_status_aggregates_across_users(tmp_path):
    """Multi-user bot: any user with content flips the bit."""
    save_profile(
        UserProfile(
            frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_c"),
        ),
        tmp_path,
    )
    save_profile(
        UserProfile(
            frontmatter=UserProfileFrontmatter(user_key="bob", bot_id="team_bot_c"),
            sections={"Vocation": "rancher"},
        ),
        tmp_path,
    )
    s = json.loads(status_path(tmp_path).read_text())
    assert s["any_has_content"] is True


def test_status_file_is_world_readable_mode_644(tmp_path):
    update_status(tmp_path)
    mode = status_path(tmp_path).stat().st_mode & 0o777
    assert mode == 0o644


def test_status_does_not_leak_user_list(tmp_path):
    """D6 privacy rationale: status file must not expose user keys."""
    save_profile(
        UserProfile(
            frontmatter=UserProfileFrontmatter(user_key="alice", bot_id="team_bot_c"),
            sections={"Goals": "x"},
        ),
        tmp_path,
    )
    save_profile(
        UserProfile(
            frontmatter=UserProfileFrontmatter(user_key="bob", bot_id="team_bot_c"),
            sections={"Goals": "y"},
        ),
        tmp_path,
    )
    s = json.loads(status_path(tmp_path).read_text())
    assert set(s.keys()) == {"any_has_content"}
    text = status_path(tmp_path).read_text()
    assert "alice" not in text
    assert "bob" not in text


# ─────────────────────────────────────────────────────────────────────────────
# DNT wipe
# ─────────────────────────────────────────────────────────────────────────────


def test_wipe_for_dnt_clears_sections_and_audit_log():
    fm = UserProfileFrontmatter(
        user_key="alice", bot_id="team_bot_a", tracking_enabled=False
    )
    profile = UserProfile(
        frontmatter=fm,
        sections={
            "Goals": "ship the book",
            "Vocation": "engineer",
            "_prelude": "intro paragraph",
            AUDIT_LOG_SECTION: "- 2026-04-01: added Vocation",
        },
    )
    wipe_for_dnt(profile)
    assert "Goals" not in profile.sections
    assert "Vocation" not in profile.sections
    assert AUDIT_LOG_SECTION not in profile.sections
    # _prelude (the file's intro/preamble) is structural, not user content;
    # keeping it preserves the file's self-explanatory header.
    assert profile.sections.get("_prelude") == "intro paragraph"


def test_wipe_for_dnt_rejects_when_flag_still_true():
    """Calling wipe with tracking_enabled=True is a programming error —
    the post-wipe state should be tracking_enabled=False, and the caller
    must set the flag first so the operation is atomic on save."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(
        frontmatter=fm, sections={"Goals": "ship"}
    )
    with pytest.raises(ValueError):
        wipe_for_dnt(profile)


def test_dnt_round_trip_via_disk(tmp_path):
    """End-to-end: populate → flip DNT → wipe → save → reload sees empty."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(
        frontmatter=fm,
        sections={
            "Goals": "ship the book",
            "Vocation": "engineer",
            AUDIT_LOG_SECTION: "- 2026-04-01: added Vocation",
        },
    )
    save_profile(profile, tmp_path)

    # Flip DNT, wipe, save
    profile.frontmatter.tracking_enabled = False
    wipe_for_dnt(profile)
    save_profile(profile, tmp_path)

    loaded = load_profile(tmp_path, "alice")
    assert loaded is not None
    assert loaded.tracking_enabled is False
    assert loaded.sections.get("Goals", "") == ""
    assert loaded.sections.get("Vocation", "") == ""
    assert loaded.sections.get(AUDIT_LOG_SECTION, "") == ""

    # Status now reports no content
    s = json.loads(status_path(tmp_path).read_text())
    assert s["any_has_content"] is False


def test_dnt_user_with_explicit_content_still_counts():
    """D5b: under DNT, the user can still explicitly populate sections.
    Such content should still flip has_content. DNT gates *passive*
    observation, not user agency."""
    fm = UserProfileFrontmatter(
        user_key="alice", bot_id="team_bot_a", tracking_enabled=False
    )
    profile = UserProfile(
        frontmatter=fm, sections={"Goals": "explicit user input"}
    )
    assert profile.has_content() is True


# ─────────────────────────────────────────────────────────────────────────────
# Section ordering / file structure
# ─────────────────────────────────────────────────────────────────────────────


def test_render_includes_all_sections_in_order(tmp_path):
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    profile = UserProfile(frontmatter=fm)
    path = save_profile(profile, tmp_path)
    text = path.read_text()
    last_pos = -1
    for section in USER_PROFILE_SECTIONS:
        pos = text.find(f"## {section}")
        assert pos > last_pos, f"section {section!r} out of order or missing"
        last_pos = pos


def test_render_includes_dnt_hint_in_header(tmp_path):
    """The file's preamble should mention `evo profile dnt on` so a user
    who opens the file directly discovers the opt-out."""
    fm = UserProfileFrontmatter(user_key="alice", bot_id="team_bot_a")
    save_profile(UserProfile(frontmatter=fm), tmp_path)
    text = profile_path(tmp_path, "alice").read_text()
    assert "evo profile dnt on" in text

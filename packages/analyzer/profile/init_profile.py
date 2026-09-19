"""profile.init_profile — Create a default profile file at bot deploy time.

Spec: docs/archive/specs/spec-rsi-layer-4-adjacency-profile-2026-04-18.md §6.1.

The deploy flow (or a backfill script) invokes ``create_default_profile``
when a bot is first added to the pod. The body is empty placeholders;
the inference loop populates fields over time.
"""

from __future__ import annotations

from pathlib import Path

from profile.model import (
    ARCHETYPE_SINGLE_USER_MEMBER,
    PROFILE_SECTIONS,
    Profile,
    ProfileFrontmatter,
)
from profile.storage import profile_path, save_profile


def create_default_profile(
    *,
    shared_dir: Path,
    bot_id: str,
    archetype: str | None = None,
    overwrite_existing: bool = False,
) -> Profile:
    """Create a default profile for a bot. Persists to disk.

    Args:
        shared_dir: Evolve shared directory
        bot_id: the bot to create a profile for
        archetype: ``primary`` | ``single_user_member`` | ``multi_user_member``;
            defaults to single_user_member if not specified.
        overwrite_existing: if True, replace an existing profile file. The
            default False preserves existing user-edited content.

    Returns the Profile.
    """
    if archetype is None:
        archetype = ARCHETYPE_SINGLE_USER_MEMBER

    path = profile_path(shared_dir, bot_id)
    if path.exists() and not overwrite_existing:
        # Return whatever's on disk (load_profile handles it; if it's
        # malformed the caller should deal)
        from profile.storage import load_profile as _load

        existing = _load(shared_dir, bot_id)
        if existing is not None:
            return existing

    frontmatter = ProfileFrontmatter(
        bot_id=bot_id,
        archetype=archetype,
    )
    sections = {section: "" for section in PROFILE_SECTIONS}
    sections["_prelude"] = (
        "This file describes the user this bot serves. "
        "You may edit it freely; the bot reads from it to tailor its behavior."
    )

    profile = Profile(frontmatter=frontmatter, sections=sections)
    save_profile(profile, shared_dir)
    return profile


def resolve_archetype_from_bot_config(
    *, role: str, multi_user: bool
) -> str:
    """Map bot role + multi_user to a profile archetype."""
    from profile.model import (
        ARCHETYPE_MULTI_USER_MEMBER,
        ARCHETYPE_PRIMARY,
        ARCHETYPE_SINGLE_USER_MEMBER,
    )

    if role == "primary":
        return ARCHETYPE_PRIMARY
    if multi_user:
        return ARCHETYPE_MULTI_USER_MEMBER
    return ARCHETYPE_SINGLE_USER_MEMBER

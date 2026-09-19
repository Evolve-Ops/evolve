"""profile.query — Convenience queries against a loaded profile.

Other modules call these rather than reaching into the Profile dataclass
directly.
"""

from __future__ import annotations

from pathlib import Path

from profile.model import Profile
from profile.storage import load_profile


def section_text(
    shared_dir: Path, bot_id: str, section: str
) -> str:
    """Return the raw markdown of a named profile section (empty if absent)."""
    profile = load_profile(shared_dir, bot_id)
    if profile is None:
        return ""
    return profile.sections.get(section, "")


def ensure_profile(
    shared_dir: Path,
    bot_id: str,
    *,
    archetype: str | None = None,
) -> Profile:
    """Return a profile for the bot, creating defaults if none exists.

    Called lazily from places that need a profile — if the bot hasn't been
    through the deploy-time backfill, this creates the file on the fly.
    """
    profile = load_profile(shared_dir, bot_id)
    if profile is not None:
        return profile

    from profile.init_profile import create_default_profile

    return create_default_profile(
        shared_dir=shared_dir,
        bot_id=bot_id,
        archetype=archetype,
    )

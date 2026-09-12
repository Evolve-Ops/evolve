"""user_profile — Per-user, bot-side profile (v2).

Spec: internal/spec-user-profile-2026-05-07.md.

One markdown file per (bot, user) at ``~/.openclaw/profiles/<user_key>.md``
on the bot user's home, owned by the bot user (mode 600). Each bot maintains
a separate profile per user_key — single-user bots have one file; multi-user
bots have N. The admin server can read only ``.status.json`` (binary signal
of whether any user has content), never the profile bodies themselves.

This module is the storage layer. The user_profile_inferrer generator (Step 2
of the migration) writes facts directly here; the evo wizard's PHASE_ABOUT_YOU
and PHASE_GOALS extractions also land here (Step 3); ``evo profile`` reads
and edits (Step 4).

Distinct from ``profile/``, which currently holds bot-admin config (archetype,
cadence, timezone) at ``{shared_dir}/profiles/<bot_id>.md``. The two stores
serve different audiences and have different privacy semantics.
"""

from user_profile.model import (
    AUDIT_LOG_SECTION,
    USER_PROFILE_SCHEMA_VERSION,
    USER_PROFILE_SECTIONS,
    UserProfile,
    UserProfileFrontmatter,
)
from user_profile.storage import (
    PROFILE_FILE_MODE,
    PROFILES_SUBDIR,
    STATUS_FILE_MODE,
    STATUS_FILENAME,
    load_profile,
    profile_path,
    render,
    safe_user_key,
    save_profile,
    status_path,
    update_status,
    wipe_for_dnt,
)

__all__ = [
    # model
    "AUDIT_LOG_SECTION",
    "USER_PROFILE_SCHEMA_VERSION",
    "USER_PROFILE_SECTIONS",
    "UserProfile",
    "UserProfileFrontmatter",
    # storage
    "PROFILE_FILE_MODE",
    "PROFILES_SUBDIR",
    "STATUS_FILE_MODE",
    "STATUS_FILENAME",
    "load_profile",
    "profile_path",
    "render",
    "safe_user_key",
    "save_profile",
    "status_path",
    "update_status",
    "wipe_for_dnt",
]

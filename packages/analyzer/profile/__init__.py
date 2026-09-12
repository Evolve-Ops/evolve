"""profile — User profile skeleton (L4).

Spec: docs/archive/specs/spec-rsi-layer-4-adjacency-profile-2026-04-18.md §6.

One markdown file per bot at ``{shared_dir}/profiles/{bot_id}.md``:

  - YAML frontmatter with bot_id, schema_version, timestamps, archetype
  - Body with fixed sections (demographics, vocation, interests, family,
    communication_preferences, values, constraints, audit_log)
  - Provenance comments inline in body for every inferred field (L5+)

L4 ships the mechanism (storage, reader, writer, bot-deploy hook). It
does NOT ship inference — profiles created in L4 are empty placeholders.
L5 adds the inference loop.

Note: dimension weights were removed in the weights deletion pass. The
referee ranks proposals by `urgency × authority + tiebreak` only.
"""

from profile.model import (
    ALL_ARCHETYPES,
    ALL_CADENCES,
    ARCHETYPE_PRIMARY,
    ARCHETYPE_SINGLE_USER_MEMBER,
    ARCHETYPE_MULTI_USER_MEMBER,
    CADENCE_AS_IT_ARISES,
    CADENCE_DAILY,
    CADENCE_URGENT_ONLY,
    CADENCE_WEEKLY,
    PROFILE_SCHEMA_VERSION,
    PROFILE_SECTIONS,
    Profile,
    ProfileFrontmatter,
)
from profile.storage import load_profile, save_profile, profile_path
from profile.query import (
    section_text,
    ensure_profile,
)
from profile.init_profile import create_default_profile

__all__ = [
    # model
    "ALL_ARCHETYPES",
    "ALL_CADENCES",
    "ARCHETYPE_PRIMARY",
    "ARCHETYPE_SINGLE_USER_MEMBER",
    "ARCHETYPE_MULTI_USER_MEMBER",
    "CADENCE_AS_IT_ARISES",
    "CADENCE_DAILY",
    "CADENCE_URGENT_ONLY",
    "CADENCE_WEEKLY",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_SECTIONS",
    "Profile",
    "ProfileFrontmatter",
    # storage
    "load_profile",
    "save_profile",
    "profile_path",
    # query
    "section_text",
    "ensure_profile",
    # init
    "create_default_profile",
]

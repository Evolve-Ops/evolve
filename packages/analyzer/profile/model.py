"""profile.model — Profile dataclass and archetype constants.

Spec: docs/archive/specs/spec-rsi-layer-4-adjacency-profile-2026-04-18.md §3.1–§3.3.

Note: dimension_weights were removed in the weights deletion pass — the
referee now ranks by `urgency × authority + tiebreak` only, so per-bot
weight tuning is no longer a thing. Archetype is preserved as a label on
the bot (who uses this bot — primary user, single-user member, multi-user
member) so a future Phase 3 "Bot setup" surface can drive concrete
behavior off it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from evolve_util import now_iso_offset as _utc_now_iso


PROFILE_SCHEMA_VERSION = 1


# Fixed section names in the body. Order is the display order.
PROFILE_SECTIONS: tuple[str, ...] = (
    "Demographics",
    "Vocation",
    "Interests",
    "Family",
    "Communication Preferences",
    "Values",
    "Constraints",
)

AUDIT_LOG_SECTION = "Audit Log"


# ─────────────────────────────────────────────────────────────────────────────
# Archetypes — kept as a label that maps to "who uses this bot"
# ─────────────────────────────────────────────────────────────────────────────

ARCHETYPE_PRIMARY = "primary"
ARCHETYPE_SINGLE_USER_MEMBER = "single_user_member"
ARCHETYPE_MULTI_USER_MEMBER = "multi_user_member"

Archetype = Literal["primary", "single_user_member", "multi_user_member"]

ALL_ARCHETYPES: tuple[str, ...] = (
    ARCHETYPE_PRIMARY,
    ARCHETYPE_SINGLE_USER_MEMBER,
    ARCHETYPE_MULTI_USER_MEMBER,
)


# Surfacing cadence — how often the bot's primary user wants the proposals
# queue to surface items. Affects filtering at list time, not what generators
# emit.
CADENCE_AS_IT_ARISES = "as_it_arises"  # default — show everything as soon as it's pending
CADENCE_DAILY = "daily"                # cap to ~1/day worth of proposals on display
CADENCE_WEEKLY = "weekly"              # cap to 1 non-critical proposal per week
CADENCE_URGENT_ONLY = "urgent_only"    # only show security_critical / operational_urgent

Cadence = Literal["as_it_arises", "daily", "weekly", "urgent_only"]

ALL_CADENCES: tuple[str, ...] = (
    CADENCE_AS_IT_ARISES,
    CADENCE_DAILY,
    CADENCE_WEEKLY,
    CADENCE_URGENT_ONLY,
)


# ─────────────────────────────────────────────────────────────────────────────
# ProfileFrontmatter
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProfileFrontmatter:
    bot_id: str
    schema_version: int = PROFILE_SCHEMA_VERSION
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    archetype: str | None = None
    surfacing_cadence: str | None = None  # None → CADENCE_AS_IT_ARISES at read time
    timezone: str | None = None  # IANA name; None → fall back to pod-wide timezone

    def __post_init__(self) -> None:
        if not self.bot_id:
            raise ValueError("ProfileFrontmatter.bot_id must be non-empty")
        if self.surfacing_cadence is not None and self.surfacing_cadence not in ALL_CADENCES:
            raise ValueError(
                f"ProfileFrontmatter.surfacing_cadence={self.surfacing_cadence!r} "
                f"not in {list(ALL_CADENCES)}"
            )

    def to_dict(self) -> dict:
        out = {
            "bot_id": self.bot_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.archetype is not None:
            out["archetype"] = self.archetype
        if self.surfacing_cadence is not None:
            out["surfacing_cadence"] = self.surfacing_cadence
        if self.timezone is not None:
            out["timezone"] = self.timezone
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileFrontmatter":
        # PyYAML auto-parses ISO-8601 strings into datetime objects; coerce
        # back to strings so the dataclass always holds strings regardless
        # of how the file was loaded.
        archetype = data.get("archetype")
        if archetype is not None:
            archetype = str(archetype)
        cadence = data.get("surfacing_cadence")
        if cadence is not None:
            cadence = str(cadence)
            # Tolerate unknown cadences by dropping them — the frontmatter
            # can be hand-edited and we shouldn't 500 on a typo.
            if cadence not in ALL_CADENCES:
                cadence = None
        tz = data.get("timezone")
        if tz is not None:
            tz = str(tz).strip() or None
        return cls(
            bot_id=str(data["bot_id"]),
            schema_version=int(data.get("schema_version", PROFILE_SCHEMA_VERSION)),
            created_at=_as_iso_string(data.get("created_at"), default=_utc_now_iso),
            updated_at=_as_iso_string(data.get("updated_at"), default=_utc_now_iso),
            archetype=archetype,
            surfacing_cadence=cadence,
            timezone=tz,
        )


def _as_iso_string(value, *, default):
    """Coerce a YAML-parsed timestamp (datetime or string) to an ISO string."""
    if value is None:
        return default()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds")
    return str(value)


# ─────────────────────────────────────────────────────────────────────────────
# Profile
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Profile:
    """A loaded profile: frontmatter + body sections.

    Body sections are stored as a dict of section_name → raw markdown
    content. L4 doesn't parse the inline provenance comments (that's L5
    when inference actually runs); the raw body is preserved.
    """

    frontmatter: ProfileFrontmatter
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def bot_id(self) -> str:
        return self.frontmatter.bot_id

"""user_profile.model — UserProfile dataclass + section constants.

Spec: docs/spec-user-profile-2026-05-07.md §D2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from evolve_util import now_iso_offset as _utc_now_iso


USER_PROFILE_SCHEMA_VERSION = 2


USER_PROFILE_SECTIONS: tuple[str, ...] = (
    "Goals",
    "Demographics",
    "Vocation",
    "Interests",
    "Family",
    "Communication Preferences",
    "Values",
    "Constraints",
)

AUDIT_LOG_SECTION = "Audit Log"


@dataclass
class UserProfileFrontmatter:
    user_key: str
    bot_id: str
    schema_version: int = USER_PROFILE_SCHEMA_VERSION
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    tracking_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.user_key:
            raise ValueError("UserProfileFrontmatter.user_key must be non-empty")
        if not self.bot_id:
            raise ValueError("UserProfileFrontmatter.bot_id must be non-empty")

    def to_dict(self) -> dict:
        return {
            "user_key": self.user_key,
            "bot_id": self.bot_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tracking_enabled": self.tracking_enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfileFrontmatter":
        # PyYAML may parse timestamps as datetime; coerce to ISO strings.
        return cls(
            user_key=str(data["user_key"]),
            bot_id=str(data["bot_id"]),
            schema_version=int(data.get("schema_version", USER_PROFILE_SCHEMA_VERSION)),
            created_at=_as_iso_string(data.get("created_at"), default=_utc_now_iso),
            updated_at=_as_iso_string(data.get("updated_at"), default=_utc_now_iso),
            tracking_enabled=bool(data.get("tracking_enabled", True)),
        )


def _as_iso_string(value, *, default):
    if value is None:
        return default()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds")
    return str(value)


@dataclass
class UserProfile:
    frontmatter: UserProfileFrontmatter
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def user_key(self) -> str:
        return self.frontmatter.user_key

    @property
    def bot_id(self) -> str:
        return self.frontmatter.bot_id

    @property
    def tracking_enabled(self) -> bool:
        return self.frontmatter.tracking_enabled

    def has_content(self) -> bool:
        """True if any non-prelude section has actual user-facing content.

        Empty sections (no body under the heading) and the Audit Log alone
        do not count — Audit Log entries can reconstruct facts, but if the
        only entries are 'no facts recorded', that's still no content.
        """
        for name, body in self.sections.items():
            if name == "_prelude" or name == AUDIT_LOG_SECTION:
                continue
            if (body or "").strip():
                return True
        return False

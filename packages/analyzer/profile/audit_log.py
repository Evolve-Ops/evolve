"""profile.audit_log — Append entries to the profile's audit log section.

Spec §3.4. Every automated profile change appends a one-liner with
timestamp + description, visible to the user.
"""

from __future__ import annotations

from evolve_util import now_iso_offset as _utc_now_iso

from profile.model import AUDIT_LOG_SECTION, Profile
from profile.storage import save_profile
from pathlib import Path


def append_audit_entry(
    profile: Profile,
    *,
    description: str,
    source: str = "auto",
    shared_dir: Path | None = None,
) -> None:
    """Append an audit-log entry to ``profile`` and (optionally) persist.

    Args:
        profile: the in-memory profile
        description: short human-readable line (no PII)
        source: where the change came from — "auto", "user-confirmed", etc.
        shared_dir: if provided, the profile is saved to disk afterward.
    """
    existing = profile.sections.get(AUDIT_LOG_SECTION, "").strip()
    timestamp = _utc_now_iso()
    new_entry = f"- {timestamp} — {description} ({source})"
    combined = f"{existing}\n{new_entry}".strip() if existing else new_entry
    profile.sections[AUDIT_LOG_SECTION] = combined

    if shared_dir is not None:
        save_profile(profile, shared_dir)

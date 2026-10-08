"""profile.storage — read/write profile markdown files atomically.

Format: YAML frontmatter between ``---`` delimiters, then markdown body
with ``## Section Name`` headings. YAML is parsed with PyYAML if
available (standard in Evolve runtime), falls back to a tiny
key:value parser for the minimal frontmatter shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evolve_util import atomic_write_text as _atomic_write

from profile.model import (
    AUDIT_LOG_SECTION,
    PROFILE_SECTIONS,
    Profile,
    ProfileFrontmatter,
)


def profile_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "profiles" / f"{bot_id}.md"


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────


def load_profile(shared_dir: Path, bot_id: str) -> Profile | None:
    """Load a profile from disk. Returns None if the file doesn't exist.

    Malformed files return None — the caller should handle missing/broken
    profiles by either recreating from defaults or surfacing a repair
    proposal.
    """
    path = profile_path(shared_dir, bot_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _parse(text, bot_id=bot_id)
    except (ValueError, KeyError):
        return None


def _parse(text: str, *, bot_id: str) -> Profile:
    """Parse a profile markdown string."""
    frontmatter_raw, body = _split_frontmatter(text)
    fm_dict = _parse_yaml_ish(frontmatter_raw)
    # Ensure bot_id present
    if "bot_id" not in fm_dict:
        fm_dict["bot_id"] = bot_id
    frontmatter = ProfileFrontmatter.from_dict(fm_dict)
    sections = _split_sections(body)
    return Profile(frontmatter=frontmatter, sections=sections)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a document into ``(frontmatter_yaml, body)``."""
    # Expected shape:
    # ---
    # yaml
    # ---
    # body...
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    # Unterminated frontmatter — treat the whole thing as body
    return "", text


def _parse_yaml_ish(source: str) -> dict:
    """Parse YAML frontmatter. Try PyYAML; fall back to a narrow parser.

    The frontmatter is now flat (bot_id, schema_version, created_at,
    updated_at, archetype). No nested mappings.
    """
    if not source.strip():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(source)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _parse_narrow_yaml(source)


def _parse_narrow_yaml(source: str) -> dict:
    """Narrow YAML subset: flat top-level key:value pairs."""
    result: dict = {}
    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Skip indented lines — frontmatter is flat now, but tolerate
        # legacy files that still carry a nested dimension_weights block.
        if line.startswith(" "):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            key = k.strip()
            value = v.strip()
            if not value:
                continue  # legacy nested-key marker; ignore
            result[key] = _coerce_scalar(value)
    return result


def _coerce_scalar(raw: str):
    # Strip quotes
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    # Booleans
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "none":
        return None
    # Numbers
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _split_sections(body: str) -> dict[str, str]:
    """Split body into section_name → content dict.

    Section headings look like ``## Name``. Content is everything until
    the next heading. Unknown sections (not in PROFILE_SECTIONS) are
    preserved as-is — we're forgiving of user edits.
    """
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).strip()
            current_name = stripped[3:].strip()
            current_lines = []
        else:
            if current_name is None:
                # Before any section heading — usually the top-of-body
                # intro text; stash it under an empty key so we don't lose it.
                sections.setdefault("_prelude", []).append(line) if isinstance(
                    sections.get("_prelude"), list
                ) else None
                if not isinstance(sections.get("_prelude"), list):
                    sections["_prelude"] = [line]
                continue
            current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()

    # Join _prelude list into a string if present
    if isinstance(sections.get("_prelude"), list):
        sections["_prelude"] = "\n".join(sections["_prelude"]).strip()

    return sections


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────


def save_profile(profile: Profile, shared_dir: Path) -> Path:
    """Atomically write a profile to disk. Updates ``updated_at`` before writing."""
    profile.frontmatter.updated_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    text = render(profile)
    path = profile_path(shared_dir, profile.bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text)
    return path


def render(profile: Profile) -> str:
    """Render the profile to its canonical markdown representation."""
    frontmatter = _render_frontmatter(profile.frontmatter)
    body_parts = [f"# User Profile — {profile.bot_id}", ""]
    # Preserve _prelude if set
    prelude = profile.sections.get("_prelude", "")
    if prelude:
        body_parts.append(prelude)
        body_parts.append("")

    for section in PROFILE_SECTIONS:
        body_parts.append(f"## {section}")
        content = profile.sections.get(section, "").strip()
        body_parts.append(content or "<!-- no entries yet -->")
        body_parts.append("")

    # Audit log comes last if present
    audit_content = profile.sections.get(AUDIT_LOG_SECTION, "").strip()
    if audit_content:
        body_parts.append(f"## {AUDIT_LOG_SECTION}")
        body_parts.append(audit_content)
        body_parts.append("")

    # Preserve any other user-added sections not in the known set
    known = set(PROFILE_SECTIONS) | {AUDIT_LOG_SECTION, "_prelude"}
    for name, content in profile.sections.items():
        if name in known:
            continue
        body_parts.append(f"## {name}")
        body_parts.append(content.strip())
        body_parts.append("")

    return frontmatter + "\n".join(body_parts).rstrip() + "\n"


def _render_frontmatter(fm: ProfileFrontmatter) -> str:
    lines = ["---"]
    lines.append(f"bot_id: {fm.bot_id}")
    lines.append(f"schema_version: {fm.schema_version}")
    lines.append(f"created_at: {fm.created_at}")
    lines.append(f"updated_at: {fm.updated_at}")
    if fm.archetype is not None:
        lines.append(f"archetype: {fm.archetype}")
    if fm.surfacing_cadence is not None:
        lines.append(f"surfacing_cadence: {fm.surfacing_cadence}")
    if fm.timezone is not None:
        lines.append(f"timezone: {fm.timezone}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"

"""user_profile.storage — read/write per-user profile markdown files.

Spec: internal/spec-user-profile-2026-05-07.md.

Files live under the bot user's home at ``~/.openclaw/profiles/<user_key>.md``.
Empty sections in v2 are rendered as just the heading with no placeholder
body — different from v1's ``<!-- no entries yet -->`` convention. This
makes the has-content check trivial (any non-whitespace under any section
heading means content) and produces a cleaner file when a user opens it
directly.

The ``.status.json`` sibling is the only file in this directory the
admin server (running as ``evolve``) can read. It carries a single bit
``any_has_content``. See D6 in the spec for the privacy rationale.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evolve_util import atomic_write_text as _atomic_write

from user_profile.model import (
    AUDIT_LOG_SECTION,
    USER_PROFILE_SECTIONS,
    UserProfile,
    UserProfileFrontmatter,
)


PROFILES_SUBDIR = ".openclaw/profiles"
STATUS_FILENAME = ".status.json"
PROFILE_FILE_MODE = 0o600
STATUS_FILE_MODE = 0o644


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def safe_user_key(user_key: str) -> str:
    """Filesystem-safe form of a user key.

    Slack/Telegram IDs sometimes contain colons or slashes (mapped to
    ``__`` and ``_`` respectively). Leading dots are also rewritten to
    a leading underscore — the profiles directory uses dotfile naming
    (``.status.json``, atomic-write ``.<name>.<rand>.tmp``) for
    structural files, so a user_key beginning with ``.`` would produce
    a profile that the status aggregator silently skips.
    """
    safe = user_key.replace(":", "__").replace("/", "_")
    if safe.startswith("."):
        safe = "_" + safe[1:]
    return safe


# Legacy alias for callers that imported the underscore-prefixed name.
_safe_user_key = safe_user_key


def profile_path(home_dir: Path, user_key: str) -> Path:
    return Path(home_dir) / PROFILES_SUBDIR / f"{safe_user_key(user_key)}.md"


def status_path(home_dir: Path) -> Path:
    return Path(home_dir) / PROFILES_SUBDIR / STATUS_FILENAME


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────


def load_profile(home_dir: Path, user_key: str) -> UserProfile | None:
    """Load a profile from disk. Returns None if the file doesn't exist
    or is malformed."""
    path = profile_path(home_dir, user_key)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _parse(text)
    except (ValueError, KeyError):
        return None


def _load_from_path(path: Path) -> UserProfile | None:
    if not path.exists():
        return None
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def _parse(text: str) -> UserProfile:
    frontmatter_raw, body = _split_frontmatter(text)
    fm_dict = _parse_yaml_ish(frontmatter_raw)
    frontmatter = UserProfileFrontmatter.from_dict(fm_dict)
    sections = _split_sections(body)
    return UserProfile(frontmatter=frontmatter, sections=sections)


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return "", text


def _parse_yaml_ish(source: str) -> dict:
    if not source.strip():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(source)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _parse_narrow_yaml(source)


def _parse_narrow_yaml(source: str) -> dict:
    result: dict = {}
    for raw_line in source.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith(" "):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            key = k.strip()
            value = v.strip()
            if not value:
                continue
            result[key] = _coerce_scalar(value)
    return result


def _coerce_scalar(raw: str):
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "none":
        return None
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
                # Pre-heading content (intro paragraph after the H1) goes
                # under "_prelude" — preserved on round-trip but never
                # counted as user-facing content.
                if "_prelude" not in sections:
                    sections["_prelude"] = ""
                sections["_prelude"] += line + "\n"
                continue
            current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).strip()

    if "_prelude" in sections:
        sections["_prelude"] = sections["_prelude"].strip()

    return sections


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────


def save_profile(profile: UserProfile, home_dir: Path) -> Path:
    """Atomically write a profile to disk. Updates ``updated_at`` before
    writing, then recomputes ``.status.json``.

    Also strips any inherited macOS ACL from the file (``chmod -N``) so the
    evolve-read ACE inherited from ``.openclaw/`` doesn't grant the admin
    server read access. POSIX mode 600 + no ACL = bot-user-only.
    Best-effort: chmod -N is silently skipped on platforms that don't
    support it (Linux test runners) — the inherited ACL only exists on
    deployed macOS bots, so the protection is correct where it matters.
    """
    profile.frontmatter.updated_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    text = render(profile)
    path = profile_path(home_dir, profile.user_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text, mode=PROFILE_FILE_MODE)
    _strip_macos_acl(path)
    update_status(home_dir)
    return path


def _strip_macos_acl(path: Path) -> None:
    """Best-effort ACL strip via ``chmod -N``. Silently no-ops if the
    chmod binary doesn't support -N (non-macOS)."""
    import subprocess
    try:
        subprocess.run(
            ["/bin/chmod", "-N", str(path)],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def render(profile: UserProfile) -> str:
    """Render the profile to its canonical markdown representation.

    Empty sections are rendered as just the ``## Heading`` line with no
    body — no placeholder comment.
    """
    frontmatter = _render_frontmatter(profile.frontmatter)
    body_parts = [
        f"# Profile — {profile.user_key} (bot: {profile.bot_id})",
        "",
        "<!-- This file is private to you. The bot reads from it to tailor",
        "     its behavior. Edit freely, or run `evo profile` to review and",
        "     correct what's been recorded. To stop the bot from learning",
        "     more about you, run `evo profile dnt on`. -->",
        "",
    ]

    prelude = profile.sections.get("_prelude", "").strip()
    if prelude:
        body_parts.append(prelude)
        body_parts.append("")

    for section in USER_PROFILE_SECTIONS:
        body_parts.append(f"## {section}")
        content = profile.sections.get(section, "").strip()
        if content:
            body_parts.append(content)
        body_parts.append("")

    audit = profile.sections.get(AUDIT_LOG_SECTION, "").strip()
    if audit:
        body_parts.append(f"## {AUDIT_LOG_SECTION}")
        body_parts.append(audit)
        body_parts.append("")

    # Preserve any user-added sections not in the known set.
    known = set(USER_PROFILE_SECTIONS) | {AUDIT_LOG_SECTION, "_prelude"}
    for name, content in profile.sections.items():
        if name in known:
            continue
        body_parts.append(f"## {name}")
        body_parts.append(content.strip())
        body_parts.append("")

    return frontmatter + "\n".join(body_parts).rstrip() + "\n"


def _render_frontmatter(fm: UserProfileFrontmatter) -> str:
    lines = ["---"]
    lines.append(f"user_key: {fm.user_key}")
    lines.append(f"bot_id: {fm.bot_id}")
    lines.append(f"schema_version: {fm.schema_version}")
    lines.append(f"created_at: {fm.created_at}")
    lines.append(f"updated_at: {fm.updated_at}")
    lines.append(f"tracking_enabled: {'true' if fm.tracking_enabled else 'false'}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Status file (D6)
# ─────────────────────────────────────────────────────────────────────────────


def update_status(home_dir: Path) -> Path:
    """Recompute ``.status.json`` from all profiles in this home.

    Single bit: ``any_has_content``. No user list, no per-user state — see
    D6 in the spec for the privacy rationale.
    """
    profiles_dir = Path(home_dir) / PROFILES_SUBDIR
    profiles_dir.mkdir(parents=True, exist_ok=True)

    any_content = False
    for entry in profiles_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".md":
            continue
        if entry.name.startswith("."):
            continue
        prof = _load_from_path(entry)
        if prof is None:
            continue
        if prof.has_content():
            any_content = True
            break

    status = {"any_has_content": any_content}
    path = status_path(home_dir)
    _atomic_write(path, json.dumps(status), mode=STATUS_FILE_MODE)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# DNT (D5b)
# ─────────────────────────────────────────────────────────────────────────────


def wipe_for_dnt(profile: UserProfile) -> UserProfile:
    """Clear all user-facing content and the Audit Log on a true → false
    DNT transition. Frontmatter (now with ``tracking_enabled = false``)
    is preserved so we know the flag is set.

    The caller is responsible for setting ``tracking_enabled = False`` and
    persisting via ``save_profile``. Calling ``wipe_for_dnt`` on a profile
    where the flag is still True is a programming error and raises.

    Returns the same profile, mutated in place.
    """
    if profile.frontmatter.tracking_enabled:
        raise ValueError(
            "wipe_for_dnt called with tracking_enabled=True; set the flag "
            "to False before wiping so the post-wipe state is coherent."
        )

    keep_keys = {"_prelude"}
    profile.sections = {k: v for k, v in profile.sections.items() if k in keep_keys}
    return profile

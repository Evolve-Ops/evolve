"""Bot guide storage — primary-authored markdown with YAML frontmatter.

Spec: internal/spec-evo-wizard-2026-05-05.md §5.

A bot guide is a per-bot, primary-authored document that says "here is
what this bot is for and how to use it." Two consumers:

  1. The bot's own runtime context — :mod:`session_surface` reads the
     guide and concatenates it into the system prompt at every session
     start, alongside SOUL.md.
  2. The secondary user's wizard (slice 4b / future) — phase 3 ("how to
     use this bot") renders from the guide's frontmatter (``audience``,
     ``tone``, ``do_say``, ``dont_say``) and prose body.

File location: ``{shared_dir}/bot_guides/{bot_id}.md``. Format:

.. code-block:: markdown

    ---
    bot_id: team_bot_a
    authored_by: pod_admin_user
    authored_at: 2026-05-06T15:30:00Z
    last_edited_at: 2026-05-06T15:30:00Z
    audience: "engineering team, ~6 people"
    tone: "direct, no emoji"
    do_say:
      - "use team_bot_a for deployment status questions"
    dont_say:
      - "don't ask team_bot_a about HR or payroll"
    ---

    # Team_bot_a — Team Guide

    Team_bot_a is the team's deployment and CI assistant…

The frontmatter is permissive: unknown keys are preserved on round-trip,
so we can add new structured fields without breaking existing files.
``last_edited_at`` is auto-stamped on write — the spec uses it as the
"this file is real, inject it" signal in :mod:`session_surface`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evolve_util import now_iso as _now_iso


_GUIDES_SUBDIR = "bot_guides"


@dataclass
class Guide:
    """In-memory representation of a bot guide. ``frontmatter`` preserves
    unknown keys verbatim so future schema additions round-trip cleanly."""

    bot_id: str
    body: str  # markdown body (no frontmatter)
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def last_edited_at(self) -> str | None:
        v = self.frontmatter.get("last_edited_at")
        return v if isinstance(v, str) and v else None

    @property
    def authored_at(self) -> str | None:
        v = self.frontmatter.get("authored_at")
        return v if isinstance(v, str) and v else None

    @property
    def authored_by(self) -> str | None:
        v = self.frontmatter.get("authored_by")
        return v if isinstance(v, str) and v else None


class GuideError(ValueError):
    """Raised when a guide file is malformed or input validation fails."""


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem layout
# ─────────────────────────────────────────────────────────────────────────────


def guide_path(shared_dir: Path, bot_id: str) -> Path:
    """Absolute path of the guide file for ``bot_id`` (regardless of whether
    it currently exists)."""
    return Path(shared_dir) / _GUIDES_SUBDIR / f"{bot_id}.md"


def guide_exists(shared_dir: Path, bot_id: str) -> bool:
    return guide_path(shared_dir, bot_id).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


def read_guide(shared_dir: Path, bot_id: str) -> Guide | None:
    """Read and parse the guide for ``bot_id``. Returns ``None`` if no file
    exists. Raises :class:`GuideError` if the file is present but malformed
    (so callers like the admin UI surface the error rather than silently
    treating a corrupt file as "no guide").

    :mod:`session_surface` uses :func:`load_for_session_surface` instead,
    which fails soft — a corrupt guide should never break a bot's session.
    """
    path = guide_path(shared_dir, bot_id)
    if not path.exists():
        return None

    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw)
    return Guide(bot_id=bot_id, frontmatter=frontmatter, body=body)


def load_for_session_surface(shared_dir: Path, bot_id: str) -> Guide | None:
    """Like :func:`read_guide` but returns ``None`` on any error rather than
    raising — callers in the runtime hot path (session_surface) want soft
    failure so a malformed guide doesn't block bot startup. Also requires
    ``last_edited_at`` in the frontmatter (per spec §5.2: "only inject if
    last_edited_at is non-null") so empty placeholders are skipped.
    """
    try:
        guide = read_guide(shared_dir, bot_id)
    except GuideError:
        return None
    except (OSError, UnicodeDecodeError):
        return None
    if guide is None:
        return None
    if not guide.last_edited_at:
        return None
    return guide


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). Frontmatter is empty dict when
    none is present. Empty body is fine."""
    text = raw.lstrip("﻿")  # strip BOM if present
    if not text.startswith("---"):
        # No frontmatter — entire content is body.
        return {}, text

    # Find the closing fence on its own line. Use \n--- so we don't match
    # an inline triple-dash mid-document.
    rest = text[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    fence_idx = rest.find("\n---")
    if fence_idx == -1:
        raise GuideError("frontmatter opened with --- but never closed")
    fm_text = rest[:fence_idx]
    body_after = rest[fence_idx + len("\n---"):]
    # Eat the trailing newline immediately after the fence, then a single
    # optional blank line — the renderer puts ``---\n\n<body>`` for
    # readability and that blank line is part of our framing, not the body.
    if body_after.startswith("\r\n"):
        body_after = body_after[2:]
    elif body_after.startswith("\n"):
        body_after = body_after[1:]
    if body_after.startswith("\r\n"):
        body_after = body_after[2:]
    elif body_after.startswith("\n"):
        body_after = body_after[1:]

    try:
        loaded = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except yaml.YAMLError as e:
        raise GuideError(f"frontmatter is not valid YAML: {e}") from e
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise GuideError(
            f"frontmatter must be a YAML mapping, got {type(loaded).__name__}"
        )
    return loaded, body_after


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────


def write_guide(
    shared_dir: Path,
    bot_id: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    body: str = "",
    authored_by: str | None = None,
) -> Guide:
    """Write the guide for ``bot_id``. ``last_edited_at`` is stamped now;
    ``authored_at`` and ``authored_by`` are stamped only on first write
    (preserved on subsequent writes).

    Atomic via temp+rename in the same directory so the file is never
    half-written if we crash mid-write.

    Returns the :class:`Guide` as-stored (with stamped fields).
    """
    if not bot_id:
        raise GuideError("bot_id is required")

    fm = dict(frontmatter or {})
    fm["bot_id"] = bot_id

    now_iso = _now_iso()
    fm["last_edited_at"] = now_iso

    # Preserve existing authored_at if a previous version exists; otherwise
    # stamp it now. Same for authored_by — but always allow the caller to
    # override with an explicit value (admin UI / CLI passes through).
    existing = read_guide(shared_dir, bot_id) if guide_exists(shared_dir, bot_id) else None
    if "authored_at" not in fm:
        fm["authored_at"] = (
            existing.authored_at if existing and existing.authored_at else now_iso
        )
    if authored_by is not None:
        fm["authored_by"] = authored_by
    elif "authored_by" not in fm and existing and existing.authored_by:
        fm["authored_by"] = existing.authored_by

    target = guide_path(shared_dir, bot_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    rendered = _render(fm, body)

    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{bot_id}-",
        suffix=".md.tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return Guide(bot_id=bot_id, frontmatter=fm, body=body)


def _render(frontmatter: dict[str, Any], body: str) -> str:
    """Render guide back to disk format (frontmatter + body)."""
    fm_yaml = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")
    body_clean = body.lstrip("\n")  # avoid double-newline at body start
    if body_clean and not body_clean.endswith("\n"):
        body_clean = body_clean + "\n"
    return f"---\n{fm_yaml}\n---\n\n{body_clean}"

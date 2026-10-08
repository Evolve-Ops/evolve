"""Voice cue extraction for the rec_pending pitch (slice 5b8 day 2).

The bot's response to a Better Engine recommendation should sound like
the bot, not like a system notification. Three on-disk sources carry
voice signals:

1. **Bot guide** (``{shared_dir}/bot-guides/<bot_id>.{md,yaml}``) — the
   primary user's authored guidance. Preferred when present. The
   wizard already loads this for secondary-chain phases; the engine
   carries it into rec_pending's prompt context too.
2. **Bot SOUL.md** (``{bot_home}/.openclaw/workspace/SOUL.md``) — the
   bot's identity / tone declaration. Has a conventional ``## Tone``
   heading we can extract.
3. **Bot RSI profile** (``{shared_dir}/profiles/<bot_id>.md``) — has a
   "Communication Preferences" section the L4+ profile-inferrer
   populates. Empty placeholder by default; non-empty when inference
   has surfaced cues.

This module reads each source best-effort and returns plain strings
(or ``None`` when nothing useful is on disk). Callers compose the
result however the prompt template wants. No exceptions ever escape —
voice is a nice-to-have, not a wedge point.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...config import bot_home as _bot_home


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def voice_summary(
    shared_dir: Path,
    bot_id: str,
    *,
    network: dict[str, Any] | None = None,
    bot_guide: Any | None = None,
) -> dict[str, str]:
    """Compose a voice-cue dict from all three sources, preferring the
    most authoritative when multiple sources speak to the same field.

    Returned keys (only present when non-empty):
      * ``tone`` — short tone description (bot guide > SOUL.md tone
        section > profile communication preferences)
      * ``do_say`` — items the bot should lean toward (bot guide)
      * ``dont_say`` — items to avoid (bot guide)
      * ``soul_tone`` — raw SOUL.md "## Tone" body when present (kept
        separately so prompts can name the source if helpful)
      * ``profile_communication_preferences`` — body of the RSI
        profile "Communication Preferences" section when non-empty
    """
    out: dict[str, str] = {}

    # 1. Bot guide — passed in by caller; cheap to extract here.
    if bot_guide is not None:
        guide_tone = _str_field(bot_guide, "tone")
        if guide_tone:
            out["tone"] = guide_tone
        guide_do = _list_field(bot_guide, "do_say")
        if guide_do:
            out["do_say"] = "; ".join(guide_do)
        guide_dont = _list_field(bot_guide, "dont_say")
        if guide_dont:
            out["dont_say"] = "; ".join(guide_dont)

    # 2. SOUL.md tone section
    soul_tone = read_soul_tone(bot_id, network=network)
    if soul_tone:
        out["soul_tone"] = soul_tone
        # If the bot guide didn't supply a tone, surface SOUL's as the
        # canonical tone signal so the prompt has *something* to use.
        if "tone" not in out:
            out["tone"] = _condense(soul_tone, max_chars=160)

    # 3. RSI profile Communication Preferences
    prefs = read_profile_communication_preferences(shared_dir, bot_id)
    if prefs:
        out["profile_communication_preferences"] = prefs

    return out


# ─────────────────────────────────────────────────────────────────────────────
# SOUL.md
# ─────────────────────────────────────────────────────────────────────────────


def read_soul_tone(
    bot_id: str,
    *,
    network: dict[str, Any] | None = None,
) -> str | None:
    """Return the body of SOUL.md's ``## Tone`` section (without the
    heading), or ``None`` if SOUL.md is missing / unreadable / has no
    tone section. Trims whitespace and caps length so a runaway SOUL
    doesn't bloat the prompt."""
    try:
        soul_path = _bot_home(bot_id, network) / ".openclaw" / "workspace" / "SOUL.md"
    except Exception:
        return None
    if not soul_path.exists():
        return None
    try:
        text = soul_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    section = _extract_markdown_section(text, "Tone")
    return section.strip() if section else None


# ─────────────────────────────────────────────────────────────────────────────
# Bot RSI profile
# ─────────────────────────────────────────────────────────────────────────────


def read_profile_communication_preferences(
    shared_dir: Path,
    bot_id: str,
) -> str | None:
    """Return the body of the bot's RSI profile "Communication
    Preferences" section. Profile lives at
    ``{shared_dir}/profiles/<bot_id>.md`` (per the L4 profile module).
    Returns ``None`` when the file is absent / unreadable / has an
    empty section."""
    profile_path = Path(shared_dir) / "profiles" / f"{bot_id}.md"
    if not profile_path.exists():
        return None
    try:
        text = profile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    section = _extract_markdown_section(text, "Communication Preferences")
    if not section:
        return None
    cleaned = section.strip()
    if not cleaned or cleaned.lower() in ("(empty)", "_(empty)_", "(none)", "tbd"):
        return None
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


_SECTION_HEADING_RE = re.compile(r"^(#+)\s+(.+)\s*$")


def _extract_markdown_section(text: str, heading: str) -> str | None:
    """Return the body of the ``# heading`` / ``## heading`` section
    (case-insensitive on heading match), stopping at the next heading
    of equal or higher level. Returns ``None`` if the heading isn't
    found."""
    target = heading.strip().lower()
    lines = text.splitlines()
    capture = False
    capture_level = 0
    body_lines: list[str] = []

    for line in lines:
        m = _SECTION_HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            name = m.group(2).strip().lower()
            if not capture:
                if name == target:
                    capture = True
                    capture_level = level
                continue
            # Already capturing — does this heading close us out?
            if level <= capture_level:
                break
            # Sub-heading inside the section: include it verbatim.
            body_lines.append(line)
            continue
        if capture:
            body_lines.append(line)

    if not capture:
        return None
    return "\n".join(body_lines).strip("\n")


def _condense(text: str, *, max_chars: int) -> str:
    """Collapse whitespace and cap to ``max_chars`` so the prompt
    template doesn't sprout a huge multi-paragraph block in places it
    expects a one-line tone."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


def _str_field(obj: Any, name: str) -> str | None:
    """Pull a string field from a Guide-shaped object. Tolerates either
    dataclass-style attribute access or dict-style key access — the
    bot guide carries a frontmatter dict and we can't depend on the
    Guide class being importable from every test context."""
    fm = _frontmatter(obj)
    if fm is None:
        return None
    v = fm.get(name)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _list_field(obj: Any, name: str) -> list[str]:
    fm = _frontmatter(obj)
    if fm is None:
        return []
    v = fm.get(name)
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if x and str(x).strip()]


def _frontmatter(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    fm = getattr(obj, "frontmatter", None)
    if isinstance(fm, dict):
        return fm
    if isinstance(obj, dict) and "frontmatter" in obj:
        inner = obj.get("frontmatter")
        if isinstance(inner, dict):
            return inner
    if isinstance(obj, dict):
        return obj
    return None

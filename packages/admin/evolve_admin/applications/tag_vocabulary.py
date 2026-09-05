"""
tag_vocabulary.py — recommended tag vocabulary for gallery apps.

Tags on gallery apps and installed manifests are a *flat* string list (see
feedback memory + design discussion 2026-05-31). There is no enforced category
hierarchy and no namespace prefix convention (`category:` / `suite:`). Any
string is a legal tag.

This module is a *recommendation* layer on top of that flat space — it provides
a canonical list of well-known tags so the auto-detector and operator-facing
suggestion UI don't fragment into variants like `task-mgmt` / `task-management`
/ `tasks`. Operators are free to use anything; canonical tags just get nicer
display and consistent grouping.

Two surfaces consume this module:
  - `backfill_application_tags.py` — one-shot backfill script that proposes
    canonical tags for existing gallery manifests by keyword matching against
    display_name + description + build_spec.
  - the wizard's spec-review step (follow-up PR) — surfaces canonical tag
    suggestions for operator accept/dismiss before forge.

Adding a tag here is intentionally low-friction (just a new entry in
RECOMMENDED_TAGS). Keep the list small and broadly useful; granular per-domain
tags (e.g. `obsidian-integration`) belong as free-form operator tags, not in
the canonical vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TagSpec:
    """One entry in the recommended-tag list.

    `keywords` are matched case-insensitively against the app's text blob
    (display_name + description + build_spec). Any hit promotes the tag.
    Keep keywords specific enough to avoid false positives — `email` is too
    broad (matches "emails users"); `email-triage` or `inbox` is better.
    """
    tag: str
    description: str
    keywords: tuple[str, ...]


# ── Canonical vocabulary ──────────────────────────────────────────────────────
#
# Order is arbitrary; auto-detect returns matched tags sorted alphabetically.
# When adding a new entry, prefer existing variants over coining new tags
# (productivity > work-productivity > general-productivity).

RECOMMENDED_TAGS: tuple[TagSpec, ...] = (
    TagSpec(
        tag="productivity",
        description="Personal or work productivity (tasks, focus, planning).",
        keywords=("task manager", "task-management", "todo", "productivity",
                  "focus", "planning", "kanban", "gtd"),
    ),
    TagSpec(
        tag="task-management",
        description="Explicit task/work-item tracking with state machine.",
        keywords=("task manager", "task management", "tasks.json",
                  "work item", "work items", "tracker"),
    ),
    TagSpec(
        tag="calendar",
        description="Calendar events, scheduling, agendas.",
        keywords=("calendar", "schedule", "agenda", "meeting", "events",
                  "google calendar", "ical"),
    ),
    TagSpec(
        tag="email",
        description="Email reading, sending, triage, summarization.",
        keywords=("email", "gmail", "inbox", "imap", "mail",
                  "triage"),
    ),
    TagSpec(
        tag="contacts",
        description="Per-person memory, CRM, relationship tracking.",
        keywords=("contact", "contacts", "crm", "relationship",
                  "people", "per-person"),
    ),
    TagSpec(
        tag="journaling",
        description="Daily journal, reflection, freeform daily entries.",
        keywords=("journal", "journaling", "diary", "reflection",
                  "morning intention", "end of day"),
    ),
    TagSpec(
        tag="notes",
        description="Note capture, meeting notes, knowledge management.",
        keywords=("note-taker", "notetaker", "meeting note", "meeting notes",
                  "obsidian", "knowledge base", "knowledge management"),
    ),
    TagSpec(
        tag="morning-routine",
        description="Daily morning digest / briefing apps.",
        keywords=("morning brief", "morning briefing", "daily digest",
                  "morning routine", "good morning"),
    ),
    TagSpec(
        tag="executive-assistant",
        description="EA-style coordination — schedule, triage, commitments.",
        keywords=("executive assistant", "ea pack", "ea-pack",
                  "personal assistant", "chief of staff",
                  "pre-meeting", "commitment tracking"),
    ),
    TagSpec(
        tag="developer-tools",
        description="Engineering workflow: code, repos, dev infra.",
        keywords=("github", "gitlab", "repo", "repository", "pull request",
                  "code", "developer", "engineering"),
    ),
    TagSpec(
        tag="github",
        description="GitHub-specific (PRs, issues, repo syncing).",
        keywords=("github", "github.com", "pull request", "github issue",
                  "github pr"),
    ),
    TagSpec(
        tag="integration",
        description="Pure data-syncing app (cron pulls remote → local files).",
        keywords=("sync", "integration", "every 15 minutes",
                  "every 30 minutes", "pull from", "fetch from",
                  "mirror"),
    ),
    TagSpec(
        tag="backup",
        description="Backup / archival / data-safety apps.",
        keywords=("backup", "data safety", "git backup", "remote backup",
                  "data-safety"),
    ),
    TagSpec(
        tag="travel",
        description="Trip planning, itineraries, travel coordination.",
        keywords=("travel", "trip", "itinerary", "flight", "hotel",
                  "vacation", "booking"),
    ),
    TagSpec(
        tag="research",
        description="Information gathering, web research, fact-finding.",
        keywords=("research", "investigate", "web search", "fact-find",
                  "literature review", "background information"),
    ),
)


# Lookup helpers ────────────────────────────────────────────────────────────────

_TAG_LOOKUP: dict[str, TagSpec] = {spec.tag: spec for spec in RECOMMENDED_TAGS}


def all_tags() -> list[str]:
    """Return every canonical tag, alphabetically sorted."""
    return sorted(_TAG_LOOKUP)


# ── Kind classification ───────────────────────────────────────────────────────
#
# Tags stay flat strings (see feedback memory). For UI styling purposes
# (operator-applied tags should render bolder than auto-detected ones, suite
# tags want a distinct affordance) we classify out-of-band — no prefix
# convention on the tag itself.
#
# - "canonical": present in RECOMMENDED_TAGS — auto-detector vocabulary.
# - "suite":     ends with `-suite` (loose naming convention used by the
#                gallery suite tags `ea-suite`, `daily-brief-suite`,
#                `workspace-suite`). Replaceable with a per-manifest
#                tag_provenance map in a future PR without breaking
#                callers.
# - "freeform":  everything else (operator-curated free-form labels like
#                `obsidian-integration`, `data-foundation`).

TAG_KIND_CANONICAL = "canonical"
TAG_KIND_SUITE = "suite"
TAG_KIND_FREEFORM = "freeform"


def tag_kind(tag: str) -> str:
    """Classify `tag` into one of TAG_KIND_*. Returns FREEFORM by default."""
    if tag in _TAG_LOOKUP:
        return TAG_KIND_CANONICAL
    if tag.endswith("-suite"):
        return TAG_KIND_SUITE
    return TAG_KIND_FREEFORM


def classify_tags(tags: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Map each tag to its kind. Useful for batch classification across a
    whole gallery's tag index."""
    return {t: tag_kind(t) for t in tags}


def is_recommended(tag: str) -> bool:
    """True if `tag` is in the canonical vocabulary.

    Operators can freely use non-canonical tags; this is purely a hint for
    the UI (e.g. style canonical tags one way, free-form tags another) and
    for auto-detection consistency.
    """
    return tag in _TAG_LOOKUP


def describe(tag: str) -> str:
    """Return the canonical description for `tag`, or empty string."""
    spec = _TAG_LOOKUP.get(tag)
    return spec.description if spec else ""


# ── Auto-detector ─────────────────────────────────────────────────────────────

# Word-boundary regex avoids "mail" matching inside "email" and similar.
# Compiled lazily on first use; the cache lives module-level so repeated
# auto_detect calls (e.g. backfill across 12 manifests) don't recompile.
_KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {}


def _pattern_for(keyword: str) -> re.Pattern[str]:
    pat = _KEYWORD_PATTERNS.get(keyword)
    if pat is None:
        # \b doesn't anchor across hyphens, but our keywords include hyphenated
        # forms (`task-management`) — for those we drop the boundary at the
        # hyphen sides so the match still works inside larger compound words.
        escaped = re.escape(keyword.lower())
        pat = re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)
        _KEYWORD_PATTERNS[keyword] = pat
    return pat


def auto_detect(
    *text_blobs: str,
    disabled_tags: Iterable[str] = (),
) -> list[str]:
    """Propose canonical tags for an app from its descriptive text.

    Concatenates the blobs (display_name, description, build_spec, existing
    tags, etc.), case-folds, and returns the alphabetically-sorted list of
    canonical tags whose keywords appear in the blob.

    Conservative by design — only returns tags from RECOMMENDED_TAGS, never
    coins new ones. Empty input returns an empty list.

    ``disabled_tags`` (optional): the operator's persistent dismissal list
    from the manifest. Any tag in it is excluded from the result even when
    its keywords match — the auto-detector should never re-propose a tag
    the operator already removed. See internal/spec-manifest-tags-2026-05-31.md.
    """
    blob = " ".join(b for b in text_blobs if b)
    if not blob:
        return []
    blocked = {t for t in disabled_tags if t}
    hits: set[str] = set()
    for spec in RECOMMENDED_TAGS:
        if spec.tag in blocked:
            continue
        for kw in spec.keywords:
            if _pattern_for(kw).search(blob):
                hits.add(spec.tag)
                break
    return sorted(hits)


def merge_tags(
    existing: Iterable[str],
    proposed: Iterable[str],
    *,
    disabled_tags: Iterable[str] = (),
) -> list[str]:
    """Merge two tag lists, dedup-preserving the existing order then appending
    new entries in sorted order. Useful for backfill scripts that want to
    keep operator-chosen tags first and add auto-detected ones after.

    ``disabled_tags`` (optional, keyword-only): any tag in this set is dropped
    from the final result, whether it appeared in ``existing`` or ``proposed``.
    The operator's dismissal is authoritative: if they listed ``email`` in
    ``disabled_tags`` it stays out of ``tags`` on every subsequent backfill,
    even if a hand-edit re-added it to ``existing``.
    """
    blocked = {t for t in disabled_tags if t}
    out: list[str] = []
    seen: set[str] = set()
    for t in existing:
        if t and t not in seen and t not in blocked:
            out.append(t)
            seen.add(t)
    for t in sorted(proposed):
        if t and t not in seen and t not in blocked:
            out.append(t)
            seen.add(t)
    return out

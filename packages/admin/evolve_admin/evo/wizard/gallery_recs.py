"""Gallery recommendations phase helpers (slice 5b5).

Pulls candidates from the existing gallery (``applications.gallery``),
filters out apps already installed on the bot, scores the rest against
the wizard's extracted profile, and returns the top-K. The engine's
:func:`evo.wizard.engine._handle_gallery_recs` calls into here for
loading + classification; the prompt builder reads the same candidates
from state context.

Scoring is deliberately simple: count keyword overlaps between each
candidate's display_name + description + application_tags and the
profile's role / environment / top_goals / current_tooling /
pain_points. No LLM call, no embedding lookup. Good enough for v1; if
the recommender ever becomes a real proposal-generator (per
spec-rsi-architecture-2026-04-17.md §6) this'll be replaced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Words shorter than this don't count toward keyword overlap — too many
# false positives ("the", "and", "for"). Profile-derived words also get
# filtered against a short stop-list.
_MIN_KEYWORD_LEN = 4

_STOP_WORDS = frozenset({
    "this", "that", "with", "from", "have", "want", "need",
    "would", "could", "should", "ours", "team",
})


def load_candidates(
    shared_dir: Path,
    bot_id: str,
    extracted_profile: dict[str, Any],
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Return the top-K gallery candidates for ``bot_id`` matching the
    user's extracted profile fields. Empty list if the gallery is empty,
    every package is already installed, or the gallery module isn't
    importable (defensive — this runs at session-prompt time and must
    not crash the wizard if the gallery surface is borked)."""
    try:
        from ...applications.gallery import list_gallery_packages
    except Exception:
        return []

    try:
        all_pkgs = list_gallery_packages(shared_dir, [bot_id])
    except Exception:
        return []

    # Drop anything already installed on this bot — there's no value in
    # recommending an app the user already has.
    available = [
        p for p in all_pkgs
        if bot_id not in (p.get("installed_on") or [])
    ]
    if not available:
        return []

    profile_keywords = _profile_keywords(extracted_profile)

    scored = [
        (_score_candidate(p, profile_keywords), p.get("pkg_id") or "", p)
        for p in available
    ]
    # Sort by score desc, then pkg_id asc for stable tie-break
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p for _score, _pid, p in scored[:top_k]]


def classify_reply(
    user_message: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify a user reply during GALLERY_RECS. Returns a dict with:

      * ``intent``  — "accept" | "dismiss_all" | "ambiguous"
      * ``accepted`` — list of pkg_ids the user wants installed (may be
        empty even when intent="accept" if we couldn't pin down which)
      * ``dismissed`` — list of pkg_ids the user explicitly skipped

    The classifier is deterministic — no LLM call. Intent priority:

      1. Explicit dismiss-all phrases ("none", "skip them", "enough",
         "later", "no thanks", "not interested") → ``dismiss_all``
      2. Otherwise, look for affirmative cues ("yes", "ok", "install",
         "go ahead", "do it", "sure") and try to identify which
         candidate(s) the user named — substring/keyword match against
         display_name. → ``accept`` if ≥1 identified; ``ambiguous``
         otherwise (the engine will re-render the prompt with a nudge).
    """
    text = (user_message or "").strip().lower()
    if not text:
        return {"intent": "ambiguous", "accepted": [], "dismissed": []}

    if _matches_dismiss_all(text):
        return {
            "intent": "dismiss_all",
            "accepted": [],
            "dismissed": [c.get("pkg_id") or "" for c in candidates if c.get("pkg_id")],
        }

    affirmative = _matches_affirmative(text)
    named = _identify_named_candidates(text, candidates)

    if named:
        named_pkg_ids = [c.get("pkg_id") or "" for c in named if c.get("pkg_id")]
        # Anything not named in this turn is implicitly dismissed
        dismissed = [
            (c.get("pkg_id") or "") for c in candidates
            if c.get("pkg_id") and c.get("pkg_id") not in named_pkg_ids
        ]
        return {
            "intent": "accept",
            "accepted": named_pkg_ids,
            "dismissed": dismissed,
        }

    if affirmative and len(candidates) == 1:
        # Only one candidate on offer — "yes" unambiguously means that one
        only = candidates[0]
        return {
            "intent": "accept",
            "accepted": [only.get("pkg_id") or ""] if only.get("pkg_id") else [],
            "dismissed": [],
        }

    if affirmative and _matches_select_all(text):
        # "yes all of them" / "install all" / "all three" → accept every candidate
        return {
            "intent": "accept",
            "accepted": [c.get("pkg_id") or "" for c in candidates if c.get("pkg_id")],
            "dismissed": [],
        }

    return {"intent": "ambiguous", "accepted": [], "dismissed": []}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────


def _profile_keywords(profile: dict[str, Any]) -> set[str]:
    """Extract a flat lowercase keyword set from the profile fields the
    wizard cares about for matching."""
    parts: list[str] = []
    for key in ("role", "environment", "current_workflow_notes"):
        v = profile.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    for key in ("top_goals", "current_tooling", "pain_points"):
        v = profile.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x)

    words: set[str] = set()
    for chunk in parts:
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", chunk.lower()):
            if len(w) >= _MIN_KEYWORD_LEN and w not in _STOP_WORDS:
                words.add(w)
    return words


def _score_candidate(
    candidate: dict[str, Any],
    profile_keywords: set[str],
) -> int:
    """Count distinct profile keywords that appear in the candidate's
    text (display_name + description + application_tags). Returns 0 for
    no overlap."""
    blob_parts: list[str] = []
    for key in ("display_name", "description"):
        v = candidate.get(key)
        if isinstance(v, str) and v:
            blob_parts.append(v)
    for key in ("application_tags", "capability_tags"):
        v = candidate.get(key)
        if isinstance(v, list):
            blob_parts.extend(str(x) for x in v if x)
    blob = " ".join(blob_parts).lower()
    if not blob:
        return 0

    score = 0
    for kw in profile_keywords:
        if kw in blob:
            score += 1
    return score


# ─────────────────────────────────────────────────────────────────────────────
# Reply classification
# ─────────────────────────────────────────────────────────────────────────────


# Same phrase/word split pattern as engine's confirm classifier — keeps
# "no" inside "not sure" from misclassifying.
_DISMISS_ALL_PHRASES = frozenset({
    "no thanks", "not interested", "not now", "skip them",
    "skip them all", "skip all", "not for me", "not really",
    "maybe later", "not today",
})
_DISMISS_ALL_WORDS = frozenset({
    "none", "no", "skip", "enough", "later", "pass", "nah", "nope",
})

_AFFIRMATIVE_PHRASES = frozenset({
    "go ahead", "do it", "do them", "sounds good", "looks good",
    "let's do", "ship it", "install it", "install them",
})
_AFFIRMATIVE_WORDS = frozenset({
    "yes", "ok", "okay", "sure", "yep", "yeah", "install", "do",
})


def _matches_dismiss_all(text: str) -> bool:
    if any(p in text for p in _DISMISS_ALL_PHRASES):
        return True
    tokens = set(re.findall(r"[a-zA-Z]+", text.lower()))
    return any(w in tokens for w in _DISMISS_ALL_WORDS)


def _matches_affirmative(text: str) -> bool:
    if any(p in text for p in _AFFIRMATIVE_PHRASES):
        return True
    tokens = set(re.findall(r"[a-zA-Z]+", text.lower()))
    return any(w in tokens for w in _AFFIRMATIVE_WORDS)


_SELECT_ALL_PHRASES = frozenset({
    "all of them", "all three", "all two", "every one", "everything",
    "all of these", "all the apps", "all apps",
})
_SELECT_ALL_WORDS = frozenset({"all", "every", "everything"})


def _matches_select_all(text: str) -> bool:
    if any(p in text for p in _SELECT_ALL_PHRASES):
        return True
    tokens = set(re.findall(r"[a-zA-Z]+", text.lower()))
    return any(w in tokens for w in _SELECT_ALL_WORDS)


def _identify_named_candidates(
    text: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the candidates whose display_name (or first
    space-separated word of it) appears in the user's reply.

    Matches are case-insensitive and word-bounded so a user typing
    "the calendar one" matches a candidate named "Calendar Sync".
    """
    if not text or not candidates:
        return []

    text_low = text.lower()
    text_tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", text_low))

    matched: list[dict[str, Any]] = []
    for c in candidates:
        name = (c.get("display_name") or "").strip().lower()
        if not name:
            continue
        if name in text_low:
            matched.append(c)
            continue
        # Match individual non-stopword words from the display name —
        # "Calendar Sync" matches "calendar" or "sync" but not "the".
        # Lower threshold for display-name words (2 chars) than for
        # profile keywords (4 chars) — short app names like "CI" or
        # "DB" are real identifiers we should match on.
        name_words = [
            w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", name)
            if len(w) >= 2 and w not in _STOP_WORDS
        ]
        if any(w in text_tokens for w in name_words):
            matched.append(c)

    return matched

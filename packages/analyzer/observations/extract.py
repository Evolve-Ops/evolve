"""observations.extract — Tuple extraction from session transcripts.

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §4.1.

The extraction is an LLM call: given a session transcript + the installed-
apps + candidate-gallery vocabulary, return a list of (noun, verb, mood,
engagement) tuples — one per topic-coherent segment.

L3 ships a well-defined interface with a **pluggable extractor** so tests
inject a deterministic mock and production wires in the real Haiku call.
The Haiku-backed implementation lives in
``observations.llm_extractor`` and is wired by the admin server at
startup or by the per-bot driver script ``extract_tuples.py``. This
module is pure logic: it turns transcript text + vocabulary into a
request, and parses the LLM response into ObservationTuple objects.

The ``extract_from_session_summary`` helper at the bottom is the bridge
used by the daily extractor driver: raw turn transcripts aren't currently
persisted in the shared dir (per the L3 design notes), so we synthesize
a single-turn Transcript whose text is a structured render of the
session_summary fields and let the existing pipeline take over.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from schema.observation import (
    MOOD_VOCABULARY,
    VERB_VOCABULARY,
    ObservationTuple,
    new_tuple_id,
)


# ─────────────────────────────────────────────────────────────────────────────
# Transcript representation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    """One turn in a session transcript."""

    role: str  # "user" | "assistant"
    text: str
    timestamp: str  # ISO8601 UTC


@dataclass
class Transcript:
    """The input to extraction."""

    bot_id: str
    session_id: str
    turns: list[Turn]

    @property
    def text_hash(self) -> str:
        h = hashlib.sha256()
        for turn in self.turns:
            h.update(turn.role.encode("utf-8"))
            h.update(b":")
            h.update(turn.text.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# LLM extractor interface
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExtractorVocabulary:
    """Dynamic vocabulary passed to the LLM at extraction time."""

    # Primary noun vocabulary — typically installed-app names + capability tags
    active_nouns: list[str]
    # Secondary noun vocabulary — typically gallery/candidate apps
    candidate_nouns: list[str]
    # Fixed vocabularies
    verbs: list[str]
    moods: list[str]

    @classmethod
    def from_active_nouns(cls, active: list[str]) -> "ExtractorVocabulary":
        return cls(
            active_nouns=list(active),
            candidate_nouns=[],
            verbs=sorted(VERB_VOCABULARY),
            moods=sorted(MOOD_VOCABULARY),
        )


# A callable: (transcript, vocabulary) -> list[raw tuple dicts]
#
# The raw tuple dict shape is:
#   {
#     "noun": str,
#     "verb": str,
#     "mood": str | None,
#     "engagement": int,
#     "segment_id": str,                # extractor-assigned
#     "timestamp_start": str,           # ISO8601
#     "timestamp_end": str,             # ISO8601
#     "confidence": float               # 0..1
#   }
#
# Production binds this to a Haiku call; tests bind it to a stub that
# returns canned outputs.
ExtractorCallable = Callable[
    [Transcript, ExtractorVocabulary], list[dict]
]


# ─────────────────────────────────────────────────────────────────────────────
# Default extractor — NOT a real LLM call
# ─────────────────────────────────────────────────────────────────────────────


def _default_extractor(
    transcript: Transcript, vocabulary: ExtractorVocabulary  # noqa: ARG001
) -> list[dict]:
    """Deterministic placeholder extractor.

    Used when no other extractor is injected. Returns one tuple spanning
    the whole transcript with neutral defaults, so production is never
    silently broken while the real LLM wiring is TODO.
    """
    if not transcript.turns:
        return []

    first = transcript.turns[0]
    last = transcript.turns[-1]
    assistant_turns = sum(1 for t in transcript.turns if t.role == "assistant")

    return [
        {
            "noun": "other",
            "verb": "exploring",
            "mood": None,
            "engagement": max(1, assistant_turns),
            "segment_id": "seg-0",
            "timestamp_start": first.timestamp,
            "timestamp_end": last.timestamp,
            "confidence": 0.5,
        }
    ]


_extractor: ExtractorCallable = _default_extractor


def set_extractor(fn: ExtractorCallable) -> None:
    """Install the extractor callable (tests + production wiring)."""
    global _extractor
    _extractor = fn


def reset_extractor() -> None:
    """Restore the default placeholder extractor."""
    global _extractor
    _extractor = _default_extractor


# ─────────────────────────────────────────────────────────────────────────────
# Extraction entry point
# ─────────────────────────────────────────────────────────────────────────────


def extract_tuples(
    transcript: Transcript,
    *,
    vocabulary: ExtractorVocabulary | None = None,
) -> list[ObservationTuple]:
    """Extract ObservationTuples from a transcript.

    Raw LLM output is validated against the fixed verb/mood vocabularies.
    Rows with out-of-vocab verbs or moods are coerced:
      - Unknown verb → dropped (can't construct a valid ObservationTuple)
      - Unknown mood → set to None
      - Out-of-range confidence → clamped to [0, 1]

    Extraction confidence below 0.1 is dropped (signal too weak).
    """
    if vocabulary is None:
        vocabulary = ExtractorVocabulary.from_active_nouns([])

    raw = _extractor(transcript, vocabulary)
    source_hash = transcript.text_hash

    tuples: list[ObservationTuple] = []
    for row in raw:
        noun = str(row.get("noun", "")).strip().lower()
        verb = str(row.get("verb", "")).strip().lower()
        mood_raw = row.get("mood")
        mood = (
            str(mood_raw).strip().lower() if mood_raw not in (None, "") else None
        )

        if not noun:
            continue
        if verb not in VERB_VOCABULARY:
            # Drop — can't salvage
            continue
        if mood is not None and mood not in MOOD_VOCABULARY:
            mood = None

        try:
            confidence = float(row.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        if confidence < 0.1:
            continue

        tuples.append(
            ObservationTuple(
                id=new_tuple_id(),
                bot_id=transcript.bot_id,
                session_id=transcript.session_id,
                segment_id=str(row.get("segment_id") or "seg-?"),
                noun=noun,
                verb=verb,
                mood=mood,
                engagement=int(row.get("engagement", 1)),
                timestamp_start=str(row.get("timestamp_start", "")),
                timestamp_end=str(row.get("timestamp_end", "")),
                source_hash=source_hash,
                extraction_confidence=confidence,
            )
        )
    return tuples


# ─────────────────────────────────────────────────────────────────────────────
# session_summary → tuples bridge
# ─────────────────────────────────────────────────────────────────────────────


# Sessions with fewer turns than this and no application invocations are
# treated as heartbeat / liveness pings — extracting tuples from them
# would just return "(other, exploring, neutral)" and waste an LLM call.
_TRIVIAL_TURN_THRESHOLD: int = 1


def is_trivial_session_summary(record: dict) -> bool:
    """Return True if this session_summary has too little signal to extract."""
    turn_count = int(record.get("turn_count") or 0)
    apps = record.get("applications_invoked") or []
    outcome = (record.get("outcome") or "").strip()
    if turn_count <= _TRIVIAL_TURN_THRESHOLD and not apps:
        return True
    if outcome.upper() in {"HEARTBEAT_OK", "HEARTBEAT", ""}:
        return True
    return False


def render_session_summary_text(record: dict) -> str:
    """Format a session_summary record as the prompt-input text the LLM sees.

    The daily extractor only has session_summary records (raw turn text
    isn't persisted in the shared dir today). We render the structured
    fields as a compact text block — this becomes the synthetic single-
    "user" turn the Transcript carries.
    """
    parts: list[str] = []
    parts.append(f"Outcome: {record.get('outcome') or '(none)'}")
    apps = record.get("applications_invoked") or []
    if apps:
        parts.append(f"Applications invoked: {', '.join(str(a) for a in apps)}")
    promises = record.get("promises_made") or []
    if promises:
        parts.append(
            "Promises made: "
            + " | ".join(str(p) for p in promises[:5])
        )
    if record.get("complexity"):
        parts.append(f"Complexity: {record['complexity']}")
    if record.get("session_class") or record.get("tier"):
        parts.append(
            f"Class: {record.get('session_class') or record.get('tier')}"
        )
    if record.get("correction_count") is not None:
        parts.append(f"Corrections: {record['correction_count']}")
    if record.get("efficiency_flag") is not None:
        parts.append(f"Efficiency flag: {bool(record.get('efficiency_flag'))}")
    if record.get("turn_count") is not None:
        parts.append(f"Turn count: {record['turn_count']}")
    return "\n".join(parts)


def session_summary_to_transcript(record: dict) -> Transcript | None:
    """Build a single-turn synthetic Transcript from a session_summary record.

    Returns None if required fields are missing or the session is trivial.
    The synthetic turn carries the rendered summary as its text and the
    record's ``ts`` (or now-UTC) as the timestamp.
    """
    bot_id = str(record.get("bot_id") or "").strip()
    session_id = str(record.get("session_id") or "").strip()
    if not bot_id or not session_id:
        return None
    if is_trivial_session_summary(record):
        return None

    ts = str(record.get("ts") or "").strip()
    if not ts:
        ts = datetime.now(timezone.utc).isoformat()

    text = render_session_summary_text(record)
    return Transcript(
        bot_id=bot_id,
        session_id=session_id,
        turns=[Turn(role="user", text=text, timestamp=ts)],
    )


def extract_from_session_summary(
    record: dict,
    *,
    vocabulary: ExtractorVocabulary | None = None,
) -> list[ObservationTuple]:
    """Extract tuples from a session_summary record (driver entry point).

    Returns [] for trivial / empty sessions without making an LLM call.
    Callers should still dedupe on ``ObservationTuple.source_hash``
    before persisting (the storage layer does this automatically).
    """
    transcript = session_summary_to_transcript(record)
    if transcript is None:
        return []
    tuples = extract_tuples(transcript, vocabulary=vocabulary)
    # The session_summary's timestamp is stable and authoritative — pin
    # both endpoints there so generators bucket the tuple to the right
    # day even if the LLM didn't echo the timestamp back.
    ts = transcript.turns[0].timestamp
    rewritten: list[ObservationTuple] = []
    for t in tuples:
        if not t.timestamp_start:
            t.timestamp_start = ts
        if not t.timestamp_end:
            t.timestamp_end = ts
        rewritten.append(t)
    return rewritten

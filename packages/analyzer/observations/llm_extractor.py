"""observations.llm_extractor — Haiku-backed tuple extractor.

Wires ``observations.extract.set_extractor()`` to a Claude Haiku call
that takes a Transcript + ExtractorVocabulary and returns raw tuple
dicts in the shape ``observations.extract.extract_tuples`` expects.

The extractor uses prompt caching: the system prompt (taxonomy, rules,
few-shot examples) is large and static across calls, so we mark the
last text block of the system prompt with ``cache_control`` so the
Anthropic API reuses it across the day's session-summary calls. Only
the user message (one session_summary at a time) varies.

Falls back silently to the pluggable default when the Anthropic SDK
isn't installed or no API key is available — production is never
silently broken; it just emits no tuples until wiring succeeds.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from observations.extract import (
    ExtractorVocabulary,
    Transcript,
    set_extractor,
)
from schema.observation import MOOD_VOCABULARY, VERB_VOCABULARY

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────


def _build_system_prompt() -> str:
    """Render the (large, static) system prompt.

    Built once per process. Keeps the verb/mood vocabularies in sync with
    schema.observation by reading from the source-of-truth frozensets.
    """
    verbs = ", ".join(sorted(VERB_VOCABULARY))
    moods = ", ".join(sorted(MOOD_VOCABULARY))
    return f"""You extract structured (noun × verb × mood × engagement) observation tuples from compact summaries of bot↔user sessions.

Each session input is a small block of structured fields the bot wrote about a recent conversation: outcome, applications invoked, promises made, complexity, class, correction count, efficiency flag, turn count. There is NO raw transcript — only this structured summary.

Your output is a list of one or more tuples, one per topic-coherent thread inside the session. Most sessions have exactly one tuple; multi-tuple output only when the summary clearly covers two distinct domains.

# Vocabularies

**verb** (REQUIRED — must be exactly one of these, no synonyms): {verbs}

**mood** (OPTIONAL — must be exactly one of these or null): {moods}

**noun** (open vocabulary, normalized to lowercase). Prefer terms from the "active nouns" list provided in the user message — those are the apps the bot has installed. If none of those fit the session, use a short generic domain word (e.g. "fitness", "email", "notes", "code", "calendar", "tasks", "research", "writing", "ops"). Avoid proper nouns, multi-word phrases longer than 2 words, or model/internal jargon ("hooks", "agents", "claude").

# Engagement

Engagement is the number of meaningful turn-pairs in the segment, integer ≥ 1. If the summary's `turn_count` is 1, set engagement to 1. If `turn_count` is 5–10, that's typically engagement 3–6 (not all turns are content-bearing).

# Confidence

Range [0, 1]. Use:
- 0.85+: outcome text + applications_invoked clearly imply a single domain action
- 0.5–0.8: domain inferable but with some interpretation
- 0.2–0.4: weak signal; you're guessing
- below 0.1: don't emit (filtered out downstream anyway)

# Output format

Return ONLY a JSON object — no preamble, no markdown fences:

{{"tuples": [
  {{
    "noun": "fitness",
    "verb": "tracking",
    "mood": "enthusiastic",
    "engagement": 3,
    "segment_id": "seg-0",
    "confidence": 0.85
  }}
]}}

Use segment_id "seg-0", "seg-1", etc. across tuples in one session. Omit timestamp_start/timestamp_end — the driver fills those from the session record.

# Examples

Input:
```
Outcome: Logged 5 mile run from this morning, asked about pace targets for half-marathon.
Applications invoked: fitness-tracker
Class: productive
Turn count: 4
```
Output:
{{"tuples": [
  {{"noun": "fitness", "verb": "tracking", "mood": "enthusiastic", "engagement": 3, "segment_id": "seg-0", "confidence": 0.9}}
]}}

Input:
```
Outcome: Drafted reply to landlord about lease renewal, then asked for help debugging a failing pytest.
Applications invoked: email, code-review
Complexity: medium
Turn count: 8
```
Output:
{{"tuples": [
  {{"noun": "email", "verb": "drafting", "mood": "neutral", "engagement": 3, "segment_id": "seg-0", "confidence": 0.85}},
  {{"noun": "code", "verb": "troubleshooting", "mood": "uncertain", "engagement": 4, "segment_id": "seg-1", "confidence": 0.8}}
]}}

Input:
```
Outcome: I keep losing my place in this — third try and it still won't compile.
Applications invoked: code-review
Class: maintenance
Corrections: 2
Turn count: 11
```
Output:
{{"tuples": [
  {{"noun": "code", "verb": "troubleshooting", "mood": "frustrated", "engagement": 8, "segment_id": "seg-0", "confidence": 0.9}}
]}}

Input:
```
Outcome: HEARTBEAT_OK
Turn count: 1
```
Output:
{{"tuples": []}}

If the session has no real signal (heartbeats, empty outcomes, pure system pings), return {{"tuples": []}}.
"""


_SYSTEM_PROMPT = _build_system_prompt()


# ─────────────────────────────────────────────────────────────────────────────
# Defaults / config
# ─────────────────────────────────────────────────────────────────────────────


DEFAULT_MODEL: str = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS: int = 600


# ─────────────────────────────────────────────────────────────────────────────
# Per-process call counter (cost guard)
# ─────────────────────────────────────────────────────────────────────────────


class CallQuotaExceeded(RuntimeError):
    """Raised by the driver when a per-run quota is hit. The extractor itself
    is quota-agnostic — the driver counts and decides when to stop."""


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_response(text: str) -> list[dict]:
    """Parse the model's JSON response into a list of raw tuple dicts.

    Tolerant of three real-world response shapes Haiku produces despite
    "return ONLY a JSON object" instructions:

    1. Pure JSON: ``{"tuples": [...]}``
    2. Fenced JSON: ``​​​json\\n{...}\\n​​​``
    3. JSON + trailing prose: ``{...}\\n\\n**Rationale:** ...``

    Uses ``json.JSONDecoder.raw_decode`` to consume the first valid JSON
    object and ignore anything after it. Returns [] on any parse failure
    — production treats parse failures as 'no tuples this session'.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines if not line.startswith("```"))
        text = text.strip()

    decoder = json.JSONDecoder()

    # First try parsing from the start
    try:
        data, _ = decoder.raw_decode(text)
    except json.JSONDecodeError:
        # Fall back to finding the first '{' — the model may have led
        # with a sentence
        start = text.find("{")
        if start < 0:
            logger.warning(
                "llm_extractor: no JSON object in response (first 80 chars=%r)",
                text[:80],
            )
            return []
        try:
            data, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as e:
            logger.warning(
                "llm_extractor: response not valid JSON: %s (first 80 chars=%r)",
                e,
                text[:80],
            )
            return []

    if not isinstance(data, dict):
        return []
    rows = data.get("tuples")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
    return out


def _format_active_nouns(vocabulary: ExtractorVocabulary) -> str:
    if vocabulary.active_nouns:
        return ", ".join(sorted(set(vocabulary.active_nouns)))
    return "(none)"


def _build_user_message(transcript: Transcript, vocabulary: ExtractorVocabulary) -> str:
    body = transcript.turns[0].text if transcript.turns else "(empty session)"
    return f"""# Active nouns for this bot

Prefer these when applicable. Free-form generic domain words are OK if none fit:

{_format_active_nouns(vocabulary)}

# Session summary

```
{body}
```

Extract tuples now."""


# ─────────────────────────────────────────────────────────────────────────────
# Extractor factory
# ─────────────────────────────────────────────────────────────────────────────


def make_llm_extractor(
    client: Any,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    """Build an ExtractorCallable backed by ``client.messages.create``.

    The system prompt is sent with ``cache_control: ephemeral`` so the
    Anthropic API caches the (large, static) prompt across calls in the
    same hour. Only the per-session user message varies.

    Args:
        client: an initialized Anthropic SDK client (anthropic.Anthropic).
        model: model identifier — defaults to claude-haiku-4-5 for cost.
        max_tokens: response cap. 600 is plenty for ≤2 tuples.

    Returns:
        A function compatible with the ExtractorCallable signature:
        (Transcript, ExtractorVocabulary) -> list[raw tuple dict].
    """

    def extract(
        transcript: Transcript, vocabulary: ExtractorVocabulary
    ) -> list[dict]:
        if not transcript.turns:
            return []

        user_msg = _build_user_message(transcript, vocabulary)

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:  # noqa: BLE001 — any API error → no tuples
            logger.warning("llm_extractor: Anthropic API call failed: %s", exc)
            return []

        text_parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        text = "".join(text_parts)

        return _parse_response(text)

    return extract


# ─────────────────────────────────────────────────────────────────────────────
# Startup helper
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_anthropic_key() -> str | None:
    """Resolve the Anthropic API key for engine LLM extraction.

    Engine background calls authenticate against the primary bot's
    auth-profiles.json (network.json → primary). ANTHROPIC_API_KEY env
    wins if set — the LaunchDaemon environment may not carry it, in
    which case the primary bot's profile is read.
    """
    from primary_bot import read_primary_bot_anthropic_key  # type: ignore

    return read_primary_bot_anthropic_key() or None


def wire_default_extractor() -> bool:
    """Wire observations.extract's pluggable extractor to a Haiku call.

    Returns True if wiring succeeded, False if it fell back to the
    no-op default. Logs at INFO/WARNING; never raises — the driver
    should remain importable even when the SDK isn't installed.
    """
    api_key = _resolve_anthropic_key()
    if not api_key:
        logger.info(
            "llm_extractor: no ANTHROPIC_API_KEY available; "
            "remaining on stub (no tuples will be extracted)"
        )
        return False
    try:
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except ImportError as e:
        logger.warning(
            "llm_extractor: anthropic SDK not installed (%s); remaining on stub",
            e,
        )
        return False

    try:
        client = Anthropic(api_key=api_key)
        set_extractor(make_llm_extractor(client))
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_extractor: wiring failed (%s); remaining on stub", e)
        return False

    logger.info("llm_extractor: wired Haiku-backed extractor (model=%s)", DEFAULT_MODEL)
    return True

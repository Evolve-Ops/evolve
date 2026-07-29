"""user_profile_inferrer.extractor — Pure fact extraction over a transcript.

Builds a Haiku prompt that takes (transcript, current_profile) and asks
"what new facts about the speaker came up that aren't already recorded?"
Returns a list of FactCandidate. Per-section confidence thresholds are
applied by the caller (hook.py) — the extractor itself just produces
candidates with confidence scores.

I/O-free: takes a callable for the LLM (so tests inject a stub) and
returns dataclasses. Hook layer handles the file writing.

Cost target: one Haiku 4.5 call per session. Typical session is ~2-3K
input tokens; extractor caps output at ~500 tokens (≤8 facts).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from user_profile.model import (
    AUDIT_LOG_SECTION,
    USER_PROFILE_SECTIONS,
)

logger = logging.getLogger(__name__)


DEFAULT_MODEL: str = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS: int = 500


# Per-section confidence thresholds (spec §D4). Sensitive sections require
# multiple confident observations before a fact lands; everyday sections
# accept a single clear mention.
DEFAULT_CONFIDENCE_THRESHOLDS: dict[str, float] = {
    "Goals": 0.70,
    "Demographics": 0.85,
    "Vocation": 0.70,
    "Interests": 0.70,
    "Family": 0.85,
    "Communication Preferences": 0.70,
    "Values": 0.70,
    "Constraints": 0.85,
}


@dataclass
class FactCandidate:
    section: str          # one of USER_PROFILE_SECTIONS
    fact: str             # short prose fact, e.g. "Vegetarian"
    confidence: float     # 0.0–1.0
    rationale: str = ""   # one-line "why" from the model; for the audit log
    source: str = "inferrer"  # "inferrer" | "wizard" | "user" | "explicit_remember"

    def passes_threshold(
        self, thresholds: dict[str, float] = DEFAULT_CONFIDENCE_THRESHOLDS
    ) -> bool:
        return self.confidence >= thresholds.get(self.section, 0.70)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────


def _build_system_prompt() -> str:
    sections = ", ".join(USER_PROFILE_SECTIONS)
    return f"""You extract new facts about a single user from a transcript of a conversation between that user and their personal bot. The bot maintains a long-running profile of the user; your job is to surface anything from this session that should be added.

# Sections (closed list)

{sections}

Every fact must be assigned to exactly one section. Only use these section names — do not invent new ones.

# What to extract

Extract facts ABOUT THE SPEAKER that are likely to remain true beyond today. Examples:
- "Works as a software engineer at a small startup" → Vocation
- "Vegetarian since college" → Constraints (a hard preference)
- "Has two kids, ages 4 and 7" → Family
- "Wants to ship a novel by year-end" → Goals
- "Prefers concise responses, no bullet points" → Communication Preferences

Do NOT extract:
- Things the user said about other people (third-party facts)
- Transient state ("had a bad day", "running late") — surface mood, not durable
- Facts already present in the current profile (you'll see it below)
- Things the bot said about the user
- Speculation — only what the user clearly stated about themselves

# Confidence

Range [0, 1]. Use:
- 0.90+: user stated the fact directly and unambiguously ("I'm vegetarian")
- 0.75–0.89: user implied it clearly across multiple turns
- 0.5–0.74: reasonable inference, some interpretation needed
- below 0.5: don't emit (filtered downstream)

The caller applies a per-section threshold; sensitive sections (Family, Demographics, Constraints) require 0.85+.

# Output format

Return ONLY a JSON object — no preamble, no markdown fences:

{{"facts": [
  {{
    "section": "Vocation",
    "fact": "Software engineer at a small startup",
    "confidence": 0.92,
    "rationale": "User said 'I work at this seed-stage startup'"
  }}
]}}

If the session yields no new facts (heartbeats, brief pings, things already known), return {{"facts": []}}.

Keep facts short — under 12 words each. Prefer one fact per concept; don't pad."""


_SYSTEM_PROMPT = _build_system_prompt()


def _format_current_profile(current_sections: dict[str, str]) -> str:
    """Render the user's existing profile sections into a compact form
    so the model can avoid duplicate extractions."""
    lines: list[str] = []
    for section in USER_PROFILE_SECTIONS:
        body = (current_sections.get(section) or "").strip()
        if not body:
            continue
        # Indent for readability; the model only needs to see what's there.
        lines.append(f"## {section}")
        lines.append(body)
        lines.append("")
    if not lines:
        return "(empty — no facts recorded yet)"
    return "\n".join(lines).rstrip()


def _build_user_message(transcript_text: str, current_sections: dict[str, str]) -> str:
    return f"""# Current profile (do not re-extract these)

{_format_current_profile(current_sections)}

# Session transcript

```
{transcript_text}
```

Extract new facts now."""


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines if not line.startswith("```"))
        text = text.strip()

    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            logger.warning(
                "user_profile_inferrer: no JSON in response (first 80=%r)",
                text[:80],
            )
            return []
        try:
            data, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as e:
            logger.warning(
                "user_profile_inferrer: invalid JSON (%s) (first 80=%r)",
                e,
                text[:80],
            )
            return []

    if not isinstance(data, dict):
        return []
    rows = data.get("facts")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _coerce_candidate(raw: dict) -> FactCandidate | None:
    """Normalize one model-emitted dict into a FactCandidate, or drop it
    if it can't be made coherent."""
    section = str(raw.get("section") or "").strip()
    if section not in USER_PROFILE_SECTIONS:
        # Reject extractions to unknown sections; the model wandered off-prompt.
        return None
    fact = str(raw.get("fact") or "").strip()
    if not fact:
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(raw.get("rationale") or "").strip()
    return FactCandidate(
        section=section,
        fact=fact,
        confidence=confidence,
        rationale=rationale,
        source="inferrer",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public extractor
# ─────────────────────────────────────────────────────────────────────────────


# An LLMCallable takes (system_prompt, user_message, model, max_tokens) and
# returns the model's textual response. This abstraction lets tests inject
# a deterministic stub without depending on the anthropic SDK.
LLMCallable = Callable[[str, str, str, int], str]


def extract_facts(
    transcript_text: str,
    current_sections: dict[str, str],
    *,
    llm_call: LLMCallable,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[FactCandidate]:
    """Extract candidate facts from one session transcript.

    Args:
        transcript_text: the full session transcript as a single string.
            Caller is responsible for any pre-processing (trimming bot-only
            heartbeats, formatting turns, etc.).
        current_sections: the user's existing profile sections, used to
            avoid duplicate extractions.
        llm_call: callable that runs the LLM. Tests inject a stub; production
            wires it to the bot's own anthropic client.
        model: which model to use. Default is Haiku for cost.
        max_tokens: response cap.

    Returns:
        List of FactCandidate. May be empty (no new facts this session).
        Confidence threshold is NOT yet applied — the caller filters via
        ``passes_threshold()`` so the audit log can record near-misses too.
    """
    if not transcript_text.strip():
        return []

    user_msg = _build_user_message(transcript_text, current_sections)

    try:
        response_text = llm_call(_SYSTEM_PROMPT, user_msg, model, max_tokens)
    except Exception as exc:  # noqa: BLE001 — any failure → no facts
        logger.warning("user_profile_inferrer: LLM call failed: %s", exc)
        return []

    raw_rows = _parse_response(response_text)
    candidates: list[FactCandidate] = []
    for row in raw_rows:
        cand = _coerce_candidate(row)
        if cand is not None:
            candidates.append(cand)
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Default LLM-call adapter (used by hook.py at runtime; not in test path)
# ─────────────────────────────────────────────────────────────────────────────


def make_anthropic_call(client: Any) -> LLMCallable:
    """Wrap an Anthropic SDK client into the LLMCallable interface.

    The system prompt is sent with ``cache_control: ephemeral`` since it's
    large and static across calls within a session-end batch.
    """

    def _call(system: str, user_msg: str, model: str, max_tokens: int) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)

    return _call


def filter_passing(
    candidates: Iterable[FactCandidate],
    thresholds: dict[str, float] = DEFAULT_CONFIDENCE_THRESHOLDS,
) -> list[FactCandidate]:
    """Filter candidates by per-section confidence threshold."""
    return [c for c in candidates if c.passes_threshold(thresholds)]

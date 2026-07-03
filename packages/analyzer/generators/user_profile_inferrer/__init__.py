"""user_profile_inferrer — Per-bot, session-end profile extractor.

Spec: docs/spec-user-profile-2026-05-07.md.

Runs inside each bot at session_end as an OpenClaw hook. Reads the just-
finished session transcript, calls the bot's own LLM (Haiku) to extract
new facts about the speaker, applies per-section confidence thresholds,
and writes high-confidence additions directly into the user's profile
file at ``~/.openclaw/profiles/<user_key>.md``.

Architecturally distinct from every other generator in this directory:
the others run in the central arbiter and emit proposals into the
shared queue. This one runs per-bot, talks only to the bot's own
filesystem and LLM, and emits no proposals.

The split exists because **inference over user-personal data must be
siloed by privacy architecture, not by policy** — bot A's LLM never
sees bot B's user's data. See ``docs/spec-user-profile-2026-05-07.md``
§D1 for the rationale.
"""

from generators.user_profile_inferrer.extractor import (
    DEFAULT_CONFIDENCE_THRESHOLDS,
    FactCandidate,
    extract_facts,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLDS",
    "FactCandidate",
    "extract_facts",
]

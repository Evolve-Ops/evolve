"""fit_review.reflection — Bite 3, the bounded LLM reflection (no real spend).

Every test injects a deterministic stub for the LLM callable, so there is NEVER
a real model call. The load-bearing properties under test are the two structural
gates the reflection enforces in code, not prompt etiquette (spec §0, §3.6):

  * cite-or-don't — a quote that is not VERBATIM in the transcript, or carries an
    unattestable session_id, is dropped; if none survive, the reflection declines.
  * bounded action space — a pkg_id not on the shortlist is rejected.

…plus the cheap-by-default discipline: an empty transcript declines WITHOUT
calling the model at all.
"""

from __future__ import annotations

import json

from fit_review import reflection
from fit_review.reflection import ReflectionContext, reflect


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


SHORTLIST = [
    {"pkg_id": "p-f6a7b8c9", "name": "Project Tracker", "matched_domains": ["task-management"]},
    {
        "pkg_id": "p-e5f6a7b8",
        "name": "Weekly Status Reporter",
        "matched_domains": ["task-management", "document-generation", "slack-comms"],
    },
]

# The user's own words — the only legitimate citation source.
QUOTE = "Can you keep track of these 7 project tasks for me? I keep losing them."
TRANSCRIPT = [
    {"session_id": "s-1", "ts": "2026-06-20T10:00:00+00:00", "text": QUOTE},
    {
        "session_id": "s-2",
        "ts": "2026-06-21T11:00:00+00:00",
        "text": "I need to send the weekly status update again, can you draft it?",
    },
]

CANDIDATE_BRIEFS = [
    {
        "noun": "task-management",
        "distinct_sessions": 20,
        "distinct_days": 8,
        "frustration_share": 0.1,
        "alignment": "confirmed",
        "top_verbs": [["recording", 64]],
    }
]


class RecordingLLM:
    """A stub LLM callable that records its calls and returns a canned string."""

    def __init__(self, response: str = ""):
        self.response = response
        self.calls: list[tuple] = []

    def __call__(self, system, user, model, max_tokens):
        self.calls.append((system, user, model, max_tokens))
        return self.response


def _ctx(transcript=None) -> ReflectionContext:
    return ReflectionContext(
        bot_id="team-bot-a",
        archetype="project-manager",
        mission="Run the team's projects.",
        candidate_briefs=CANDIDATE_BRIEFS,
        shortlist=SHORTLIST,
        transcript_turns=list(TRANSCRIPT if transcript is None else transcript),
    )


def _suggest_json(pkg_id="p-e5f6a7b8", quote=QUOTE, session_id="s-1", ts="2026-06-20T10:00:00+00:00"):
    return json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "The user repeatedly asks the bot to hold and track project tasks.",
            "suggested_gallery_pkg_id": pkg_id,
            "cited_evidence": [{"quote": quote, "session_id": session_id, "ts": ts}],
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_suggest_happy_path_returns_cited_suggestion():
    llm = RecordingLLM(_suggest_json())
    r = reflect(_ctx(), llm_call=llm, model=None)
    assert r.is_suggestion
    assert r.suggested_gallery_pkg_id == "p-e5f6a7b8"
    assert len(r.cited_evidence) == 1
    ev = r.cited_evidence[0]
    assert ev.quote == QUOTE
    assert ev.session_id == "s-1"
    assert r.recommended_need
    assert len(llm.calls) == 1  # exactly one bounded call


# ─────────────────────────────────────────────────────────────────────────────
# Cite-or-don't — the load-bearing gate
# ─────────────────────────────────────────────────────────────────────────────


def test_non_verbatim_quote_is_dropped_and_declines():
    # The model "suggests" but its quote is a paraphrase not in the transcript.
    fabricated = _suggest_json(quote="I would love a fancy dashboard with charts")
    llm = RecordingLLM(fabricated)
    r = reflect(_ctx(), llm_call=llm, model=None)
    assert not r.is_suggestion
    assert r.decision == reflection.DECISION_NO_CANDIDATE
    assert "verbatim" in r.reason or "survive" in r.reason


def test_quote_with_unattestable_session_id_is_dropped():
    # Quote is verbatim, but attributed to a session the transcript doesn't carry.
    llm = RecordingLLM(_suggest_json(session_id="s-999"))
    r = reflect(_ctx(), llm_call=llm, model=None)
    assert not r.is_suggestion


def test_partial_evidence_keeps_only_the_verbatim_quote():
    payload = json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "Track tasks.",
            "suggested_gallery_pkg_id": "p-f6a7b8c9",
            "cited_evidence": [
                {"quote": "totally made up sentence", "session_id": "s-1", "ts": "x"},
                {"quote": QUOTE, "session_id": "s-1", "ts": "2026-06-20T10:00:00+00:00"},
            ],
        }
    )
    r = reflect(_ctx(), llm_call=RecordingLLM(payload), model=None)
    assert r.is_suggestion
    assert len(r.cited_evidence) == 1
    assert r.cited_evidence[0].quote == QUOTE


# ─────────────────────────────────────────────────────────────────────────────
# Bounded action space
# ─────────────────────────────────────────────────────────────────────────────


def test_off_shortlist_pkg_is_rejected():
    llm = RecordingLLM(_suggest_json(pkg_id="p-not-a-real-pkg"))
    r = reflect(_ctx(), llm_call=llm, model=None)
    assert not r.is_suggestion
    assert "shortlist" in r.reason


# ─────────────────────────────────────────────────────────────────────────────
# Declines + robustness
# ─────────────────────────────────────────────────────────────────────────────


def test_model_decline_is_passed_through():
    llm = RecordingLLM(json.dumps({"decision": "no_candidate", "reason": "evidence thin"}))
    r = reflect(_ctx(), llm_call=llm, model=None)
    assert not r.is_suggestion
    assert r.reason == "evidence thin"


def test_unparseable_response_declines():
    r = reflect(_ctx(), llm_call=RecordingLLM("not json at all"), model=None)
    assert not r.is_suggestion


def test_empty_transcript_declines_without_calling_the_model():
    llm = RecordingLLM(_suggest_json())
    r = reflect(_ctx(transcript=[]), llm_call=llm, model=None)
    assert not r.is_suggestion
    # Cheap-by-default: no transcript ⇒ no spend.
    assert llm.calls == []


def test_llm_exception_declines():
    def boom(system, user, model, max_tokens):
        raise RuntimeError("dispatch blew up")

    r = reflect(_ctx(), llm_call=boom, model=None)
    assert not r.is_suggestion
    assert "failed" in r.reason

"""fit_review.reflection — the one sanctioned LLM call (Bite 3).

Spec: docs/spec-fit-reviewer-2026-06-12.md §3.4 + §7 (Bite 3).

This is the **bounded, rare, per-bot place expensive synthesis is allowed**. It
runs only after the pure-Python targeting step (Bite 1) has decided the bot has a
purpose-aligned, above-floor, gallery-matched need (``decision ==
targets_found``). Given that purpose, the targeting evidence, the bounded
transcript sample, and the SHORTLIST of real gallery apps, it answers one narrow
question:

    Is there exactly one capability whose need the transcript *demonstrates*? If
    yes, name the app (from the shortlist only) and quote the user's own words. If
    no, say so. Zero suggestions is a valid, common, good answer.

Design — mirrors ``user_profile_inferrer/extractor.py``:
  * **I/O-free.** Takes a callable for the LLM (so tests inject a deterministic
    stub) and returns a dataclass. The runner layer owns the file writes and the
    real OpenClaw dispatch. No model spend in tests.
  * **Cite-or-don't is enforced in code, not prompt etiquette** (spec §0, §3.6).
    After the model responds, this module:
      1. drops any cited quote that is NOT verbatim-present in the transcript it
         was given (the model cannot fabricate a quote),
      2. drops any citation whose ``session_id`` is not one the transcript
         attests to,
      3. requires ``suggested_gallery_pkg_id`` to be one of the SHORTLISTED real
         pkg_ids (the bounded action space — the model never invents an app),
      4. and if, after all that, no evidence survives, returns ``no_candidate``.
    The poller (Bite 4) re-verifies deterministically (Gate A) — this is the
    cheap first gate, defense in depth.

What this module deliberately does NOT do:
  * It does not compute ``value_estimate`` / ``altitude`` / ``support`` — those
    are deterministic, computed by the runner from the targeting report (Gate B).
  * It does not read or write any file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fit_review.candidate import CitedEvidence

logger = logging.getLogger(__name__)


# The reflection runs on the standard/power rung (NOT Haiku — this is the one
# place judgment matters, spec §3.4). The runner resolves the actual model id
# (the pod's ``standard`` role) and passes it in; ``None`` lets the dispatch fall
# back to the bot's agent default. Output is capped tight: 0–1 candidate.
DEFAULT_MODEL: str | None = None
DEFAULT_MAX_TOKENS: int = 600

# Cap the transcript fed to the model. The capture buffer is already bounded by
# policy (≤200 turns / ≤48h), but we hard-cap chars too so a pathological turn
# can't blow the input budget (spec §3.4: ≤ ~8K input tokens). Oldest turns drop
# first; ~16K chars ≈ 4K tokens leaves headroom for the framing + catalog.
MAX_TRANSCRIPT_CHARS = 16_000


# An LLMCallable takes (system_prompt, user_message, model, max_tokens) and
# returns the model's textual response. Same shape as the user-profile inferrer's
# (``model`` may be None here, letting the dispatch inherit the bot default).
LLMCallable = Callable[[str, str, Optional[str], int], str]


@dataclass
class ReflectionContext:
    """The bounded input the reflection sees (assembled in-bot by the runner).

    Deliberately small: purpose (1 line), the targeting candidates (a few rows),
    the gallery shortlist (real pkg_ids), and the recent user transcript. The
    model never sees the bot's whole history — only what targeting already
    selected (spec §3.2).
    """

    bot_id: str
    archetype: str | None
    mission: str | None
    # One brief per above-floor candidate noun (from the targeting report).
    candidate_briefs: list[dict[str, Any]]
    # identity: see applications.app_identity.resolve_app_id — the shortlist
    # rendered into the prompt is gallery catalog rows, and the pkg_id the
    # model echoes back is validated against those same keys below. Nothing
    # here is a manifest. See fit_review/__init__.py.
    # The real installable action space: [{pkg_id, name, matched_domains}, ...].
    shortlist: list[dict[str, Any]]
    # Recent USER turns: [{session_id, ts, text}, ...] (assistant text excluded
    # by the capture policy). The citation source for cite-or-don't.
    transcript_turns: list[dict[str, Any]]


@dataclass
class ReflectionResult:
    """The LLM-derived half of a candidate (the runner adds the deterministic
    half). ``decision == "no_candidate"`` means: emit nothing."""

    decision: str  # "suggest" | "no_candidate"
    recommended_need: str = ""
    suggested_gallery_pkg_id: str | None = None
    cited_evidence: list[CitedEvidence] = field(default_factory=list)
    reason: str = ""

    @property
    def is_suggestion(self) -> bool:
        return self.decision == "suggest"


DECISION_SUGGEST = "suggest"
DECISION_NO_CANDIDATE = "no_candidate"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You are the capability reviewer for a bot. Once per cycle you read what the bot is FOR (its declared purpose) against what its user actually DID (a sample of the user's own messages), and you decide whether there is exactly ONE capability the evidence demands.

You are given:
  - the bot's declared PURPOSE (archetype + mission),
  - the TARGETING candidates: the recurring activity domains that already cleared a support floor (these are WHERE to look — they are not themselves citations),
  - a SHORTLIST of real, installable gallery apps (each with a pkg_id and the domains it covers) — this is your ENTIRE action space,
  - a TRANSCRIPT: recent messages the user sent to the bot, each tagged [session_id | ts].

Your job: pick the ONE shortlisted app whose need the transcript demonstrates, and prove it with the user's OWN WORDS. Or decline.

HARD RULES (these are checked in code after you answer — breaking them gets your answer dropped):
  1. You may ONLY suggest an app whose pkg_id is in the SHORTLIST. Never invent a pkg_id or name an app not listed.
  2. Every cited quote MUST be copied VERBATIM from the TRANSCRIPT — exact characters, no paraphrase, no cleanup. Copy the session_id and ts from the same turn. A quote that is not found verbatim in the transcript will be discarded.
  3. Cite at least one quote that shows the RECURRING NEED the app would serve. If the transcript does not contain such a quote, you MUST decline — do not stretch, do not infer, do not fabricate.
  4. At most ONE suggestion per run. Zero suggestions is a valid, common, GOOD answer. Declining when the evidence is thin is correct behavior, not a failure.
  5. Do NOT assert any number or statistic. The numbers are computed elsewhere; your job is the need and the quotes.

Output ONLY a JSON object — no preamble, no markdown fences:

To SUGGEST:
{
  "decision": "suggest",
  "recommended_need": "<one or two sentences: the recurring need, in plain prose>",
  "suggested_gallery_pkg_id": "<a pkg_id from the SHORTLIST>",
  "cited_evidence": [
    {"quote": "<verbatim user words from the transcript>", "session_id": "<from that turn>", "ts": "<from that turn>"}
  ]
}

To DECLINE:
{"decision": "no_candidate", "reason": "<short why — e.g. 'no transcript quote demonstrates a recurring need for any shortlisted app'>"}
"""


def _format_purpose(ctx: ReflectionContext) -> str:
    arch = ctx.archetype or "(none)"
    mission = ctx.mission or "(no mission stated)"
    return f"archetype: {arch}\nmission: {mission}"


def _format_candidates(ctx: ReflectionContext) -> str:
    if not ctx.candidate_briefs:
        return "(none)"
    lines: list[str] = []
    for c in ctx.candidate_briefs:
        verbs = ", ".join(
            f"{v}×{n}" for v, n in (c.get("top_verbs") or [])[:3]
        ) or "(n/a)"
        lines.append(
            f"- {c.get('noun')}: {c.get('distinct_sessions', 0)} sessions / "
            f"{c.get('distinct_days', 0)} days; top verbs: {verbs}; "
            f"alignment={c.get('alignment', '?')}"
        )
    return "\n".join(lines)


def _format_shortlist(ctx: ReflectionContext) -> str:
    if not ctx.shortlist:
        return "(none)"
    lines: list[str] = []
    for g in ctx.shortlist:
        covers = ", ".join(g.get("matched_domains") or [])
        lines.append(
            # identity: see resolve_app_id — the shortlist rendered into the prompt is catalog rows.
            f"- pkg_id={g.get('pkg_id')} \"{g.get('name')}\" — covers: {covers}"
        )
    return "\n".join(lines)


def _format_transcript(turns: list[dict[str, Any]]) -> str:
    """Render the user turns, newest last, capped at MAX_TRANSCRIPT_CHARS.

    Each turn is labelled with its session_id + ts so the model copies the right
    provenance onto each quote. We keep the MOST RECENT turns when over budget
    (drop oldest), since recency best reflects current need.
    """
    rendered: list[str] = []
    for t in turns:
        sid = str(t.get("session_id") or "")
        ts = str(t.get("ts") or "")
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        rendered.append(f"[{sid} | {ts}]\n{text}")
    block = "\n\n".join(rendered)
    if len(block) > MAX_TRANSCRIPT_CHARS:
        # Keep the tail (most recent) within budget.
        block = block[-MAX_TRANSCRIPT_CHARS:]
    return block or "(no recent user messages)"


def _build_user_message(ctx: ReflectionContext) -> str:
    return f"""# Bot
{ctx.bot_id}

# Declared purpose
{_format_purpose(ctx)}

# Targeting candidates (WHERE to look — recurring, above-floor domains)
{_format_candidates(ctx)}

# Shortlist (your ENTIRE action space — real installable apps)
{_format_shortlist(ctx)}

# Transcript (recent USER messages — your citation source)
{_format_transcript(ctx.transcript_turns)}

Decide now. Suggest exactly one shortlisted app with verbatim quotes, or decline."""


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing + cite-or-don't enforcement
# ─────────────────────────────────────────────────────────────────────────────


def _parse_response(text: str) -> dict | None:
    """Extract the JSON object from the model response, or None."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        stripped = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            return None
        try:
            data, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _norm(s: str) -> str:
    """Normalize whitespace for the verbatim-quote substring check.

    Collapses any run of whitespace to a single space and lowercases — this
    tolerates only trivial whitespace/case differences (a model copying with a
    stray newline or different capitalization), NOT paraphrase. The words must
    actually appear in the transcript.
    """
    return " ".join(s.split()).lower()


def _verify_evidence(
    raw_evidence: Any, transcript_turns: list[dict[str, Any]]
) -> list[CitedEvidence]:
    """Keep only citations whose quote is verbatim-present in the transcript AND
    whose session_id the transcript attests to. This is the cite-or-don't gate.
    """
    # Build the normalized haystack (all user text) + the attestable session set.
    haystacks: list[str] = []
    known_sessions: set[str] = set()
    for t in transcript_turns:
        text = str(t.get("text") or "")
        if text.strip():
            haystacks.append(_norm(text))
        sid = str(t.get("session_id") or "")
        if sid:
            known_sessions.add(sid)
    full_haystack = "\n".join(haystacks)

    survivors: list[CitedEvidence] = []
    for ev in raw_evidence or []:
        if not isinstance(ev, dict):
            continue
        quote = str(ev.get("quote") or "").strip()
        session_id = str(ev.get("session_id") or "").strip()
        ts = str(ev.get("ts") or "").strip()
        if not quote:
            continue
        if _norm(quote) not in full_haystack:
            # Fabricated / paraphrased quote — drop it (cite-or-don't).
            logger.info(
                "fit_review.reflection: dropping non-verbatim quote (first 60=%r)",
                quote[:60],
            )
            continue
        if not session_id or session_id not in known_sessions:
            # A quote with no attestable session can't be re-verified by the
            # poller's Gate A (and isn't ``CitedEvidence.is_citable``) — drop it.
            logger.info(
                "fit_review.reflection: dropping quote with missing/unknown "
                "session_id=%r",
                session_id,
            )
            continue
        survivors.append(
            CitedEvidence(quote=quote, session_id=session_id, ts=ts)
        )
    return survivors


def reflect(
    ctx: ReflectionContext,
    *,
    llm_call: LLMCallable,
    model: str | None = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ReflectionResult:
    """Run the one bounded reflection. Returns 0–1 cited suggestion.

    Args:
        ctx: the assembled, bounded review context.
        llm_call: callable that runs the model. Tests inject a stub; production
            wires it to the bot's local OpenClaw agent (one call, no spend in
            tests).
        model: model id to pin; ``None`` inherits the bot's agent default.
        max_tokens: output cap.

    Returns:
        ReflectionResult. ``decision == "no_candidate"`` whenever the transcript
        is empty, the model declines, the model errors / returns garbage, the
        chosen pkg_id is not on the shortlist, or no cited quote survives the
        verbatim check. In every one of those cases the runner writes nothing.
    """
    # No transcript ⇒ nothing to cite ⇒ decline WITHOUT spending a call. This
    # also covers the opt-out / empty-buffer case the runner already gates on.
    if not ctx.transcript_turns:
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason="no transcript available within capture policy — cannot cite",
        )

    user_msg = _build_user_message(ctx)
    try:
        response_text = llm_call(_SYSTEM_PROMPT, user_msg, model, max_tokens)
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ no candidate
        logger.warning("fit_review.reflection: LLM call failed: %s", exc)
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE, reason=f"llm call failed: {exc}"
        )

    data = _parse_response(response_text)
    if data is None:
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason="unparseable model response",
        )

    decision = str(data.get("decision") or "").strip().lower()
    if decision != DECISION_SUGGEST:
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason=str(data.get("reason") or "model declined"),
        )

    # ── It claims a suggestion — run the cite-or-don't + bounded-action gates ──
    pkg_id = str(data.get("suggested_gallery_pkg_id") or "").strip() or None
    shortlist_ids = {
        # identity: see resolve_app_id — validates the model's pick against the shortlist's catalog keys.
        str(g.get("pkg_id") or "") for g in ctx.shortlist if g.get("pkg_id")
    }
    if not pkg_id or pkg_id not in shortlist_ids:
        # The model named an app outside the bounded action space — reject.
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason=(
                f"suggested pkg_id {pkg_id!r} is not on the shortlist "
                "(bounded action space)"
            ),
        )

    survivors = _verify_evidence(data.get("cited_evidence"), ctx.transcript_turns)
    if not survivors:
        # No verbatim, attestable quote survived ⇒ cite-or-don't says: don't.
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason="no cited quote survived the verbatim / attestation check",
        )

    recommended_need = str(data.get("recommended_need") or "").strip()
    if not recommended_need:
        return ReflectionResult(
            decision=DECISION_NO_CANDIDATE,
            reason="suggestion had no recommended_need prose",
        )

    return ReflectionResult(
        decision=DECISION_SUGGEST,
        recommended_need=recommended_need,
        suggested_gallery_pkg_id=pkg_id,
        cited_evidence=survivors,
    )

"""arbiter.refine — Operator-driven proposal iteration via the bot's LLM.

When the operator types feedback on a proposal in the In Process queue,
this module is responsible for:

  1. Resolving the bot's Anthropic API key (from auth-profiles.json) so
     the cost lands on the bot's account, not a centralised admin key.
     This is the "per-bot inference, never centralised" architecture
     principle: each bot pays for its own improvement.

  2. Building a structured prompt that asks the LLM to revise only the
     prose fields (problem, admin_surface_summary, action.context for
     Investigation/WorkflowInstruction) — never the structural fields
     that feed the fingerprint (action.kind, target_path, trigger_obs).
     This preserves dedup behavior across iterations.

  3. Validating the response and returning a typed result the caller
     can apply to the proposal.

The actual mutation (and recording the ProposalRevision) is the caller's
responsibility — this module is pure logic plus an API call.

Tests inject a fake ``llm_call`` callable so they don't hit the network.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from schema.proposal import Investigation, Proposal, WorkflowInstruction


logger = logging.getLogger(__name__)


# Broken-config fallback for the refine model. Real selection routes
# through ``_resolve_refine_model`` → ``models.resolve_tier("tier3", config,
# bot_id=bot_id)`` so pod-wide or per-bot tier3 overrides win. The literal
# only matters when the analyzer package isn't importable (test isolation)
# or tier3 resolves to a non-Anthropic provider — refine.py calls the
# Anthropic SDK directly and can't dispatch openai/google models from here.
_DEFAULT_MODEL_FALLBACK: str = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS: int = 800

# Module-private sentinel — lets make_anthropic_caller distinguish
# "caller wants the tier3 default" (resolve at construction time) from
# "caller passed an explicit model id" (use that string as-is). Without
# the sentinel, the tier3 resolution would either always run (preventing
# explicit overrides) or never run (preventing the tier system from
# winning over the literal).
_USE_TIER3_DEFAULT: object = object()


def _resolve_refine_model(bot_id: str | None) -> str:
    """Resolve the model for a proposal-refine dispatch.

    Routes through the pod's tier3 (Grunt) configuration via
    ``models.resolve_tier`` so:
      * pod-wide tier3 swaps (operator picks a different cheap-tier
        model) propagate here automatically
      * per-bot ``tier_assignments`` override is honored — a bot whose
        operator pinned tier3 to Sonnet gets Sonnet for its refines
      * fallback chains in the tier config are respected

    refine.py calls the Anthropic SDK directly (``client.messages.create``),
    so the ``anthropic/`` prefix the tier registry uses internally is
    stripped before returning. Non-Anthropic tier3 resolutions (e.g.
    fallback to ``openai/gpt-4o-mini``) yield the hardcoded fallback
    with a warning — the SDK can't dispatch those.

    Mirrors the resolver pattern used by ``applications.reviewer._resolve_tier3``
    and ``applications.bot_forge._resolve_critique_model``.
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        resolved = resolve_tier("tier3", load_config(), bot_id=bot_id)
    except Exception as exc:
        logger.debug("arbiter.refine: tier3 resolve failed: %s", exc)
        return _DEFAULT_MODEL_FALLBACK
    if not resolved.startswith("anthropic/"):
        logger.warning(
            "arbiter.refine: tier3 resolved to non-Anthropic %r — refine "
            "uses the Anthropic SDK directly and can't dispatch this. "
            "Falling back to %r.", resolved, _DEFAULT_MODEL_FALLBACK,
        )
        return _DEFAULT_MODEL_FALLBACK
    return resolved.split("/", 1)[1]


_SYSTEM_PROMPT = """You are revising a system-generated proposal based on operator feedback.

You will receive the original proposal as JSON and a feedback message. Produce a revised version that addresses the feedback. Output ONLY a JSON object — no preamble, no markdown fences:

{
  "problem": "<revised one-line operator-facing summary>",
  "admin_surface_summary": "<revised, max 120 chars>",
  "action_context": "<revised prose; only include this field if the original action has a 'context' field>"
}

Rules:
- ONLY modify prose fields. Do NOT modify structural fields (action.kind, target paths, trigger_observations, claim, fingerprint inputs).
- Stay within the same proposal kind. An Investigation must remain an investigation; a WorkflowInstruction must remain instructions.
- Address the feedback directly. If the feedback says "this is too aggressive", soften the tone. If it says "explain why", add the rationale. If it says "include more detail about X", add that detail.
- Preserve the proposal's intent. Don't fundamentally change what's being proposed — just refine how it's described.
- Output valid JSON. The three field names must be exactly: problem, admin_surface_summary, action_context.
- If the original action has no context field (e.g. a ConfigPatch or TierAdjustment), omit action_context from your output.

Confidence guidance: only emit changes you're confident improve the proposal in light of the feedback. If the feedback is ambiguous, prefer minimal edits.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RefineResult:
    """Output of a refine call.

    ``ok`` is False on parse failure / API error. The caller should NOT
    apply the changes when ok is False; the proposal is left untouched.
    """

    ok: bool
    new_problem: str = ""
    new_admin_surface_summary: str = ""
    new_action_context: str | None = None  # None = don't touch action.context
    raw_response: str = ""
    error: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Bot API key lookup
# ─────────────────────────────────────────────────────────────────────────────


def read_bot_anthropic_key(bot_id: str, network: dict[str, Any]) -> str:
    """Resolve the Anthropic API key for ``bot_id``.

    Order: bot's OpenClaw auth store (preferred — keeps cost on the bot's
    account) → ANTHROPIC_API_KEY env var (test/dev fallback). Returns empty
    string if neither is available; callers MUST surface a clear "no key"
    error (or degrade) rather than silently falling through to a different
    bot's account.

    The SOURCE read is delegated to ``evolve_admin.oc_store`` (per-agent sqlite
    store → legacy auth-profiles.json → transitional bak); the PARSER stays
    ``primary_bot.extract_anthropic_key``. The plain direct-JSON read this
    replaces returned "" for every bot once OpenClaw 2026.6 moved the JSON into
    the sqlite store.
    """
    import os

    try:
        from evolve_admin.config import bot_home  # type: ignore
        from evolve_admin.oc_store import read_anthropic_key  # type: ignore

        key = read_anthropic_key(bot_id, home=bot_home(bot_id, network))
        if key:
            return key
    except Exception as exc:
        # oc_store unavailable (cross-package import edge) — fall through to env.
        # Logged (not silently swallowed) so a real import/read failure shows up.
        logger.debug(
            "read_bot_anthropic_key: oc_store read failed for %r (%s); using env",
            bot_id,
            exc,
        )

    # Test / dev fallback. Production should never hit this path unless
    # ANTHROPIC_API_KEY is intentionally exported for debugging.
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction + response parsing
# ─────────────────────────────────────────────────────────────────────────────


def _action_context(proposal: Proposal) -> str | None:
    """Return the prose context of an action, if it has one. Investigation
    and WorkflowInstruction carry operator-facing prose; other kinds don't."""
    a = proposal.action
    if isinstance(a, Investigation):
        return a.context
    if isinstance(a, WorkflowInstruction):
        # The "context" for a WorkflowInstruction is its content — that's
        # what the operator reads to decide what to do.
        return a.content
    return None


def _action_summary(proposal: Proposal) -> str:
    """Short rendering of the action's prose for the revision audit log."""
    ctx = _action_context(proposal)
    if ctx is None:
        return ""
    if len(ctx) > 200:
        return ctx[:200] + "…"
    return ctx


def _build_user_message(proposal: Proposal, feedback: str) -> str:
    """Compose the user-facing prompt body."""
    action_kind = getattr(proposal.action, "kind", type(proposal.action).__name__)
    payload = {
        "kind": action_kind,
        "problem": proposal.problem,
        "admin_surface_summary": proposal.admin_surface_summary,
        "action_context": _action_context(proposal),
    }
    return (
        "ORIGINAL PROPOSAL:\n"
        + json.dumps(payload, indent=2)
        + "\n\nOPERATOR FEEDBACK:\n"
        + feedback.strip()
        + "\n\nProduce the JSON revision now."
    )


def _parse_response(raw: str) -> RefineResult:
    """Parse the LLM's JSON output into a RefineResult.

    Tolerant of fenced JSON and prose preambles so a chatty model doesn't
    derail the call.
    """
    text = raw.strip()
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
            return RefineResult(
                ok=False,
                raw_response=raw,
                error="parse_failure: no JSON object in response",
            )
        try:
            data, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as e:
            return RefineResult(
                ok=False, raw_response=raw, error=f"parse_failure: {e}"
            )

    if not isinstance(data, dict):
        return RefineResult(
            ok=False, raw_response=raw, error="parse_failure: not a JSON object"
        )

    new_problem = str(data.get("problem", "")).strip()
    new_summary = str(data.get("admin_surface_summary", "")).strip()
    if not new_problem and not new_summary:
        return RefineResult(
            ok=False,
            raw_response=raw,
            error="parse_failure: response had neither problem nor admin_surface_summary",
        )
    # admin_surface_summary contract: max 120 chars. Truncate rather than
    # reject so a slightly verbose model output still produces a usable
    # revision.
    if len(new_summary) > 120:
        new_summary = new_summary[:117] + "…"

    raw_ctx = data.get("action_context")
    new_ctx: str | None = None
    if isinstance(raw_ctx, str) and raw_ctx.strip():
        new_ctx = raw_ctx.strip()

    return RefineResult(
        ok=True,
        new_problem=new_problem,
        new_admin_surface_summary=new_summary,
        new_action_context=new_ctx,
        raw_response=raw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Default LLM caller (Anthropic)
# ─────────────────────────────────────────────────────────────────────────────


def make_anthropic_caller(
    api_key: str,
    *,
    bot_id: str | None = None,
    model: "str | object" = _USE_TIER3_DEFAULT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[[str], str]:
    """Build an LLM-call callable that takes a user message and returns
    raw response text. The caller is responsible for prompt assembly;
    this function only owns the API transport.

    ``bot_id`` enables per-bot tier3 override when the default model is
    used. Callers should always pass this (it's free — the resolution
    happens once at construction time).

    ``model`` defaults to a sentinel resolved at construction time via
    :func:`_resolve_refine_model` (pod tier3, per-bot-aware). Pass an
    explicit string to force a specific id, e.g. when a test wires a
    deterministic fake or an evaluation harness pins a comparison model.

    Raises on import failure or wiring failure so callers can return a
    clear "infrastructure not available" error to the operator.
    """
    from anthropic import Anthropic  # type: ignore[import-not-found]

    if model is _USE_TIER3_DEFAULT:
        model = _resolve_refine_model(bot_id)

    client = Anthropic(api_key=api_key)

    def call(user_msg: str) -> str:
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
        text_parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        return "".join(text_parts)

    return call


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


def refine_proposal(
    proposal: Proposal,
    feedback: str,
    *,
    llm_call: Callable[[str], str],
) -> RefineResult:
    """Run a refine cycle on ``proposal`` with the operator's feedback.

    The caller injects ``llm_call`` (typically built via
    :func:`make_anthropic_caller`) so tests can swap in a deterministic
    fake. Errors during the API call are caught here and returned as
    a non-ok ``RefineResult`` rather than raising — the operator sees
    "refine failed: <reason>" instead of a 500.
    """
    if not feedback.strip():
        return RefineResult(ok=False, error="feedback is empty")

    user_msg = _build_user_message(proposal, feedback)
    try:
        raw = llm_call(user_msg)
    except Exception as exc:  # noqa: BLE001 — surface any API failure cleanly
        logger.warning("refine_proposal: llm_call raised: %s", exc)
        return RefineResult(
            ok=False, error=f"api_error: {type(exc).__name__}: {exc}"
        )

    return _parse_response(raw)


def apply_refinement(
    proposal: Proposal, result: RefineResult, *, feedback: str, actor: str
) -> None:
    """Mutate ``proposal`` in place with the revised fields and append a
    ProposalRevision recording the prior state + feedback.

    Caller is responsible for persisting the proposal afterwards. No state
    transitions are performed — refine doesn't change status.
    """
    if not result.ok:
        raise ValueError("apply_refinement called with a failed RefineResult")

    from datetime import datetime, timezone

    from schema.proposal import ProposalRevision

    revision = ProposalRevision(
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        actor=actor,
        feedback=feedback.strip(),
        prior_problem=proposal.problem,
        prior_admin_surface_summary=proposal.admin_surface_summary,
        prior_action_summary=_action_summary(proposal),
    )

    proposal.problem = result.new_problem
    proposal.admin_surface_summary = result.new_admin_surface_summary

    # Update the action's prose context. Do nothing for action kinds that
    # don't carry prose (ConfigPatch/TierAdjustment/etc.) — refine still
    # applied the problem/summary edits, which is the meaningful change
    # for those kinds.
    if result.new_action_context is not None:
        a = proposal.action
        if isinstance(a, Investigation):
            a.context = result.new_action_context
        elif isinstance(a, WorkflowInstruction):
            a.content = result.new_action_context

    proposal.revisions.append(revision)

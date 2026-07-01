"""Action tool — refine a pending or in-process proposal via LLM.

Mirrors the admin UI's POST /api/arbiter/proposals/<id>/refine flow
(server.py:23836). The operator types feedback in prose; the bot's
own Anthropic key is used to revise the proposal's surface text
(``problem``, ``admin_surface_summary``, optionally ``action.context``)
while structural fields (action.kind, action args, fingerprint,
status) stay fixed. The original wording is preserved as a
ProposalRevision entry so the change history is auditable.

Risk tier: write_safe.

* Reversible in practice — every prior revision is captured in
  ``proposal.revisions``; the operator can read the history and
  re-refine back toward earlier wording.
* Bounded blast radius — one proposal, prose-only edits, no
  config/code/state change.
* Spend is bounded — one small Anthropic call against the bot's own
  api_key (cost lands on the bot, not pod-wide).

Constraints (matching the admin UI):

* Proposal status must be ``pending`` or ``applied`` — refining a
  terminal proposal makes no sense.
* Proposal must have ``bot_id`` — pod-wide proposals have no bot to
  bill the LLM call against.
* The bot must have an ``anthropic`` entry in its ``auth-profiles.json``
  with a valid api_key.
* ``feedback`` is required (non-empty after strip).

Memory references:
* feedback_per_bot_inference — LLM inference over user/proposal data
  runs in the bot's own credentials, never centrally. This tool
  honours that by using ``read_bot_anthropic_key`` rather than a
  shared admin key.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _find_proposal(shared_dir: Path, proposal_id: str):
    """Locate a proposal across all subdirs — same pattern as
    action_proposal._find_proposal. Duplicated here to keep this
    module self-contained (action_proposal.py is the only other
    caller and refactoring to a shared helper can wait until a
    third caller appears)."""
    try:
        from arbiter import store as arbiter_store
    except ImportError as exc:
        log.warning("action.proposal.refine: arbiter store unavailable: %s", exc)
        return None, None
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return None, None
    proposal, _path, subdir = located
    return proposal, subdir


def _refine_handler(
    shared_dir: Path,
    network_path: Path,
    proposal_id: str,
    feedback: str,
) -> dict[str, Any]:
    """Refine a proposal's prose using the bot's own LLM credentials.

    Mirrors server.py::api_arbiter_proposal_refine end-to-end:
    locate → status check → bot_id check → load bot key → call LLM →
    apply + persist. Errors are returned as dicts (never raised) so
    the model sees a clean error and can recover in prose.
    """
    if not feedback or not feedback.strip():
        return {"ok": False, "error": "feedback is required"}

    try:
        from arbiter import store as arbiter_store
        from arbiter.refine import (
            apply_refinement,
            make_anthropic_caller,
            read_bot_anthropic_key,
            refine_proposal,
        )
        from evolve_admin.config import load_network
    except ImportError as exc:
        return {"ok": False, "error": f"arbiter/refine unavailable: {exc}"}

    proposal, subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}

    if proposal.status not in ("pending", "applied"):
        return {
            "ok": False,
            "error": (
                f"cannot refine proposal in status {proposal.status!r}; "
                "expected 'pending' or 'applied'"
            ),
        }

    if not proposal.bot_id:
        return {
            "ok": False,
            "error": (
                "this proposal has no bot_id (pod-wide). Refine is per-bot "
                "only — there's no bot account to bill the LLM call against."
            ),
        }

    try:
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"failed to load network.json: {exc}"}

    api_key = read_bot_anthropic_key(proposal.bot_id, network)
    if not api_key:
        return {
            "ok": False,
            "error": (
                f"no Anthropic API key found for bot {proposal.bot_id!r}. "
                "The bot's auth-profiles.json must contain an anthropic "
                "api_key entry for refine to work."
            ),
        }

    try:
        llm_call = make_anthropic_caller(api_key, bot_id=proposal.bot_id)
    except ImportError as exc:
        return {
            "ok": False,
            "error": (
                f"anthropic SDK not available: {exc}. Install on admin host."
            ),
        }

    result = refine_proposal(proposal, feedback, llm_call=llm_call)
    if not result.ok:
        return {"ok": False, "error": f"refine failed: {result.error}"}

    try:
        apply_refinement(proposal, result, feedback=feedback, actor="evo")
        arbiter_store.write_proposal(proposal, shared_dir, subdir=subdir)
    except Exception as exc:  # noqa: BLE001
        log.exception("action.proposal.refine: persist failed")
        return {"ok": False, "error": f"persist failed: {exc}"}

    return {
        "ok": True,
        "proposal_id": proposal.id,
        "revision_count": len(proposal.revisions),
        "new_problem": proposal.problem,
        "new_admin_surface_summary": proposal.admin_surface_summary,
        # Refine doesn't change status; the proposal stays in its
        # current subdir. Verify by re-fetching and inspecting the
        # latest revision in proposal.revisions[-1].
        "verify_via": {
            "tool": (
                "pod_state.proposals.pending"
                if proposal.status == "pending"
                else "pod_state.proposals.in_process"
            ),
            "args": {"proposal_id": proposal.id},
            "expect": (
                "this proposal appears with the revised problem text and "
                f"revision_count={len(proposal.revisions)}"
            ),
        },
    }


def _refine_validate(
    shared_dir: Path,
    network_path: Path,
    proposal_id: str,
    feedback: str,
) -> dict[str, Any]:
    """Dry-run: confirm proposal exists, is refinable (pending|applied),
    has a bot_id, and the bot has an Anthropic key configured.

    We deliberately do NOT call the LLM during validate — that would
    burn spend just to render a confirmation button. The handler will
    re-check the bot key at execution time.
    """
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}
    if not feedback or not feedback.strip():
        return {
            "ok": False,
            "reason": (
                "feedback is required — describe what to change about the "
                "proposal's wording (one or two sentences is enough)"
            ),
        }

    proposal, _subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}

    if proposal.status not in ("pending", "applied"):
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}'; refine only "
                "applies to 'pending' or 'applied'"
            ),
        }

    if not proposal.bot_id:
        return {
            "ok": False,
            "reason": (
                "this proposal is pod-wide (no bot_id); refine needs a "
                "bot to bill the LLM call against"
            ),
        }

    # Surface the bot-key check at validate time so the operator gets
    # a clean "you need to configure an Anthropic key for <bot>" rather
    # than a button that fails at execution.
    try:
        from arbiter.refine import read_bot_anthropic_key
        from evolve_admin.config import load_network
    except ImportError as exc:
        return {"ok": False, "reason": f"arbiter/refine unavailable: {exc}"}

    try:
        network = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"failed to load network.json: {exc}"}

    if not read_bot_anthropic_key(proposal.bot_id, network):
        return {
            "ok": False,
            "reason": (
                f"bot {proposal.bot_id!r} has no Anthropic api_key in its "
                "auth-profiles.json — refine can't bill an LLM call. Set "
                "the key first."
            ),
        }

    return {
        "ok": True,
        "context": {
            "bot_id": proposal.bot_id,
            "current_status": proposal.status,
            "summary": (proposal.admin_surface_summary or proposal.problem)[:200],
            "existing_revisions": len(proposal.revisions),
        },
    }


REFINE_TOOL = Tool(
    name="action.proposal.refine",
    description=(
        "Iterate on a pending or in-process proposal's prose using the "
        "bot's own LLM. The structural fields (action kind, args, "
        "fingerprint, lifecycle status) stay fixed; only the surface "
        "wording — problem statement, admin summary, action context — "
        "gets revised based on the feedback you provide. Original "
        "wording is preserved in proposal.revisions. Use when a "
        "proposal is real but worded poorly, missing context, or "
        "framing something the wrong way. Bills against the bot's own "
        "Anthropic key (cost lands on that bot, not pod-wide). Refuses "
        "pod-wide proposals (no bot to bill) and terminal-status "
        "proposals."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "Proposal id — get from pod_state.proposals.pending "
                    "or pod_state.proposals.in_process."
                ),
            },
            "feedback": {
                "type": "string",
                "description": (
                    "What to change about the proposal's wording. One or "
                    "two sentences. Example: 'this is about the cost "
                    "spike, not the audit — reframe around the spend '"
                    "'pattern, not the audit cadence.'"
                ),
            },
        },
        "required": ["proposal_id", "feedback"],
        "additionalProperties": False,
    },
    handler=_refine_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_refine_validate,
    tags=("action", "proposal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REFINE_TOOL)

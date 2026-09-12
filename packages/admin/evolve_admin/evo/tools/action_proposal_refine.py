"""Action tool — refine a pending or in-process proposal via LLM.

Calls the admin UI's POST /api/arbiter/proposals/<id>/refine endpoint
over the unix socket, FAIL CLOSED when the daemon is unreachable
(roadmap 7.1 C2). The operator types feedback in prose; the bot's
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
* The pod's primary bot must have a credentialed LLM provider
  (resolved provider-agnostically via ``infra_llm``, #3466; the bot's
  own tier3 model pin is honored when credentialed).
* ``feedback`` is required (non-empty after strip).
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
    """Refine a proposal's prose via the admin daemon.

    Routes through ``POST /api/arbiter/proposals/<id>/refine`` — the
    same locate → status check → bot_id check → LLM call → apply +
    persist flow the admin UI runs, executed daemon-side. FAIL CLOSED
    (roadmap 7.1 C2): when the daemon is unreachable the tool refuses
    and writes nothing; there is no in-process persist path. Errors
    are returned as dicts (never raised) so the model sees a clean
    error and can recover in prose.
    """
    if not feedback or not feedback.strip():
        return {"ok": False, "error": "feedback is required"}

    # Read-only enrichment for the verify hint — never gates the action.
    proposal, _subdir = _find_proposal(shared_dir, proposal_id)

    from ..admin_client import daemon_refusal, require_daemon_call
    reachable, status, body = require_daemon_call(
        "POST", f"/api/arbiter/proposals/{proposal_id}/refine",
        body={"feedback": feedback, "actor": "evo"},
        # The endpoint makes an LLM call — allow well beyond the 10s default.
        timeout=120.0,
    )
    if not reachable:
        return daemon_refusal()
    if status == 404:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}
    if status != 200:
        detail = (body or {}).get("error") if isinstance(body, dict) else None
        return {
            "ok": False,
            "error": detail or f"admin daemon returned status {status}",
        }
    resp = body if isinstance(body, dict) else {}

    revision_count = resp.get("revision_count", 0)
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "revision_count": revision_count,
        "new_problem": resp.get("new_problem"),
        "new_admin_surface_summary": resp.get("new_admin_surface_summary"),
        # Refine doesn't change status; the proposal stays in its
        # current subdir. Verify by re-fetching and inspecting the
        # latest revision in proposal.revisions[-1].
        "verify_via": {
            "tool": (
                "pod_state.proposals.pending"
                if (proposal is None or proposal.status == "pending")
                else "pod_state.proposals.in_process"
            ),
            "args": {"proposal_id": proposal_id},
            "expect": (
                "this proposal appears with the revised problem text and "
                f"revision_count={revision_count}"
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

    # Surface the credential check at validate time so the operator gets
    # a clean "you need to credential an LLM provider" rather than a
    # button that fails at execution.
    try:
        from infra_llm import resolve_infra_llm  # type: ignore
        from evolve_admin.config import load_network
    except ImportError as exc:
        return {"ok": False, "reason": f"arbiter/refine unavailable: {exc}"}

    try:
        network = load_network(network_path)
    except Exception:  # noqa: BLE001 — fall back to the pod default location
        network = None

    if resolve_infra_llm("fast", network=network) is None:
        return {
            "ok": False,
            "reason": (
                "no LLM provider credentialed for the pod's primary bot — "
                "refine can't bill an LLM call. Add a provider API key first."
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
    wire_description=(
        "Revise a pending or applied proposal's prose (problem, "
        "summary, action context) from your feedback using the bot's "
        "own LLM; structural fields stay fixed and prior wording is "
        "kept in proposal.revisions. Use when a proposal is real but "
        "worded poorly. Refuses pod-wide proposals (no bot to bill) "
        "and terminal-status proposals."
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

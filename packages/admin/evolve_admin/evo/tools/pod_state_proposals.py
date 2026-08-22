"""Pod-state read tools — arbiter proposals.

Exposes two tools:

* ``pod_state.proposals.pending`` — currently-pending arbiter proposals
  awaiting operator review. The Better-Engine queue.

* ``pod_state.proposals.snoozed`` — proposals the operator (or auto-snooze
  daemon) deferred to a future wake condition. ``snoozed_until`` is the
  timestamp at which they'll be re-evaluated.

Both wrap ``arbiter.store.iter_proposals``. The projection here is
narrower than Proposal.to_dict — we surface what the operator needs to
decide ("what is this, why, urgency, age") and skip the verbose action
schemas, provenance dicts, and revision histories that don't help the
model's reasoning.

When the spec's Phase 1.2+ adds write tools, ``action.proposal.accept``
and friends will live in ``action_proposal.py`` — kept separate so read
and write tools don't share import-time concerns.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)

_DEFAULT_MAX_PROPOSALS = 50


def _project_proposal(p_dict: dict[str, Any]) -> dict[str, Any]:
    """Pick the operator-relevant fields from a Proposal.to_dict() blob.

    What we keep:
      - identity (id, bot_id, generator_id, dimension)
      - status + lifecycle (status, snoozed_until, created_at)
      - the human-readable surface (admin_surface_summary, problem,
        urgency, approval_audience)

    What we drop (verbose, not LLM-useful):
      - action dict (per-Action-kind schema; deep)
      - claim / revert_on_failure (apply-time concerns)
      - trigger_observations / provenance (introspection of the
        generator's decision process)
      - revisions / history / guardian_annotations / conflicts_with
        (cross-link cruft)
      - motivating_signals (list of signal ids — model can query
        signals separately via pod_state.signals)
      - signature / schema_version

    The dropped fields are still in the file on disk; a future
    pod_state.proposals.detail(id) tool can return them in full.
    """
    return {
        "id": p_dict.get("id"),
        "bot_id": p_dict.get("bot_id"),
        "generator_id": p_dict.get("generator_id"),
        "dimension": p_dict.get("dimension"),
        "status": p_dict.get("status"),
        "urgency": p_dict.get("urgency"),
        "approval_audience": p_dict.get("approval_audience"),
        "summary": p_dict.get("admin_surface_summary") or "",
        "problem": p_dict.get("problem"),
        "created_at": p_dict.get("created_at"),
        # Snooze fields appear only when the proposal is actually
        # snoozed — keep the projection compact for pending tool.
        **(
            {"snoozed_until": p_dict.get("snoozed_until")}
            if p_dict.get("snoozed_until")
            else {}
        ),
    }


def _iter_proposals_handler(
    shared_dir: Path,
    subdir: str,
    *,
    bot_id: str | None,
    urgency: str | None,
    dimension: str | None,
    max_results: int,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    """Shared implementation for the two list tools.

    ``subdir`` is "pending" or "snoozed"; the underlying call is the
    same iter_proposals with a different subdir filter. Filters AND
    together. Defensive against missing arbiter/store import or
    missing proposals/ directory.

    ``proposal_id`` narrows to one specific id — used by verify_via
    after action.proposal.{apply,reject,mark_complete} to confirm a
    proposal moved out of (or stayed in) the queue. The count is 0
    when the proposal no longer matches the subdir (i.e. it
    transitioned), which is exactly the verify case.
    """
    try:
        from arbiter import store as arbiter_store
    except ImportError as exc:
        log.warning(
            "pod_state.proposals.%s: arbiter store unavailable: %s",
            subdir, exc,
        )
        return {
            "count": 0,
            "proposals": [],
            "truncated": False,
            "error": "arbiter store unavailable in this runtime",
        }

    requested = int(max_results) if max_results is not None else _DEFAULT_MAX_PROPOSALS
    cap = max(1, min(requested, 200))

    matched: list[dict[str, Any]] = []
    try:
        for p in arbiter_store.iter_proposals(shared_dir, subdirs=(subdir,)):
            if bot_id and p.bot_id != bot_id:
                continue
            if urgency and p.urgency != urgency:
                continue
            if dimension and p.dimension != dimension:
                continue
            if proposal_id and p.id != proposal_id:
                continue
            matched.append(_project_proposal(p.to_dict()))
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.proposals.%s: iteration failed", subdir)
        return {
            "count": 0,
            "proposals": [],
            "truncated": False,
            "error": f"iteration failed: {exc}",
        }

    truncated = len(matched) > cap
    return {
        "count": len(matched),
        "proposals": matched[:cap],
        "truncated": truncated,
    }


# ─── pod_state.proposals.pending ───────────────────────────────────────────


def _pending_handler(
    shared_dir: Path,
    bot_id: str | None = None,
    urgency: str | None = None,
    dimension: str | None = None,
    proposal_id: str | None = None,
    max_results: int = _DEFAULT_MAX_PROPOSALS,
) -> dict[str, Any]:
    return _iter_proposals_handler(
        shared_dir, "pending",
        bot_id=bot_id, urgency=urgency, dimension=dimension,
        proposal_id=proposal_id,
        max_results=max_results,
    )


PENDING_TOOL = Tool(
    name="pod_state.proposals.pending",
    description=(
        "List arbiter proposals currently in the pending state — "
        "Better Engine recommendations awaiting operator approval. "
        "Each entry has the proposal id, target bot, generator, "
        "dimension, urgency, the operator-facing summary, and the "
        "problem statement. Filter by bot_id, urgency, dimension, or "
        "proposal_id. Use this when the operator asks 'what's in my "
        "queue?', 'any pending changes for admin_bot?', or as verify_via "
        "after action.proposal.{apply,reject,mark_complete} — passing "
        "the proposal_id; count=0 confirms the proposal left pending."
    ),
    wire_description=(
        "List pending arbiter proposals awaiting operator approval — "
        "each with id, bot, generator, dimension, urgency, summary, "
        "and problem. Filter by bot_id, urgency, dimension, or "
        "proposal_id. Use for 'what's in my queue?' and as verify_via "
        "after action.proposal.{apply,reject,mark_complete} (count=0 "
        "confirms the proposal left pending)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Restrict to proposals targeting this bot.",
            },
            "urgency": {
                "type": "string",
                "description": (
                    "Restrict to proposals at this urgency. Canonical "
                    "values: 'security_critical', "
                    "'operational_urgent', 'cost_alert', "
                    "'substrate_warn', 'improvement', 'hygiene', "
                    "'whimsy'. Legacy urgencies (e.g. "
                    "'needs_attention') may appear in-flight — filter "
                    "accepts any string."
                ),
            },
            "dimension": {
                "type": "string",
                "description": (
                    "Restrict to proposals scoped to one Better Engine "
                    "dimension (e.g. 'cost', 'security', 'reliability')."
                ),
            },
            "proposal_id": {
                "type": "string",
                "description": (
                    "Restrict to one specific proposal id. Used by "
                    "verify_via — count=0 confirms the proposal left "
                    "pending; count=1 confirms it's still there."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Cap on proposals returned. Default 50.",
            },
        },
        "additionalProperties": False,
    },
    handler=_pending_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "proposals"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(PENDING_TOOL)


# ─── pod_state.proposals.snoozed ───────────────────────────────────────────


def _snoozed_handler(
    shared_dir: Path,
    bot_id: str | None = None,
    urgency: str | None = None,
    dimension: str | None = None,
    proposal_id: str | None = None,
    max_results: int = _DEFAULT_MAX_PROPOSALS,
) -> dict[str, Any]:
    return _iter_proposals_handler(
        shared_dir, "snoozed",
        bot_id=bot_id, urgency=urgency, dimension=dimension,
        proposal_id=proposal_id,
        max_results=max_results,
    )


SNOOZED_TOOL = Tool(
    name="pod_state.proposals.snoozed",
    description=(
        "List arbiter proposals the operator (or auto-snooze daemon) "
        "deferred. Each entry includes a snoozed_until timestamp "
        "indicating when the proposal will be re-evaluated and "
        "potentially re-surfaced as pending. Use this when the "
        "operator asks 'what did I snooze recently?' or 'is anything "
        "waiting on a future condition?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Restrict to proposals targeting this bot.",
            },
            "urgency": {
                "type": "string",
                "enum": ["urgent", "improvement", "background"],
                "description": "Restrict to proposals at this urgency.",
            },
            "dimension": {
                "type": "string",
                "description": "Restrict to one Better Engine dimension.",
            },
            "proposal_id": {
                "type": "string",
                "description": (
                    "Restrict to one specific proposal id (used by "
                    "verify_via after action.proposal.snooze)."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Cap on results. Default 50.",
            },
        },
        "additionalProperties": False,
    },
    handler=_snoozed_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "proposals", "snoozed"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(SNOOZED_TOOL)


# ─── pod_state.proposals.in_process ─────────────────────────────────────────
#
# Reads applied-status proposals that are NOT auto-succeeding — the ones
# sitting in the In Process tab on the Recommendations page, waiting for
# the operator to confirm offline follow-through is done.
#
# Added 2026-05-20 after a transcript where the operator was looking at
# the In Process tab and asked evo to help mark a proposal complete —
# but evo had only `pod_state.proposals.pending` and saw nothing
# matching the title, then confabulated possibilities. The applied/
# subdir was completely invisible to evo.


def _in_process_handler(
    shared_dir: Path,
    bot_id: str | None = None,
    urgency: str | None = None,
    dimension: str | None = None,
    proposal_id: str | None = None,
    max_results: int = _DEFAULT_MAX_PROPOSALS,
) -> dict[str, Any]:
    """List in-process proposals — applied + manual-completion kinds.

    Filter additionally on the action kind so auto-applied proposals
    (kind=ConfigPatch / UpsertCronJob / etc. that have a claim and
    will be auto-promoted by the verify daemon once the claim window
    expires — verify/daemon.py, NOT the retired apply.py) don't
    surface here. Only
    Investigation, WorkflowInstruction, AddSignalCollection — the
    kinds the operator actually closes by hand from the In Process
    tab.
    """
    try:
        from arbiter import store as arbiter_store
        from arbiter.apply import is_manual_completion_kind
    except ImportError as exc:
        log.warning(
            "pod_state.proposals.in_process: arbiter unavailable: %s", exc,
        )
        return {
            "count": 0,
            "proposals": [],
            "truncated": False,
            "error": "arbiter store unavailable in this runtime",
        }

    requested = int(max_results) if max_results is not None else _DEFAULT_MAX_PROPOSALS
    cap = max(1, min(requested, 200))

    matched: list[dict[str, Any]] = []
    try:
        for p in arbiter_store.iter_proposals(shared_dir, subdirs=("applied",)):
            kind = getattr(p.action, "kind", type(p.action).__name__)
            if not is_manual_completion_kind(kind):
                # Skip auto-succeed kinds — they belong in history /
                # archived once the sweep runs, not in the operator's
                # In Process view.
                continue
            if bot_id and p.bot_id != bot_id:
                continue
            if urgency and p.urgency != urgency:
                continue
            if dimension and p.dimension != dimension:
                continue
            if proposal_id and p.id != proposal_id:
                continue
            projected = _project_proposal(p.to_dict())
            # Augment with the action_kind so the model can tell
            # Investigation from WorkflowInstruction from
            # AddSignalCollection without a separate detail call.
            projected["action_kind"] = kind
            matched.append(projected)
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.proposals.in_process: iteration failed")
        return {
            "count": 0,
            "proposals": [],
            "truncated": False,
            "error": f"iteration failed: {exc}",
        }

    truncated = len(matched) > cap
    return {
        "count": len(matched),
        "proposals": matched[:cap],
        "truncated": truncated,
    }


IN_PROCESS_TOOL = Tool(
    name="pod_state.proposals.in_process",
    description=(
        "List arbiter proposals in the In Process tab — applied "
        "status with a manual-completion action kind "
        "(Investigation, WorkflowInstruction, AddSignalCollection). "
        "These are proposals the operator accepted that require "
        "offline follow-through: investigation work, manual config "
        "changes per a workflow instruction, or signal collection "
        "code that's pending engineering. They sit here until the "
        "operator clicks 'Mark complete' on the Recommendations "
        "page (or evo calls action.proposal.mark_complete on their "
        "behalf). Filter by bot_id, urgency, dimension, or "
        "proposal_id. Use this when the operator asks 'what's in "
        "process?', 'help me with the proposal I accepted', 'mark "
        "the security-cve-scan one complete' — anything pointing "
        "at the In Process tab — and as verify_via after "
        "action.proposal.mark_complete (count=0 confirms the "
        "proposal moved out of in_process)."
    ),
    wire_description=(
        "List applied arbiter proposals awaiting manual completion — "
        "the Recommendations page's In Process tab (Investigation, "
        "WorkflowInstruction, AddSignalCollection kinds). Filter by "
        "bot_id, urgency, dimension, or proposal_id. Use for 'what's "
        "in process?' / 'mark it complete' asks, and as verify_via "
        "after action.proposal.mark_complete (count=0 confirms it "
        "left in_process)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Restrict to proposals targeting this bot.",
            },
            "urgency": {
                "type": "string",
                "description": (
                    "Restrict to proposals at this urgency. Canonical "
                    "values: 'security_critical', 'operational_urgent', "
                    "'cost_alert', 'substrate_warn', 'improvement', "
                    "'hygiene', 'whimsy'."
                ),
            },
            "dimension": {
                "type": "string",
                "description": (
                    "Restrict to proposals scoped to one Better Engine "
                    "dimension (e.g. 'cost', 'security', 'reliability')."
                ),
            },
            "proposal_id": {
                "type": "string",
                "description": (
                    "Restrict to one specific proposal id. Used by "
                    "verify_via after action.proposal.mark_complete "
                    "(count=0 confirms the proposal left in_process)."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Cap on results. Default 50.",
            },
        },
        "additionalProperties": False,
    },
    handler=_in_process_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "proposals", "in_process"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(IN_PROCESS_TOOL)

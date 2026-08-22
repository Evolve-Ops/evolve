"""Action tool — apply a proposal end-to-end.

The linchpin of the resolver pattern (spec §13). Synchronous,
end-to-end runner of the existing arbiter applier infrastructure.
When the operator confirms a fix in chat, evo calls this and the
proposal applies for real — same code path as if they clicked
**Take this on** in the Recommendations UI, just driven from chat.

What happens:

  1. Find the proposal in any subdir (pending / approved_*).
  2. If status is pending, promote pending → approved_auto first
     (file moves from ``proposals/pending/`` to
     ``proposals/approved_auto/``).
  3. Call ``arbiter.apply.apply(proposal, actor="evo")`` — the
     canonical apply chain:
       - snapshot capture (if proposal has a claim)
       - applier dispatch via ``get_applier(action.kind)``
       - transition approved_auto → applied (in-memory)
       - for claim-less + non-deferred kinds: applied → succeeded
       - generator track_record bump
  4. Move the file from approved_auto/ to its new subdir based on
     final status (applied / succeeded / failed_flagged).
  5. Return structured ``{ok, action_kind, to_state, applied_changes,
     verify_via, ...}``.

The tool is **write_risky**: the action writes real config and may
restart gateways. Under the operator's authority tier:

  - ``ask`` → proxy stages as a confirmation button.
  - ``auto-small`` → still asks (write_risky requires auto).
  - ``auto`` → auto-runs UNLESS the proposal's action kind is in
    ``_FORCE_ASK_ACTION_KINDS`` below.

**Tier override for policy-weighted classes (spec §13.4 Q2).**
Some action kinds carry judgment that warrants operator review
even under ``auto`` authority. ``validate()`` returns
``requires_confirmation: True`` for those — the proxy + AGENTS.md
rule combine to force-ask regardless of authority tier.

Currently force-ask kinds:

  - SoulEdit                      — rewrites evo's identity file
  - ThrottleGenerator             — throttles RSI generators
  - PauseGenerator                — pauses an RSI generator entirely
  - UpdatePermissionBaseline      — changes the pod's permission
                                    baseline (security posture)
  - UpdateContentScanCatalog      — changes the content-scan rules
  - UpdateAutonomyPosture (promotions only — direction-aware, see
    ``_force_ask_for_action``) — widening a bot's autonomy is
    permanently human-confirmed; demotions follow plain tier semantics

This list lives ALONGSIDE the proposal schema, not in it. Adding a
class here doesn't need a schema migration; removing one means
deferring to plain authority-tier semantics. Update sparingly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Proposal action kinds that should always stage a confirmation
# button regardless of the operator's authority tier. See module
# docstring for rationale.
_FORCE_ASK_ACTION_KINDS = frozenset({
    "SoulEdit",
    "ThrottleGenerator",
    "PauseGenerator",
    "UpdatePermissionBaseline",
    "UpdateContentScanCatalog",
})


def _force_ask_for_action(proposal) -> bool:
    """The spec §13.4 Q2 tier override, including the direction-aware
    autonomy carve-out.

    ``UpdateAutonomyPosture`` PROMOTIONS always stage a confirmation —
    they are permanently excluded from every auto-approve lane
    (spec-autonomy-ladder-2026-06-10.md §3.2); demotions follow plain
    authority-tier semantics (narrowing is always safe to apply).
    Direction comes from the action's CAS witness and fails closed to
    promotion.
    """
    action_kind = _action_kind_of(proposal)
    if action_kind in _FORCE_ASK_ACTION_KINDS:
        return True
    if action_kind == "UpdateAutonomyPosture":
        try:
            from autonomy.catalog import action_is_promotion
        except ImportError:
            return True  # can't prove it narrows ⇒ treat as widening
        return action_is_promotion(
            getattr(proposal.action, "expected_current_rung", None),
            getattr(proposal.action, "rung", None),
        )
    return False


def _import_arbiter():
    """Lazy import the arbiter modules so this file loads cleanly in
    contexts where the analyzer package isn't importable (eg the
    brittleness test harness). Returns (apply_module, store_module,
    sm_module) or None when unavailable."""
    try:
        from arbiter import apply as arbiter_apply
        from arbiter import store as arbiter_store
        from arbiter import state_machine as arbiter_sm
        return arbiter_apply, arbiter_store, arbiter_sm
    except ImportError as exc:
        log.warning("action.proposal.apply: arbiter unavailable: %s", exc)
        return None


def _action_kind_of(proposal) -> str:
    """Extract the action kind name from a Proposal. Used by both
    validate and handler — keep behavior identical."""
    return getattr(proposal.action, "kind", type(proposal.action).__name__)


def _find_proposal(shared_dir: Path, proposal_id: str):
    """Look up a proposal across all subdirs. Returns (proposal, subdir)
    or (None, None) when missing/unavailable."""
    imported = _import_arbiter()
    if imported is None:
        return None, None
    _, arbiter_store, _ = imported
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return None, None
    proposal, _path, subdir = located
    return proposal, subdir


# ─── apply handler ───────────────────────────────────────────────────────────


def _apply_handler(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a proposal synchronously end-to-end.

    Phase E.3.4 path: try the admin daemon's existing
    ``POST /api/arbiter/proposals/<id>/act`` endpoint first. Falls back
    to the in-process arbiter apply chain on socket failure.

    The endpoint runs the same approve→apply→transition→persist sequence
    this tool used to do in-process — admin UI's Act button uses the
    same route. Routing through it keeps L2 appliers (which use
    /tmp+sudo+cp+kickstart on cross-bot openclaw.json — see memory
    ``project_l1_l2_applier_architecture``) running admin-daemon-side
    where the sudo grants live; after Phase E.2.b they can't run from
    evo's unprivileged user anyway.
    """
    if not proposal_id:
        return {"ok": False, "error": "proposal_id is required"}

    # Primary path: admin daemon /api/arbiter/proposals/<id>/act.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", f"/api/arbiter/proposals/{proposal_id}/act",
        # The endpoint doesn't read a reason field today, but pass it
        # anyway — future-proof + matches the tool's contract.
        body={"reason": reason or "evo applying via tool"},
        # Apply chains can take longer than the default 10s.
        timeout=30.0,
    )
    if used_daemon and status in (200, 201):
        # Normalize the act-endpoint shape to the tool's contract.
        # The endpoint returns {ok, new_status, message, …}; the tool
        # historically returned a similar shape plus action_kind.
        out = dict(body) if isinstance(body, dict) else {}
        out.setdefault("proposal_id", proposal_id)
        out.setdefault("via", "admin_daemon")
        return out
    if used_daemon and status == 404:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}
    if used_daemon and status == 409:
        return {
            "ok": False,
            "error": (
                "proposal is not in 'pending' status — admin daemon "
                "refused the act (see daemon_body for detail)"
            ),
            "daemon_body": body,
        }
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_body": body,
        }

    # Fallback: in-process arbiter chain (migration window only).
    imported = _import_arbiter()
    if imported is None:
        return {"ok": False, "error": "arbiter modules unavailable in this runtime"}
    arbiter_apply, arbiter_store, arbiter_sm = imported

    proposal, current_subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}

    # Only pending / approved_* proposals can be applied. Anything
    # else (already applied, snoozed, rejected, etc.) is a no-op or
    # an illegal transition — surface the state, don't try to recover.
    if proposal.status not in ("pending", "approved_auto", "approved_human"):
        return {
            "ok": False,
            "error": (
                f"proposal is in status '{proposal.status}'; apply only "
                "works for 'pending' or 'approved_*' proposals"
            ),
            "current_status": proposal.status,
        }

    action_kind = _action_kind_of(proposal)

    # Pre-flight: confirm an applier is registered for this kind. The
    # apply chain raises KeyError otherwise; we want a structured
    # error here, not an unstructured raise.
    try:
        from arbiter.appliers import get_applier
        get_applier(action_kind)
    except (ImportError, KeyError) as exc:
        return {
            "ok": False,
            "error": (
                f"no applier registered for action kind '{action_kind}': {exc}. "
                "Action kinds added to the schema must register an applier "
                "before they can be applied."
            ),
            "action_kind": action_kind,
        }

    # Step 1: promote pending → approved_auto in memory. Note that
    # both pending and approved_auto map to the same on-disk subdir
    # (``proposals/pending/`` per arbiter.store._STATUS_TO_SUBDIR), so
    # no file move is needed here — only the in-memory status flip.
    # The proposal's new state will land on disk when the final
    # move_proposal at step 3 runs.
    promoted_from = None
    if proposal.status == "pending":
        try:
            arbiter_sm.transition(
                proposal, "approved_auto",
                actor="evo",
                reason=(reason or "evo applying via tool"),
            )
            promoted_from = "pending"
        except arbiter_sm.IllegalTransitionError as exc:
            return {"ok": False, "error": f"illegal transition pending→approved_auto: {exc}"}

    # Step 2: run the apply chain. arbiter.apply.apply handles snapshot
    # capture, applier dispatch, and the in-memory transitions
    # (approved_auto→applied, and applied→succeeded for claim-less
    # non-deferred kinds). It does NOT move the file — that's our job.
    try:
        outcome = arbiter_apply.apply(
            proposal,
            actor="evo",
            shared_dir=shared_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.proposal.apply: arbiter.apply raised")
        return {
            "ok": False,
            "error": f"apply chain raised: {type(exc).__name__}: {exc}",
            "promoted_from": promoted_from,
        }

    if not outcome.ok:
        return {
            "ok": False,
            "proposal_id": proposal.id,
            "action_kind": action_kind,
            "current_status": outcome.proposal.status,
            "error": outcome.message or "applier reported failure",
            "promoted_from": promoted_from,
        }

    # Step 3: move the file to its destination subdir based on final
    # status. Source subdir is wherever the proposal originally lived
    # (pending/ for pending and approved_*, snoozed/ for snoozed, etc.).
    # move_proposal's to_subdir=None default routes via the status→
    # subdir map — applied→applied/, succeeded→archived/, etc.
    final_status = outcome.proposal.status
    final_subdir = arbiter_store.subdir_for_status(final_status)
    try:
        if final_subdir != current_subdir:
            arbiter_store.move_proposal(
                proposal, shared_dir,
                from_subdir=current_subdir,
                # to_subdir defaults to the current status — exactly
                # what we want post-transition.
            )
        else:
            # Same subdir, in-memory state changed → rewrite in place.
            # Happens when an applied/applied transition or similar
            # doesn't cross a subdir boundary.
            arbiter_store.write_proposal(
                proposal, shared_dir, subdir=current_subdir,
            )
    except OSError as exc:
        # The apply succeeded in memory + on whatever the applier
        # wrote, but the proposal file is stuck in the source subdir
        # with stale status. Surface the gap; operator can re-run or
        # manually move.
        return {
            "ok": False,
            "applied": True,    # the underlying action DID apply
            "proposal_id": proposal.id,
            "action_kind": action_kind,
            "current_status": final_status,
            "error": (
                f"apply succeeded but moving the proposal file from "
                f"{current_subdir}/ → {final_subdir}/ failed: {exc}"
            ),
        }

    return {
        "ok": True,
        "proposal_id": proposal.id,
        "action_kind": action_kind,
        "from_status": promoted_from or "approved_*",
        "to_status": final_status,
        "applier_message": outcome.message or "",
        "applied_changes": (outcome.result.details if outcome.result else {}) or {},
        "summary": proposal.admin_surface_summary or (proposal.problem[:200]),
        # Post-action verify hint (spec §3.7 lever #4 + §13.7).
        # After a successful apply, the proposal is no longer in
        # proposals/pending/ — the model can confirm by asking
        # pod_state.proposals.pending(proposal_id=X) and seeing count=0.
        "verify_via": {
            "tool": "pod_state.proposals.pending",
            "args": {"proposal_id": proposal.id},
            "expect": (
                f"proposal_id no longer in pending; final status="
                f"{final_status}"
            ),
        },
    }


# ─── apply validate ──────────────────────────────────────────────────────────


def _apply_validate(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run gate. Confirms the proposal exists, is in an
    apply-able state, and has a registered applier. Returns
    ``{ok, reason?, context?, requires_confirmation?}``.

    The ``requires_confirmation`` field is the spec §13.4 Q2 tier
    override — true when the proposal's action kind is in
    ``_FORCE_ASK_ACTION_KINDS``. The proxy / AGENTS.md rule combine
    so the model stages this as an offer even under ``auto``
    authority. False or missing → standard authority-tier semantics
    apply.
    """
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}

    proposal, _subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}

    if proposal.status not in ("pending", "approved_auto", "approved_human"):
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}', not "
                "'pending' or 'approved_*'; apply only works for those"
            ),
        }

    action_kind = _action_kind_of(proposal)

    # Confirm an applier is registered. Without one, apply will fail
    # at the dispatch step; better to catch here before the proxy
    # renders a button.
    try:
        from arbiter.appliers import get_applier
        get_applier(action_kind)
    except ImportError as exc:
        return {
            "ok": False,
            "reason": f"arbiter appliers unavailable in this runtime: {exc}",
        }
    except KeyError:
        return {
            "ok": False,
            "reason": (
                f"no applier registered for action kind '{action_kind}'. "
                "This proposal can't be auto-applied; the operator needs "
                "to handle it manually."
            ),
        }

    requires_confirmation = _force_ask_for_action(proposal)

    return {
        "ok": True,
        "context": {
            "proposal_id": proposal.id,
            "action_kind": action_kind,
            "bot_id": proposal.bot_id,
            "summary": proposal.admin_surface_summary or proposal.problem[:200],
            "urgency": proposal.urgency,
            "current_status": proposal.status,
        },
        "requires_confirmation": requires_confirmation,
    }


# ─── Tool registration ───────────────────────────────────────────────────────


APPLY_TOOL = Tool(
    name="action.proposal.apply",
    description=(
        "Synchronously apply a pending proposal end-to-end. Runs the "
        "existing applier chain (write config / install MCP server / "
        "transition state / etc.) and returns the structured result — "
        "same code path as the operator clicking 'Take this on' in the "
        "Recommendations UI, just driven from chat. The linchpin of "
        "the resolver pattern (spec §13): when the operator confirms a "
        "fix in chat, call this and report what happened.\n"
        "\n"
        "Honors authority tier: write_risky tools auto-run under "
        "'auto'; ask the operator under 'ask' or 'auto-small'. "
        "ADDITIONALLY, some action kinds (SoulEdit, ThrottleGenerator, "
        "PauseGenerator, UpdatePermissionBaseline, "
        "UpdateContentScanCatalog) carry validate.requires_confirmation"
        "=True regardless of authority tier — always stage these as "
        "offers, never auto-apply (spec §13.4 Q2)."
    ),
    wire_description=(
        "Synchronously apply a pending proposal end-to-end via the "
        "applier chain — same path as 'Take this on' in the "
        "Recommendations UI. Call when the operator confirms a fix in "
        "chat, then report the result. Some action kinds return "
        "validate.requires_confirmation=True: always stage those as "
        "offers, never auto-apply, regardless of authority tier."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "Proposal id (UUID). Get from "
                    "pod_state.proposals.pending or .snoozed."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line reason recorded in the proposal's "
                    "state-transition history. Visible in the audit log. "
                    "Default: 'evo applying via tool'."
                ),
            },
        },
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
    handler=_apply_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_apply_validate,
    tags=("action", "proposal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(APPLY_TOOL)

"""Action tool — apply a proposal end-to-end.

The linchpin of the resolver pattern (spec §13). Synchronous,
end-to-end runner of the existing arbiter applier infrastructure.
When the operator confirms a fix in chat, evo calls this and the
proposal applies for real — same code path as if they clicked
**Take this on** in the Recommendations UI, just driven from chat.

What happens: the tool POSTs the admin daemon's
``/api/arbiter/proposals/<id>/act`` endpoint over the unix socket —
the same approve → apply-chain → transition → persist sequence the
admin UI's Act button runs, executed daemon-side where the sudo
grants live — and returns the structured result. FAIL CLOSED
(roadmap 7.1 C2, operator decision 2026-08-25): when the daemon is
unreachable the tool refuses with an operator-legible error; there
is no in-process fallback, so the proposal store keeps exactly one
writer.

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
  - InstallApp                    — puts new code (and sometimes new
                                    plugin entries) on a bot
  - AgentsAppend                  — appends free-text standing
                                    instructions to a bot's AGENTS.md
                                    (same file SoulEdit target=agents
                                    writes; eligibility already calls
                                    the kind human-only)
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
    "AgentsAppend",
    "InstallApp",
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
    """Apply a proposal synchronously end-to-end via the admin daemon.

    The ONLY path is the admin daemon's
    ``POST /api/arbiter/proposals/<id>/act`` endpoint — FAIL CLOSED
    (roadmap 7.1 C2, operator decision 2026-08-25): when the daemon is
    unreachable this tool refuses and writes nothing. The former
    in-process fallback was a migration-window bridge; keeping it would
    keep the store's second writer alive and stay inducible by killing
    the socket.

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

    from ..admin_client import daemon_refusal, require_daemon_call
    used_daemon, status, body = require_daemon_call(
        "POST", f"/api/arbiter/proposals/{proposal_id}/act",
        body={
            "actor": "evo",
            "reason": reason or "evo applying via tool",
        },
        # Apply chains can take longer than the default 10s.
        timeout=30.0,
    )
    if not used_daemon:
        return daemon_refusal()
    if status in (200, 201):
        # Normalize the act-endpoint shape to the tool's contract.
        # The endpoint returns {ok, new_status, message, …}; the tool
        # historically returned a similar shape plus action_kind.
        out = dict(body) if isinstance(body, dict) else {}
        out.setdefault("proposal_id", proposal_id)
        out.setdefault("via", "admin_daemon")
        # Post-action verify hint (spec §3.7 lever #4 + §13.7). After a
        # successful apply the proposal is no longer in pending/ — the
        # model can confirm via pod_state.proposals.pending(count=0).
        out.setdefault("verify_via", {
            "tool": "pod_state.proposals.pending",
            "args": {"proposal_id": proposal_id},
            "expect": "proposal_id no longer in pending after apply",
        })
        return out
    if status == 404:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}
    if status == 409:
        return {
            "ok": False,
            "error": (
                "proposal is not in 'pending' status — admin daemon "
                "refused the act (see daemon_body for detail)"
            ),
            "daemon_body": body,
        }
    return {
        "ok": False,
        "error": f"admin daemon returned status {status}",
        "daemon_body": body,
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

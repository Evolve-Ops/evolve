"""Action tools — arbiter proposals.

All writes route through the admin daemon's
/api/arbiter/proposals/<id>/* endpoints over the unix socket, FAIL
CLOSED (roadmap 7.1 C2, operator decision 2026-08-25): when the daemon
is unreachable the tools refuse with an operator-legible error and
write nothing. The proposal store has exactly one writer — the admin
daemon. Local reads (validate/lookup/enrichment) still use the store
API directly.

Phase 1.3 ships one tool here: ``action.proposal.snooze``. The
``accept`` and ``reject`` tools are write_risky / destructive
respectively (apply changes / terminal rejection) and live in
Phase 1.4+.

* ``action.proposal.snooze(proposal_id, duration?, reason?)`` —
  pending → snoozed. Defaults to 1-week snooze (matches the admin
  UI's /api/arbiter/proposals/<id>/snooze default). Recoverable — the
  snooze-wake daemon transitions snoozed back to pending when
  snoozed_until passes, or the operator can manually accept/reject.

Risk tier: write_safe (reversible, bounded, recoverable). Auto-runs
under 'auto-small' and 'auto' authority; renders a confirm button
under 'ask'.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Duration parsing — accepts the same vocabulary as the admin UI's
# /api/arbiter/proposals/<id>/snooze endpoint:
#   <N>h | <N>d | <N>w | <N>m   (hours / days / weeks / months-as-30d)
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hdwm])\s*$", re.IGNORECASE)


def _parse_duration(raw: str | None) -> timedelta | None:
    if not raw:
        return None
    m = _DURATION_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    unit = m.group(2).lower()
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "m":
        return timedelta(days=30 * n)
    return None


def _find_proposal(shared_dir: Path, proposal_id: str):
    """Locate a proposal across all subdirs. Returns the Proposal +
    its current subdir, or (None, None) when not found.

    The proposal store splits files by status subdir, but the admin
    UI's /api/arbiter/proposals/<id>/* endpoints accept just the id
    and let the store search across subdirs. We mirror that for
    convenience — the tool caller (and the model) doesn't have to
    know which subdir a proposal currently lives in.
    """
    try:
        from arbiter import store as arbiter_store
    except ImportError as exc:
        log.warning("action.proposal.snooze: arbiter store unavailable: %s", exc)
        return None, None
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return None, None
    proposal, _path, subdir = located
    return proposal, subdir


def _daemon_proposal_post(
    proposal_id: str,
    endpoint: str,
    body: dict[str, Any],
    *,
    conflict_hint: str,
) -> "tuple[dict[str, Any], dict[str, Any] | None]":
    """POST /api/arbiter/proposals/<id>/<endpoint> on the admin daemon —
    the ONLY write path (roadmap 7.1 C2, fail closed; operator decision
    2026-08-25). Returns ``(daemon_body, None)`` on HTTP 200 or
    ``({}, error_dict)`` for the caller to return as the tool result.
    There is deliberately no in-process arbiter-store fallback: a
    fallback that activates on "daemon unreachable" is a bypass an
    attacker can induce by killing the socket.

    ``conflict_hint`` prefixes the 409 (illegal transition / wrong
    status) error so the model knows which action was refused.
    """
    from ..admin_client import daemon_refusal, require_daemon_call
    reachable, status, resp = require_daemon_call(
        "POST", f"/api/arbiter/proposals/{proposal_id}/{endpoint}", body=body,
    )
    if not reachable:
        return {}, daemon_refusal()
    if status == 404:
        return {}, {"ok": False, "error": f"proposal '{proposal_id}' not found"}
    if status != 200:
        detail = (resp or {}).get("error") if isinstance(resp, dict) else None
        return {}, {
            "ok": False,
            "error": (
                f"{conflict_hint}: {detail}" if detail
                else f"admin daemon returned status {status}"
            ),
        }
    return (resp if isinstance(resp, dict) else {}), None


def _snooze_handler(
    shared_dir: Path,
    proposal_id: str,
    duration: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Snooze a pending proposal via the admin daemon.

    Defaults to a 1-week snooze (matches admin UI default). The daemon's
    /snooze endpoint writes snoozed_until, transitions pending →
    snoozed, and moves the file — this tool holds no store-write path
    (7.1 C2, fail closed). The local read below only enriches the
    response.
    """
    delta = _parse_duration(duration) or timedelta(weeks=1)
    snoozed_until = (datetime.now(timezone.utc) + delta).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")

    # Read-only enrichment (from_status / summary) — the daemon is the
    # authority on existence and legality; a failed read here never
    # blocks the action.
    proposal, current_subdir = _find_proposal(shared_dir, proposal_id)

    body, err = _daemon_proposal_post(proposal_id, "snooze", {
        "until": snoozed_until,
        "actor": "evo",
        "reason": (reason or "evo snoozed via tool"),
    }, conflict_hint="snooze refused")
    if err is not None:
        return err

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "from_status": current_subdir,   # subdir == status for v1
        "to_status": body.get("new_status") or "snoozed",
        "snoozed_until": snoozed_until,
        "summary": (
            (proposal.admin_surface_summary or proposal.problem)
            if proposal is not None else None
        ),
        # Post-action verify hint (spec §3.7 reliability lever #4).
        # The model should call this read tool after the action to
        # confirm the proposal actually landed in the expected state
        # — closes race conditions, partial failures, and operator-
        # reversal blindness. AGENTS.md "Post-action verify" section
        # teaches the broader pattern.
        "verify_via": {
            "tool": "pod_state.proposals.snoozed",
            "args": {"proposal_id": proposal_id},
            "expect": "this proposal_id appears in the snoozed list",
        },
    }


def _snooze_validate(
    shared_dir: Path,
    proposal_id: str,
    duration: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm the proposal exists, is in a state that allows
    snoozing (currently 'pending'), and the duration parses cleanly."""
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}

    if duration is not None and _parse_duration(duration) is None:
        return {
            "ok": False,
            "reason": (
                f"duration {duration!r} is invalid — use formats like "
                "'1d', '1w', '2w', '1m'"
            ),
        }

    proposal, _subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}

    # The state machine allows pending → snoozed; any other status
    # is rejected. Check explicitly here for a clean error message.
    if proposal.status != "pending":
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}', not "
                "'pending'; snooze only applies to pending proposals"
            ),
        }

    return {
        "ok": True,
        "context": {
            "summary": proposal.admin_surface_summary or proposal.problem,
        },
    }


SNOOZE_TOOL = Tool(
    name="action.proposal.snooze",
    description=(
        "Snooze a pending arbiter proposal — defer it until "
        "snoozed_until passes, at which point the snooze-wake daemon "
        "moves it back to pending for re-evaluation. Use when a "
        "proposal is real but not urgent right now (you'll decide "
        "later, or you want to gather more data first). Default "
        "duration 1 week. The operator can still manually accept or "
        "reject during the snooze window."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "Proposal id (UUID) — get from "
                    "pod_state.proposals.pending."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "How long to snooze, as '<N>h', '<N>d', '<N>w', "
                    "or '<N>m' (hours/days/weeks/months-as-30d). "
                    "Default '1w'. Examples: '24h', '7d', '2w', '1m'."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line operator-facing reason, recorded in "
                    "the proposal's history."
                ),
            },
        },
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
    handler=_snooze_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_snooze_validate,
    tags=("action", "proposal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(SNOOZE_TOOL)


# ─── action.proposal.reject ──────────────────────────────────────────────────


def _reject_handler(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Reject a proposal terminally via the admin daemon.

    Routes through /api/arbiter/proposals/<id>/dismiss — the daemon's
    single queue-clearing terminal action (the separate /reject
    endpoint was deliberately retired; ``dismissed`` is the surviving
    human-driven terminal status). The proposal lands in archived/
    with status=dismissed, plus the dismiss side effects evo's old
    direct write skipped: rejection-log entry (generator cooldown) and
    signature suppression. Use when a proposal is genuinely wrong
    (false positive, won't be actionable, operator has handled it
    differently and wants to suppress further surfacing). This tool
    holds no store-write path (7.1 C2, fail closed).
    """
    # Read-only enrichment — never gates the action.
    proposal, current_subdir = _find_proposal(shared_dir, proposal_id)

    body, err = _daemon_proposal_post(proposal_id, "dismiss", {
        "actor": "evo",
        "reason": (reason or "evo rejected via tool"),
        "note": (reason or "").strip(),
    }, conflict_hint="reject refused")
    if err is not None:
        return err

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "from_status": current_subdir,
        "to_status": body.get("new_status") or "dismissed",
        "summary": (
            (proposal.admin_surface_summary or proposal.problem[:200])
            if proposal is not None else None
        ),
        # Post-action verify hint (spec §3.7 lever #4).
        # Reject is terminal — the proposal is in archived/ with
        # status=dismissed. Model can confirm it's NO LONGER pending.
        "verify_via": {
            "tool": "pod_state.proposals.pending",
            "args": {"proposal_id": proposal_id},
            "expect": "proposal_id no longer in pending (terminally rejected)",
        },
    }


def _reject_validate(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm the proposal exists and is in a state that
    allows reject. Per spec §13.4 Q2, reject is destructive — ALWAYS
    require confirmation regardless of authority tier."""
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}

    proposal, _subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}

    # pending/snoozed only: the daemon lands rejects as ``dismissed``,
    # and the state machine has no approved_* → dismissed edge — an
    # approved_* reject would 409 daemon-side, so don't stage a button
    # for it (independent review of the 7.1 C2 migration, finding 1).
    if proposal.status not in ("pending", "snoozed"):
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}'; reject only "
                "applies to pending / snoozed proposals"
            ),
        }

    return {
        "ok": True,
        "context": {
            "proposal_id": proposal.id,
            "summary": proposal.admin_surface_summary or proposal.problem[:200],
            "current_status": proposal.status,
        },
        # Destructive tier always requires confirmation — the tool's
        # risk_tier=DESTRUCTIVE already gates this at the proxy,
        # but flag it explicitly for the model's reasoning too.
        "requires_confirmation": True,
    }


REJECT_TOOL = Tool(
    name="action.proposal.reject",
    description=(
        "Terminally reject a proposal — moves pending/snoozed → "
        "archived/ with status=dismissed (the store's queue-clearing "
        "terminal status; also records the rejection in the generator "
        "cooldown log and suppression store). DESTRUCTIVE: no auto-reopen "
        "via re-observation. Use when a proposal is genuinely wrong (false "
        "positive, won't be actionable, operator handled it differently). "
        "ALWAYS asks regardless of authority tier — destructive tools never "
        "auto-run."
    ),
    wire_description=(
        "Terminally reject a proposal — archives it as dismissed and "
        "records the rejection for generator cooldown + suppression. "
        "DESTRUCTIVE: no auto-reopen. Use when a proposal is genuinely "
        "wrong. ALWAYS asks regardless of authority tier."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "Proposal id (UUID).",
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line reason recorded in the proposal's history. "
                    "Default: 'evo rejected via tool'."
                ),
            },
        },
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
    handler=_reject_handler,
    risk_tier=RiskTier.DESTRUCTIVE,
    validate=_reject_validate,
    tags=("action", "proposal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(REJECT_TOOL)


# ─── action.proposal.mark_complete (Phase 1.5e) ──────────────────────────────


def _mark_complete_handler(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Promote an applied deferred-completion proposal to succeeded.

    Deferred-completion proposals (Investigation, WorkflowInstruction,
    AddSignalCollection) sit in the ``applied`` state after their
    applier runs because the actual work happens outside the system —
    the operator follows up manually, then confirms "yes, done." This
    tool is that confirmation.

    State-machine transition: applied → succeeded (terminal).

    Refuses if the proposal isn't applied or isn't a deferred-completion
    kind. External-completion kinds (BuildApp) auto-promote via their
    own sweep — operator shouldn't mark them complete by hand.

    The kind guards below are read-only policy checks this tool owns
    (the daemon's /complete endpoint only checks status); the write
    itself routes through the daemon (7.1 C2, fail closed).
    """
    try:
        from arbiter.apply import (
            is_manual_completion_kind,
            is_external_completion_kind,
        )
    except ImportError as exc:
        return {"ok": False, "error": f"arbiter unavailable: {exc}"}

    proposal, _current_subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}

    kind = getattr(proposal.action, "kind", type(proposal.action).__name__)
    if is_external_completion_kind(kind):
        return {
            "ok": False,
            "error": (
                f"proposal {proposal_id} is action_kind {kind!r}, which "
                "is completed by an external sweep (not by mark_complete). "
                "Operator should wait for the sweep to auto-promote."
            ),
        }
    if not is_manual_completion_kind(kind):
        return {
            "ok": False,
            "error": (
                f"action_kind {kind!r} is not a manual-completion kind "
                "(the verify daemon auto-promotes claim-bearing kinds "
                "to succeeded once the claim window expires). Manual "
                "mark_complete only applies to "
                "Investigation, WorkflowInstruction, AddSignalCollection."
            ),
        }

    body, err = _daemon_proposal_post(proposal_id, "complete", {
        "actor": "evo",
        "reason": (reason or "evo marked complete via tool"),
    }, conflict_hint="mark_complete refused")
    if err is not None:
        return err

    return {
        "ok": True,
        "proposal_id": proposal.id,
        "from_status": "applied",
        "to_status": body.get("new_status") or "succeeded",
        "action_kind": kind,
        "summary": proposal.admin_surface_summary or proposal.problem,
        # Post-action verify hint (spec §3.7 lever #4). The proposal
        # should no longer appear in pending/snoozed/applied; the
        # history call surfaces archived records too.
        "verify_via": {
            "tool": "pod_state.proposals.pending",
            "args": {},
            "expect": f"proposal_id {proposal_id} no longer in pending",
        },
    }


def _mark_complete_validate(
    shared_dir: Path,
    proposal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: proposal exists, is applied, and is manual-completion kind."""
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}

    try:
        from arbiter.apply import (
            is_manual_completion_kind,
            is_external_completion_kind,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"arbiter unavailable: {exc}"}

    proposal, current_subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}

    if proposal.status != "applied":
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}', not 'applied'; "
                "mark_complete only applies to proposals awaiting manual "
                "confirmation"
            ),
        }

    kind = getattr(proposal.action, "kind", type(proposal.action).__name__)
    if is_external_completion_kind(kind):
        return {
            "ok": False,
            "reason": (
                f"action_kind {kind!r} is owned by an external sweep — "
                "wait for auto-promotion, do not mark complete by hand"
            ),
        }
    if not is_manual_completion_kind(kind):
        return {
            "ok": False,
            "reason": (
                f"action_kind {kind!r} auto-promotes when its claim "
                "verifies (or auto-applies for claim-less typed kinds); "
                "mark_complete is for Investigation / WorkflowInstruction "
                "/ AddSignalCollection only"
            ),
        }

    return {
        "ok": True,
        "context": {
            "action_kind": kind,
            "current_subdir": current_subdir,
            "summary": (proposal.admin_surface_summary or proposal.problem)[:200],
        },
    }


MARK_COMPLETE_TOOL = Tool(
    name="action.proposal.mark_complete",
    description=(
        "Mark a deferred-completion proposal as succeeded. Applies to "
        "proposals in the ``applied`` state whose action_kind requires "
        "manual confirmation: Investigation (operator reviewed the "
        "diagnosis), WorkflowInstruction (operator followed the steps), "
        "AddSignalCollection (engineer shipped the monitor). Transitions "
        "applied → succeeded (terminal) and moves the file to "
        "proposals/archived/. Refuses other action_kinds — typed config "
        "edits auto-promote via their claim, external-completion kinds "
        "(BuildApp) auto-promote via their sweep."
    ),
    wire_description=(
        "Mark a deferred-completion proposal as succeeded (terminal) — "
        "only for 'applied' proposals whose action_kind needs manual "
        "confirmation: Investigation, WorkflowInstruction, "
        "AddSignalCollection. Refuses other kinds: typed config edits "
        "auto-promote via their claim, external kinds (BuildApp) via "
        "their sweep."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "Proposal id (eg 'p-abc12345').",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional note for the audit history — what was done, "
                    "what was found, why the proposal is now complete."
                ),
            },
        },
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
    handler=_mark_complete_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_mark_complete_validate,
    tags=("action", "proposal", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(MARK_COMPLETE_TOOL)

"""evo MCP tools for the proposal-dispatch loop.

Phase 3.3b of the Slice 3 spec
(internal/spec-take-this-on-evo-dispatch-2026-06-04.md). The
``_dispatch_to_target`` mechanism in
``packages/admin/evolve_admin/web/server.py`` writes a JSON envelope to
``{shared_dir}/dispatches/{target}/<proposal_id>.json`` whenever an
operator clicks "Have evo fix this" on a Recommendations proposal.
This module gives **evo** two MCP tools to close the loop:

* ``pod_state.dispatches`` — read all pending envelopes from
  ``{shared_dir}/dispatches/evo/``. Evo calls this to see what's been
  handed off.

* ``action.dispatch.acknowledge`` — report the outcome of a
  dispatched proposal back to the admin store. Evo calls this once
  the fix has been applied (or determined to be impossible). The
  proposal status transitions ``dispatched -> applied`` (or
  ``failed_flagged``) and the envelope is unlinked so the next
  ``pod_state.dispatches`` call doesn't return it.

Bot-target polling (non-evo bots) and forge-target consumption are
separate follow-up phases — the dispatch envelopes still land in
``dispatches/<bot_id>/`` and ``dispatches/forge/`` for those, but
this module only wires evo.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Valid acknowledge outcomes. Matches the server-side endpoint's
# DispatchOutcome Literal (packages/analyzer/schema/proposal.py).
_VALID_OUTCOMES = ("applied", "failed", "in_process")


# ─────────────────────────────────────────────────────────────────────────────
# pod_state.dispatches — read tool
# ─────────────────────────────────────────────────────────────────────────────


def _list_handler(shared_dir: Path) -> dict[str, Any]:
    """Return all pending dispatch envelopes addressed to evo.

    Reads ``{shared_dir}/dispatches/evo/*.json``. Each envelope was
    written by the admin server when the operator dispatched a
    proposal with ``dispatch_target="evo"``. The handler returns:

      {
        "ok": True,
        "count": N,
        "dispatches": [
          {
            "proposal_id": ...,
            "dispatched_at": ...,
            "message": ...,         # verbatim instruction
            "bot_id": ...,          # which bot the finding is about
            "generator_id": ...,    # which generator emitted it
            "problem": ...,         # 1-line summary
            "action_kind": ...,     # Investigation in the v1 path
          },
          ...
        ]
      }

    Sorted by dispatched_at ascending (oldest first) so evo handles
    the queue FIFO. Malformed envelopes are silently skipped — a
    bad envelope shouldn't block evo from processing the rest.
    """
    dispatch_dir = shared_dir / "dispatches" / "evo"
    if not dispatch_dir.exists():
        return {"ok": True, "count": 0, "dispatches": []}

    out = []
    try:
        entries = sorted(dispatch_dir.iterdir())
    except OSError as exc:
        return {"ok": False, "error": f"dispatch dir read failed: {exc}"}

    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        # Skip .dispatch-*.json tempfiles from atomic-write mid-flight.
        if entry.name.startswith("."):
            continue
        try:
            env = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("dispatches.list: skip malformed %s: %s", entry, exc)
            continue
        proposal = env.get("proposal") or {}
        action = proposal.get("action") or {}
        out.append({
            "proposal_id": env.get("proposal_id"),
            "dispatched_at": env.get("dispatched_at"),
            "message": env.get("message"),
            "bot_id": proposal.get("bot_id"),
            "generator_id": proposal.get("generator_id"),
            "problem": (
                proposal.get("admin_surface_summary")
                or proposal.get("problem")
                or ""
            ),
            "action_kind": action.get("kind"),
        })

    out.sort(key=lambda d: d.get("dispatched_at") or "")
    return {"ok": True, "count": len(out), "dispatches": out}


LIST_TOOL = Tool(
    name="pod_state.dispatches",
    description=(
        "Read all pending dispatch envelopes addressed to evo. When the "
        "operator clicks 'Have evo fix this' on a Recommendations "
        "proposal, the admin server writes an envelope to "
        "{shared_dir}/dispatches/evo/. Call this tool at session start "
        "(or when the operator asks 'anything for me to do?') to see "
        "what's queued. Each envelope carries the instruction (message), "
        "the proposal id, and the originating bot + generator. After "
        "you've acted on a dispatch, use action.dispatch.acknowledge to "
        "report the result and remove the envelope from the queue."
    ),
    wire_description=(
        "Read all pending dispatch envelopes addressed to evo (queued "
        "when the operator clicks 'Have evo fix this' on a "
        "Recommendations proposal). Call at session start or when the "
        "operator asks 'anything for me to do?'. Each envelope carries "
        "the instruction, proposal id, and originating bot + "
        "generator. After acting, report via "
        "action.dispatch.acknowledge."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_list_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "dispatches"),
    authorization_scope="admin",
)

register(LIST_TOOL)


# ─────────────────────────────────────────────────────────────────────────────
# action.dispatch.acknowledge — write tool
# ─────────────────────────────────────────────────────────────────────────────


def _envelope_path(shared_dir: Path, proposal_id: str) -> Path:
    return shared_dir / "dispatches" / "evo" / f"{proposal_id}.json"


def _find_proposal(shared_dir: Path, proposal_id: str):
    """Locate a proposal across all subdirs. Same shape as the helper
    in action_proposal.py. Returns (Proposal, subdir) or (None, None)."""
    try:
        from arbiter import store as arbiter_store
    except ImportError:
        return None, None
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return None, None
    proposal, _path, subdir = located
    return proposal, subdir


def _acknowledge_handler(
    shared_dir: Path,
    proposal_id: str,
    outcome: str,
    message: str | None = None,
    applied_changes: list | None = None,
) -> dict[str, Any]:
    """Report the outcome of a dispatched proposal via the admin
    daemon's POST /api/arbiter/proposals/<id>/dispatch/result endpoint
    — FAIL CLOSED (roadmap 7.1 C2): when the daemon is unreachable
    the tool refuses and writes nothing to the proposal store.

    On success (all daemon-side except the envelope unlink):
      - Updates dispatch_state.result with outcome + message +
        applied_changes
      - Transitions status:
          outcome="applied" or "in_process" -> applied
          outcome="failed"                  -> failed_flagged
      - Moves the proposal from pending/ to applied/ or archived/
      - Unlinks the envelope (local — dispatches/evo/ is evo's own
        queue dir, not part of the single-writer proposal store) so
        the next pod_state.dispatches call doesn't re-surface it
    """
    if outcome not in _VALID_OUTCOMES:
        return {
            "ok": False,
            "error": (
                f"outcome must be one of {_VALID_OUTCOMES}; got "
                f"{outcome!r}"
            ),
        }

    from ..admin_client import daemon_refusal, require_daemon_call
    reachable, status, body = require_daemon_call(
        "POST", f"/api/arbiter/proposals/{proposal_id}/dispatch/result",
        body={
            "outcome": outcome,
            "message": message or "",
            "applied_changes": list(applied_changes or []),
        },
    )
    if not reachable:
        return daemon_refusal()
    if status == 404:
        return {"ok": False, "error": f"proposal '{proposal_id}' not found"}
    if status == 409:
        detail = (body or {}).get("error") if isinstance(body, dict) else None
        return {
            "ok": False,
            "error": detail or (
                "acknowledge refused — proposal is not in 'dispatched' status"
            ),
        }
    if status != 200:
        detail = (body or {}).get("error") if isinstance(body, dict) else None
        return {
            "ok": False,
            "error": detail or f"admin daemon returned status {status}",
        }
    resp = body if isinstance(body, dict) else {}

    # Best-effort: unlink the envelope so the next pod_state.dispatches
    # call doesn't re-surface it. Source of truth is the proposal
    # status (now applied / failed_flagged), so a stuck envelope
    # would still be invisible to actions — but cleaning up keeps
    # the dispatches/ dir honest.
    env_path = _envelope_path(shared_dir, proposal_id)
    try:
        os.unlink(env_path)
    except OSError as exc:
        log.info(
            "action.dispatch.acknowledge: envelope unlink skipped "
            "(probably already gone): %s",
            exc,
        )

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "new_status": resp.get("new_status"),
        "outcome": outcome,
        "envelope_unlinked": not env_path.exists(),
        # Post-action verify hint (matches the pattern from
        # action.proposal.snooze / .reject / .accept).
        "verify_via": {
            "tool": "pod_state.dispatches",
            "args": {},
            "expect": (
                "this proposal_id should NO LONGER appear in the "
                "dispatches list"
            ),
        },
    }


def _acknowledge_validate(
    shared_dir: Path,
    proposal_id: str,
    outcome: str,
    message: str | None = None,
    applied_changes: list | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm the proposal exists, is dispatched, and the
    outcome is one of the legal values. Doesn't write anything."""
    if not proposal_id:
        return {"ok": False, "reason": "proposal_id is required"}
    if not isinstance(proposal_id, str) or not re.match(
        r"^[a-zA-Z0-9-]+$", proposal_id,
    ):
        return {
            "ok": False,
            "reason": f"proposal_id {proposal_id!r} doesn't look like a UUID",
        }
    if outcome not in _VALID_OUTCOMES:
        return {
            "ok": False,
            "reason": (
                f"outcome must be one of {_VALID_OUTCOMES}; got {outcome!r}"
            ),
        }
    proposal, _subdir = _find_proposal(shared_dir, proposal_id)
    if proposal is None:
        return {"ok": False, "reason": f"proposal '{proposal_id}' not found"}
    if proposal.status != "dispatched":
        return {
            "ok": False,
            "reason": (
                f"proposal is in status '{proposal.status}', not "
                "'dispatched'; acknowledge only applies to dispatched"
            ),
        }
    return {
        "ok": True,
        "context": {
            "summary": proposal.admin_surface_summary or proposal.problem,
            "target": (
                proposal.dispatch_state.target
                if proposal.dispatch_state
                else None
            ),
        },
    }


ACKNOWLEDGE_TOOL = Tool(
    name="action.dispatch.acknowledge",
    description=(
        "Report the outcome of a dispatched proposal. Call this after "
        "you've acted on a dispatch envelope from pod_state.dispatches. "
        "The proposal transitions out of 'dispatched' state into "
        "'applied' (for outcome='applied' or 'in_process') or "
        "'failed_flagged' (for outcome='failed'), and the envelope is "
        "removed from the queue.\n\n"
        "Use outcome='applied' when you successfully made the fix. "
        "Use outcome='failed' when the fix was attempted but didn't "
        "work — include the error in 'message'. Use outcome='in_process' "
        "when the fix needs human follow-up — the proposal becomes "
        "visible in the In Process tab of Recommendations with your "
        "notes."
    ),
    wire_description=(
        "Report the outcome of a dispatched proposal after acting on "
        "an envelope from pod_state.dispatches; the envelope is then "
        "removed from the queue. outcome='applied' (fix made) or "
        "'in_process' (needs human follow-up) → status 'applied'; "
        "outcome='failed' → 'failed_flagged' — include the error in "
        "`message`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "Proposal id from the envelope (the same UUID you "
                    "got from pod_state.dispatches)."
                ),
            },
            "outcome": {
                "type": "string",
                "enum": list(_VALID_OUTCOMES),
                "description": (
                    "What happened. 'applied' = fix successfully made. "
                    "'failed' = attempted and failed. 'in_process' = "
                    "needs human follow-up."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "1–3 sentence summary of what you did (or why you "
                    "couldn't). The operator sees this in the proposal "
                    "modal."
                ),
            },
            "applied_changes": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Optional structured summary of changes. Each entry "
                    "is a small dict like {'file': 'x.py', 'kind': "
                    "'patch'} or {'cron_id': '...', 'kind': 'added'}. "
                    "Rendered as bullet points in the proposal modal."
                ),
            },
        },
        "required": ["proposal_id", "outcome"],
        "additionalProperties": False,
    },
    handler=_acknowledge_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_acknowledge_validate,
    tags=("action", "dispatch"),
    authorization_scope="admin",
)

register(ACKNOWLEDGE_TOOL)

"""evo.arbiter_bridge — In-process action calls into the L1-L6 arbiter.

Slice 5b8 wired the rec_pending wizard to ``BetterEngine.record_feedback``
for accept / reject / snooze. That records a learning signal but does
NOT mutate the proposal's lifecycle in the arbiter store — chat
approvals were a soft signal, not a real apply. Step 1 of the unify
plan closes that gap.

This module is the in-process equivalent of the ``/api/arbiter/proposals/<id>/{act,dismiss,snooze}``
admin-server endpoints, minus the HTTP wrapping. The wizard handler
imports it; both code paths (HTTP + chat) end up running the same
analyzer-side logic against the same proposal store.

Asymmetric routing rule (the wizard caller enforces this; this module
just exposes the proposal-side primitives):

  * Proposal-derived rec (``source_ref["proposal_id"]`` set) →
    call the bridge AND ``BetterEngine.record_feedback`` (dual-write:
    arbiter for state, BetterEngine for learning weights)

  * Other recs (onboarding / scoreboard / compliance / whimsy) →
    keep using ``BetterEngine.record_feedback`` only — they have no
    proposal_id to act on.

All helpers return a typed result rather than raising, so the wizard
can degrade cleanly when the proposal isn't found / illegal transition
/ etc. without wedging the chat session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class BridgeResult:
    """Outcome of a bridge call.

    ``ok`` is False on any failure (proposal missing, illegal
    transition, applier exception). ``message`` is a short
    operator-readable string for logging and for surfacing in the
    wizard's audit trail.
    """

    ok: bool
    new_status: str | None = None
    message: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Lazy analyzer imports — wrapped so a missing analyzer dep returns a clean
# result instead of crashing the wizard.
# ─────────────────────────────────────────────────────────────────────────────


def _import_arbiter():
    """Import the analyzer-side modules the bridge needs. Returns
    ``(store, sm, apply_mod, track_record_mod)`` or raises ImportError."""
    from arbiter import apply as apply_mod  # type: ignore
    from arbiter import state_machine as sm  # type: ignore
    from arbiter import store  # type: ignore
    from arbiter import track_record as track_record_mod  # type: ignore

    return store, sm, apply_mod, track_record_mod


# ─────────────────────────────────────────────────────────────────────────────
# Accept (= /act endpoint logic)
# ─────────────────────────────────────────────────────────────────────────────


def accept_proposal(
    shared_dir: Path, proposal_id: str, *, actor: str = "user:evo"
) -> BridgeResult:
    """Approve and apply a proposal. Same logic as ``/api/arbiter/proposals/<id>/act``.

    ``pending → approved_human → applier runs → applied (or
    failed_flagged on applier failure)``. For claim-less,
    non-deferred-completion kinds, ``apply.apply()`` further promotes
    to ``succeeded`` in the same call.
    """
    try:
        store, sm, apply_mod, _tr = _import_arbiter()
    except ImportError as exc:
        return BridgeResult(
            ok=False, message=f"arbiter unavailable: {exc}"
        )

    # find → check → transition → pre-write hold the store lock (same
    # shape as the admin-UI act endpoint): without it a concurrent
    # dismisser/sweep moving the file between our find and the pre-write
    # gets resurrected into pending/ by the plain write. apply() runs
    # outside the lock; the final move is CAS-checked.
    with store.locked(shared_dir):
        located = store.find_proposal(shared_dir, proposal_id)
        if located is None:
            return BridgeResult(ok=False, message="proposal not found")
        proposal, _path, subdir = located

        if proposal.status != "pending":
            return BridgeResult(
                ok=False,
                message=(
                    f"cannot accept proposal in status {proposal.status!r}; "
                    "expected 'pending'"
                ),
            )

        try:
            sm.transition(
                proposal,
                "approved_human",
                actor=actor,
                reason="accepted via evo chat",
            )
            store.write_proposal(proposal, shared_dir, subdir=subdir)
        except Exception as exc:  # noqa: BLE001
            return BridgeResult(
                ok=False, message=f"transition to approved_human failed: {exc}"
            )

    outcome = apply_mod.apply(
        proposal, actor=actor, shared_dir=shared_dir
    )
    if not outcome.ok:
        try:
            sm.transition(
                proposal,
                "failed_flagged",
                actor=actor,
                reason=outcome.message or "applier failed",
            )
            from arbiter.track_record import (  # type: ignore
                bump_for_status_transition,
            )

            try:
                bump_for_status_transition(
                    shared_dir, proposal, to_status="failed_flagged"
                )
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "evo.arbiter_bridge: failed to mark proposal %s flagged: %s",
                proposal_id,
                exc,
            )

    try:
        store.move_proposal(proposal, shared_dir, from_subdir=subdir,
                            expected_status="approved_human")
    except OSError as exc:
        return BridgeResult(
            ok=False,
            new_status=proposal.status,
            message=f"transition wrote new status but move failed: {exc}",
        )

    return BridgeResult(
        ok=outcome.ok,
        new_status=proposal.status,
        message=outcome.message or "",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss (= /dismiss endpoint logic, minus the signal-feedback layer)
# ─────────────────────────────────────────────────────────────────────────────


def dismiss_proposal(
    shared_dir: Path,
    proposal_id: str,
    *,
    reason: str = "",
    actor: str = "user:evo",
) -> BridgeResult:
    """Transition the proposal to ``dismissed`` and archive. Mirrors
    ``/api/arbiter/proposals/<id>/dismiss``.

    Records the rejection log entry so the runner-level cooldown filter
    suppresses re-emission. Skips the signal-feedback verdict path
    (that's a UI-specific affordance not modelled in chat).
    """
    try:
        store, sm, _apply, _tr = _import_arbiter()
    except ImportError as exc:
        return BridgeResult(ok=False, message=f"arbiter unavailable: {exc}")

    located = store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return BridgeResult(ok=False, message="proposal not found")
    proposal, _path, subdir = located

    try:
        sm.transition(
            proposal,
            "dismissed",
            actor=actor,
            reason=reason or "dismissed via evo chat",
        )
    except Exception as exc:  # noqa: BLE001
        return BridgeResult(ok=False, message=f"illegal transition: {exc}")

    try:
        store.move_proposal(proposal, shared_dir, from_subdir=subdir)
    except OSError as exc:
        return BridgeResult(
            ok=False,
            new_status=proposal.status,
            message=f"transition wrote new status but move failed: {exc}",
        )

    # Rejection log + track_record bump (best-effort).
    try:
        from arbiter.rejection_log import write_rejection  # type: ignore

        write_rejection(shared_dir, proposal, actor=actor, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evo.arbiter_bridge: rejection log write skipped (%s)", exc
        )

    # Security Warden do-not-reflag accounting. Mirrors the HTTP
    # /dismiss endpoint's _record_warden_dismissal: when a prompt-
    # injection finding is dismissed enough times, the warden auto-
    # suppresses the pattern set so it stops re-firing. Best-effort —
    # this is observability, not lifecycle.
    #
    # Today security_warden proposals don't reach the chat surface
    # (member_bot filter excludes security urgency), so this branch is
    # mostly defensive. If that filter loosens, parity here keeps the
    # auto-suppression behavior consistent across surfaces.
    try:
        _record_warden_dismissal_if_relevant(shared_dir, proposal)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evo.arbiter_bridge: warden dismissal accounting skipped (%s)",
            exc,
        )

    try:
        from arbiter.track_record import bump_for_status_transition  # type: ignore

        bump_for_status_transition(
            shared_dir, proposal, to_status="dismissed"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evo.arbiter_bridge: track_record bump skipped (%s)", exc
        )

    return BridgeResult(
        ok=True, new_status=proposal.status, message="dismissed"
    )


def _record_warden_dismissal_if_relevant(shared_dir: Path, proposal) -> None:
    """If ``proposal`` is a security_warden prompt-injection finding,
    record the dismissal so repeat-firing patterns auto-suppress.
    Mirrors :func:`evolve_admin.web.server._record_warden_dismissal`.

    Returns silently for any non-security_warden proposal or one that's
    missing the expected provenance shape — defensive against future
    proposal shape changes.
    """
    if proposal.generator_id != "security_warden":
        return
    if not any(
        isinstance(t, str) and t.startswith("prompt_injection:")
        for t in (proposal.trigger_observations or [])
    ):
        return
    signals = getattr(proposal.provenance, "signals", {}) or {}
    patterns = signals.get("patterns") or []
    if not patterns:
        return
    try:
        from generators.security_warden import do_not_reflag as dnr  # type: ignore
    except ImportError:
        return
    result = dnr.record_dismissal(shared_dir, proposal.bot_id, patterns)
    if isinstance(result, dict) and result.get("promoted"):
        logger.info(
            "evo.arbiter_bridge: security_warden auto-suppressed pattern_set "
            "for bot=%s after %d dismissals",
            proposal.bot_id,
            result.get("count"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Snooze (= /snooze endpoint logic)
# ─────────────────────────────────────────────────────────────────────────────


def snooze_proposal(
    shared_dir: Path,
    proposal_id: str,
    *,
    days: int | None = None,
    actor: str = "user:evo",
) -> BridgeResult:
    """Transition the proposal to ``snoozed`` with ``snoozed_until``
    set. Default snooze is 7 days; ``days`` overrides.

    The arbiter snooze daemon (separate process) wakes snoozed proposals
    when ``snoozed_until`` passes, transitioning them back to pending.
    """
    try:
        store, sm, _apply, _tr = _import_arbiter()
    except ImportError as exc:
        return BridgeResult(ok=False, message=f"arbiter unavailable: {exc}")

    located = store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return BridgeResult(ok=False, message="proposal not found")
    proposal, _path, subdir = located

    duration_days = days if (days is not None and days > 0) else 7
    until = (
        datetime.now(timezone.utc) + timedelta(days=duration_days)
    ).isoformat(timespec="seconds")

    try:
        sm.transition(
            proposal,
            "snoozed",
            actor=actor,
            reason=f"snoozed via evo chat for {duration_days}d",
        )
        proposal.snoozed_until = until
    except Exception as exc:  # noqa: BLE001
        return BridgeResult(ok=False, message=f"illegal transition: {exc}")

    try:
        store.move_proposal(proposal, shared_dir, from_subdir=subdir)
    except OSError as exc:
        return BridgeResult(
            ok=False,
            new_status=proposal.status,
            message=f"transition wrote new status but move failed: {exc}",
        )

    return BridgeResult(
        ok=True,
        new_status=proposal.status,
        message=f"snoozed until {until}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Mark complete (= /complete endpoint logic)
# ─────────────────────────────────────────────────────────────────────────────


def complete_proposal(
    shared_dir: Path, proposal_id: str, *, actor: str = "user:evo"
) -> BridgeResult:
    """Mark a manual-completion proposal as done. Mirrors
    ``/api/arbiter/proposals/<id>/complete``.

    Used for Investigation and WorkflowInstruction proposals after the
    operator has finished the offline work — transitions ``applied →
    succeeded`` and archives. Refuses any other status (the lifecycle
    rule: only proposals already in In Process can be marked complete).
    """
    try:
        store, sm, _apply, _tr = _import_arbiter()
    except ImportError as exc:
        return BridgeResult(ok=False, message=f"arbiter unavailable: {exc}")

    located = store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return BridgeResult(ok=False, message="proposal not found")
    proposal, _path, subdir = located

    if proposal.status != "applied":
        return BridgeResult(
            ok=False,
            message=(
                f"cannot complete proposal in status {proposal.status!r}; "
                "expected 'applied' (the In Process queue)"
            ),
        )

    try:
        sm.transition(
            proposal,
            "succeeded",
            actor=actor,
            reason="marked complete via evo chat",
        )
    except Exception as exc:  # noqa: BLE001
        return BridgeResult(ok=False, message=f"illegal transition: {exc}")

    try:
        store.move_proposal(proposal, shared_dir, from_subdir=subdir)
    except OSError as exc:
        return BridgeResult(
            ok=False,
            new_status=proposal.status,
            message=f"transition wrote new status but move failed: {exc}",
        )

    # Track-record bump for the success transition.
    try:
        from arbiter.track_record import bump_for_status_transition  # type: ignore

        bump_for_status_transition(
            shared_dir, proposal, to_status="succeeded"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evo.arbiter_bridge: track_record bump skipped (%s)", exc
        )

    return BridgeResult(
        ok=True, new_status=proposal.status, message="marked complete"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Refine (= /refine endpoint logic, with chat-side feedback as input)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RefineBridgeResult:
    """Outcome of a chat refine call.

    On success, ``rec_dict`` carries the refreshed Recommendation
    materialised from the now-updated proposal — the wizard re-pitches
    with this so the user sees the revised content.
    """

    ok: bool
    rec_dict: dict | None = None
    revision_count: int = 0
    message: str = ""


def refine_proposal(
    shared_dir: Path,
    proposal_id: str,
    feedback: str,
    *,
    network: dict,
    actor: str = "user:evo",
) -> RefineBridgeResult:
    """Run a refine cycle on ``proposal_id`` using the user's chat
    message as feedback.

    Wraps ``arbiter.refine.refine_proposal`` + ``apply_refinement`` so
    the wizard handler doesn't have to know about the LLM-call plumbing.
    Uses the proposal's bot's Anthropic key (loaded from the bot's
    ``auth-profiles.json``) so the cost lands on the bot's account —
    same architectural rule as the admin server's /refine endpoint.

    Returns a ``RefineBridgeResult`` with the refreshed Recommendation
    dict on success. The wizard handler updates ``state._pending_rec``
    and re-pitches.

    Failure modes (all return ``ok=False`` with a readable message):
      * Empty feedback string
      * Proposal not found
      * Proposal status not pending/applied (refine only valid in those)
      * Pod-wide proposal (bot_id == "<pod>" sentinel, no account to bill)
      * Bot has no Anthropic API key on file
      * Anthropic SDK not installed
      * LLM call / parse failure
    """
    if not (feedback or "").strip():
        return RefineBridgeResult(ok=False, message="feedback is empty")

    try:
        store, _sm, _apply, _tr = _import_arbiter()
    except ImportError as exc:
        return RefineBridgeResult(
            ok=False, message=f"arbiter unavailable: {exc}"
        )

    located = store.find_proposal(shared_dir, proposal_id)
    if located is None:
        return RefineBridgeResult(ok=False, message="proposal not found")
    proposal, _path, subdir = located

    if proposal.status not in ("pending", "applied"):
        return RefineBridgeResult(
            ok=False,
            message=(
                f"cannot refine proposal in status {proposal.status!r}; "
                "expected 'pending' or 'applied'"
            ),
        )

    if proposal.bot_id == "<pod>":
        return RefineBridgeResult(
            ok=False,
            message=(
                "this proposal is pod-wide. Refine is per-bot only — "
                "there's no bot account to bill the LLM call against."
            ),
        )

    try:
        from arbiter.refine import (  # type: ignore
            apply_refinement,
            make_llm_caller,
            refine_proposal as _arbiter_refine,
        )
    except ImportError as exc:
        return RefineBridgeResult(
            ok=False, message=f"arbiter.refine unavailable: {exc}"
        )

    try:
        llm_call = make_llm_caller(bot_id=proposal.bot_id)
    except (ImportError, RuntimeError) as exc:
        return RefineBridgeResult(
            ok=False,
            message=(
                "no LLM provider credentialed for the pod's primary bot — "
                f"refine needs a provider API key: {exc}"
            ),
        )

    result = _arbiter_refine(proposal, feedback.strip(), llm_call=llm_call)
    if not result.ok:
        return RefineBridgeResult(
            ok=False, message=f"refine failed: {result.error}"
        )

    apply_refinement(
        proposal, result, feedback=feedback.strip(), actor=actor
    )
    try:
        store.write_proposal(proposal, shared_dir, subdir=subdir)
    except OSError as exc:
        return RefineBridgeResult(
            ok=False, message=f"refine succeeded but persist failed: {exc}"
        )

    # Materialize the now-updated proposal as a Recommendation dict so
    # the wizard can re-pitch with the revised content.
    rec_dict: dict | None = None
    try:
        from evolve_admin.better_engine.proposal_reader import (  # type: ignore
            proposal_to_recommendation,
        )

        rec = proposal_to_recommendation(proposal)
        if rec is not None:
            rec_dict = rec.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evo.arbiter_bridge: failed to materialize refreshed rec for "
            "%s: %s",
            proposal_id,
            exc,
        )

    return RefineBridgeResult(
        ok=True,
        rec_dict=rec_dict,
        revision_count=len(proposal.revisions),
        message="refined",
    )


# ─────────────────────────────────────────────────────────────────────────────
# In-process queue listing — for chat-side follow-up pitches
# ─────────────────────────────────────────────────────────────────────────────


def list_in_process_for_bot(shared_dir: Path, bot_id: str) -> list:
    """Return the list of in-process Proposals for ``bot_id``.

    "In process" = ``applied`` status + manual-completion action kind
    (Investigation, WorkflowInstruction). Ordered oldest-first so the
    chat surface naturally picks up the proposal that's been waiting
    longest when offering follow-up. Empty list when nothing is in
    process — that's the signal to fall through to the inbox.

    Best-effort: any read failure returns an empty list rather than
    raising; the wizard degrades to inbox-only behaviour.
    """
    try:
        from arbiter import store  # type: ignore
        from arbiter.apply import is_manual_completion_kind  # type: ignore
    except ImportError:
        return []

    try:
        proposals = list(store.iter_proposals(shared_dir, subdirs=("applied",)))
    except Exception:
        return []

    out = []
    for p in proposals:
        if p.bot_id != bot_id:
            continue
        kind = getattr(
            p.action, "kind", type(p.action).__name__
        )
        if not is_manual_completion_kind(kind):
            continue
        out.append(p)

    # Oldest first — based on the transition into "applied" if available,
    # otherwise the proposal's created_at.
    def _applied_at(proposal) -> str:
        for entry in proposal.history:
            if entry.to_status == "applied":
                return entry.at
        return proposal.created_at or ""

    out.sort(key=_applied_at)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Lineage — past proposals with the same fingerprint
# ─────────────────────────────────────────────────────────────────────────────


def proposal_lineage_summary(
    shared_dir: Path, proposal_id: str, *, max_entries: int = 5
) -> dict | None:
    """Return a chat-friendly lineage summary for ``proposal_id``.

    Walks the archived proposal store for same-fingerprint history and
    returns a compact summary the pitch prompt can weave into its
    framing. Returns ``None`` when there's no history to mention (no
    proposal, no past entries, or the lineage index can't be built).

    Shape::

        {
            "total": 3,
            "by_status": {"dismissed": 2, "succeeded": 1},
            "latest": {"dismissed": "2026-04-26T...", "succeeded": "2026-03-10T..."},
            "entries": [{"status": ..., "terminal_at": ..., "problem": ...}, ...]
        }

    The wizard pitch builder reads this and renders a one-line cue —
    "you dismissed something similar twice, last time 12 days ago" —
    so the bot acknowledges the history rather than re-pitching blind.
    """
    try:
        from arbiter.lineage import LineageIndex  # type: ignore
        from arbiter.store import find_proposal  # type: ignore
    except ImportError:
        return None

    try:
        located = find_proposal(shared_dir, proposal_id)
    except Exception:
        return None
    if located is None:
        return None
    proposal = located[0]

    try:
        idx = LineageIndex.build(shared_dir)
    except Exception:
        return None

    entries = idx.lineage_for(
        proposal, max_entries=max_entries, exclude_id=proposal.id
    )
    if not entries:
        return None

    by_status: dict[str, int] = {}
    latest: dict[str, str] = {}
    serialized: list[dict] = []
    for e in entries:
        by_status[e.status] = by_status.get(e.status, 0) + 1
        ts = e.terminal_at or e.created_at or ""
        if ts and (e.status not in latest or ts > latest[e.status]):
            latest[e.status] = ts
        serialized.append(
            {
                "status": e.status,
                "terminal_at": e.terminal_at,
                "problem": e.problem[:200],
            }
        )

    return {
        "total": len(entries),
        "by_status": by_status,
        "latest": latest,
        "entries": serialized,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helper — extract proposal_id from a Recommendation source_ref
# ─────────────────────────────────────────────────────────────────────────────


def proposal_id_for_rec(rec: dict) -> str | None:
    """Return the proposal_id this rec was materialised from, or None
    if it came from a non-proposal adapter (onboarding / scoreboard /
    compliance / whimsy).

    The wizard handler uses this to decide whether to dispatch through
    the bridge or stay on the existing ``BetterEngine.record_feedback``
    path. ``source_ref`` is set by ``proposal_to_recommendation()`` at
    the bridge adapter; non-proposal adapters leave it empty.
    """
    if not isinstance(rec, dict):
        return None
    src = rec.get("source_ref")
    if not isinstance(src, dict):
        return None
    pid = src.get("proposal_id")
    if isinstance(pid, str) and pid:
        return pid
    return None

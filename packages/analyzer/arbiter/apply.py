"""arbiter.apply — Dispatch an approved proposal's Action through its Applier.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §5.4.

This module:
  - takes a Proposal that has been routed (either approved_auto or approved_human)
  - looks up the Applier by ``action.kind``
  - calls ``capture_snapshot``, stores the RevertPlan on the proposal
  - calls ``apply``
  - transitions the proposal through ``applied`` on success, or records a
    failure and leaves the proposal for the verify daemon to pick up

The verify daemon (L2) owns the post-horizon decision (succeeded vs
failed_reverted vs failed_flagged). ``arbiter.apply`` only advances to
the ``applied`` state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from pathlib import Path

from arbiter.appliers import get_applier, ApplyResult
from arbiter.snapshot import capture as capture_snapshot
from arbiter.state_machine import transition
from schema.proposal import Proposal

log = logging.getLogger(__name__)

# Below this resolver confidence we don't trust a reading enough to use it as a
# baseline (same floor the verify daemon applies — see verify.evaluate
# METRIC_CONFIDENCE_FLOOR). A low-confidence read leaves the placeholder so the
# claim resolves as unresolved/retry rather than verifying against noise.
_BASELINE_CONFIDENCE_FLOOR = 0.5


def _fill_apply_time_baseline(proposal: Proposal, *, as_of: datetime) -> bool:
    """Resolve a deferred claim's metric NOW and write it onto the baseline.

    No-op unless the claim opted in via ``baseline_at_apply`` (only Budget Hawk's
    cost.daily_usd claims do today). The claimed metric is trailing-historical
    (e.g. last 7 days of cost), so the change the applier just made does not
    affect it — reading it here yields the correct pre-change baseline, aligned
    with the trailing window the verify daemon will resolve ``window_days`` later.

    Returns True iff a real reading was written. An unresolvable or
    low-confidence metric leaves the generator's placeholder untouched and logs.
    """
    claim = proposal.claim
    if claim is None or not getattr(claim, "baseline_at_apply", False):
        return False
    try:
        from metrics.registry import resolve as resolve_metric

        mv = resolve_metric(claim.metric, proposal.bot_id, as_of)
    except Exception as exc:  # noqa: BLE001 — unknown metric / resolver error
        log.warning(
            "arbiter.apply: baseline fill failed for %s (metric %s): %s",
            proposal.id, claim.metric, exc,
        )
        return False
    if mv.confidence < _BASELINE_CONFIDENCE_FLOOR:
        log.warning(
            "arbiter.apply: baseline metric %s low confidence %.2f for %s; "
            "keeping placeholder %s",
            claim.metric, mv.confidence, proposal.id, claim.baseline,
        )
        return False
    claim.baseline = mv.value
    return True


# Action kinds whose proposals stay in ``applied`` awaiting an explicit
# "Mark complete" from the operator, rather than auto-promoting to
# ``succeeded``. The applier still runs (Investigation no-ops; the
# WorkflowInstruction applier writes the markdown file the operator will
# follow). The proposal then sits in the In Process queue until the
# operator confirms the manual work is done.
_MANUAL_COMPLETION_KINDS = frozenset(
    {
        "Investigation",
        "WorkflowInstruction",
        # Proposal-synthesizer SignalGapProposals — an engineer writes the
        # monitor; operator marks complete when the code lands.
        "AddSignalCollection",
    }
)

# Action kinds whose proposals stay in ``applied`` awaiting an EXTERNAL
# sweep that owns the final transition. The applier kicks off long-running
# work (forge build, future async pipelines) and returns immediately;
# a separate sweep watches the external system and promotes ``applied``
# to ``succeeded`` or ``failed_flagged`` when the work resolves.
_EXTERNAL_COMPLETION_KINDS = frozenset({"BuildApp", "InstallApp"})

# Kinds exempt from breaker suppression, because they do not write bot
# config: the operator-facing kinds plus BuildApp (which writes a manifest
# into {shared_dir} and hands off to forge).
#
# This is a DIFFERENT question from "who owns applied → succeeded", and the
# two only looked like one question while BuildApp was the sole external-
# completion kind. InstallApp is external-completion too, but it puts files
# and sometimes plugin entries on the bot — exactly what a tripped
# config_change breaker exists to hold off — so it is not exempt. Membership
# here is unchanged for every kind that shipped before InstallApp.
_BREAKER_EXEMPT_KINDS = _MANUAL_COMPLETION_KINDS | {"BuildApp"}


def is_manual_completion_kind(action_kind: str) -> bool:
    """Whether a proposal of this action kind requires explicit operator
    completion after apply (vs. auto-succeeding when there's no claim)."""
    return action_kind in _MANUAL_COMPLETION_KINDS


def is_external_completion_kind(action_kind: str) -> bool:
    """Whether a proposal of this action kind is closed out by an
    external sweep (e.g. forge_sweep watching a forge job) rather than
    by apply.py auto-succeeding or the operator clicking Mark complete."""
    return action_kind in _EXTERNAL_COMPLETION_KINDS


def is_deferred_completion_kind(action_kind: str) -> bool:
    """True for any action kind whose ``applied → succeeded`` transition
    is owned by something other than apply.py — either operator (manual)
    or another sweep (external). Used to decide whether to auto-promote
    claim-less proposals on apply."""
    return is_manual_completion_kind(action_kind) or is_external_completion_kind(
        action_kind
    )


# Action kinds that surface information for a human to read or do, rather than
# an automated, reversible, verifiable change to the bot. These are FYIs /
# observations ("look into it"), not actionable proposals — they carry no claim
# and produce no verifiable bot-state change. The Effectiveness-Layer triage
# (internal/spec-effectiveness-layer-2026-06-09.md §11) routes these out of the
# actionable proposal queue into a calmer "Observations" stream so the queue
# reflects only proposals that ask the operator to approve a real action.
INFORMATIONAL_KINDS = frozenset(
    {
        "Investigation",       # "surfaces the situation to a human; no state change"
        "VetoAnnotation",      # guardian risk info; does not mutate state
        "WorkflowInstruction", # manual workflow doc the operator follows by hand
        "AddSignalCollection", # a dev task (write a monitor), not a bot action
    }
)


def is_informational_kind(action_kind: str) -> bool:
    """True if this action kind is an observation/FYI for a human rather than an
    actionable, automated bot change. Used to keep the actionable proposal queue
    free of 'look into it' items (see INFORMATIONAL_KINDS)."""
    return action_kind in INFORMATIONAL_KINDS


@dataclass
class ApplyOutcome:
    """Returned by ``apply()``."""

    ok: bool
    proposal: Proposal
    result: ApplyResult | None = None
    message: str = ""
    # Set when apply was deliberately deferred — typically because a
    # circuit breaker is tripped on the target bot and applying the
    # config change would fight the trip (spec §5.5 "don't fight the
    # breaker"). Callers should recognize this as "queue and retry on
    # the next cycle" rather than a hard failure. The proposal stays
    # at approved_* status so the next sweep picks it up.
    deferred: bool = False
    deferred_reason: str = ""


def apply(
    proposal: Proposal,
    *,
    actor: str = "arbiter",
    shared_dir: Path | None = None,
) -> ApplyOutcome:
    """Apply an approved proposal.

    Preconditions:
      - ``proposal.status`` is ``"approved_auto"`` or ``"approved_human"``.

    Effects on success:
      - Snapshot is captured and attached as ``proposal.revert_on_failure``
        (if a claim is present — investigation/no-claim proposals skip this
        since there's nothing to revert).
      - Applier's ``apply()`` runs.
      - Proposal transitions to ``applied``.

    Effects on applier failure:
      - Proposal remains in its current state.
      - Caller surfaces the failure (L1 returns ``ApplyOutcome(ok=False)``).

    Effects on snapshot failure:
      - Same as applier failure — we don't apply without a snapshot.
    """
    if proposal.status not in ("approved_auto", "approved_human"):
        return ApplyOutcome(
            ok=False,
            proposal=proposal,
            message=(
                f"apply() called on proposal {proposal.id!r} with status "
                f"{proposal.status!r}; expected approved_auto or approved_human"
            ),
        )

    kind = getattr(proposal.action, "kind", type(proposal.action).__name__)

    # Breaker-suppression check — spec §5.5 "don't fight the breaker".
    # Don't apply config changes against a tripped bot. The kinds in
    # _BREAKER_EXEMPT_KINDS don't write to bot config, so they proceed
    # normally.
    # The proposal stays at approved_* on the deferral, so the next
    # apply sweep picks it up. When the breaker clears, the apply runs
    # naturally.
    if shared_dir is not None and kind not in _BREAKER_EXEMPT_KINDS:
        target_bot = getattr(proposal, "bot_id", None)
        if target_bot:
            try:
                from breakers.suppression import (  # type: ignore[import]
                    find_suppressing_breaker,
                )
                sup_rec = find_suppressing_breaker(
                    shared_dir, target_bot, category="config_change",
                )
            except Exception:  # noqa: BLE001 — fail-open
                sup_rec = None
            if sup_rec is not None:
                return ApplyOutcome(
                    ok=False,
                    proposal=proposal,
                    deferred=True,
                    deferred_reason=(
                        f"breaker {sup_rec.type} tripped on "
                        f"{sup_rec.bot_id} (trip_id {sup_rec.trip_id[:8]}); "
                        f"deferring apply until reactivation"
                    ),
                    message=(
                        f"deferred: {target_bot} has an active "
                        f"{sup_rec.type} breaker — config changes will "
                        f"resume after the breaker is cleared"
                    ),
                )

    # Step 1: capture snapshot (only if we have a claim; otherwise there's
    # nothing meaningful to revert).
    if proposal.claim is not None:
        try:
            revert_plan = capture_snapshot(proposal.action, proposal.bot_id)
        except Exception as e:  # noqa: BLE001 — snapshot errors surface
            return ApplyOutcome(
                ok=False,
                proposal=proposal,
                message=f"snapshot capture failed: {e}",
            )
        proposal.revert_on_failure = revert_plan

    # Step 2: apply
    #
    # Threading proposal.id into applier.apply() is opt-in per applier — we
    # inspect the signature and pass ``proposal_id`` only when the applier
    # declares the kwarg. This avoids touching every concrete applier when
    # only one (Phase 3d's UpdatePermissionConfigApplier) actually needs it
    # to record set_by="rsi:<proposal_id>" in the overrides file.
    applier = get_applier(kind)
    try:
        import inspect
        apply_kwargs: dict = {}
        try:
            sig = inspect.signature(applier.apply)
            if "proposal_id" in sig.parameters:
                apply_kwargs["proposal_id"] = proposal.id
        except (TypeError, ValueError):
            # Signature unobtainable (C-extension, etc.) — fall back to
            # the bare two-arg call; the applier loses the proposal_id hint
            # but doesn't crash.
            pass
        result = applier.apply(proposal.action, proposal.bot_id, **apply_kwargs)
    except Exception as e:  # noqa: BLE001 — apply errors surface
        return ApplyOutcome(
            ok=False,
            proposal=proposal,
            message=f"applier raised: {e}",
        )

    if not result.ok:
        # If the applier explicitly tagged this as a "flag" failure
        # (refused before any side effects, e.g. BuildApp detecting an
        # existing manifest), transition the proposal to failed_flagged
        # so it lands in the operator-review queue. Without this it
        # would sit at approved_*  in pending/ forever.
        details = result.details or {}
        if isinstance(details, dict) and details.get("fail_action") == "flag":
            try:
                transition(
                    proposal,
                    "failed_flagged",
                    actor=actor,
                    reason=result.message or "applier flagged for review",
                )
            except Exception as exc:  # noqa: BLE001 — fall through if illegal
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "arbiter.apply: could not flag proposal %s: %s",
                    proposal.id,
                    exc,
                )
        return ApplyOutcome(
            ok=False,
            proposal=proposal,
            result=result,
            message=f"applier returned not-ok: {result.message}",
        )

    # Persist the applier's result.details under provenance.signals so
    # downstream sweeps (e.g. forge_sweep watching BuildApp proposals)
    # can correlate the proposal back to the work the applier kicked off.
    # Stored under a leading-underscore key to mark it as runtime
    # metadata, distinct from the generator's original signals.
    if result.details:
        proposal.provenance.signals["_apply_details"] = dict(result.details)

    # Step 2.5: fill the apply-time baseline for claims that defer it (Budget
    # Hawk's cost.daily_usd). Done after a successful apply so we only capture a
    # baseline for changes that actually landed; the metric is trailing-
    # historical, so the just-applied change hasn't perturbed it yet.
    _fill_apply_time_baseline(proposal, as_of=datetime.now(timezone.utc))

    # Step 3: transition to applied
    transition(proposal, "applied", actor=actor, reason=result.message or "applied")

    # Step 4: claim-less proposals close out immediately UNLESS their
    # action kind defers completion to someone else. Manual-completion
    # kinds (Investigation, WorkflowInstruction) wait for the operator's
    # Mark complete; external-completion kinds (BuildApp) wait for a
    # sweep that watches the external system. Every other claim-less
    # kind auto-promotes — for those the applier IS the work.
    auto_succeeded = False
    if proposal.claim is None and not is_deferred_completion_kind(kind):
        transition(
            proposal,
            "succeeded",
            actor=actor,
            reason="no claim to verify; closed on apply",
        )
        auto_succeeded = True

    # Bump the generator's TrackRecord for the transitions we just made.
    # Best-effort: a bump failure never blocks the apply itself, but we
    # log at WARNING so silent regressions in bookkeeping show up in
    # operator-tailed logs rather than vanishing.
    #
    # Note on the double-bump: when an auto-succeed happens (claim-less,
    # non-deferred kind), this call site bumps BOTH ``proposals_applied``
    # and ``proposals_verified_success``. The proposal really did transit
    # both states, and the audit history records both transitions. The
    # consequence is that ``proposals_applied`` is the count of all applies
    # *including* those that immediately succeeded — not "applied but not
    # yet verified." Readers of these counters should rely on the
    # invariant ``first_shot + after_iteration == verified_success``
    # rather than ``applied - verified_success == still_applied``.
    if shared_dir is not None:
        try:
            from arbiter.track_record import bump_for_status_transition

            bump_for_status_transition(shared_dir, proposal, to_status="applied")
            if auto_succeeded:
                bump_for_status_transition(
                    shared_dir, proposal, to_status="succeeded"
                )
        except Exception as exc:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "arbiter.apply: track_record bump failed for %s: %s",
                proposal.id,
                exc,
            )

    return ApplyOutcome(ok=True, proposal=proposal, result=result)

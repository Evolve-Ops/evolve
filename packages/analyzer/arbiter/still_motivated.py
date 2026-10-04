"""arbiter.still_motivated — last-mile freshness check for proposals.

Producer-side :func:`arbiter.store.sweep_resolve_proposals` archives
stale proposals at the end of each generator run, and the daily
:func:`arbiter.auto_resolve.sweep_auto_resolve` covers cleared
motivating signals. Between those passes the underlying condition can
clear, and a surface that reads pending proposals straight off disk
will happily resurface something the operator already addressed.

This module is the defense-in-depth: a cheap, read-only check the
report path runs on each proposal before exposing it. Symbolically,

  >>> verdict = is_still_motivated(proposal, shared_dir)
  >>> if verdict is False:
  ...     # condition has demonstrably cleared — skip + archive
  ...     archive_stale(proposal, shared_dir, reason=...)

Three layers, tried in order:

  1. ``motivating_signals[]`` — if every referenced Signal is inactive
     (resolved / dismissed / missing), the proposal's predicates have
     cleared. Same predicate as :func:`arbiter.auto_resolve`, hoisted
     here so the report path doesn't have to wait for the daily sweep.
  2. ``proposal.claim`` re-probe — if the proposal carries a Claim,
     resolve its metric *now*. If the live value already meets the
     claim target (baseline + magnitude in the claimed direction), the
     state the proposal proposes to change is already in the desired
     place. Same logic as :mod:`tools.cleanup_stale_proposals`, online.
  3. Otherwise — return ``None``. The caller treats unknown as "still
     surface": fail-safe is to over-show, not to silently drop.

:func:`archive_stale` performs the
``pending`` → ``resolved_externally`` transition + ``move_proposal``
when the caller decides to act on a False verdict. Per-proposal
failures (illegal transition, write errors) are swallowed and logged
— the report path stays usable even if the on-disk side-effect lags.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from arbiter.state_machine import IllegalTransitionError, transition
from arbiter.store import find_proposal, move_proposal
from schema.proposal import Proposal


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: motivating-signals check
# ─────────────────────────────────────────────────────────────────────────────


_INACTIVE_SIGNAL_STATES = frozenset({"resolved", "dismissed"})


def _signals_all_inactive(
    shared_dir: Path, signal_ids: list[str]
) -> bool:
    """True iff every signal id refers to an inactive (or missing) signal.

    Missing signals are treated as inactive — matches the auto_resolve
    semantics: once retention has pruned a signal's archived/ entry,
    the proposal's motivating link is definitionally stale.
    """
    # Local import: signals.store imports arbiter.store on some code
    # paths, so we keep this module's import graph shallow.
    from signals import store as signals_store

    for sid in signal_ids:
        found = signals_store.find_signal(shared_dir, sid)
        if found is None:
            continue  # treated as inactive (retention pruned)
        sig, _path, _subdir = found
        if sig.state not in _INACTIVE_SIGNAL_STATES:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: claim re-probe
# ─────────────────────────────────────────────────────────────────────────────


def _claim_already_met(
    proposal: Proposal, *, now: datetime
) -> Optional[bool]:
    """Re-resolve the claim metric; return True if the target is already met.

    Returns:
        True   — live metric reports the claim target is already met.
        False  — live metric reports the target is NOT met.
        None   — could not determine (no claim, missing bot_id, metric
                 unregistered, resolver raised).

    Mirrors :mod:`tools.cleanup_stale_proposals`: for ``direction == "up"``
    a value ``>= baseline + magnitude`` clears the proposal; for
    ``direction == "down"`` a value ``<= baseline - magnitude`` clears
    it. Other directions (e.g. ``flat``) fall through to ``None`` —
    the metric alone can't falsify a flatness claim.
    """
    if proposal.claim is None:
        return None
    if not proposal.bot_id:
        return None  # pod-wide claims need a different reverse path

    try:
        # Importing the metrics package side-effect-registers every
        # resolver. Without this, callers that haven't already touched
        # the metrics package see UnknownMetricError on the very first
        # resolve — and we'd fall through to "no opinion" purely for
        # an import-order reason rather than a real freshness signal.
        import metrics  # noqa: F401
        from metrics.registry import UnknownMetricError, resolve as resolve_metric
    except ImportError:  # pragma: no cover — metrics package missing
        return None

    try:
        mv = resolve_metric(proposal.claim.metric, proposal.bot_id, now)
    except UnknownMetricError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.info(
            "still_motivated: metric %s resolve failed for %s: %s",
            proposal.claim.metric, proposal.id, exc,
        )
        return None

    direction = proposal.claim.direction
    if direction == "up":
        target = proposal.claim.baseline + proposal.claim.magnitude
        return mv.value >= target
    if direction == "down":
        target = proposal.claim.baseline - proposal.claim.magnitude
        return mv.value <= target
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def is_still_motivated(
    proposal: Proposal,
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> Optional[bool]:
    """Return whether ``proposal``'s underlying condition is still active.

    Layered evaluation — the first layer that has a confident opinion
    wins. See module docstring for the full rationale.

    Layer ordering matters: ``motivating_signals`` is the strongest
    signal (signals are the producer's own state machine for the
    condition), so it's tried first. The claim layer is a backstop for
    proposals whose generator didn't link signals — it can fire False
    even when the signals layer is silent (no motivating_signals[]).

    Returns ``True`` (still motivated), ``False`` (cleared), or ``None``
    (no opinion). Never raises.
    """
    now = now or datetime.now(timezone.utc)

    # Layer 1: motivating signals.
    if proposal.motivating_signals:
        try:
            if _signals_all_inactive(shared_dir, list(proposal.motivating_signals)):
                return False
            # At least one signal is still firing — proposal is alive.
            return True
        except Exception as exc:  # noqa: BLE001
            log.info(
                "still_motivated: signal check failed for %s: %s",
                proposal.id, exc,
            )

    # Layer 2: claim re-probe.
    claim_check = _claim_already_met(proposal, now=now)
    if claim_check is True:
        return False
    if claim_check is False:
        return True

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Archive helper
# ─────────────────────────────────────────────────────────────────────────────


def archive_stale(
    proposal: Proposal,
    shared_dir: Path,
    *,
    reason: str,
    actor: str = "still_motivated",
) -> bool:
    """Transition ``proposal`` to ``resolved_externally`` + move to archived.

    Surfaces that detect staleness (e.g. the Home briefing's digest
    builder) call this so the on-disk producer-side state catches up
    without waiting for the next cadence. Best-effort: returns False on
    any failure rather than raising, so the surface remains usable.
    """
    located = find_proposal(shared_dir, proposal.id)
    if located is None:
        return False
    found_prop, _path, src_subdir = located
    try:
        transition(found_prop, "resolved_externally", actor=actor, reason=reason)
    except IllegalTransitionError as exc:
        log.info(
            "still_motivated: illegal transition for %s from %s: %s",
            found_prop.id, found_prop.status, exc,
        )
        return False
    try:
        move_proposal(found_prop, shared_dir, from_subdir=src_subdir)
    except OSError as exc:
        log.warning(
            "still_motivated: archive write failed for %s: %s",
            found_prop.id, exc,
        )
        return False
    return True

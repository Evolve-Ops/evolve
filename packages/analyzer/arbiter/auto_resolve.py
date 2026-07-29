"""arbiter.auto_resolve — Archive proposals whose motivating signals have cleared.

When the Alerts/Signal store auto-resolves a Signal (via
``sweep_resolve``), any pending Proposal whose ``motivating_signals[]``
references only resolved/dismissed signals is now stale — the
operator should not have to dismiss it manually. The 2026-05-12
production triage surfaced this gap: ~half the 24 pending proposals
were already addressed in config (heartbeat overrides set, sticky
crons removed) but the proposals stayed pending because no path
translated "signal cleared" into "proposal archived."

Behavior:

  - Iterate ``proposals/pending/`` and ``proposals/snoozed/``.
  - For each proposal with a non-empty ``motivating_signals[]``,
    look up each referenced signal in the Signal store.
  - If ALL referenced signals are inactive (state ∈
    ``resolved`` / ``dismissed``, OR the signal file is missing entirely
    — past retention), transition the proposal to
    ``resolved_externally`` and move it to ``proposals/archived/``.
  - Proposals with empty ``motivating_signals[]`` are skipped — we
    cannot reason about their underlying condition.
  - ``applied/`` proposals are skipped — those are awaiting verify,
    not a signal-clearance archive.

Designed to run once a day under launchd as the ``evolve`` user.
Pure Python, no LLM. Idempotent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from arbiter.state_machine import IllegalTransitionError, transition
from arbiter.store import (
    iter_proposals,
    move_proposal,
)
from schema.proposal import Proposal
from signals import store as signals_store


log = logging.getLogger(__name__)


# Inactive signal states. A signal in either of these states is no longer
# fueling its proposal — its underlying condition has cleared or the
# operator dismissed the alert. Auto-resolve treats both as "cleared."
_INACTIVE_SIGNAL_STATES = frozenset({"resolved", "dismissed"})

ACTOR = "auto_resolver"
REASON = "all motivating signals cleared"

# Tier3 staleness: pending app_audit_tier3 findings that have been sitting
# untouched for this many days get auto-archived. 7d aligns with the
# Sunday-cycle audit cadence — by the time a finding survives a full
# audit pass without the operator engaging, the next cycle is already
# producing fresh findings, and the queue would otherwise be purely
# net-additive (as of 2026-06-09: 119 pending, 1 ever archived).
_TIER3_GENERATOR_ID = "app_audit_tier3"
_TIER3_STALENESS_DAYS = 7
TIER3_STALE_REASON = "tier3_staleness_7d"


@dataclass
class AutoResolveResult:
    """Outcome of one sweep — returned for tests and CLI logging."""

    proposals_scanned: int = 0
    proposals_skipped_no_signals: int = 0
    proposals_skipped_signals_active: int = 0
    proposals_resolved: int = 0
    proposals_resolved_tier3_stale: int = 0
    resolved_ids: list[str] = field(default_factory=list)


def _is_signal_inactive(shared_dir: Path, signal_id: str) -> bool:
    """True if the signal is resolved, dismissed, or missing entirely.

    A missing signal file means retention (90d archive prune) swept it —
    by which point any motivating-signal reference older than 90 days
    is definitionally stale. Treating "not found" as inactive prevents
    very-old proposals from being pinned forever by a signal that no
    longer exists on disk.
    """
    found = signals_store.find_signal(shared_dir, signal_id)
    if found is None:
        return True
    sig, _path, _subdir = found
    return sig.state in _INACTIVE_SIGNAL_STATES


def _all_motivating_signals_inactive(
    shared_dir: Path, proposal: Proposal
) -> bool:
    """True iff EVERY id in proposal.motivating_signals is inactive.

    Empty motivating_signals[] returns False — we cannot reason about
    the proposal's underlying condition without a link to verify.
    """
    if not proposal.motivating_signals:
        return False
    return all(
        _is_signal_inactive(shared_dir, sid) for sid in proposal.motivating_signals
    )


def _is_unengaged_tier3_stale(proposal: Proposal, now: datetime) -> bool:
    """True if the proposal is a pending tier3 finding older than the
    staleness window with no operator engagement.

    "No engagement" means the proposal's history contains only the
    initial ``draft → pending`` transition (or is empty, for legacy
    proposals that predate the audit trail). Any further entry —
    snoozed, refined, dispatched, etc. — counts as engagement and
    pins the proposal for the human to triage.

    Limited to ``pending`` proposals only: a snoozed proposal already
    fails the engagement check (its history carries the snooze
    transition), but checking status explicitly keeps the rule's
    intent obvious.
    """
    if proposal.generator_id != _TIER3_GENERATOR_ID:
        return False
    if proposal.status != "pending":
        return False
    history = proposal.history
    if len(history) > 1:
        return False
    if len(history) == 1:
        entry = history[0]
        if not (entry.from_status == "draft" and entry.to_status == "pending"):
            return False
    try:
        created_at = datetime.fromisoformat(proposal.created_at)
    except (TypeError, ValueError):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created_at).total_seconds() / 86400.0
    return age_days >= _TIER3_STALENESS_DAYS


def _archive_resolved_externally(
    proposal: Proposal, shared_dir: Path, *, reason: str
) -> bool:
    """Transition to ``resolved_externally`` and move to archived/.

    Returns True on success, False if either the transition was
    illegal (raced state on disk) or the move raised OSError. Both
    failure modes are logged and the caller continues the sweep.
    """
    from_subdir = "pending" if proposal.status == "pending" else "snoozed"
    try:
        transition(proposal, "resolved_externally", actor=ACTOR, reason=reason)
    except IllegalTransitionError as exc:
        log.info(
            "auto_resolve: skipping %s — illegal transition from %s: %s",
            proposal.id, proposal.status, exc,
        )
        return False
    try:
        move_proposal(proposal, shared_dir, from_subdir=from_subdir)
    except OSError as exc:
        log.warning(
            "auto_resolve: move_proposal failed for %s: %s",
            proposal.id, exc,
        )
        return False
    return True


def sweep_auto_resolve(
    shared_dir: Path, *, now: datetime | None = None
) -> AutoResolveResult:
    """One pass over pending+snoozed proposals; auto-resolve cleared ones.

    Two rules archive a proposal:

      1. **All motivating signals cleared** — the underlying condition
         the proposal was reacting to no longer fires.
      2. **Tier3 staleness** — a pending ``app_audit_tier3`` finding
         older than 7 days with no operator engagement (history
         contains only ``draft → pending``). Tier3 lacks the dedup +
         dismiss flows the other high-volume generators have, so
         findings accumulate; the staleness rule retires findings
         the operator demonstrably is not going to act on.

    Never raises — per-proposal errors are logged and the sweep
    continues. Returns a summary the daemon wrapper surfaces via
    stdout for operator visibility.
    """
    now_dt = now if now is not None else datetime.now(timezone.utc)
    result = AutoResolveResult()

    for proposal in iter_proposals(shared_dir, subdirs=("pending", "snoozed")):
        result.proposals_scanned += 1

        # Rule 2 (tier3 staleness) runs first because it applies even
        # when motivating signals are still firing — the operator is
        # not engaging, regardless of whether the condition cleared.
        if _is_unengaged_tier3_stale(proposal, now_dt):
            if _archive_resolved_externally(
                proposal, shared_dir, reason=TIER3_STALE_REASON
            ):
                result.proposals_resolved += 1
                result.proposals_resolved_tier3_stale += 1
                result.resolved_ids.append(proposal.id)
                log.info(
                    "auto_resolve: archived tier3-stale %s (created_at=%s)",
                    proposal.id, proposal.created_at,
                )
            continue

        # Rule 1: signal-clearance archive.
        if not proposal.motivating_signals:
            result.proposals_skipped_no_signals += 1
            continue

        if not _all_motivating_signals_inactive(shared_dir, proposal):
            result.proposals_skipped_signals_active += 1
            continue

        if _archive_resolved_externally(proposal, shared_dir, reason=REASON):
            result.proposals_resolved += 1
            result.resolved_ids.append(proposal.id)
            log.info(
                "auto_resolve: archived %s (gen=%s, motivating_signals=%d)",
                proposal.id,
                proposal.generator_id,
                len(proposal.motivating_signals),
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="arbiter.auto_resolve")
    parser.add_argument("--shared-dir", default="/Users/Shared/evolve")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    result = sweep_auto_resolve(Path(args.shared_dir))
    log.info(
        "auto_resolve sweep: scanned=%d resolved=%d (tier3_stale=%d) "
        "skipped_no_signals=%d skipped_active=%d",
        result.proposals_scanned,
        result.proposals_resolved,
        result.proposals_resolved_tier3_stale,
        result.proposals_skipped_no_signals,
        result.proposals_skipped_signals_active,
    )
    return 0

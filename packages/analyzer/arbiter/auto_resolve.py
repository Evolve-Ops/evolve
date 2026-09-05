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

Two guards keep an operator statement from being overruled by a clock
in a different store (both added 2026-08-31, after PR #3865 made the
cascade reachable by giving Signals a 14-day TTL backstop):

  - **Snoozed proposals are never archived by this sweep.** A snooze is
    an operator statement with its own timer, exactly as
    ``signals.stale_ttl`` already treats a snoozed *Signal*.
    ``arbiter.snooze_wake`` returns the proposal to pending at its wake
    time, so this defers the decision rather than pinning the item.
  - **A TTL-resolved signal does not clear an engaged proposal.**
    ``resolved_externally`` asserts "the condition cleared";
    ``signals.stale_ttl`` only establishes "no producer re-observed
    this for ttl_days", and marks its resolutions
    ``resolution_kind="ttl_backstop"`` to say so. Where the verdict
    leans on such a signal AND the operator has engaged with the
    proposal, the sweep holds it. Unengaged proposals still archive —
    the sweep is narrowed, not disabled.

The invariant both guards encode: **no live item is ever archived out
from under an operator.**

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
    subdir_for_status,
)
from schema.proposal import Proposal
from schema.signal import TTL_BACKSTOP_RESOLUTION
from signals import store as signals_store


log = logging.getLogger(__name__)


# Inactive signal states. A signal in either of these states is no longer
# fueling its proposal — its underlying condition has cleared or the
# operator dismissed the alert. Auto-resolve treats both as "cleared."
_INACTIVE_SIGNAL_STATES = frozenset({"resolved", "dismissed"})

ACTOR = "auto_resolver"
REASON = "all motivating signals cleared"

# ── Pending-proposal TTL (generalized 2026-08-30) ───────────────────────────
# A pending proposal that the operator has not engaged with, and that its
# generator has not re-asserted, for ``ttl`` days gets auto-archived as
# ``resolved_externally``. Opt-in per generator:
#
#   - Charter-backed generators declare ``pending_ttl_days`` in their
#     charter.yaml (Charter.pending_ttl_days) — first user:
#     ``engagement_amplifier`` (2026-08-30 alerts review, root cause 3).
#   - Producers WITHOUT a charter keep their TTL in the code defaults
#     below. ``app_audit_tier3`` is one of these (its proposals come from
#     ``app_audit_tier3.py``, not a ``generators/<id>/`` package) — the
#     original tier3-only staleness rule (2026-06-09) is refactored onto
#     this mechanism with its behavior preserved: 7d aligns with the
#     Sunday-cycle audit cadence — by the time a finding survives a full
#     audit pass without the operator engaging, the next cycle is already
#     producing fresh findings, and the queue would otherwise be purely
#     net-additive (as of 2026-06-09: 119 pending, 1 ever archived).
#
# "No re-assertion" means dedup-refresh entries (the arbiter re-detecting
# the same fingerprint) ADVANCE the staleness clock instead of pinning the
# proposal: a proposal the generator keeps re-asserting stays alive; one
# whose generator went quiet (or stopped running entirely, so the
# ``resolves_when_silent`` sweep never fires) drains after the TTL.
_TIER3_GENERATOR_ID = "app_audit_tier3"
_TIER3_STALENESS_DAYS = 7
TIER3_STALE_REASON = "tier3_staleness_7d"

# TTLs for producers that have no charter.yaml to declare one in.
_DEFAULT_PENDING_TTLS: dict[str, int] = {
    _TIER3_GENERATOR_ID: _TIER3_STALENESS_DAYS,
}

# History entries with this reason prefix are machine re-assertions
# (arbiter.ingest._refresh_existing), not operator engagement.
_REFRESH_REASON_PREFIX = "dedup-refresh"


@dataclass
class AutoResolveResult:
    """Outcome of one sweep — returned for tests and CLI logging."""

    proposals_scanned: int = 0
    proposals_skipped_no_signals: int = 0
    proposals_skipped_signals_active: int = 0
    # Snoozed proposals, which Rule 1 no longer touches at all.
    proposals_skipped_snoozed: int = 0
    # Proposals held back because their "cleared" verdict rested on a
    # TTL-resolved signal AND the operator had engaged with them.
    proposals_skipped_ttl_engaged: int = 0
    proposals_resolved: int = 0
    # All TTL archives (every generator with a pending TTL).
    proposals_resolved_ttl_stale: int = 0
    # Subset of the above from app_audit_tier3 — kept so the original
    # tier3 rule's operator-facing accounting is unchanged.
    proposals_resolved_tier3_stale: int = 0
    resolved_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SignalVerdict:
    """What one signal (or a proposal's whole motivating set) tells us.

    ``inactive`` is the archive precondition Rule 1 has always used.
    ``ttl_resolved`` records whether that verdict leans on at least one
    signal the TTL backstop resolved — i.e. on "nobody re-observed it"
    rather than on "the condition cleared".
    """

    inactive: bool
    ttl_resolved: bool


def _signal_verdict(shared_dir: Path, signal_id: str) -> _SignalVerdict:
    """Inactivity verdict for one motivating signal.

    A missing signal file means retention (90d archive prune) swept it —
    by which point any motivating-signal reference older than 90 days
    is definitionally stale. Treating "not found" as inactive prevents
    very-old proposals from being pinned forever by a signal that no
    longer exists on disk. That branch is deliberately NOT marked
    ttl_resolved: its justification is retention age, not the TTL
    backstop, and the engagement gate below must not change it.
    """
    found = signals_store.find_signal(shared_dir, signal_id)
    if found is None:
        return _SignalVerdict(inactive=True, ttl_resolved=False)
    sig, _path, _subdir = found
    inactive = sig.state in _INACTIVE_SIGNAL_STATES
    return _SignalVerdict(
        inactive=inactive,
        ttl_resolved=(
            inactive and sig.resolution_kind == TTL_BACKSTOP_RESOLUTION
        ),
    )


def _motivating_signals_verdict(
    shared_dir: Path, proposal: Proposal
) -> _SignalVerdict:
    """Aggregate verdict over proposal.motivating_signals.

    ``inactive`` is ALL (unchanged: one still-firing signal keeps the
    proposal alive). ``ttl_resolved`` is ANY — with a mixed set, part of
    the "everything cleared" verdict rests on a claim nobody verified,
    so the whole verdict inherits the weaker footing.

    Empty motivating_signals[] returns inactive=False — we cannot reason
    about the proposal's underlying condition without a link to verify.
    """
    if not proposal.motivating_signals:
        return _SignalVerdict(inactive=False, ttl_resolved=False)
    verdicts = [
        _signal_verdict(shared_dir, sid) for sid in proposal.motivating_signals
    ]
    return _SignalVerdict(
        inactive=all(v.inactive for v in verdicts),
        ttl_resolved=any(v.ttl_resolved for v in verdicts),
    )


def _load_charter_pending_ttls() -> dict[str, int]:
    """Read ``pending_ttl_days`` from every charter in the generators
    code directory.

    Reads the charters directly (no registry record / fingerprint check
    — this sweep only needs the declared TTL, and must keep draining a
    generator's leftovers even when the generator itself fails to
    load). Unreadable or malformed charters are skipped: a missing TTL
    means "no TTL", which fails safe — the proposal just stays pending.
    """
    ttls: dict[str, int] = {}
    generators_dir = Path(__file__).resolve().parent.parent / "generators"
    try:
        entries = sorted(generators_dir.iterdir())
    except OSError:
        return ttls
    for entry in entries:
        if not entry.is_dir():
            continue
        charter_path = entry / "charter.yaml"
        if not charter_path.exists():
            charter_path = entry / "charter.yml"
            if not charter_path.exists():
                continue
        try:
            from registry.charter_loader import load_charter_from_yaml

            charter, _fp = load_charter_from_yaml(charter_path)
        except Exception as exc:  # noqa: BLE001 — per-charter isolation
            log.debug("pending-TTL: skipping charter %s: %s", charter_path, exc)
            continue
        if charter.pending_ttl_days is not None and charter.pending_ttl_days > 0:
            ttls[charter.id] = int(charter.pending_ttl_days)
    return ttls


def resolve_pending_ttls() -> dict[str, int]:
    """Effective per-generator pending TTLs: code defaults (charterless
    producers like app_audit_tier3) overlaid by charter declarations."""
    ttls = dict(_DEFAULT_PENDING_TTLS)
    ttls.update(_load_charter_pending_ttls())
    return ttls


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _scan_history(proposal: Proposal) -> tuple[bool, datetime | None]:
    """Walk a proposal's history once; return (engaged, staleness_ref).

    ``engaged`` is True as soon as the history carries an entry that is
    not machine bookkeeping. Machine bookkeeping is exactly two shapes:
    the initial ``draft → pending`` transition (or nothing at all, for
    legacy proposals predating the audit trail), and any number of
    same-status ``dedup-refresh`` entries appended when the generator
    re-emits the same fingerprint. Anything else — snoozed, refined,
    dispatched — is an operator acting on the proposal.

    ``staleness_ref`` starts at ``created_at`` and is advanced by each
    dedup-refresh, so a proposal the generator keeps re-asserting is
    live rather than stale. It is None when ``created_at`` is
    unparseable, and is only meaningful when ``engaged`` is False.

    Both of the sweep's rules read this one walk: Rule 2 needs the
    reference timestamp, Rule 1's TTL gate needs the engagement bit.
    They shared a definition of "engagement" by coincidence before;
    now they share the code.
    """
    reference = _parse_utc(proposal.created_at)
    for index, entry in enumerate(proposal.history):
        if (
            index == 0
            and entry.from_status == "draft"
            and entry.to_status == "pending"
        ):
            continue
        if entry.from_status == entry.to_status and entry.reason.startswith(
            _REFRESH_REASON_PREFIX
        ):
            # Only ever ADVANCE an existing reference. A None reference
            # means created_at was unparseable, and a refresh timestamp
            # must not rescue it into a usable staleness clock — the
            # pre-2026-08-31 rule refused to archive such a proposal at
            # all, and that fail-safe is deliberate.
            refreshed_at = _parse_utc(entry.at)
            if (
                reference is not None
                and refreshed_at is not None
                and refreshed_at > reference
            ):
                reference = refreshed_at
            continue
        return True, reference
    return False, reference


def _is_operator_engaged(proposal: Proposal) -> bool:
    """True if the operator has demonstrably acted on this proposal.

    A snoozed proposal is engaged by construction (its history carries
    the snooze transition), as is one that was refined or dispatched.
    """
    engaged, _reference = _scan_history(proposal)
    return engaged


def _is_unengaged_pending_stale(
    proposal: Proposal, now: datetime, ttl_days: int
) -> bool:
    """True if the proposal is pending, past its TTL, with no operator
    engagement and no recent re-assertion.

    Limited to ``pending`` proposals only: a snoozed proposal already
    fails the engagement check (its history carries the snooze
    transition), but checking status explicitly keeps the rule's
    intent obvious.
    """
    if proposal.status != "pending":
        return False
    engaged, reference = _scan_history(proposal)
    if engaged or reference is None:
        return False
    age_days = (now - reference).total_seconds() / 86400.0
    return age_days >= ttl_days


def _ttl_reason(generator_id: str, ttl_days: int) -> str:
    """Audit-trail reason for a TTL archive. app_audit_tier3 keeps its
    historical reason string so nothing downstream (or an operator
    grepping the log) sees a rename of the same rule."""
    if generator_id == _TIER3_GENERATOR_ID and ttl_days == _TIER3_STALENESS_DAYS:
        return TIER3_STALE_REASON
    return f"pending_ttl_{ttl_days}d"


def _archive_resolved_externally(
    proposal: Proposal, shared_dir: Path, *, reason: str
) -> bool:
    """Transition to ``resolved_externally`` and move to archived/.

    Returns True on success, False if either the transition was
    illegal (raced state on disk) or the move raised OSError. Both
    failure modes are logged and the caller continues the sweep.
    """
    # Both rules reach here with a pending proposal today (Rule 2 is
    # pending-only, Rule 1 skips snoozed), but derive the source subdir
    # from the status rather than assuming — the store owns that map.
    from_subdir = subdir_for_status(proposal.status)
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
    shared_dir: Path,
    *,
    now: datetime | None = None,
    pending_ttls: dict[str, int] | None = None,
) -> AutoResolveResult:
    """One pass over pending+snoozed proposals; auto-resolve cleared ones.

    Two rules archive a proposal:

      1. **All motivating signals cleared** — the underlying condition
         the proposal was reacting to no longer fires.
         Never fires for a snoozed proposal, and never on the strength
         of a TTL-resolved signal alone when the operator has engaged
         with the proposal (see the module docstring).
      2. **Pending TTL** — a pending proposal from a generator with a
         pending TTL (charter ``pending_ttl_days``, or the code
         defaults for charterless producers like ``app_audit_tier3``)
         past that TTL with no operator engagement and no re-assertion.
         Retires findings the operator demonstrably is not going to
         act on, and drains leftovers from generators that stopped
         re-emitting (fingerprint-scheme changes, disabled generators).

    ``pending_ttls`` overrides the resolved per-generator TTL map —
    tests and callers with a pre-loaded registry pass their own;
    ``None`` loads it from the code defaults + charter declarations.

    Never raises — per-proposal errors are logged and the sweep
    continues. Returns a summary the daemon wrapper surfaces via
    stdout for operator visibility.
    """
    now_dt = now if now is not None else datetime.now(timezone.utc)
    ttls = pending_ttls if pending_ttls is not None else resolve_pending_ttls()
    result = AutoResolveResult()

    for proposal in iter_proposals(shared_dir, subdirs=("pending", "snoozed")):
        result.proposals_scanned += 1

        # A snooze is an operator statement with its own timer, and
        # neither rule may overrule it. signals.stale_ttl already
        # honours exactly this for snoozed *Signals* ("the TTL has no
        # business overriding it"); before 2026-08-31 the proposal side
        # did not, so a snoozed proposal whose motivating signals aged
        # out was archived terminally and snooze_wake could never
        # recover it (resolved_externally has no outgoing edge).
        # Deferring is not pinning: arbiter.snooze_wake returns the
        # proposal to pending at its wake time, where both rules see it
        # again.
        if proposal.status == "snoozed":
            result.proposals_skipped_snoozed += 1
            continue

        # Rule 2 (pending TTL) runs first because it applies even
        # when motivating signals are still firing — the operator is
        # not engaging, regardless of whether the condition cleared.
        ttl_days = ttls.get(proposal.generator_id)
        if ttl_days is not None and _is_unengaged_pending_stale(
            proposal, now_dt, ttl_days
        ):
            if _archive_resolved_externally(
                proposal,
                shared_dir,
                reason=_ttl_reason(proposal.generator_id, ttl_days),
            ):
                result.proposals_resolved += 1
                result.proposals_resolved_ttl_stale += 1
                if proposal.generator_id == _TIER3_GENERATOR_ID:
                    result.proposals_resolved_tier3_stale += 1
                result.resolved_ids.append(proposal.id)
                log.info(
                    "auto_resolve: archived ttl-stale %s (gen=%s, "
                    "ttl=%dd, created_at=%s)",
                    proposal.id, proposal.generator_id, ttl_days,
                    proposal.created_at,
                )
            continue

        # Rule 1: signal-clearance archive.
        if not proposal.motivating_signals:
            result.proposals_skipped_no_signals += 1
            continue

        verdict = _motivating_signals_verdict(shared_dir, proposal)
        if not verdict.inactive:
            result.proposals_skipped_signals_active += 1
            continue

        # The archive asserts "the condition cleared". A TTL-resolved
        # signal does not support that claim — it only says no producer
        # re-observed the condition for ttl_days. Where the operator has
        # engaged with the proposal, that weaker claim is not enough to
        # take the item out of their inbox terminally.
        if verdict.ttl_resolved and _is_operator_engaged(proposal):
            result.proposals_skipped_ttl_engaged += 1
            log.info(
                "auto_resolve: holding %s (gen=%s) — operator-engaged and "
                "its cleared verdict rests on a TTL-resolved signal",
                proposal.id,
                proposal.generator_id,
            )
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
        "auto_resolve sweep: scanned=%d resolved=%d (ttl_stale=%d, "
        "tier3_stale=%d) skipped_no_signals=%d skipped_active=%d "
        "skipped_snoozed=%d skipped_ttl_engaged=%d",
        result.proposals_scanned,
        result.proposals_resolved,
        result.proposals_resolved_ttl_stale,
        result.proposals_resolved_tier3_stale,
        result.proposals_skipped_no_signals,
        result.proposals_skipped_signals_active,
        result.proposals_skipped_snoozed,
        result.proposals_skipped_ttl_engaged,
    )
    return 0

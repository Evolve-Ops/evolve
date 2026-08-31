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
    proposals_resolved: int = 0
    # All TTL archives (every generator with a pending TTL).
    proposals_resolved_ttl_stale: int = 0
    # Subset of the above from app_audit_tier3 — kept so the original
    # tier3 rule's operator-facing accounting is unchanged.
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


def _is_unengaged_pending_stale(
    proposal: Proposal, now: datetime, ttl_days: int
) -> bool:
    """True if the proposal is pending, past its TTL, with no operator
    engagement and no recent re-assertion.

    "No engagement" means the history contains only machine entries:
    the initial ``draft → pending`` transition (or nothing, for legacy
    proposals that predate the audit trail) plus any number of
    same-status ``dedup-refresh`` entries appended when the generator
    re-emits the same fingerprint. Any other entry — snoozed, refined,
    dispatched, etc. — counts as engagement and pins the proposal for
    the human to triage.

    The staleness clock starts at ``created_at`` and is advanced by
    each dedup-refresh: a proposal the generator keeps re-asserting is
    live, not stale. (The original tier3-only rule pinned any proposal
    with more than one history entry, which made a machine refresh
    indistinguishable from operator engagement.)

    Limited to ``pending`` proposals only: a snoozed proposal already
    fails the engagement check (its history carries the snooze
    transition), but checking status explicitly keeps the rule's
    intent obvious.
    """
    if proposal.status != "pending":
        return False
    reference = _parse_utc(proposal.created_at)
    if reference is None:
        return False
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
            refreshed_at = _parse_utc(entry.at)
            if refreshed_at is not None and refreshed_at > reference:
                reference = refreshed_at
            continue
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
    shared_dir: Path,
    *,
    now: datetime | None = None,
    pending_ttls: dict[str, int] | None = None,
) -> AutoResolveResult:
    """One pass over pending+snoozed proposals; auto-resolve cleared ones.

    Two rules archive a proposal:

      1. **All motivating signals cleared** — the underlying condition
         the proposal was reacting to no longer fires.
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
        "auto_resolve sweep: scanned=%d resolved=%d (ttl_stale=%d, "
        "tier3_stale=%d) skipped_no_signals=%d skipped_active=%d",
        result.proposals_scanned,
        result.proposals_resolved,
        result.proposals_resolved_ttl_stale,
        result.proposals_resolved_tier3_stale,
        result.proposals_skipped_no_signals,
        result.proposals_skipped_signals_active,
    )
    return 0

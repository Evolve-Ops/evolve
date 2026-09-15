"""arbiter.track_record — Centralized track_record counter bumps.

Every code path that transitions a proposal to a terminal status calls
:func:`bump_for_status_transition` so the generator's ``TrackRecord`` is
kept up to date. This is the single point of truth for "what does each
status transition cost the generator's authority?".

Without this helper the success/failure counters were never being
incremented at all — ``compute_authority`` saw zeros and returned 1.0
for every generator. Wiring this in fixes the long-standing gap as a
prerequisite to the iteration split (first_shot vs. after_iteration).

Best-effort: a failure to load/persist the registry never breaks the
underlying transition. The proposal lifecycle takes precedence over
bookkeeping; we log and move on.
"""

from __future__ import annotations

import logging
from pathlib import Path

from evolve_util import now_iso_offset as _utc_now_iso

from schema.proposal import Proposal


logger = logging.getLogger(__name__)


# Statuses that count as successful verification — generator earns a win.
_SUCCESS_STATUSES = frozenset({"succeeded"})

# Statuses that count as a verified failure — generator takes a loss.
_FAILURE_STATUSES = frozenset(
    {"failed_reverted", "failed_flagged", "failed_revert_failed"}
)

# Statuses that count as human rejection — used for the rejected_human
# counter (separate from authority's win/loss column).
_REJECTION_STATUSES = frozenset({"rejected", "dismissed"})


def _build_registry(shared_dir: Path):
    """Construct a Registry pointed at the same dirs the runner uses.
    Lazy import keeps this module light for callers that only want the
    constants above."""
    from registry.registry import Registry  # type: ignore

    generators_code_dir = (
        Path(__file__).parent.parent / "generators"
    )
    records_dir = Path(shared_dir) / "generators"
    reg = Registry(
        generators_code_dir=generators_code_dir,
        records_dir=records_dir,
    )
    reg.load_all(strict=False)
    return reg


def build_registry_for_bumps(shared_dir: Path):
    """Public helper for callers that bump many proposals in one cycle.

    Builds a Registry once, returns it. Pass the result to
    :func:`bump_for_status_transition` via the ``registry`` kwarg to
    avoid re-walking the generators_code_dir per bump. The verify daemon
    uses this to amortise registry construction across all proposals
    processed in a cycle.

    Returns ``None`` if the registry can't be loaded (rare — a missing
    ``generators_code_dir`` or unreadable shared_dir). Callers should
    treat ``None`` as "fall back to per-call construction" — i.e. don't
    pass it to ``bump_for_status_transition``.
    """
    try:
        return _build_registry(shared_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "track_record: registry pre-build failed (%s); "
            "callers will build per-bump",
            exc,
        )
        return None


def bump_proposals_emitted(
    shared_dir: Path,
    generator_id: str,
    *,
    registry=None,
    count: int = 1,
) -> None:
    """Increment ``track_record.proposals_emitted`` for one source generator.

    The runner already bumps emitted-count for Proposals an ``observe()``
    returned directly (see ``generator_runner._update_tr``). Phase 6c
    coaches return ``[]`` and instead emit CandidateProposals; their
    contribution becomes visible to authority/scoring only when the
    proposal_synthesizer's promotion path bumps the source generator
    here.

    Best-effort: a missing registry or unknown ``generator_id`` is logged
    and swallowed — promotion succeeds either way.
    """
    def updater(tr) -> None:
        tr.proposals_emitted += count

    try:
        reg = registry if registry is not None else _build_registry(shared_dir)
        reg.update_track_record(generator_id, updater)
    except Exception as exc:  # noqa: BLE001 — never break the write
        logger.warning(
            "track_record emit bump skipped for generator %s: %s",
            generator_id,
            exc,
        )


def bump_for_status_transition(
    shared_dir: Path,
    proposal: Proposal,
    *,
    to_status: str | None = None,
    registry=None,
) -> None:
    """Bump the right TrackRecord counter for one status transition.

    ``to_status`` defaults to the proposal's current status — pass it
    explicitly only when you've not yet committed the transition (e.g.,
    you want to update counters speculatively or in a different order).

    ``registry`` is an optional pre-built Registry instance for callers
    that bump many proposals in a tight loop (the verify daemon's cycle,
    a forge sweep with multiple resolutions). Avoids the cost of
    walking the generators_code_dir + records_dir per call. When None,
    a fresh registry is built — fine for one-off callers like the
    admin server's per-request endpoints.

    The helper inspects ``proposal.revisions`` to decide whether a
    succeeded transition counts as first_shot or after_iteration.
    """
    target = to_status or proposal.status
    iterated = bool(proposal.revisions)
    generator_id = proposal.generator_id

    def updater(tr) -> None:
        if target in _SUCCESS_STATUSES:
            tr.proposals_verified_success += 1
            if iterated:
                tr.proposals_succeeded_after_iteration += 1
            else:
                tr.proposals_succeeded_first_shot += 1
            tr.last_verification_at = _utc_now_iso()
        elif target in _FAILURE_STATUSES:
            tr.proposals_verified_failed += 1
            tr.last_verification_at = _utc_now_iso()
        elif target in _REJECTION_STATUSES:
            tr.proposals_rejected_human += 1
        elif target == "applied":
            tr.proposals_applied += 1
        # superseded / resolved_externally / pending* don't move counters

    try:
        reg = registry if registry is not None else _build_registry(shared_dir)
        reg.update_track_record(generator_id, updater)
    except Exception as exc:  # noqa: BLE001 — never break the transition
        logger.warning(
            "track_record bump skipped for generator %s (status=%s): %s",
            generator_id,
            target,
            exc,
        )

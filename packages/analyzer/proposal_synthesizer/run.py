"""proposal_synthesizer.run — Sweep candidates/pending/ through the gate.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §7, §9 (Phase 1).

Phase 1 behavior: the synthesizer LLM stage is not yet wired up.
This entrypoint reads every candidate in ``candidates/pending/``,
runs the deterministic gate, and routes each candidate to one of
three states:

  - ``pass``              → move to ``candidates/synthesizing/``
                            (terminal for now; nothing reads from
                            this dir in Phase 1, so the candidates
                            accumulate for inspection)
  - ``drop``              → append to ``dropped/<date>.jsonl`` and
                            delete from pending
  - ``watchlist``         → move to ``candidates/watchlist/``
  - ``aggregated_into``   → delete from pending; the aggregate that
                            replaced this candidate is in passed.

Operator-visible side effects: NONE in Phase 1. Proposals continue to
emit through the existing path. The candidate store is purely
observational — its purpose is to validate gate behavior against the
operator's actual decisions on the parallel Proposal stream.

Usage:

    python3 -m proposal_synthesizer.run --shared-dir /Users/Shared/evolve

The script is idempotent — re-running over an empty ``pending/`` is a
no-op, and re-running over the same pending candidates would produce
the same decisions (modulo repetition-index updates from the first
run).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from proposal_synthesizer.gate import (
    GateDecision,
    GateResult,
    load_repetition_index,
    run_gate,
    save_repetition_index,
    update_repetition_index,
)
from proposal_synthesizer.promote import is_promotable, promote_to_proposal
from proposal_synthesizer.store import (
    candidates_root,
    delete_candidate,
    iter_candidates,
    record_drop,
    write_candidate,
)


def run_once(shared_dir: Path, *, now: datetime | None = None) -> GateResult:
    """Sweep pending candidates, run gate, route outputs. Returns the
    gate result for inspection / test assertions.

    Phase 2 disposition of passed candidates:

      - **Promotable candidate** (has draft_action + draft_risk_tag,
        not a substrate aggregate) → written as a Proposal in
        ``proposals/pending/`` and removed from the candidate store.
        The motivating_signals[] on the Proposal preserves the trail.
      - **Substrate aggregate** → moved to ``candidates/synthesizing/``
        to await the LLM synthesizer (Phase 3+). Visible in the UI
        under the "awaiting synthesis" surface.
    """
    now = now or datetime.now(timezone.utc)

    pending = list(iter_candidates(shared_dir, subdirs=("pending",)))
    if not pending:
        return GateResult()

    rep_index = load_repetition_index(shared_dir)
    result = run_gate(pending, repetition_index=rep_index, now=now)

    # Persist passed candidates: promote what we can, park the rest.
    for cand in result.passed:
        if is_promotable(cand):
            # Mechanical promotion → write Proposal, drop candidate.
            promoted = promote_to_proposal(cand, shared_dir)
            if promoted is None:
                # Promotion failed (write error, missing fields the
                # is_promotable check didn't catch). Park in
                # synthesizing/ so the candidate doesn't vanish — the
                # synthesis log records the failure for the operator.
                cand.state = "synthesizing"
                write_candidate(cand, shared_dir)
        else:
            # Substrate aggregate (or otherwise unpromotable) — park
            # for the LLM synthesizer.
            cand.state = "synthesizing"
            write_candidate(cand, shared_dir)

    for cand in result.watchlist:
        cand.state = "watchlist"
        write_candidate(cand, shared_dir)

    # Remove input candidates from pending. For passed cases the
    # candidate is either promoted to a Proposal (file deleted from
    # candidate store) or moved to synthesizing/ (file rewritten
    # under that subdir); for "watchlist" cases the candidate was
    # rewritten under watchlist/; for "drop" the file is just
    # removed; for "aggregated_into" the file is removed (the
    # aggregate lives under its own id).
    for dec in result.decisions:
        if dec.disposition == "drop":
            record_drop(
                shared_dir,
                dec.candidate,
                reason=dec.reason,
                note=dec.note,
            )
        # Always remove from pending regardless of disposition.
        delete_candidate(shared_dir, dec.candidate.id, subdir="pending")

    # Update repetition index with the batch's fingerprints.
    rep_index = update_repetition_index(rep_index, pending, now=now)
    save_repetition_index(shared_dir, rep_index)

    return result


def _format_summary(result: GateResult) -> str:
    counts: dict[str, int] = {}
    for d in result.decisions:
        counts[d.disposition] = counts.get(d.disposition, 0) + 1
    pieces = [f"{k}={v}" for k, v in sorted(counts.items())]
    aggs = len(result.new_aggregates)
    return (
        f"gate: {len(result.decisions)} decisions ({', '.join(pieces) or 'none'}); "
        f"{aggs} new aggregate(s); "
        f"{len(result.passed)} passed → synthesizing/; "
        f"{len(result.watchlist)} → watchlist/"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sweep candidates/pending/ through the substantiveness gate.",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Shared dir root (contains candidates/, signals/, proposals/).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-run summary print.",
    )
    args = parser.parse_args(argv)

    # Ensure the candidates/ root exists; the rest are created
    # lazily on write.
    candidates_root(args.shared_dir).mkdir(parents=True, exist_ok=True)

    result = run_once(args.shared_dir)
    if not args.quiet:
        print(_format_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

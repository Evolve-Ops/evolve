#!/usr/bin/env python3
"""sweep_stale_dispatches.py — time out ghost dispatches.

Scans ``{shared_dir}/proposals/pending/`` for proposals in
``dispatched`` state whose ``dispatch_state.dispatched_at`` is older
than the threshold (default 24h) and transitions them to
``failed_flagged`` with a stale-dispatch reason. Dispatched proposals
live in ``pending/`` per ``arbiter.store._STATUS_TO_SUBDIR``; on
transition they move to ``archived/`` the usual way.

Spec: docs/spec-take-this-on-evo-dispatch-2026-06-04.md §"Safety +
invariants" item 3.

Usage:
    sudo -u evolve python3 tools/sweep_stale_dispatches.py \\
        --shared-dir /Users/Shared/evolve

By default lists what would change without writing. Pass ``--apply``
to actually transition + move.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning a tz-aware UTC datetime.

    Tolerates the ``Z`` suffix and naive timestamps (assumed UTC). Returns
    None if parsing fails — caller treats that as "skip; we can't tell
    how stale it is".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_age(delta: timedelta) -> str:
    """Render a timedelta as e.g. '25h 14m' for the failure reason."""
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def main(argv: list[str] | None = None) -> int:
    from arbiter.state_machine import (
        IllegalTransitionError,
        transition,
    )
    from arbiter.store import (
        find_proposal,
        iter_proposals,
        move_proposal,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-dir", type=Path, required=True)
    parser.add_argument(
        "--threshold-hours",
        type=float,
        default=24.0,
        help="Dispatch age (hours) past which a proposal is considered stale.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write transitions; default is dry-run.",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="ISO timestamp to treat as 'now' (testing).",
    )
    args = parser.parse_args(argv)

    now = _parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"ERROR: --now {args.now!r} is not parseable", file=sys.stderr)
        return 1
    threshold = timedelta(hours=args.threshold_hours)

    # (proposal_id, age, reason)
    plans: list[tuple[str, timedelta, str]] = []

    for proposal in iter_proposals(args.shared_dir, subdirs=("pending",)):
        if proposal.status != "dispatched":
            continue
        if proposal.dispatch_state is None:
            continue
        dispatched_at = _parse_iso(proposal.dispatch_state.dispatched_at)
        if dispatched_at is None:
            print(
                f"  {proposal.id[:8]}: dispatch_state.dispatched_at "
                f"{proposal.dispatch_state.dispatched_at!r} not parseable — skipping"
            )
            continue
        age = now - dispatched_at
        if age < threshold:
            continue
        reason = (
            f"dispatch timed out: target {proposal.dispatch_state.target!r} "
            f"didn't report after {_format_age(age)} (threshold "
            f"{args.threshold_hours}h)"
        )
        plans.append((proposal.id, age, reason))

    if not plans:
        print("No stale dispatches found.")
        return 0

    print(f"Planned timeout of {len(plans)} stale dispatch(es):")
    for pid, age, reason in plans:
        print(f"  {pid} (age {_format_age(age)}) → failed_flagged ({reason})")

    if not args.apply:
        print("\n--apply not set; no changes written. Re-run with --apply.")
        return 0

    failed_count = 0
    swept = 0
    for pid, _age, reason in plans:
        located = find_proposal(args.shared_dir, pid)
        if located is None:
            print(f"  {pid}: vanished, skipping")
            continue
        proposal, _, src_subdir = located
        # Re-check status under the lock — another writer may have
        # resolved it between our scan and our act.
        if proposal.status != "dispatched":
            print(f"  {pid}: status changed to {proposal.status!r} mid-sweep, skipping")
            continue
        try:
            transition(
                proposal,
                "failed_flagged",
                actor="stale_dispatch_sweep",
                reason=reason,
            )
        except IllegalTransitionError as exc:
            print(f"  {pid}: illegal transition: {exc}")
            failed_count += 1
            continue
        try:
            move_proposal(proposal, args.shared_dir, from_subdir=src_subdir)
            swept += 1
        except OSError as exc:
            print(f"  {pid}: archive write/unlink failed: {exc}")
            failed_count += 1

    print(f"\nSwept: {swept}, failures: {failed_count}")
    return 0 if failed_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

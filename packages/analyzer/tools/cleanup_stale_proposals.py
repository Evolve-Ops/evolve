#!/usr/bin/env python3
"""Archive pending arbiter proposals whose underlying issue is no longer
present, by re-running each (generator, bot)'s detector once and using the
same auto-resolve logic as the runner.

This is a one-shot operator tool for the existing stale backlog. Going
forward, ``generator_runner`` runs the same sweep on every cycle for any
generator whose charter declares ``resolves_when_silent: true``.

Usage:
    sudo -u evolve python3 tools/cleanup_stale_proposals.py \\
        --shared-dir /Users/Shared/evolve

Lists planned actions only, by default. Pass --apply to actually archive.

If a pending file cannot be unlinked (sticky bit + non-evolve ownership),
the tool prints the failure and continues — fix the ownership manually
and re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    from arbiter.state_machine import (
        IllegalTransitionError,
        transition,
    )
    from arbiter.store import (
        find_proposal,
        iter_proposals,
        move_proposal,
    )
    from evolve_config import get_members, load_config
    # Importing the metrics package side-effect-registers all resolvers.
    import metrics  # noqa: F401

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-dir", type=Path, required=True)
    parser.add_argument(
        "--network",
        type=Path,
        default=Path("/Users/Shared/evolve/network.json"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config = load_config(str(args.network))
    members = set(get_members(config))
    if not members:
        print("ERROR: no members in network.json — refusing to sweep")
        return 1

    print(f"Pod members ({len(members)}): {sorted(members)}")

    plans: list[tuple[str, str, str]] = []  # (proposal_id, target_status, reason)

    for proposal in iter_proposals(
        args.shared_dir, subdirs=("pending", "snoozed")
    ):
        if proposal.bot_id and proposal.bot_id not in members:
            plans.append(
                (
                    proposal.id,
                    "superseded",
                    f"bot {proposal.bot_id!r} is not in pod membership",
                )
            )
            continue

        # For sensor-style cleanup we re-probe the metric the proposal
        # claimed. If the metric currently reports the desired direction,
        # the issue is gone. Best-effort — proposals without a claim are
        # skipped (we have no programmatic re-check signal).
        if proposal.claim is None:
            continue

        try:
            from metrics.registry import resolve as resolve_metric
            from datetime import datetime, timezone

            mv = resolve_metric(
                proposal.claim.metric,
                proposal.bot_id,
                datetime.now(timezone.utc),
            )
        except Exception as exc:
            print(f"  {proposal.id[:8]}: metric resolve failed: {exc}")
            continue

        # The claim says "we expect metric to move to <baseline+magnitude>
        # in <direction>". For our six known stales the metric is binary
        # (1.0 = healthy, 0.0 = broken) and direction is "up". Compare
        # against the claimed magnitude as the threshold.
        target = proposal.claim.baseline + proposal.claim.magnitude
        if proposal.claim.direction == "up" and mv.value >= target:
            plans.append(
                (
                    proposal.id,
                    "resolved_externally",
                    f"metric {proposal.claim.metric}={mv.value} ≥ target {target}",
                )
            )
        elif proposal.claim.direction == "down" and mv.value <= target:
            plans.append(
                (
                    proposal.id,
                    "resolved_externally",
                    f"metric {proposal.claim.metric}={mv.value} ≤ target {target}",
                )
            )

    if not plans:
        print("Nothing to archive — all pending proposals are still relevant.")
        return 0

    print(f"\nPlanned archive of {len(plans)} proposal(s):")
    for pid, status, reason in plans:
        print(f"  {pid} → {status} ({reason})")

    if not args.apply:
        print("\n--apply not set; no changes written. Re-run with --apply.")
        return 0

    archived = 0
    failed = 0
    for pid, status, reason in plans:
        located = find_proposal(args.shared_dir, pid)
        if located is None:
            print(f"  {pid}: vanished, skipping")
            continue
        proposal, _, src_subdir = located
        try:
            transition(proposal, status, actor="cleanup_tool", reason=reason)
        except IllegalTransitionError as exc:
            print(f"  {pid}: illegal transition: {exc}")
            failed += 1
            continue
        try:
            move_proposal(proposal, args.shared_dir, from_subdir=src_subdir)
            archived += 1
        except OSError as exc:
            print(f"  {pid}: archive write OK but unlink failed: {exc}")
            print(
                f"     → fix manually: sudo rm "
                f"{args.shared_dir}/proposals/{src_subdir}/{pid}.json"
            )
            failed += 1

    print(f"\nArchived: {archived}, failures: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""ci_freshness.py — D-CS7 Verdict core for a "is this scheduled job still
running?" dead-man's-switch, extracted from
``.github/workflows/secret-history-scan.yml``'s ``freshness`` job so the
verdict logic is fixture-tested rather than living only in bash.

THE CASE THIS EXISTS FOR (#4244/#4253, 2026-09-13). The freshness job asks
two independent, server-side, per-event-type questions — "show me completed
`schedule` runs" and "show me completed `workflow_dispatch` runs" — because
a single unfiltered page used to get crowded out by `push` runs on a busy
week, and "not on the page I fetched" silently became "does not exist"
(reported as ``never-run`` while the scan ran perfectly every Tuesday).
Filtering server-side by event fixed *that* pagination shape, but the same
conflation recurs one level down if only ONE of the two per-event queries
fails: treating the failed side as "zero runs" is the identical bug in a
smaller box. ``evaluate_freshness`` is the one place that decision is made,
so it is the one place this class of regression can be pinned with a
fixture (see ``test_paginated_away_subject_is_unknown_not_never_run``).

The workflow still owns: the two ``gh api`` queries (whether each was
reachable, and the runs each returned) and the per-run job-match loop that
narrows "some run happened" down to "a run where the target job actually
completed" (``any_runs_found`` / ``latest_run`` below). Everything after
that point — reachability into a state — is this module.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "analyzer"))
from verdict import Verdict  # noqa: E402


@dataclass(frozen=True)
class LatestRun:
    id: str
    created_at: str  # "%Y-%m-%dT%H:%M:%SZ"
    url: str


def evaluate_freshness(
    *,
    workflow: str,
    job_name: str,
    sched_reachable: bool,
    disp_reachable: bool,
    any_runs_found: bool,
    latest_run: LatestRun | None,
    max_age_days: int,
) -> Verdict:
    """``workflow``/``job_name`` identify the control; the rest is exactly
    what the bash gathers: whether each per-event query succeeded, whether
    ANY completed schedule/dispatch run turned up at all, and the newest
    run (if any) whose target job was confirmed to have completed."""
    subject_base = f"{workflow}:{job_name}"

    unreachable = [name for name, ok in (("schedule", sched_reachable),
                                          ("workflow_dispatch", disp_reachable)) if not ok]
    if len(unreachable) == 2:
        return Verdict.unknown(
            f"could not read {workflow} run history from the Actions API — "
            "an unreachable API is not evidence the job stopped running"
        )
    if unreachable:
        # Partial failure: proceeding as if the unreachable side had zero
        # runs is #4253's bug moved one field over — "not fetched" and
        # "does not exist" must not collapse into the same answer just
        # because the OTHER query happened to succeed.
        return Verdict.unknown(
            f"could not read {workflow} run history for {unreachable[0]} — the "
            "runs that did come back are not sufficient evidence that none "
            "exist on the side that failed"
        )

    if latest_run is None:
        detail = (
            "no completed scheduled or dispatched run was found at all"
            if not any_runs_found else
            f"runs of {workflow} exist, but none of them contains a completed "
            f"'{job_name}' job"
        )
        return Verdict.broken(subject_base, detail)

    created = datetime.strptime(latest_run.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    subject = f"{subject_base}@{latest_run.id}"
    evidence = {"created_at": latest_run.created_at, "url": latest_run.url, "age_days": age_days}

    if age_days > max_age_days:
        return Verdict.broken(
            subject, f"last verdict is {age_days:.1f} days old (limit {max_age_days})",
            evidence=evidence,
        )
    return Verdict.ok(subject, evidence=evidence)


def _main(argv: list[str]) -> int:
    """CLI glue for the workflow step: reads the gathered facts as JSON on
    stdin, prints ``key=value`` lines (GITHUB_OUTPUT shape) on stdout."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--job-name", required=True)
    ap.add_argument("--max-age-days", type=int, required=True)
    args = ap.parse_args(argv)

    payload = json.load(sys.stdin)
    latest = payload.get("latest_run")
    v = evaluate_freshness(
        workflow=args.workflow,
        job_name=args.job_name,
        sched_reachable=payload["sched_reachable"],
        disp_reachable=payload["disp_reachable"],
        any_runs_found=payload["any_runs_found"],
        latest_run=LatestRun(**latest) if latest else None,
        max_age_days=args.max_age_days,
    )
    # never-run / stale / fresh / unknown is the workflow's own pre-existing
    # vocabulary (downstream steps key off these exact strings); both
    # never-run and stale are `broken` in D-CS7 terms, distinguished here by
    # whether a run was found to be stale in the first place.
    if v.is_unknown:
        state = "unknown"
    elif v.is_ok:
        state = "fresh"
    else:
        state = "stale" if latest is not None else "never-run"
    detail = v.reason or v.evidence.get("reason") or ""
    print(f"state={state}")
    print(f"detail={detail}")
    if v.evidence.get("url"):
        print(f"last_url={v.evidence['url']}")
    if v.evidence.get("created_at"):
        print(f"last_at={v.evidence['created_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

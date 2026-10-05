"""``python -m edr.bridge`` — the operator surface for the actuator bridge.

Six subcommands (all operate on an EDR-local store — never a pod's
shared dir):

  list     — every dispatch-eligible Proposal (approved + agent-able +
             proof artifact): id, issue, route, proof. What dispatch
             would pick up right now.
  approve  — the R1 human gate: move one pending agent-able triage
             Proposal to ``approved_human`` through the real arbiter
             lifecycle. Refusals (not found / not approvable /
             human-routed / no proof) print to stderr and exit 1 with
             NOTHING mutated — see ``edr.bridge.lifecycle``.
  brief    — print the rendered dispatch brief. A preview surface: it
             works for any agent-able proposal that is pending OR
             approved (``BRIEFABLE_STATUSES``), so an operator can read
             exactly what a worker session would be told BEFORE
             approving. Printing a brief dispatches nothing.
  dispatch — the P1.3b actuator: worktree off latest origin/main +
             ONE headless worker session + typed result capture (G3
             merge check included) — see ``edr.bridge.dispatch``.
             ``--dry-run`` prints the eligibility verdict, the planned
             branch + worktree path, and the rendered brief; it
             launches NOTHING and mutates NOTHING.
  verify   — the P1.4a independent verifier: runs the DECLARED proof
             command in two clean worktrees (must FAIL on origin/main,
             PASS on the PR branch — red→green, structurally) and
             records the verdict on the Proposal. Status unchanged.
             Exit codes: 0 = ``verified``; 2 = the check ran and
             recorded a non-verified verdict (not-falsifiable / failed
             / error — read the output); 1 = refused, nothing ran.
  close    — the loop-close gate: ``applied → succeeded`` ONLY when the
             verdict is ``verified``, the PR was merged BY A HUMAN
             (``mergedBy`` recorded), and the G3 check was clean — see
             ``edr.verify``.

``--shared-dir`` is required on every subcommand. ``brief`` and
``dispatch`` also take ``--repo`` (defaults from ``gh repo view`` inside
a checkout — the same helper the github_issues adapter uses);
``dispatch`` and ``verify`` take ``--repo-root`` (defaults to the cwd's
git toplevel).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import arbiter.store as arbiter_store

from edr import verify as verify_mod
from edr.adapters.github_issues import _default_repo
from edr.bridge import brief as brief_mod
from edr.bridge import dispatch as dispatch_mod
from edr.bridge import lifecycle

# brief works on pending items too — preview-before-approve is the
# point of the dry-run surface. Applied/archived/snoozed items are out
# of the dispatch lifecycle and get a clear refusal instead.
BRIEFABLE_STATUSES: frozenset[str] = (
    lifecycle.APPROVABLE_STATUSES | lifecycle.ELIGIBLE_STATUSES
)

_SHARED_DIR_HELP = (
    "EDR store root (the dir holding signals/ and proposals/). Required "
    "— use an EDR-local dir, never a pod's shared dir."
)


def _cmd_list(args: argparse.Namespace) -> int:
    eligible = lifecycle.eligible_for_dispatch(args.shared_dir)
    for proposal in eligible:
        verdict = lifecycle.triage_verdict(proposal) or {}
        print(
            f"{proposal.id}  issue=#{verdict.get('issue_number')}  "
            f"route={verdict.get('route', '')}  "
            f"proof={verdict.get('proof_artifact', '')}"
        )
    print(f"bridge: eligible {len(eligible)}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    try:
        proposal = lifecycle.approve(args.shared_dir, args.proposal_id)
    except lifecycle.ApprovalRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"approved: {proposal.id} pending -> {proposal.status}")
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    repo = args.repo or _default_repo()
    if not repo:
        print(
            "error: --repo is required (could not derive a default via "
            "`gh repo view`)",
            file=sys.stderr,
        )
        return 1
    located = arbiter_store.find_proposal(args.shared_dir, args.proposal_id)
    if located is None:
        print(
            f"error: proposal not found: {args.proposal_id!r}",
            file=sys.stderr,
        )
        return 1
    proposal, _path, subdir = located
    if proposal.status not in BRIEFABLE_STATUSES:
        print(
            f"error: {proposal.id} is {proposal.status!r} (subdir "
            f"{subdir}/) — brief previews pending/approved proposals "
            f"only ({sorted(BRIEFABLE_STATUSES)})",
            file=sys.stderr,
        )
        return 1
    try:
        print(brief_mod.brief_for(proposal, repo=repo), end="")
    except brief_mod.BriefUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    repo = args.repo or _default_repo()
    if not repo:
        print(
            "error: --repo is required (could not derive a default via "
            "`gh repo view`)",
            file=sys.stderr,
        )
        return 1
    repo_root = args.repo_root or dispatch_mod._repo_root_default()
    if not repo_root:
        print(
            "error: --repo-root is required (cwd is not inside a git "
            "checkout)",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        try:
            plan = dispatch_mod.dry_run(
                args.shared_dir, args.proposal_id, repo=repo,
                repo_root=repo_root,
            )
        except dispatch_mod.DispatchRefused as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"dry-run: {plan['proposal_id']} ({plan['status']})")
        print(f"eligibility: {plan['eligibility']}")
        print(f"branch:      {plan['branch']}")
        print(f"worktree:    {plan['worktree']}")
        print("--- brief " + "-" * 62)
        print(plan["brief"], end="")
        print("--- end brief (nothing launched, nothing mutated) " + "-" * 22)
        return 0

    try:
        result = dispatch_mod.dispatch(
            args.shared_dir, args.proposal_id, repo=repo, repo_root=repo_root
        )
    except dispatch_mod.DispatchRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    actuator = result["actuator"]
    print(
        f"dispatched: {result['proposal_id']} -> {result['status']}\n"
        f"branch:        {result['branch']} "
        f"(pushed: {actuator['branch_pushed']})\n"
        f"pr_url:        {actuator['pr_url']}\n"
        f"proof_status:  {actuator['proof_status']} "
        f"(report {actuator['report_status']})\n"
        f"g3_merge_check: {actuator['g3_merge_check']} "
        f"— {actuator['g3_detail']}"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    repo_root = args.repo_root or dispatch_mod._repo_root_default()
    if not repo_root:
        print(
            "error: --repo-root is required (cwd is not inside a git "
            "checkout)",
            file=sys.stderr,
        )
        return 1
    try:
        result = verify_mod.verify(
            args.shared_dir, args.proposal_id, repo_root=repo_root
        )
    except verify_mod.VerifyRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    block = result["verify"]
    print(
        f"verify: {result['proposal_id']} -> {result['verdict']}\n"
        f"proof_cmd:   {block['proof_cmd']}\n"
        f"main_exit:   {block['main_exit']} (must be non-zero)\n"
        f"branch_exit: {block['branch_exit']} (must be 0)\n"
        f"detail:      {block['detail']}\n"
        f"status:      {result['status']} (unchanged — close is a "
        "separate, human-gated act)"
    )
    # 0 only for verified; 2 = ran + recorded a non-verified verdict
    # (scriptable without parsing); 1 is reserved for refusals.
    return 0 if result["verdict"] == verify_mod.VERDICT_VERIFIED else 2


def _cmd_close(args: argparse.Namespace) -> int:
    try:
        proposal = verify_mod.close(args.shared_dir, args.proposal_id)
    except verify_mod.CloseRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    evidence = proposal.provenance.signals[verify_mod.CLOSE_SLOT]
    print(
        f"closed: {proposal.id} applied -> {proposal.status}\n"
        f"merged_by: {evidence['merged_by']} at {evidence['merged_at']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m edr.bridge",
        description=(
            "EDR actuator bridge: list dispatch-eligible proposals, "
            "human-approve a dispatch (R1), preview the dispatch brief, "
            "and dispatch one headless worker session (P1.3b)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser(
        "list", help="print every dispatch-eligible proposal"
    )
    p_list.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_list.set_defaults(func=_cmd_list)

    p_approve = subparsers.add_parser(
        "approve",
        help=(
            "human-gated approval (R1): pending -> approved_human via "
            "the real arbiter lifecycle"
        ),
    )
    p_approve.add_argument("proposal_id", help="e.g. edr-triage-gh-123")
    p_approve.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_approve.set_defaults(func=_cmd_approve)

    p_brief = subparsers.add_parser(
        "brief",
        help=(
            "print the rendered dispatch brief (dry-run preview; works "
            "on pending or approved agent-able proposals)"
        ),
    )
    p_brief.add_argument("proposal_id", help="e.g. edr-triage-gh-123")
    p_brief.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_brief.add_argument(
        "--repo",
        default=None,
        help=(
            "owner/repo the brief targets. Defaults from `gh repo view` "
            "when run inside a checkout."
        ),
    )
    p_brief.set_defaults(func=_cmd_brief)

    p_dispatch = subparsers.add_parser(
        "dispatch",
        help=(
            "dispatch ONE approved proposal to a headless worker session "
            "(worktree + brief + typed result capture; --dry-run "
            "launches/mutates nothing)"
        ),
    )
    p_dispatch.add_argument("proposal_id", help="e.g. edr-triage-gh-123")
    p_dispatch.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_dispatch.add_argument(
        "--repo",
        default=None,
        help=(
            "owner/repo the dispatch targets. Defaults from `gh repo "
            "view` when run inside a checkout."
        ),
    )
    p_dispatch.add_argument(
        "--repo-root",
        default=None,
        type=Path,
        help=(
            "git checkout the dispatch worktree hangs off. Defaults to "
            "the cwd's git toplevel (`git rev-parse --show-toplevel`)."
        ),
    )
    p_dispatch.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the eligibility verdict, planned branch + worktree "
            "path, and the rendered brief; launch NOTHING, mutate NOTHING"
        ),
    )
    p_dispatch.set_defaults(func=_cmd_dispatch)

    p_verify = subparsers.add_parser(
        "verify",
        help=(
            "run the declared proof command in clean worktrees: must "
            "FAIL on origin/main and PASS on the PR branch (P1.4a; "
            "records the verdict, status unchanged; exit 0 only on "
            "'verified', 2 on a recorded non-verified verdict)"
        ),
    )
    p_verify.add_argument("proposal_id", help="e.g. edr-triage-gh-123")
    p_verify.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_verify.add_argument(
        "--repo-root",
        default=None,
        type=Path,
        help=(
            "git checkout the clean verify worktrees hang off. Defaults "
            "to the cwd's git toplevel (`git rev-parse --show-toplevel`)."
        ),
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_close = subparsers.add_parser(
        "close",
        help=(
            "close the loop (applied -> succeeded) ONLY when the proof "
            "verdict is 'verified', the PR was merged by a recorded "
            "human, and the G3 check was clean"
        ),
    )
    p_close.add_argument("proposal_id", help="e.g. edr-triage-gh-123")
    p_close.add_argument(
        "--shared-dir", required=True, type=Path, help=_SHARED_DIR_HELP
    )
    p_close.set_defaults(func=_cmd_close)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

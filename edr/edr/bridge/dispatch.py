"""edr.bridge.dispatch — the dispatch runner + result capture (P1.3b).

The actuator half of the bridge (design: internal/edr/design-edr-2026-06-11.md
§5.1 + §5.1.1; P1.3a — ``edr.bridge.lifecycle`` + ``edr.bridge.brief`` —
is the foundation this module completes). One entrypoint::

    dispatch(shared_dir, proposal_id, *, repo, repo_root, ...) -> dict

takes ONE approved, dispatch-eligible Proposal and:

  1. re-checks eligibility through the real ``dispatch_eligibility()``
     (approval may have been revoked / the record mutated since list);
  2. creates an isolated git worktree off latest ``origin/main`` on the
     deterministic ``branch_for(...)`` branch (duplicate-dispatch guard:
     refuses if that branch already exists on origin);
  3. renders the real ``brief_for(...)`` and launches ONE headless
     worker session in the worktree (the §5.1 contract: single bounded
     session, PR-open only — never a swarm, never a merge);
  4. parses the worker's final output for the typed report block
     (``REPORT_FIELDS`` / ``PROOF_STATUS_VALUES`` from P1.3a — the SAME
     constants that were rendered into the brief);
  5. runs the post-hoc G3 merge check (§5.1.1.3) against the reported
     PR — a merged PR is a protocol VIOLATION, flagged loudly;
  6. persists the typed result to
     ``Proposal.provenance.signals["actuator"]`` (§5.1.1.4, mirroring
     the triage slot convention) and moves the Proposal through the real
     arbiter lifecycle. The verify stage (P1.4) owns ``applied``.

This module is a TIER INTERFACE (tiered-oversight §4): every contract
knob is MODULE-LEVEL DATA below — refusal codes, the G3 verdict values,
report statuses, the runner invocation — so P1.4 and tests assert on
constants, never prose.

Seam discipline (``subprocess-patch-fakes-break-on-module-move``)
─────────────────────────────────────────────────────────────────────────
ALL subprocess use is behind three injected callables on ``dispatch()``:

  ``runner(brief, *, cwd) -> str``        — launches the worker session,
                                            returns its captured output.
                                            Default: ``run_headless_claude``.
  ``pr_query(pr_url) -> dict``            — fetches PR state for the G3
                                            check. Default: ``query_pr``
                                            (``gh pr view``).
  ``worktree_ops``                        — git fetch / ls-remote /
                                            worktree-add. Default:
                                            ``GitWorktreeOps()``.

Tests inject fakes at these seams and never launch anything; they never
``patch("module.subprocess.run")``.

The production runner invocation (documented, data below)
─────────────────────────────────────────────────────────────────────────
::

    claude -p <brief> --output-format text \
        --max-turns {RUNNER_MAX_TURNS} --dangerously-skip-permissions

with ``cwd=<the worktree>``, stdout captured, ``RUNNER_TIMEOUT_SECONDS``
wall clock. ``-p`` is non-interactive print mode: the process exits when
the session ends and stdout carries the session's final output — which
the brief contractually ends with the typed report block.
``--dangerously-skip-permissions`` is the honest v1 gap, same shape as
§5.1.1.3: a headless worker must edit files, run the proof, push, and
open a PR without a human at a permission prompt; v1 containment is
contractual (the brief) + verified (the G3 check + P1.4), and a
structurally sandboxed worker (scoped token, allowlisted tools) is the
P2 hardening item. ``--max-turns`` bounds the session (§5.1: one bite,
never an unbounded loop). The runner never raises on worker death or
timeout — it returns whatever output was captured, and the dispatch flow
judges by EVIDENCE (report parse + the pushed-branch check), not exit
codes.

Failure-path policy (the decision this module implements)
─────────────────────────────────────────────────────────────────────────
After the session ends, the bridge re-checks origin for the dispatch
branch (the same ``branch_exists_on_origin`` seam as the duplicate
guard):

  branch pushed     → ``approved_human → applied`` (real lifecycle;
                      actor ``DISPATCH_ACTOR``). Something real exists on
                      origin for the verify stage to check — even when
                      the report was unparseable (the worker may have
                      died after pushing; verify, don't guess). The
                      cross-subdir pending/→applied/ move passes
                      ``expected_status`` so the store's CAS guard
                      engages (a concurrent revoke wins over us).
  branch NOT pushed → the Proposal KEEPS ``approved_human``: nothing
                      real exists to verify, and re-dispatch stays one
                      command away (the duplicate-branch guard passes
                      again precisely because nothing landed on origin).
                      The attempt is still recorded in the actuator
                      block.

The actuator block is OVERWRITE-WITH-LATEST plus an ``attempts``
counter: the Proposal record carries the current dispatch state (what
P1.4 consumes), not an audit log — the durable trail is the pushed
branches, PRs, and the append-only ``Proposal.history``.

The actuator result block — ``Proposal.provenance.signals["actuator"]``
─────────────────────────────────────────────────────────────────────────
::

    {
        "schema": 1,
        "branch": "<the deterministic dispatch branch (authoritative)>",
        "branch_pushed": bool,            # post-session origin evidence
        "pr_url": "<url>" | None,
        "proof_status": "<PROOF_STATUS_VALUES>" | None,
        "report_status": "parsed" | "unparseable",
        "g3_merge_check": "clean" | "VIOLATION" | "unknown",
        "g3_detail": "<why — state / merged-by / query failure>",
        "dispatched_at": "<ISO UTC>",
        "runner": "claude-headless-v1",
        "notes": "<the worker's notes line>" | "",
        "raw_report_tail": "<last RAW_TAIL_CHARS chars of output>",
        "attempts": int,                  # 1-based, survives re-dispatch
        "worktree": "<path the session ran in>",
    }

``branch`` is always the PLANNED deterministic branch — the bridge
dispatched it and checks it; a worker reporting a different branch is
logged at WARNING (the raw tail preserves the worker's exact claim).

Transition actor
─────────────────────────────────────────────────────────────────────────
``DISPATCH_ACTOR = "edr_bridge"``. Per ``arbiter.state_machine
.transition``'s actor vocabulary, ``"user"`` is reserved for human acts
(P1.3a's approve) and system components use a short component identifier
(``"verify_daemon"``, a generator_id). Dispatch is the SYSTEM acting on
a standing human approval, so it records the bridge's own identifier —
the history line must never read as if a human moved it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The analyzer is the installed evolve-analyzer package (flat layout), so
# its subpackages import by their bare names — the same modules the shipped
# admin server and daemons load. Import, never fork.
import arbiter.state_machine as state_machine
import arbiter.store as arbiter_store
from evolve_util import now_iso
from schema.proposal import Proposal

from edr.bridge.brief import (
    PROOF_STATUS_VALUES,
    REPORT_FIELDS,
    BriefUnavailable,
    branch_for,
    brief_for,
)
from edr.bridge.lifecycle import dispatch_eligibility, triage_verdict

log = logging.getLogger(__name__)

# ── DATA: the lifecycle act (see "Transition actor" in the docstring) ────────

DISPATCH_ACTOR = "edr_bridge"
DISPATCH_REASON = "EDR bridge: dispatched headless worker session (P1.3b)"
APPLIED_STATUS = "applied"

# ── DATA: the actuator result contract ───────────────────────────────────────

ACTUATOR_SLOT = "actuator"  # Proposal.provenance.signals[ACTUATOR_SLOT]
ACTUATOR_SCHEMA = 1
RUNNER_ID = "claude-headless-v1"
RAW_TAIL_CHARS = 2000

REPORT_PARSED = "parsed"
REPORT_UNPARSEABLE = "unparseable"

# G3 post-hoc merge-check verdicts (§5.1.1.3). VIOLATION is deliberately
# shouty — it is the loud flag the operator greps for.
G3_CLEAN = "clean"
G3_VIOLATION = "VIOLATION"
G3_UNKNOWN = "unknown"

# ── DATA: machine-readable refusal codes (DispatchRefused.code) ──────────────

REFUSAL_NOT_FOUND = "proposal not found"
REFUSAL_NOT_ELIGIBLE = "not dispatch-eligible"
REFUSAL_NO_BRIEF = "brief unavailable"
REFUSAL_BRANCH_EXISTS = (
    "dispatch branch already exists on origin (duplicate-dispatch guard)"
)

# ── DATA: the production runner invocation (documented in the docstring) ─────

RUNNER_MAX_TURNS = 200
RUNNER_TIMEOUT_SECONDS = 3600  # one bite (~30 min) + generous headroom

# Where dispatch worktrees live, under the repo root (gitignored).
# Retained after the session for post-mortem; cleanup is a manual
# `git worktree remove` — v1 dispatches are individually human-approved,
# so accumulation is slow and visible.
WORKTREES_SUBDIR = ".edr/worktrees"

# gh-CLI fields for the G3 PR query.
PR_QUERY_FIELDS = "state,mergedAt,mergedBy,headRefName"

GIT_TIMEOUT_SECONDS = 120


class DispatchRefused(Exception):
    """``dispatch()`` refused — nothing was launched, nothing mutated.

    ``code`` is one of the ``REFUSAL_*`` constants (machine-readable;
    callers may branch on it); ``str(exc)`` carries the operator-facing
    detail.
    """

    def __init__(self, proposal_id: str, code: str, detail: str = ""):
        self.proposal_id = proposal_id
        self.code = code
        message = f"dispatch refused for {proposal_id!r}: {code}"
        if detail:
            message += f" — {detail}"
        super().__init__(message)


# ── Production seams (the ONLY subprocess use in this module) ────────────────


class GitWorktreeOps:
    """Production git operations for dispatch isolation.

    Injected into ``dispatch()`` as ``worktree_ops``; tests substitute a
    fake object with the same three methods. Every method shells out to
    git against ``repo_root`` — nothing here touches the EDR store.
    """

    def _git(self, repo_root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return result.stdout

    def fetch(self, repo_root: Path) -> None:
        """``git fetch origin main`` — the worktree branches off LATEST main."""
        self._git(repo_root, "fetch", "origin", "main")

    def branch_exists_on_origin(self, repo_root: Path, branch: str) -> bool:
        """True when ``branch`` exists on origin (``git ls-remote --heads``).

        Used twice per dispatch: BEFORE (the duplicate-dispatch guard)
        and AFTER the session (the pushed-work evidence check that
        decides applied-vs-retry — see the failure-path policy).
        """
        out = self._git(
            repo_root, "ls-remote", "--heads", "origin", branch
        )
        return bool(out.strip())

    def add_worktree(
        self, repo_root: Path, worktree_path: Path, branch: str
    ) -> None:
        """Create the isolated worktree on a NEW branch off origin/main."""
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            repo_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            "origin/main",
        )


def run_headless_claude(brief: str, *, cwd: Path) -> str:
    """Production runner seam: ONE headless Claude Code worker session.

    The exact invocation (see the module docstring for the rationale of
    each flag)::

        claude -p <brief> --output-format text \
            --max-turns {RUNNER_MAX_TURNS} --dangerously-skip-permissions

    cwd = the dispatch worktree; stdout captured; wall-clock bounded by
    ``RUNNER_TIMEOUT_SECONDS``. NEVER raises on worker death/timeout —
    returns whatever output was captured (possibly ""), because the
    dispatch flow judges by evidence (report parse + the pushed-branch
    check), not exit codes.
    """
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                brief,
                "--output-format",
                "text",
                "--max-turns",
                str(RUNNER_MAX_TURNS),
                "--dangerously-skip-permissions",
            ],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=RUNNER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        log.error("dispatch runner timed out after %ss", exc.timeout)
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return out or ""
    except OSError as exc:
        log.error("dispatch runner failed to launch: %s", exc)
        return ""
    if result.returncode != 0:
        log.warning(
            "dispatch runner exited %s (judging by evidence, not rc)",
            result.returncode,
        )
    return result.stdout or ""


def query_pr(pr_url: str) -> dict:
    """Production PR-state seam for the G3 check (``gh pr view``).

    Returns the parsed ``--json {PR_QUERY_FIELDS}`` dict. Raises on any
    failure (no auth, no such PR, bad JSON) — ``_g3_merge_check`` maps
    every raise to the ``unknown`` verdict with the reason recorded.
    """
    result = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", PR_QUERY_FIELDS],
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def _repo_root_default() -> Path | None:
    """The cwd's git toplevel (the CLI's ``--repo-root`` default)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


# ── Pure helpers ─────────────────────────────────────────────────────────────


def worktree_path_for(repo_root: Path, branch: str) -> Path:
    """Deterministic worktree location for a dispatch branch."""
    return Path(repo_root) / WORKTREES_SUBDIR / branch.replace("/", "-")


_REPORT_LINE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(f) for f in REPORT_FIELDS) + r")\s*:\s*(.*?)\s*$",
    re.MULTILINE,
)

# Worker spellings of "no PR" (the brief's template offers "none").
_NO_PR_VALUES = frozenset({"", "none", "n/a"})


def parse_report(output: str) -> dict[str, str] | None:
    """Parse the typed report block from the worker's captured output.

    Scans for ``<field>: <value>`` lines (the P1.3a ``REPORT_FIELDS``),
    keeping the LAST occurrence of each field — the report is the
    worker's FINAL message, and earlier output may quote the brief's
    own template. Returns the field dict only when ALL fields are
    present AND ``proof_status`` is one of ``PROOF_STATUS_VALUES``;
    anything else returns None (missing/malformed → the caller records
    ``report_status: "unparseable"`` with the raw tail — never crash,
    never guess).
    """
    found: dict[str, str] = {}
    for match in _REPORT_LINE_RE.finditer(output or ""):
        found[match.group(1)] = match.group(2)
    if set(found) != set(REPORT_FIELDS):
        return None
    if found["proof_status"] not in PROOF_STATUS_VALUES:
        return None
    return found


def _g3_merge_check(
    pr_url: str | None, pr_query: Callable[[str], dict]
) -> tuple[str, str]:
    """The §5.1.1.3 post-hoc merge check → ``(verdict, detail)``.

    verdict ∈ {G3_CLEAN, G3_VIOLATION, G3_UNKNOWN}; detail says why
    (PR state, who merged, or why the answer is unknown). Never raises.
    """
    if not pr_url:
        return G3_UNKNOWN, "no PR reported by the worker"
    try:
        info = pr_query(pr_url)
    except Exception as exc:  # the seam may fail any way gh can
        return G3_UNKNOWN, f"PR query failed: {exc}"
    if not isinstance(info, dict):
        return G3_UNKNOWN, "PR query returned no usable data"
    merged_at = info.get("mergedAt")
    if info.get("state") == "MERGED" or merged_at:
        merged_by = (info.get("mergedBy") or {}).get("login", "?")
        return (
            G3_VIOLATION,
            f"PR is MERGED (mergedAt={merged_at!r}, mergedBy={merged_by!r})"
            " — G3 forbids the worker session from merging",
        )
    return G3_CLEAN, f"PR state is {info.get('state')!r} (not merged)"


# ── Pre-flight (shared by dispatch and dry_run; read-only) ───────────────────


def _preflight(
    shared_dir: Path, proposal_id: str, *, repo: str
) -> tuple[Proposal, str, str, str]:
    """Load + gate one proposal → ``(proposal, subdir, branch, brief)``.

    Pure reads. Raises ``DispatchRefused`` (machine-readable ``code``)
    when the proposal is missing, fails the REAL ``dispatch_eligibility``
    re-check (approval may have been revoked / the record mutated since
    ``list``), or cannot render a brief.
    """
    located = arbiter_store.find_proposal(Path(shared_dir), proposal_id)
    if located is None:
        raise DispatchRefused(proposal_id, REFUSAL_NOT_FOUND)
    proposal, _path, subdir = located

    ok, reason = dispatch_eligibility(proposal)
    if not ok:
        raise DispatchRefused(
            proposal_id,
            REFUSAL_NOT_ELIGIBLE,
            detail=f"{reason} (status {proposal.status!r}, subdir {subdir}/)",
        )

    try:
        brief = brief_for(proposal, repo=repo)
    except BriefUnavailable as exc:
        raise DispatchRefused(
            proposal_id, REFUSAL_NO_BRIEF, detail=str(exc)
        ) from exc

    verdict = triage_verdict(proposal) or {}
    branch = branch_for(verdict["issue_number"], proposal.human_title or "")
    return proposal, subdir, branch, brief


def dry_run(
    shared_dir: Path, proposal_id: str, *, repo: str, repo_root: Path
) -> dict[str, Any]:
    """The ``--dry-run`` surface: gate + plan, launch NOTHING, mutate NOTHING.

    Deliberately takes NO seams — it must be impossible for a dry run to
    shell out. Same refusals as ``dispatch()``'s pre-flight (the
    eligibility verdict, as ``DispatchRefused``); on success returns the
    plan: the eligibility reason, the deterministic branch, the planned
    worktree path, and the rendered brief.

    The duplicate-branch guard is NOT evaluated here (it needs the
    network); the real dispatch re-runs every gate.
    """
    proposal, subdir, branch, brief = _preflight(
        shared_dir, proposal_id, repo=repo
    )
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "subdir": subdir,
        "eligibility": "eligible",
        "branch": branch,
        "worktree": str(worktree_path_for(repo_root, branch)),
        "brief": brief,
    }


# ── The dispatch runner ──────────────────────────────────────────────────────


def dispatch(
    shared_dir: Path,
    proposal_id: str,
    *,
    repo: str,
    repo_root: Path,
    runner: Callable[..., str] = run_headless_claude,
    pr_query: Callable[[str], dict] = query_pr,
    worktree_ops: Any = None,
) -> dict[str, Any]:
    """Dispatch ONE approved work item to a headless worker session.

    The full flow is the module docstring's six steps; the result dict
    is ``{proposal_id, status, branch, worktree, actuator}`` where
    ``actuator`` is exactly the block persisted to
    ``Proposal.provenance.signals["actuator"]`` and ``status`` is the
    Proposal's post-dispatch status (``applied``, or still
    ``approved_human`` on the no-pushed-work path — see the
    failure-path policy).

    Raises ``DispatchRefused`` (machine-readable ``code``, nothing
    launched, nothing mutated) for: unknown proposal, failed eligibility
    re-check, un-renderable brief, or a dispatch branch already on
    origin (duplicate-dispatch guard).
    """
    shared_dir = Path(shared_dir)
    repo_root = Path(repo_root)
    if worktree_ops is None:
        worktree_ops = GitWorktreeOps()

    proposal, subdir, branch, brief = _preflight(
        shared_dir, proposal_id, repo=repo
    )
    status_at_load = proposal.status

    # Isolation: branch off LATEST origin/main; refuse a duplicate
    # dispatch (the branch on origin is the durable evidence of a prior
    # dispatch — clean it up deliberately, don't pile a second worker on).
    worktree_ops.fetch(repo_root)
    if worktree_ops.branch_exists_on_origin(repo_root, branch):
        raise DispatchRefused(
            proposal_id,
            REFUSAL_BRANCH_EXISTS,
            detail=(
                f"branch {branch!r} already exists on origin; a prior "
                "dispatch pushed work — verify or delete that branch "
                "before re-dispatching"
            ),
        )
    worktree_path = worktree_path_for(repo_root, branch)
    worktree_ops.add_worktree(repo_root, worktree_path, branch)

    log.info(
        "bridge: dispatching %s → branch %s (worktree %s)",
        proposal_id,
        branch,
        worktree_path,
    )
    output = runner(brief, cwd=worktree_path)

    # ── Result capture: parse the typed report (never crash, never guess).
    report = parse_report(output)
    if report is None:
        report_status = REPORT_UNPARSEABLE
        pr_url: str | None = None
        proof_status: str | None = None
        notes = ""
        log.warning(
            "bridge: %s worker report missing/malformed — recording "
            "unparseable with raw tail",
            proposal_id,
        )
    else:
        report_status = REPORT_PARSED
        raw_pr = report["pr_url"]
        pr_url = None if raw_pr.lower() in _NO_PR_VALUES else raw_pr
        proof_status = report["proof_status"]
        notes = report["notes"]
        if report["branch"] != branch:
            log.warning(
                "bridge: %s worker reported branch %r, dispatched %r "
                "(raw tail preserves the claim)",
                proposal_id,
                report["branch"],
                branch,
            )

    # ── G3 post-hoc merge check (§5.1.1.3) — VIOLATION is loud.
    g3_verdict, g3_detail = _g3_merge_check(pr_url, pr_query)
    if g3_verdict == G3_VIOLATION:
        log.error(
            "bridge: G3 VIOLATION on %s — %s (pr=%s)",
            proposal_id,
            g3_detail,
            pr_url,
        )

    # ── Evidence check: did the worker push ANYTHING to origin?
    branch_pushed = worktree_ops.branch_exists_on_origin(repo_root, branch)

    prior = (proposal.provenance.signals or {}).get(ACTUATOR_SLOT)
    attempts = (prior.get("attempts", 0) if isinstance(prior, dict) else 0) + 1
    actuator = {
        "schema": ACTUATOR_SCHEMA,
        "branch": branch,
        "branch_pushed": branch_pushed,
        "pr_url": pr_url,
        "proof_status": proof_status,
        "report_status": report_status,
        "g3_merge_check": g3_verdict,
        "g3_detail": g3_detail,
        "dispatched_at": now_iso(),
        "runner": RUNNER_ID,
        "notes": notes,
        "raw_report_tail": (output or "")[-RAW_TAIL_CHARS:],
        "attempts": attempts,
        "worktree": str(worktree_path),
    }
    proposal.provenance.signals[ACTUATOR_SLOT] = actuator

    # ── Lifecycle (the failure-path policy in the module docstring):
    # pushed work → applied (P1.4's queue); nothing pushed → keep the
    # approval standing for re-dispatch, recording the attempt.
    if branch_pushed:
        state_machine.transition(
            proposal,
            APPLIED_STATUS,
            actor=DISPATCH_ACTOR,
            reason=DISPATCH_REASON,
        )
        # Cross-subdir move (pending/ → applied/): expected_status is the
        # store's CAS guard — a concurrent revoke/reject wins over us.
        arbiter_store.move_proposal(
            proposal,
            shared_dir,
            from_subdir=subdir,
            expected_status=status_at_load,
        )
        log.info(
            "bridge: %s dispatched → applied (branch pushed; verify "
            "stage owns it now)",
            proposal_id,
        )
    else:
        # In-place rewrite (same subdir): status untouched, attempt
        # recorded. Re-dispatch is one command away.
        arbiter_store.move_proposal(proposal, shared_dir, from_subdir=subdir)
        log.warning(
            "bridge: %s worker pushed NOTHING — proposal stays %s "
            "(attempt %d recorded; re-dispatch when ready)",
            proposal_id,
            proposal.status,
            attempts,
        )

    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "branch": branch,
        "worktree": str(worktree_path),
        "actuator": actuator,
    }

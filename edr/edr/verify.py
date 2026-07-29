"""edr.verify — the independent proof verifier + human-gated loop close (P1.4a).

The verify stage of the bridge (design: docs/edr/design-edr-2026-06-11.md
§5.2). Two entrypoints::

    verify(shared_dir, proposal_id, *, repo_root, ...) -> dict
    close(shared_dir, proposal_id, *, pr_query=...)    -> Proposal

THE PROOF CONVENTION (v1 — constrains how operators write ``Proof:`` lines)
─────────────────────────────────────────────────────────────────────────
**The ``Proof:`` payload IS an executable shell command**, e.g.::

    Proof: uv run --no-sync python -m pytest edr/tests/test_x.py -q

The proof-artifact verdict is MECHANICAL: verification is the
falsifiability check, run in CLEAN worktrees the actuator never touched:

  - on **origin/main**: the command must FAIL (non-zero exit) — a proof
    that passes on main proves nothing (``not-falsifiable``);
  - on **the PR branch**: the command must PASS (exit 0) — else
    ``failed``.

Both hold → ``verified``. This is red→green, enforced structurally: the
fix's own test must demonstrate the defect on main and its absence on
the branch. A ``Proof:`` line that is prose, a link, or a non-command
cannot verify — triage authors write commands or route to a human.
(The command must be runnable from the root of a fresh checkout;
``uv run --no-sync`` style invocations are the safe spelling — a bare
``python`` from the repo root may shadow packages.)

Independence (§5.2 — the load-bearing discipline)
─────────────────────────────────────────────────────────────────────────
**The verifier is INDEPENDENT and blind to the actuator's reasoning.**
It checks the DECLARED proof — ``provenance.signals["triage"]
["proof_artifact"]``, written at triage time, before any worker existed —
against two clean worktrees. It never reads the worker's prose: not
``notes``, not ``raw_report_tail``, not the reported ``proof_status``
("providing implementation code to verification agents too early biases
their outputs" — design §5.2). The ONLY actuator fields consumed are
mechanical coordinates: ``branch`` (verify) and ``pr_url`` /
``g3_merge_check`` (close). A clean reasoning trail is not a passing
proof; conversely a worker's claim of ``green`` is never trusted — the
verifier reruns the proof itself.

The verify flow
─────────────────────────────────────────────────────────────────────────
  1. load the Proposal; refuse (machine-readable ``REFUSAL_*`` codes,
     nothing mutated) unless status ``applied`` with a well-formed
     actuator block carrying a branch and a triage block carrying a
     proof command;
  2. fetch origin; refuse if the actuator's branch is not on origin;
  3. build two CLEAN throwaway worktrees under
     ``{repo_root}/.edr/verify/<proposal-id>/{main,branch}`` (detached,
     at ``origin/main`` and ``origin/<branch>`` — never the actuator's
     own worktree);
  4. run the proof command in each (shell, timeout-bounded, exit code +
     output tail captured);
  5. verdict per the convention table (``VERDICT_*`` data below); any
     environment failure mid-run → ``error`` with the why — recorded,
     never raised (the evidence of a failed attempt is still evidence);
  6. persist ``provenance.signals["verify"]`` via the real store
     (in-place rewrite — ``move_proposal`` with src == dest). The status
     is UNCHANGED: verify records evidence; closing is a separate
     human-gated act.
  7. clean both worktrees up (best-effort, in ``finally``: a failed
     removal is logged, never raised — stale dirs under ``.edr/`` are
     gitignored and a re-verify recreates them after a manual
     ``git worktree remove --force``).

The verify evidence block — ``provenance.signals["verify"]``
─────────────────────────────────────────────────────────────────────────
::

    {
        "schema": 1,
        "proof_cmd": "<the declared Proof: payload, verbatim>",
        "main_exit": int | None,      # None = the run never completed
        "branch_exit": int | None,
        "verdict": "verified" | "not-falsifiable" | "failed" | "error",
        "detail": "<why, one line>",
        "checked_at": "<ISO UTC>",
        "main_output_tail": "<last OUTPUT_TAIL_CHARS chars>",
        "branch_output_tail": "<last OUTPUT_TAIL_CHARS chars>",
    }

The block is OVERWRITE-WITH-LATEST (same policy as the actuator block):
the Proposal carries the current verdict; the durable trail is the
append-only ``Proposal.history`` plus the PR itself.

The close gate (the loop-close, human-gated by EVIDENCE)
─────────────────────────────────────────────────────────────────────────
``close()`` transitions ``applied → succeeded`` ONLY when ALL of:

  1. status is ``applied``;
  2. the verify verdict is ``verified`` (this module's own evidence);
  3. the PR is MERGED — and a HUMAN merged it: the same
     ``gh pr view --json state,mergedAt,mergedBy`` seam shape as
     dispatch's G3 check, with ``mergedBy`` REQUIRED and recorded in the
     close evidence (no merger identity → no close);
  4. the actuator's ``g3_merge_check`` was not ``VIOLATION`` (a PR the
     worker session merged itself can never close cleanly — that record
     goes to a human, period).

The human act in this gate is the MERGE (design §5.3, R1: "human
reviews + merges every time"); ``close()`` is the system verifying that
act's evidence and recording it, so the transition actor is the bridge's
own identifier (``CLOSE_ACTOR`` = P1.3b's ``DISPATCH_ACTOR``), never
``"user"``. ``applied → succeeded`` is a direct legal edge in
``arbiter.state_machine`` (the same edge the product's verify daemon
uses), and the cross-subdir move (applied/ → archived/) passes
``expected_status`` so the store's CAS guard engages.

Close evidence — ``provenance.signals["close"]``::

    {"schema": 1, "merged_at": ..., "merged_by": ..., "closed_at": ...}

Seam discipline (``subprocess-patch-fakes-break-on-module-move``)
─────────────────────────────────────────────────────────────────────────
ALL subprocess use is behind injected callables:

  ``worktree_ops``                 — git fetch / ls-remote / detached
                                     worktree add / remove. Default:
                                     ``GitVerifyWorktreeOps()``.
  ``proof_runner(cmd, *, cwd)``    — runs the proof command, returns
                                     ``(exit_code, output)``. Default:
                                     ``run_proof_command`` (shell,
                                     ``PROOF_TIMEOUT_SECONDS``).
  ``pr_query(pr_url)``             — close's merge-evidence check.
                                     Default: ``dispatch.query_pr``
                                     (the SAME seam the G3 check uses).

Tests inject fakes at these parameters and never create worktrees, run
proofs, or touch the network.

CLI: ``python -m edr.bridge verify|close <proposal-id>`` — the bridge
CLI is the single operator surface for the whole loop (list → approve →
dispatch → verify → close), so the new subcommands live there rather
than in a parallel ``python -m edr.verify`` entrypoint.
"""

from __future__ import annotations

import logging
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

from edr.bridge.dispatch import (
    ACTUATOR_SLOT,
    APPLIED_STATUS,
    DISPATCH_ACTOR,
    G3_VIOLATION,
    GIT_TIMEOUT_SECONDS,
    query_pr,
)
from edr.bridge.lifecycle import triage_verdict

log = logging.getLogger(__name__)

# ── DATA: the verdict vocabulary (the convention table in the docstring) ─────

VERDICT_VERIFIED = "verified"  # red on origin/main AND green on the branch
VERDICT_NOT_FALSIFIABLE = "not-falsifiable"  # green on origin/main
VERDICT_FAILED = "failed"  # red on the branch
VERDICT_ERROR = "error"  # a run never completed; detail says why

# ── DATA: the verify evidence contract ───────────────────────────────────────

VERIFY_SLOT = "verify"  # Proposal.provenance.signals[VERIFY_SLOT]
VERIFY_SCHEMA = 1
OUTPUT_TAIL_CHARS = 2000

# ── DATA: the close evidence contract + lifecycle act ────────────────────────

CLOSE_SLOT = "close"  # Proposal.provenance.signals[CLOSE_SLOT]
CLOSE_SCHEMA = 1
SUCCEEDED_STATUS = "succeeded"
# The system records the human's merge evidence — same actor identity as
# dispatch (see "The close gate" in the docstring; never "user").
CLOSE_ACTOR = DISPATCH_ACTOR
CLOSE_REASON = (
    "EDR bridge: proof verified + PR human-merged — loop closed (P1.4a)"
)

# ── DATA: machine-readable refusal codes ─────────────────────────────────────

# Shared by verify() and close():
REFUSAL_NOT_FOUND = "proposal not found"
REFUSAL_NOT_APPLIED = "not in applied status"
REFUSAL_NO_ACTUATOR = "missing/malformed actuator block"
# verify()-only:
REFUSAL_NO_PROOF = "no proof command on the triage verdict"
REFUSAL_BRANCH_NOT_ON_ORIGIN = "actuator branch not found on origin"
# close()-only:
REFUSAL_NOT_VERIFIED = "proof verdict is not 'verified'"
REFUSAL_G3_VIOLATION = "actuator g3_merge_check is VIOLATION"
REFUSAL_NO_PR = "no PR recorded on the actuator block"
REFUSAL_PR_STATE_UNKNOWN = "PR state could not be determined"
REFUSAL_NOT_MERGED = "PR is not merged (with a recorded merger)"

# ── DATA: the production proof run ───────────────────────────────────────────

PROOF_TIMEOUT_SECONDS = 900  # one targeted falsifiability check, per side

MAIN_REF = "origin/main"

# Where verify worktrees live, under the repo root (gitignored via .edr/).
VERIFY_SUBDIR = ".edr/verify"


class VerifyRefused(Exception):
    """``verify()`` refused — nothing ran, nothing mutated.

    ``code`` is one of the ``REFUSAL_*`` constants (machine-readable;
    callers may branch on it); ``str(exc)`` carries the operator-facing
    detail.
    """

    def __init__(self, proposal_id: str, code: str, detail: str = ""):
        self.proposal_id = proposal_id
        self.code = code
        message = f"verify refused for {proposal_id!r}: {code}"
        if detail:
            message += f" — {detail}"
        super().__init__(message)


class CloseRefused(Exception):
    """``close()`` refused — nothing was mutated.

    Same shape as ``VerifyRefused``: machine-readable ``code``,
    operator-facing detail in ``str(exc)``.
    """

    def __init__(self, proposal_id: str, code: str, detail: str = ""):
        self.proposal_id = proposal_id
        self.code = code
        message = f"close refused for {proposal_id!r}: {code}"
        if detail:
            message += f" — {detail}"
        super().__init__(message)


# ── Production seams (the ONLY subprocess use in this module) ────────────────


class GitVerifyWorktreeOps:
    """Production git operations for verify isolation.

    Injected into ``verify()`` as ``worktree_ops``; tests substitute a
    fake with the same four methods. Verify worktrees are DETACHED
    (``--detach``) — they exist to run one command at one ref and are
    removed afterward; no branch is ever created.
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
        """``git fetch origin`` — ALL heads, so both ``origin/main`` and
        the actuator's PR branch resolve locally (dispatch only needs
        main; verify needs the branch ref too)."""
        self._git(repo_root, "fetch", "origin")

    def branch_exists_on_origin(self, repo_root: Path, branch: str) -> bool:
        """True when ``branch`` exists on origin (``git ls-remote --heads``)."""
        out = self._git(repo_root, "ls-remote", "--heads", "origin", branch)
        return bool(out.strip())

    def add_worktree(
        self, repo_root: Path, worktree_path: Path, ref: str
    ) -> None:
        """Create a clean DETACHED worktree at ``ref``."""
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            repo_root,
            "worktree",
            "add",
            "--detach",
            str(worktree_path),
            ref,
        )

    def remove_worktree(self, repo_root: Path, worktree_path: Path) -> None:
        """Remove a verify worktree (``--force``: proof runs may dirty it)."""
        self._git(
            repo_root, "worktree", "remove", "--force", str(worktree_path)
        )


def run_proof_command(cmd: str, *, cwd: Path) -> tuple[int, str]:
    """Production proof-runner seam: ONE shell command in ONE worktree.

    Returns ``(exit_code, combined_output)``. The command is the
    verbatim ``Proof:`` payload (the convention: it IS an executable
    shell command), run via the shell with cwd = the clean worktree,
    wall-clock bounded by ``PROOF_TIMEOUT_SECONDS``. Raises on timeout /
    launch failure — ``verify()`` maps every raise from this seam to the
    ``error`` verdict with the reason recorded, so a hung proof becomes
    evidence, not a crash.
    """
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=PROOF_TIMEOUT_SECONDS,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output


# ── Pure helpers ─────────────────────────────────────────────────────────────


def verify_worktree_paths(
    repo_root: Path, proposal_id: str
) -> tuple[Path, Path]:
    """Deterministic clean-worktree locations → ``(main_path, branch_path)``."""
    base = Path(repo_root) / VERIFY_SUBDIR / proposal_id
    return base / "main", base / "branch"


def proof_verdict(main_exit: int, branch_exit: int) -> tuple[str, str]:
    """The mechanical verdict table → ``(verdict, detail)``.

    Green-on-main wins over red-on-branch: a proof that passes on main
    proves nothing regardless of what it does on the branch — the
    falsifiability failure is the more fundamental finding.
    """
    if main_exit == 0:
        return (
            VERDICT_NOT_FALSIFIABLE,
            f"the proof PASSES on {MAIN_REF} (exit 0) — it proves "
            "nothing; a valid proof must FAIL on main (red→green)",
        )
    if branch_exit != 0:
        return (
            VERDICT_FAILED,
            f"the proof FAILS on the PR branch (exit {branch_exit}) — "
            "the declared close condition does not hold",
        )
    return (
        VERDICT_VERIFIED,
        f"red on {MAIN_REF} (exit {main_exit}), green on the PR branch "
        "(exit 0) — red→green holds",
    )


# ── Pre-flight (read-only; refusals mutate nothing) ──────────────────────────


def _load_applied(
    shared_dir: Path,
    proposal_id: str,
    refusal: type[VerifyRefused] | type[CloseRefused],
) -> tuple[Proposal, str, dict[str, Any]]:
    """Load + gate → ``(proposal, subdir, actuator_block)``.

    The shared verify/close pre-flight: the proposal must exist, be
    ``applied``, and carry a dict-shaped actuator block. ``refusal`` is
    the caller's exception class (same codes, distinguishable act).
    """
    located = arbiter_store.find_proposal(Path(shared_dir), proposal_id)
    if located is None:
        raise refusal(proposal_id, REFUSAL_NOT_FOUND)
    proposal, _path, subdir = located

    if proposal.status != APPLIED_STATUS:
        raise refusal(
            proposal_id,
            REFUSAL_NOT_APPLIED,
            detail=(
                f"status is {proposal.status!r} (subdir {subdir}/); "
                f"only {APPLIED_STATUS!r} proposals carry dispatched "
                "work to verify/close"
            ),
        )

    actuator = (proposal.provenance.signals or {}).get(ACTUATOR_SLOT)
    if not isinstance(actuator, dict):
        raise refusal(
            proposal_id,
            REFUSAL_NO_ACTUATOR,
            detail=(
                "applied but provenance.signals['actuator'] is missing "
                "or not a dict — was this proposal dispatched through "
                "the bridge?"
            ),
        )
    return proposal, subdir, actuator


def _preflight_verify(
    shared_dir: Path, proposal_id: str
) -> tuple[Proposal, str, str, str]:
    """Verify's gate → ``(proposal, subdir, proof_cmd, branch)``.

    Pure reads. The proof command comes from the TRIAGE block — the
    proof DECLARED before any worker existed — never from anything the
    worker wrote (independence, §5.2).
    """
    proposal, subdir, actuator = _load_applied(
        shared_dir, proposal_id, VerifyRefused
    )

    branch = actuator.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise VerifyRefused(
            proposal_id,
            REFUSAL_NO_ACTUATOR,
            detail="actuator block carries no branch — nothing to check",
        )

    verdict = triage_verdict(proposal) or {}
    proof_cmd = verdict.get("proof_artifact")
    if not isinstance(proof_cmd, str) or not proof_cmd.strip():
        raise VerifyRefused(
            proposal_id,
            REFUSAL_NO_PROOF,
            detail=(
                "triage block carries no proof_artifact — §5.2: no "
                "proof, no loop (re-triage with a Proof: line)"
            ),
        )
    return proposal, subdir, proof_cmd.strip(), branch.strip()


# ── The independent verifier ─────────────────────────────────────────────────


def verify(
    shared_dir: Path,
    proposal_id: str,
    *,
    repo_root: Path,
    worktree_ops: Any = None,
    proof_runner: Callable[..., tuple[int, str]] = run_proof_command,
) -> dict[str, Any]:
    """Run the falsifiability check for ONE applied proposal.

    The full flow is the module docstring's seven steps; the result dict
    is ``{proposal_id, status, verdict, verify}`` where ``verify`` is
    exactly the evidence block persisted to
    ``Proposal.provenance.signals["verify"]`` and ``status`` is the
    Proposal's status — UNCHANGED by this call (closing is ``close()``,
    a separate human-gated act).

    Raises ``VerifyRefused`` (machine-readable ``code``, nothing ran,
    nothing mutated) for: unknown proposal, status ≠ applied,
    missing/malformed actuator block (or no branch on it), no declared
    proof command, or a branch that does not exist on origin. Once the
    proof phase starts, failures are RECORDED (verdict ``error``) rather
    than raised.
    """
    shared_dir = Path(shared_dir)
    repo_root = Path(repo_root)
    if worktree_ops is None:
        worktree_ops = GitVerifyWorktreeOps()

    proposal, subdir, proof_cmd, branch = _preflight_verify(
        shared_dir, proposal_id
    )

    worktree_ops.fetch(repo_root)
    if not worktree_ops.branch_exists_on_origin(repo_root, branch):
        raise VerifyRefused(
            proposal_id,
            REFUSAL_BRANCH_NOT_ON_ORIGIN,
            detail=(
                f"branch {branch!r} is not on origin — the dispatched "
                "work has vanished (deleted after merge? re-dispatch?); "
                "nothing to verify"
            ),
        )

    main_path, branch_path = verify_worktree_paths(repo_root, proposal_id)
    main_exit: int | None = None
    branch_exit: int | None = None
    main_tail = ""
    branch_tail = ""

    # Past this point nothing raises out: a verify attempt always leaves
    # evidence on the proposal, even when the environment fought back.
    phase = f"creating the clean {MAIN_REF} worktree"
    try:
        worktree_ops.add_worktree(repo_root, main_path, MAIN_REF)
        phase = f"creating the clean origin/{branch} worktree"
        worktree_ops.add_worktree(repo_root, branch_path, f"origin/{branch}")

        phase = f"running the proof on {MAIN_REF}"
        main_exit, main_output = proof_runner(proof_cmd, cwd=main_path)
        main_tail = (main_output or "")[-OUTPUT_TAIL_CHARS:]

        phase = "running the proof on the PR branch"
        branch_exit, branch_output = proof_runner(proof_cmd, cwd=branch_path)
        branch_tail = (branch_output or "")[-OUTPUT_TAIL_CHARS:]

        verdict, detail = proof_verdict(main_exit, branch_exit)
    except Exception as exc:  # environment failure → evidence, not a crash
        verdict = VERDICT_ERROR
        detail = f"could not complete the check — {phase}: {exc}"
        log.error("verify: %s — %s", proposal_id, detail)
    finally:
        # Best-effort cleanup (docstring step 7): failures are logged,
        # never raised — .edr/ is gitignored and a stale worktree only
        # blocks the next verify of the SAME proposal until a manual
        # `git worktree remove --force`.
        for path in (main_path, branch_path):
            try:
                worktree_ops.remove_worktree(repo_root, path)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "verify: could not remove worktree %s: %s", path, exc
                )

    verify_block = {
        "schema": VERIFY_SCHEMA,
        "proof_cmd": proof_cmd,
        "main_exit": main_exit,
        "branch_exit": branch_exit,
        "verdict": verdict,
        "detail": detail,
        "checked_at": now_iso(),
        "main_output_tail": main_tail,
        "branch_output_tail": branch_tail,
    }
    proposal.provenance.signals[VERIFY_SLOT] = verify_block

    # In-place persist (src == dest → atomic rewrite, no status change):
    # verify records evidence; the lifecycle only moves at close().
    arbiter_store.move_proposal(proposal, shared_dir, from_subdir=subdir)
    log.info(
        "verify: %s → %s (main_exit=%s, branch_exit=%s)",
        proposal_id,
        verdict,
        main_exit,
        branch_exit,
    )

    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "verdict": verdict,
        "verify": verify_block,
    }


# ── The loop-close gate ──────────────────────────────────────────────────────


def close(
    shared_dir: Path,
    proposal_id: str,
    *,
    pr_query: Callable[[str], dict] = query_pr,
) -> Proposal:
    """Close the loop: ``applied → succeeded``, gated on EVIDENCE.

    Requires ALL of (each refusal raises ``CloseRefused`` with a
    machine-readable code; NOTHING is mutated): status ``applied``; a
    verify verdict of ``verified`` (this module's own evidence — run
    ``verify()`` first); the PR MERGED by a recorded human
    (``mergedBy`` required); and an actuator ``g3_merge_check`` that is
    not ``VIOLATION``. See "The close gate" in the module docstring for
    why the transition actor is the bridge identifier, not ``"user"``.

    On success: appends the ``applied → succeeded`` history entry via
    the real ``arbiter.state_machine.transition`` (a direct legal edge),
    records ``provenance.signals["close"]``, and moves the file
    applied/ → archived/ with the store's CAS guard engaged
    (``expected_status``: a concurrent transition wins over us).
    Returns the updated Proposal.
    """
    shared_dir = Path(shared_dir)
    proposal, subdir, actuator = _load_applied(
        shared_dir, proposal_id, CloseRefused
    )

    verify_block = (proposal.provenance.signals or {}).get(VERIFY_SLOT)
    found_verdict = (
        verify_block.get("verdict")
        if isinstance(verify_block, dict)
        else None
    )
    if found_verdict != VERDICT_VERIFIED:
        raise CloseRefused(
            proposal_id,
            REFUSAL_NOT_VERIFIED,
            detail=(
                f"verify verdict is {found_verdict!r} — run "
                "`python -m edr.bridge verify` and get "
                f"{VERDICT_VERIFIED!r} first (no proof, no close)"
                if verify_block is not None
                else "no verify evidence on the proposal — run "
                "`python -m edr.bridge verify` first"
            ),
        )

    if actuator.get("g3_merge_check") == G3_VIOLATION:
        raise CloseRefused(
            proposal_id,
            REFUSAL_G3_VIOLATION,
            detail=(
                "the dispatch recorded a G3 merge violation — a PR the "
                "worker session merged itself can never close cleanly; "
                "this record needs a human decision"
            ),
        )

    pr_url = actuator.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise CloseRefused(
            proposal_id,
            REFUSAL_NO_PR,
            detail=(
                "actuator block carries no pr_url — close requires a "
                "human-merged PR as the loop's terminal evidence"
            ),
        )

    try:
        info = pr_query(pr_url)
    except Exception as exc:  # the seam may fail any way gh can
        raise CloseRefused(
            proposal_id,
            REFUSAL_PR_STATE_UNKNOWN,
            detail=f"PR query failed: {exc}",
        ) from exc
    if not isinstance(info, dict):
        raise CloseRefused(
            proposal_id,
            REFUSAL_PR_STATE_UNKNOWN,
            detail="PR query returned no usable data",
        )

    merged_at = info.get("mergedAt")
    merged_by = (info.get("mergedBy") or {}).get("login")
    if not (info.get("state") == "MERGED" or merged_at):
        raise CloseRefused(
            proposal_id,
            REFUSAL_NOT_MERGED,
            detail=(
                f"PR state is {info.get('state')!r} — the close gate is "
                "a HUMAN merging the PR; merge it (or reject the "
                "proposal) first"
            ),
        )
    if not merged_by:
        raise CloseRefused(
            proposal_id,
            REFUSAL_NOT_MERGED,
            detail=(
                "PR is merged but mergedBy is unavailable — the close "
                "evidence requires WHO merged (a human act needs a "
                "recorded human)"
            ),
        )

    # All gates passed — the real lifecycle: applied → succeeded (direct
    # legal edge), bridge actor, CAS-guarded cross-subdir move.
    state_machine.transition(
        proposal, SUCCEEDED_STATUS, actor=CLOSE_ACTOR, reason=CLOSE_REASON
    )
    proposal.provenance.signals[CLOSE_SLOT] = {
        "schema": CLOSE_SCHEMA,
        "merged_at": merged_at,
        "merged_by": merged_by,
        "closed_at": now_iso(),
    }
    arbiter_store.move_proposal(
        proposal,
        shared_dir,
        from_subdir=subdir,
        expected_status=APPLIED_STATUS,
    )
    log.info(
        "close: %s → %s (merged by %s at %s)",
        proposal_id,
        proposal.status,
        merged_by,
        merged_at,
    )
    return proposal

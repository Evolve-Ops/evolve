"""edr/tests/test_verify.py — P1.4a proof (independent verifier + loop close).

The verifier proven against the REAL imported libraries on tmp_path
stores. Proposals are never hand-crafted: each test seeds a ``dev_issue``
Signal through the real ``signals.store.observe()`` (via the P1.1
adapter's normalizer), runs the real P1.2 triage, approves through the
real P1.3a ``lifecycle.approve()``, and then simulates post-dispatch
state by hand-writing a minimal actuator block through the REAL store
lifecycle (transition → applied + ``move_proposal``) — the shapes under
test are exactly what production writes.

ALL subprocess seams are FAKED (``worktree_ops`` / ``proof_runner`` /
``pr_query`` injected at the entrypoints' parameters — never
``patch("module.subprocess.run")``). No test creates a git worktree,
runs a proof command, or touches the network.

  1. verdicts       — verified (1, 0) / not-falsifiable (0, 0) /
                      failed (1, 1) / green-main-wins (0, 1); the pure
                      ``proof_verdict`` table; status UNCHANGED;
  2. evidence       — the verify block persisted via the real store,
                      JSON-round-trippable, output tails clamped;
  3. error path     — a proof run that cannot complete is RECORDED
                      (verdict ``error``), never raised; worktrees
                      cleaned up regardless;
  4. refusals       — missing branch on origin / non-applied / malformed
                      actuator / no declared proof: machine-readable
                      codes, zero mutation, nothing run;
  5. independence   — the verify block NEVER contains worker-written
                      text (notes / raw_report_tail), and the worker's
                      own ``proof_status`` claim never drives the
                      verdict (§5.2: blind to the actuator's reasoning);
  6. close          — happy path (verified + human-merged PR →
                      ``succeeded`` in archived/, merged_by recorded)
                      and every refusal (unverified / unmerged / no
                      merger / G3 VIOLATION / wrong status / PR state
                      unknown), each mutating NOTHING;
  7. CLI smoke      — ``python -m edr.bridge verify|close`` via
                      ``main(argv)`` (refusal paths only: the CLI wires
                      the default REAL seams, which tests never run).

Design: internal/edr/design-edr-2026-06-11.md §5.2 (proof-artifact
discipline; the verifier is independent and blind to the actuator's
reasoning).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Real library imports — same modules the shipped daemons load.
import arbiter.state_machine as state_machine
import arbiter.store as arbiter_store
import signals.store as signals_store
from edr import verify as verify_mod
from edr.adapters.github_issues import issue_to_signal_fields
from edr.bridge import dispatch as dispatch_mod
from edr.bridge import lifecycle
from edr.bridge.__main__ import main as bridge_main
from edr.triage import dev_issue as triage

# The Proof: payload IS an executable shell command (the P1.4a
# convention) — triage records it verbatim; verify runs it verbatim.
PROOF = "uv run --no-sync python -m pytest edr/tests/test_scheduler.py -q"
AGENT_BODY = f"It flakes on the second run.\nProof: {PROOF}\nMore context."
REPO = "example-org/example-repo"

# Worker-written prose markers — these must NEVER leak into the verify
# block (independence guard, §5.2).
WORKER_NOTES = "WORKER-PROSE-MARKER: the fix was elegant, trust me"
WORKER_RAW_TAIL = "RAW-TAIL-MARKER: ...full worker narrative tail..."


# ── Seeding (real observe → triage → approve → simulated dispatch) ───────────


def _issue(number: int, **overrides) -> dict:
    issue = {
        "number": number,
        "title": f"Flaky test in the scheduler suite ({number})",
        "body": AGENT_BODY,
        "labels": [{"name": "bug"}, {"name": "edr:agent-able"}],
        "author": {"login": "contributor-a"},
        "createdAt": "2026-06-10T00:00:00Z",
        "updatedAt": "2026-06-11T00:00:00Z",
        "url": f"https://github.com/{REPO}/issues/{number}",
        "state": "OPEN",
    }
    issue.update(overrides)
    return issue


def _seed_approved(shared_dir, number: int, **issue_overrides) -> str:
    """Signal → triage → real human approve; returns the proposal id."""
    fields = issue_to_signal_fields(_issue(number, **issue_overrides))
    signals_store.observe(shared_dir, **fields)
    triage.run(shared_dir)
    pid = f"edr-triage-gh-{number}"
    lifecycle.approve(shared_dir, pid)
    return pid


def _actuator_block(number: int, branch: str, **overrides) -> dict:
    """A minimal well-formed P1.3b actuator block (worker prose included
    so the independence guard has something to catch)."""
    block = {
        "schema": dispatch_mod.ACTUATOR_SCHEMA,
        "branch": branch,
        "branch_pushed": True,
        "pr_url": f"https://github.com/{REPO}/pull/{7000 + number}",
        "proof_status": "green",  # the worker's CLAIM — never trusted
        "report_status": dispatch_mod.REPORT_PARSED,
        "g3_merge_check": dispatch_mod.G3_CLEAN,
        "g3_detail": "PR state is 'OPEN' (not merged)",
        "dispatched_at": "2026-06-12T00:00:00Z",
        "runner": dispatch_mod.RUNNER_ID,
        "notes": WORKER_NOTES,
        "raw_report_tail": WORKER_RAW_TAIL,
        "attempts": 1,
        "worktree": "/tmp/example-dispatch-worktree",
    }
    block.update(overrides)
    return block


def _seed_applied(shared_dir, number: int, **actuator_overrides) -> tuple[str, str]:
    """Approved proposal + simulated post-dispatch state via the REAL
    store lifecycle (the P1.3b shape on disk). Returns (pid, branch)."""
    pid = _seed_approved(shared_dir, number)
    proposal, subdir = _reload(shared_dir, pid)
    branch = actuator_overrides.pop(
        "branch", f"edr/gh-{number}-flaky-test-in-the-scheduler-suite"
    )
    state_machine.transition(
        proposal,
        "applied",
        actor=dispatch_mod.DISPATCH_ACTOR,
        reason="test: simulate P1.3b dispatch result",
    )
    proposal.provenance.signals[dispatch_mod.ACTUATOR_SLOT] = _actuator_block(
        number, branch, **actuator_overrides
    )
    arbiter_store.move_proposal(
        proposal, shared_dir, from_subdir=subdir,
        expected_status="approved_human",
    )
    return pid, branch


def _reload(shared_dir, proposal_id: str):
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    assert located is not None
    proposal, _path, subdir = located
    return proposal, subdir


def _store_snapshot(shared_dir) -> dict[str, bytes]:
    """Every file under the store, path → raw bytes (mutation detector)."""
    root = Path(shared_dir)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── Fake seams (inject at the entrypoints' params; nothing ever runs) ────────


class FakeVerifyOps:
    """Records every call; ``branch_on_origin`` answers the ls-remote
    check."""

    def __init__(self, *, branch_on_origin: bool = True):
        self.branch_on_origin = branch_on_origin
        self.calls: list[tuple] = []

    def fetch(self, repo_root):
        self.calls.append(("fetch", Path(repo_root)))

    def branch_exists_on_origin(self, repo_root, branch):
        self.calls.append(("exists", branch))
        return self.branch_on_origin

    def add_worktree(self, repo_root, worktree_path, ref):
        self.calls.append(("add", Path(worktree_path), ref))

    def remove_worktree(self, repo_root, worktree_path):
        self.calls.append(("remove", Path(worktree_path)))


class FakeProofRunner:
    """Answers by worktree side (the cwd's last path component:
    ``main`` or ``branch``); can raise on a chosen side."""

    def __init__(
        self,
        main_exit: int,
        branch_exit: int,
        *,
        main_output: str = "main proof output",
        branch_output: str = "branch proof output",
        raise_on: str | None = None,
        exc: Exception | None = None,
    ):
        self.exits = {"main": main_exit, "branch": branch_exit}
        self.outputs = {"main": main_output, "branch": branch_output}
        self.raise_on = raise_on
        self.exc = exc or RuntimeError("proof runner exploded")
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, cmd: str, *, cwd) -> tuple[int, str]:
        side = Path(cwd).name
        self.calls.append((cmd, Path(cwd)))
        if side == self.raise_on:
            raise self.exc
        return self.exits[side], self.outputs[side]


class FakePrQuery:
    def __init__(self, result):
        self.result = result  # dict, or an Exception to raise
        self.calls: list[str] = []

    def __call__(self, pr_url: str) -> dict:
        self.calls.append(pr_url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


OPEN_PR = {
    "state": "OPEN",
    "mergedAt": None,
    "mergedBy": None,
    "headRefName": "x",
}
HUMAN_MERGED_PR = {
    "state": "MERGED",
    "mergedAt": "2026-06-12T10:00:00Z",
    "mergedBy": {"login": "human-reviewer"},
    "headRefName": "x",
}
MERGED_NO_MERGER_PR = {
    "state": "MERGED",
    "mergedAt": "2026-06-12T10:00:00Z",
    "mergedBy": None,
    "headRefName": "x",
}


def _verify(shared_dir, pid, *, runner, ops, repo_root):
    return verify_mod.verify(
        shared_dir,
        pid,
        repo_root=repo_root,
        worktree_ops=ops,
        proof_runner=runner,
    )


# ── 1+2. The verdict table, on the real store ────────────────────────────────


def test_verify_verified_path(tmp_path):
    """Red on origin/main (exit 1) + green on the branch (exit 0) →
    ``verified``; evidence persisted; status UNCHANGED (applied)."""
    store = tmp_path / "store"
    repo_root = tmp_path / "checkout"
    pid, branch = _seed_applied(store, 601)
    main_path, branch_path = verify_mod.verify_worktree_paths(repo_root, pid)

    ops = FakeVerifyOps()
    runner = FakeProofRunner(1, 0)
    result = _verify(store, pid, runner=runner, ops=ops, repo_root=repo_root)

    assert result["verdict"] == verify_mod.VERDICT_VERIFIED
    # The proof ran VERBATIM (the declared Proof: payload), once per
    # clean worktree, in order main → branch.
    assert runner.calls == [(PROOF, main_path), (PROOF, branch_path)]
    # Worktree mechanics: fetch → branch check → two detached adds at
    # the right refs → both removed (cleanup) — and the actuator's own
    # dispatch worktree is never touched.
    assert [c[0] for c in ops.calls] == [
        "fetch", "exists", "add", "add", "remove", "remove",
    ]
    assert ("exists", branch) in ops.calls
    assert ("add", main_path, verify_mod.MAIN_REF) in ops.calls
    assert ("add", branch_path, f"origin/{branch}") in ops.calls
    assert ("remove", main_path) in ops.calls
    assert ("remove", branch_path) in ops.calls

    # The typed evidence block.
    block = result["verify"]
    assert block["schema"] == verify_mod.VERIFY_SCHEMA
    assert block["proof_cmd"] == PROOF
    assert block["main_exit"] == 1
    assert block["branch_exit"] == 0
    assert block["verdict"] == verify_mod.VERDICT_VERIFIED
    assert "red→green holds" in block["detail"]
    assert block["checked_at"]
    assert block["main_output_tail"] == "main proof output"
    assert block["branch_output_tail"] == "branch proof output"

    # Authoritative on-disk state: verify records evidence ONLY — the
    # status and subdir are unchanged, history did not grow (closing is
    # a separate human-gated act).
    on_disk, subdir = _reload(store, pid)
    assert result["status"] == on_disk.status == "applied"
    assert subdir == "applied"
    assert on_disk.provenance.signals[verify_mod.VERIFY_SLOT] == block
    assert len(on_disk.history) == 2  # approve + applied; no new edge
    # And the block survives the store's JSON round-trip byte-faithfully.
    raw = json.loads(
        (store / "proposals" / "applied" / f"{pid}.json").read_text()
    )
    assert raw["provenance"]["signals"]["verify"] == block


def test_verify_not_falsifiable_when_green_on_main(tmp_path):
    """Exit 0 on origin/main → the proof proves nothing — and the
    worker's own 'green' claim in the actuator block never rescues it
    (the verdict is the verifier's own runs, full stop)."""
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 602, proof_status="green")

    result = _verify(
        store, pid,
        runner=FakeProofRunner(0, 0),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result["verdict"] == verify_mod.VERDICT_NOT_FALSIFIABLE
    assert "proves nothing" in result["verify"]["detail"]
    on_disk, subdir = _reload(store, pid)
    assert on_disk.status == "applied"  # evidence recorded, no close
    assert subdir == "applied"


def test_verify_failed_when_red_on_branch(tmp_path):
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 603)

    result = _verify(
        store, pid,
        runner=FakeProofRunner(1, 1),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result["verdict"] == verify_mod.VERDICT_FAILED
    assert result["verify"]["branch_exit"] == 1
    assert "FAILS on the PR branch" in result["verify"]["detail"]


def test_verify_green_main_wins_over_red_branch(tmp_path):
    """(0, 1): both 'green on main' and 'red on branch' hold — the
    falsifiability failure is the more fundamental finding and wins."""
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 604)

    result = _verify(
        store, pid,
        runner=FakeProofRunner(0, 1),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result["verdict"] == verify_mod.VERDICT_NOT_FALSIFIABLE


def test_proof_verdict_table():
    """The pure mechanical table, all four exit combinations."""
    assert verify_mod.proof_verdict(1, 0)[0] == verify_mod.VERDICT_VERIFIED
    assert (
        verify_mod.proof_verdict(0, 0)[0]
        == verify_mod.VERDICT_NOT_FALSIFIABLE
    )
    assert verify_mod.proof_verdict(1, 1)[0] == verify_mod.VERDICT_FAILED
    assert (
        verify_mod.proof_verdict(0, 1)[0]
        == verify_mod.VERDICT_NOT_FALSIFIABLE
    )


def test_verify_output_tails_are_clamped(tmp_path):
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 605)
    long_out = "x" * (verify_mod.OUTPUT_TAIL_CHARS * 3) + "THE-END"

    result = _verify(
        store, pid,
        runner=FakeProofRunner(1, 0, main_output=long_out),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    tail = result["verify"]["main_output_tail"]
    assert len(tail) == verify_mod.OUTPUT_TAIL_CHARS
    assert tail.endswith("THE-END")  # the TAIL, not the head


# ── 3. Error path: recorded, never raised; cleanup regardless ────────────────


def test_verify_error_is_recorded_not_raised(tmp_path, caplog):
    store = tmp_path / "store"
    repo_root = tmp_path / "checkout"
    pid, _branch = _seed_applied(store, 606)
    main_path, branch_path = verify_mod.verify_worktree_paths(repo_root, pid)
    ops = FakeVerifyOps()
    runner = FakeProofRunner(
        1, 0, raise_on="main", exc=OSError("uv: command not found")
    )

    with caplog.at_level("ERROR", logger="edr.verify"):
        result = _verify(
            store, pid, runner=runner, ops=ops, repo_root=repo_root
        )

    block = result["verify"]
    assert result["verdict"] == block["verdict"] == verify_mod.VERDICT_ERROR
    assert block["main_exit"] is None  # the run never completed
    assert block["branch_exit"] is None  # never reached
    assert "running the proof on origin/main" in block["detail"]
    assert "uv: command not found" in block["detail"]
    # Cleanup ran for BOTH worktrees despite the failure.
    assert ("remove", main_path) in ops.calls
    assert ("remove", branch_path) in ops.calls
    # The error attempt is still evidence: persisted, status unchanged.
    on_disk, subdir = _reload(store, pid)
    assert on_disk.status == "applied"
    assert (
        on_disk.provenance.signals[verify_mod.VERIFY_SLOT]["verdict"]
        == verify_mod.VERDICT_ERROR
    )


def test_verify_worktree_cleanup_failure_is_swallowed(tmp_path, caplog):
    """A failed `git worktree remove` must not crash the verify or
    poison the verdict (best-effort cleanup, documented)."""
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 607)

    class RemoveFailsOps(FakeVerifyOps):
        def remove_worktree(self, repo_root, worktree_path):
            super().remove_worktree(repo_root, worktree_path)
            raise OSError("worktree is locked")

    with caplog.at_level("WARNING", logger="edr.verify"):
        result = _verify(
            store, pid,
            runner=FakeProofRunner(1, 0),
            ops=RemoveFailsOps(),
            repo_root=tmp_path / "checkout",
        )
    assert result["verdict"] == verify_mod.VERDICT_VERIFIED
    assert "could not remove worktree" in caplog.text


# ── 4. Refusals: machine-readable, zero mutation, nothing run ────────────────


def test_verify_refuses_when_branch_not_on_origin(tmp_path):
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 608)
    ops = FakeVerifyOps(branch_on_origin=False)
    runner = FakeProofRunner(1, 0)

    snapshot = _store_snapshot(store)
    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, pid, runner=runner, ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_BRANCH_NOT_ON_ORIGIN
    assert runner.calls == []  # no proof ran
    assert [c[0] for c in ops.calls] == ["fetch", "exists"]  # no worktrees
    assert _store_snapshot(store) == snapshot  # nothing mutated


def test_verify_refuses_non_applied_and_unknown(tmp_path):
    store = tmp_path / "store"
    ops = FakeVerifyOps()
    runner = FakeProofRunner(1, 0)

    # Unknown id.
    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, "edr-triage-gh-999", runner=runner, ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_FOUND

    # Approved but never dispatched (status approved_human, not applied).
    pid = _seed_approved(store, 609)
    snapshot = _store_snapshot(store)
    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, pid, runner=runner, ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_APPLIED
    assert "approved_human" in str(excinfo.value)

    assert runner.calls == []
    assert ops.calls == []  # refusals fired before ANY git operation
    assert _store_snapshot(store) == snapshot


def test_verify_refuses_malformed_actuator_block(tmp_path):
    store = tmp_path / "store"
    ops = FakeVerifyOps()
    runner = FakeProofRunner(1, 0)

    # Applied, but the actuator slot is not a dict.
    pid = _seed_approved(store, 610)
    proposal, subdir = _reload(store, pid)
    state_machine.transition(
        proposal, "applied", actor=dispatch_mod.DISPATCH_ACTOR
    )
    proposal.provenance.signals[dispatch_mod.ACTUATOR_SLOT] = "garbage"
    arbiter_store.move_proposal(proposal, store, from_subdir=subdir)
    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, pid, runner=runner, ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NO_ACTUATOR

    # Applied with a dict actuator block that carries no branch.
    pid2, _branch = _seed_applied(store, 611, branch="")
    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, pid2, runner=runner, ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NO_ACTUATOR
    assert "no branch" in str(excinfo.value)

    assert runner.calls == []
    assert ops.calls == []


def test_verify_refuses_when_no_declared_proof(tmp_path):
    """A corrupted/empty proof_artifact on the triage block refuses —
    the verifier only ever runs the DECLARED proof (§5.2)."""
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 612)
    proposal, subdir = _reload(store, pid)
    proposal.provenance.signals["triage"]["proof_artifact"] = ""
    arbiter_store.move_proposal(proposal, store, from_subdir=subdir)

    with pytest.raises(verify_mod.VerifyRefused) as excinfo:
        _verify(
            store, pid,
            runner=FakeProofRunner(1, 0),
            ops=FakeVerifyOps(),
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NO_PROOF


# ── 5. Independence: blind to the actuator's reasoning (§5.2) ────────────────


def test_verify_block_never_contains_worker_prose(tmp_path):
    """The actuator block carries worker-written text (notes +
    raw_report_tail); the verify record must contain NONE of it — the
    verifier checks the declared proof against clean worktrees and
    nothing else."""
    store = tmp_path / "store"
    pid, _branch = _seed_applied(store, 613)

    # Sanity: the worker prose IS on the proposal's actuator block.
    proposal, _ = _reload(store, pid)
    actuator = proposal.provenance.signals[dispatch_mod.ACTUATOR_SLOT]
    assert actuator["notes"] == WORKER_NOTES
    assert actuator["raw_report_tail"] == WORKER_RAW_TAIL

    result = _verify(
        store, pid,
        runner=FakeProofRunner(1, 0),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )

    # The ENTIRE verify record (returned and persisted), serialized: no
    # worker-written fragment anywhere.
    on_disk, _ = _reload(store, pid)
    for block in (
        result["verify"],
        on_disk.provenance.signals[verify_mod.VERIFY_SLOT],
    ):
        serialized = json.dumps(block)
        assert "WORKER-PROSE-MARKER" not in serialized
        assert "RAW-TAIL-MARKER" not in serialized


def test_verdict_ignores_worker_proof_claim(tmp_path):
    """A worker claiming proof_status 'green' with a proof that is
    green-on-main still gets not-falsifiable — and a worker claiming
    'red' with a proof that holds still gets verified. The claim is
    inert either way."""
    store = tmp_path / "store"
    pid_a, _ = _seed_applied(store, 614, proof_status="green")
    result_a = _verify(
        store, pid_a,
        runner=FakeProofRunner(0, 0),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result_a["verdict"] == verify_mod.VERDICT_NOT_FALSIFIABLE

    pid_b, _ = _seed_applied(store, 615, proof_status="red")
    result_b = _verify(
        store, pid_b,
        runner=FakeProofRunner(1, 0),
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result_b["verdict"] == verify_mod.VERDICT_VERIFIED


# ── 6. The close gate ────────────────────────────────────────────────────────


def _verified(store, number: int, **actuator_overrides) -> str:
    """An applied proposal with a real ``verified`` verify record."""
    pid, _branch = _seed_applied(store, number, **actuator_overrides)
    result = _verify(
        store, pid,
        runner=FakeProofRunner(1, 0),
        ops=FakeVerifyOps(),
        repo_root=Path(store).parent / "checkout",
    )
    assert result["verdict"] == verify_mod.VERDICT_VERIFIED
    return pid


def test_close_happy_path(tmp_path):
    store = tmp_path / "store"
    pid = _verified(store, 620)
    pr_query = FakePrQuery(HUMAN_MERGED_PR)

    proposal = verify_mod.close(store, pid, pr_query=pr_query)

    assert proposal.status == "succeeded"
    expected_pr = f"https://github.com/{REPO}/pull/{7000 + 620}"
    assert pr_query.calls == [expected_pr]

    # Authoritative on-disk state: succeeded in archived/, the close
    # evidence carries WHO merged, and the applied → succeeded edge went
    # through the real state machine with the bridge actor.
    on_disk, subdir = _reload(store, pid)
    assert on_disk.status == "succeeded"
    assert subdir == "archived"
    evidence = on_disk.provenance.signals[verify_mod.CLOSE_SLOT]
    assert evidence == {
        "schema": verify_mod.CLOSE_SCHEMA,
        "merged_at": "2026-06-12T10:00:00Z",
        "merged_by": "human-reviewer",
        "closed_at": evidence["closed_at"],  # ISO stamp, asserted below
    }
    assert evidence["closed_at"]
    assert [
        (e.from_status, e.to_status, e.actor) for e in on_disk.history
    ] == [
        ("pending", "approved_human", "user"),
        ("approved_human", "applied", dispatch_mod.DISPATCH_ACTOR),
        ("applied", "succeeded", verify_mod.CLOSE_ACTOR),
    ]
    assert on_disk.history[-1].reason == verify_mod.CLOSE_REASON
    # The verify evidence travels with the archived record.
    assert (
        on_disk.provenance.signals[verify_mod.VERIFY_SLOT]["verdict"]
        == verify_mod.VERDICT_VERIFIED
    )


def test_close_refuses_without_verified_verdict(tmp_path):
    store = tmp_path / "store"

    # No verify record at all.
    pid, _branch = _seed_applied(store, 621)
    snapshot = _store_snapshot(store)
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid, pr_query=FakePrQuery(HUMAN_MERGED_PR))
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_VERIFIED
    assert "no verify evidence" in str(excinfo.value)
    assert _store_snapshot(store) == snapshot

    # A verify record that is not 'verified'.
    pid2, _branch = _seed_applied(store, 622)
    result = _verify(
        store, pid2,
        runner=FakeProofRunner(1, 1),  # failed
        ops=FakeVerifyOps(),
        repo_root=tmp_path / "checkout",
    )
    assert result["verdict"] == verify_mod.VERDICT_FAILED
    snapshot = _store_snapshot(store)
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid2, pr_query=FakePrQuery(HUMAN_MERGED_PR))
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_VERIFIED
    assert "'failed'" in str(excinfo.value)
    assert _store_snapshot(store) == snapshot


def test_close_refuses_unmerged_or_unknown_pr(tmp_path):
    store = tmp_path / "store"
    pid = _verified(store, 623)
    snapshot = _store_snapshot(store)

    # PR still open — the human has not merged; no close.
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid, pr_query=FakePrQuery(OPEN_PR))
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_MERGED
    assert "'OPEN'" in str(excinfo.value)

    # Merged but no recorded merger — the evidence requires WHO.
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(
            store, pid, pr_query=FakePrQuery(MERGED_NO_MERGER_PR)
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_MERGED
    assert "mergedBy is unavailable" in str(excinfo.value)

    # Query failure → unknown, refused (never guessed).
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(
            store, pid,
            pr_query=FakePrQuery(OSError("gh: not logged in")),
        )
    assert excinfo.value.code == verify_mod.REFUSAL_PR_STATE_UNKNOWN
    assert "not logged in" in str(excinfo.value)

    assert _store_snapshot(store) == snapshot  # all three mutated NOTHING


def test_close_refuses_g3_violation(tmp_path):
    """A G3 VIOLATION (the worker session merged its own PR) can never
    close cleanly — even with a verified proof and a merged PR."""
    store = tmp_path / "store"
    pid = _verified(store, 624, g3_merge_check=dispatch_mod.G3_VIOLATION)
    snapshot = _store_snapshot(store)
    pr_query = FakePrQuery(HUMAN_MERGED_PR)

    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid, pr_query=pr_query)
    assert excinfo.value.code == verify_mod.REFUSAL_G3_VIOLATION
    assert pr_query.calls == []  # refused before any network query
    assert _store_snapshot(store) == snapshot


def test_close_refuses_wrong_status_and_missing_pr(tmp_path):
    store = tmp_path / "store"

    # Approved (never dispatched).
    pid = _seed_approved(store, 625)
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid, pr_query=FakePrQuery(HUMAN_MERGED_PR))
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_APPLIED

    # Unknown id.
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(
            store, "edr-triage-gh-999", pr_query=FakePrQuery(HUMAN_MERGED_PR)
        )
    assert excinfo.value.code == verify_mod.REFUSAL_NOT_FOUND

    # Verified but the actuator block recorded no PR.
    pid2 = _verified(store, 626, pr_url=None)
    with pytest.raises(verify_mod.CloseRefused) as excinfo:
        verify_mod.close(store, pid2, pr_query=FakePrQuery(HUMAN_MERGED_PR))
    assert excinfo.value.code == verify_mod.REFUSAL_NO_PR


# ── 7. CLI smoke (refusal paths — the CLI wires the REAL default seams) ──────


def test_cli_verify_refusal_exits_nonzero(tmp_path, capsys):
    rc = bridge_main(
        [
            "verify",
            "edr-triage-gh-999",
            "--shared-dir",
            str(tmp_path),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert verify_mod.REFUSAL_NOT_FOUND in capsys.readouterr().err


def test_cli_close_refusal_exits_nonzero(tmp_path, capsys):
    store = tmp_path / "store"
    pid = _seed_approved(store, 627)  # approved, never dispatched
    rc = bridge_main(["close", pid, "--shared-dir", str(store)])
    assert rc == 1
    assert verify_mod.REFUSAL_NOT_APPLIED in capsys.readouterr().err

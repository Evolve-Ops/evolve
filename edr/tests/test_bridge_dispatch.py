"""edr/tests/test_bridge_dispatch.py — P1.3b proof (dispatch + result capture).

The dispatch runner proven against the REAL imported libraries on
tmp_path stores. Proposals are never hand-crafted: each test seeds a
``dev_issue`` Signal through the real ``signals.store.observe()`` (via
the P1.1 adapter's normalizer), runs the real P1.2 triage, and approves
through the real P1.3a ``lifecycle.approve()`` — the shapes under test
are exactly what production writes.

ALL subprocess seams are FAKED (runner / pr_query / worktree_ops
injected at ``dispatch()``'s parameters — never
``patch("module.subprocess.run")``). No test launches a session,
creates a git worktree, or touches the network.

  1. happy path       — well-formed report + open PR → actuator block,
                        g3 clean, approved_human → applied (subdir +
                        history via the real lifecycle);
  2. G3 violation     — pr_query says MERGED → "VIOLATION" + ERROR log;
  3. unparseable      — report_status "unparseable", raw tail kept;
                        branch pushed → still applied (evidence rules);
  4. nothing pushed   — approval stands for re-dispatch, attempt
                        recorded (attempts counter survives);
  5. guards           — duplicate branch on origin / ineligible /
                        unknown id refuse with machine-readable codes
                        and zero seam calls past the refusal point;
  6. dry-run          — prints the brief, zero store mutation, takes NO
                        seams by construction;
  7. parse_report     — last-occurrence-wins, full-field + enum
                        validation.

Design: docs/edr/design-edr-2026-06-11.md §5.1 + §5.1.1 (the four v1
bridge decisions), §7 (G3 never-merge).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

# Real library imports — same modules the shipped daemons load.
import arbiter.store as arbiter_store
import signals.store as signals_store
from edr.adapters.github_issues import issue_to_signal_fields
from edr.bridge import brief as bridge_brief
from edr.bridge import dispatch as dispatch_mod
from edr.bridge import lifecycle
from edr.bridge.__main__ import main as bridge_main
from edr.triage import dev_issue as triage

PROOF = "pytest edr/tests/test_x.py::test_y goes red->green"
AGENT_BODY = f"It flakes on the second run.\nProof: {PROOF}\nMore context."
REPO = "example-org/example-repo"


# ── Seeding (adapter normalizer + real store + real triage + real approve) ───


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


# ── Fake seams (inject at dispatch()'s params; nothing ever launches) ────────


class FakeWorktreeOps:
    """Records every call; ``on_origin`` answers ls-remote in order
    (first = the duplicate guard, second = the post-session pushed
    check)."""

    def __init__(self, on_origin: list[bool]):
        self.on_origin = list(on_origin)
        self.calls: list[tuple] = []

    def fetch(self, repo_root):
        self.calls.append(("fetch", Path(repo_root)))

    def branch_exists_on_origin(self, repo_root, branch):
        self.calls.append(("exists", branch))
        return self.on_origin.pop(0)

    def add_worktree(self, repo_root, worktree_path, branch):
        self.calls.append(("add", Path(worktree_path), branch))


class FakeRunner:
    def __init__(self, output: str):
        self.output = output
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, brief: str, *, cwd) -> str:
        self.calls.append((brief, Path(cwd)))
        return self.output


class FakePrQuery:
    def __init__(self, result):
        self.result = result  # dict, or an Exception to raise
        self.calls: list[str] = []

    def __call__(self, pr_url: str) -> dict:
        self.calls.append(pr_url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _report(branch: str, pr_url: str, proof_status: str = "green") -> str:
    return (
        "I fixed the flaky test and the proof ran red->green.\n\n"
        f"branch: {branch}\n"
        f"pr_url: {pr_url}\n"
        f"proof_status: {proof_status}\n"
        "notes: proof ran red then green\n"
    )


OPEN_PR = {
    "state": "OPEN",
    "mergedAt": None,
    "mergedBy": None,
    "headRefName": "x",
}
MERGED_PR = {
    "state": "MERGED",
    "mergedAt": "2026-06-12T01:02:03Z",
    "mergedBy": {"login": "worker-session"},
    "headRefName": "x",
}


def _dispatch(shared_dir, pid, *, runner, pr_query, ops, repo_root):
    return dispatch_mod.dispatch(
        shared_dir,
        pid,
        repo=REPO,
        repo_root=repo_root,
        runner=runner,
        pr_query=pr_query,
        worktree_ops=ops,
    )


# ── 1. Happy path ────────────────────────────────────────────────────────────


def test_dispatch_happy_path(tmp_path):
    store = tmp_path / "store"
    repo_root = tmp_path / "checkout"
    pid = _seed_approved(store, 501)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(501, proposal.human_title)
    pr_url = f"https://github.com/{REPO}/pull/7501"

    ops = FakeWorktreeOps(on_origin=[False, True])  # guard clear; pushed
    runner = FakeRunner(_report(branch, pr_url))
    pr_query = FakePrQuery(OPEN_PR)

    result = _dispatch(
        store, pid, runner=runner, pr_query=pr_query, ops=ops,
        repo_root=repo_root,
    )

    # The session: exactly one launch, brief rendered by the REAL
    # brief_for (proof verbatim + G3 clause), cwd = the planned worktree.
    assert len(runner.calls) == 1
    brief_text, cwd = runner.calls[0]
    assert PROOF in brief_text
    assert "NEVER merge the PR" in brief_text
    assert cwd == dispatch_mod.worktree_path_for(repo_root, branch)
    # Worktree mechanics in order: fetch → guard → add → pushed-check.
    assert [c[0] for c in ops.calls] == ["fetch", "exists", "add", "exists"]
    assert ("add", cwd, branch) in ops.calls

    # The typed actuator result (§5.1.1.4).
    actuator = result["actuator"]
    assert actuator["schema"] == dispatch_mod.ACTUATOR_SCHEMA
    assert actuator["branch"] == branch
    assert actuator["branch_pushed"] is True
    assert actuator["pr_url"] == pr_url
    assert actuator["proof_status"] == "green"
    assert actuator["report_status"] == dispatch_mod.REPORT_PARSED
    assert actuator["g3_merge_check"] == dispatch_mod.G3_CLEAN
    assert actuator["runner"] == dispatch_mod.RUNNER_ID
    assert actuator["attempts"] == 1
    assert actuator["notes"] == "proof ran red then green"
    assert pr_query.calls == [pr_url]

    # Authoritative on-disk state: applied status, applied/ subdir, the
    # actuator block persisted, history grew through the real lifecycle
    # with the SYSTEM actor (dispatch is the system acting on a standing
    # human approval — never recorded as "user").
    on_disk, subdir = _reload(store, pid)
    assert result["status"] == on_disk.status == "applied"
    assert subdir == "applied"
    assert on_disk.provenance.signals["actuator"] == actuator
    assert [(e.from_status, e.to_status, e.actor) for e in on_disk.history] == [
        ("pending", "approved_human", "user"),
        ("approved_human", "applied", dispatch_mod.DISPATCH_ACTOR),
    ]
    assert on_disk.history[-1].reason == dispatch_mod.DISPATCH_REASON


# ── 2. The G3 post-hoc merge check ───────────────────────────────────────────


def test_g3_violation_flagged_loudly(tmp_path, caplog):
    """A merged PR is a protocol VIOLATION (§5.1.1.3): recorded in the
    actuator block AND logged at ERROR."""
    store = tmp_path / "store"
    pid = _seed_approved(store, 502)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(502, proposal.human_title)
    pr_url = f"https://github.com/{REPO}/pull/7502"

    with caplog.at_level("ERROR", logger="edr.bridge.dispatch"):
        result = _dispatch(
            store,
            pid,
            runner=FakeRunner(_report(branch, pr_url)),
            pr_query=FakePrQuery(MERGED_PR),
            ops=FakeWorktreeOps(on_origin=[False, True]),
            repo_root=tmp_path / "checkout",
        )

    actuator = result["actuator"]
    assert actuator["g3_merge_check"] == dispatch_mod.G3_VIOLATION
    assert "MERGED" in actuator["g3_detail"]
    assert "worker-session" in actuator["g3_detail"]
    assert "G3 VIOLATION" in caplog.text
    # The violation is evidence for review, not a rollback trigger: the
    # work exists and is merged — the proposal still lands in applied/
    # where the verify stage (P1.4) sees the flag.
    on_disk, _ = _reload(store, pid)
    assert on_disk.provenance.signals["actuator"]["g3_merge_check"] == (
        dispatch_mod.G3_VIOLATION
    )
    assert on_disk.status == "applied"


def test_g3_unknown_when_no_pr_or_query_fails(tmp_path):
    store = tmp_path / "store"
    pid = _seed_approved(store, 503)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(503, proposal.human_title)

    # Worker reported no PR ("none" per the brief's template).
    result = _dispatch(
        store,
        pid,
        runner=FakeRunner(_report(branch, "none")),
        pr_query=FakePrQuery(OPEN_PR),
        ops=FakeWorktreeOps(on_origin=[False, True]),
        repo_root=tmp_path / "checkout",
    )
    actuator = result["actuator"]
    assert actuator["pr_url"] is None
    assert actuator["g3_merge_check"] == dispatch_mod.G3_UNKNOWN
    assert "no PR reported" in actuator["g3_detail"]

    # Query blows up → unknown with the why, never a crash.
    pid2 = _seed_approved(store, 504)
    proposal2, _ = _reload(store, pid2)
    branch2 = bridge_brief.branch_for(504, proposal2.human_title)
    result2 = _dispatch(
        store,
        pid2,
        runner=FakeRunner(
            _report(branch2, f"https://github.com/{REPO}/pull/9")
        ),
        pr_query=FakePrQuery(OSError("gh: not logged in")),
        ops=FakeWorktreeOps(on_origin=[False, True]),
        repo_root=tmp_path / "checkout",
    )
    actuator2 = result2["actuator"]
    assert actuator2["g3_merge_check"] == dispatch_mod.G3_UNKNOWN
    assert "PR query failed" in actuator2["g3_detail"]
    assert "not logged in" in actuator2["g3_detail"]


# ── 3. Unparseable report (worker died / freelanced the format) ──────────────


def test_unparseable_report_with_pushed_branch_still_applies(tmp_path, caplog):
    """Evidence rules: the report is garbage but the branch IS on origin
    — something real exists to verify, so the proposal moves to applied
    with report_status 'unparseable' and the raw tail preserved (never
    crash, never guess)."""
    store = tmp_path / "store"
    pid = _seed_approved(store, 505)
    garbage = "I did some things and then the context window closed mid-sen"

    with caplog.at_level("WARNING", logger="edr.bridge.dispatch"):
        result = _dispatch(
            store,
            pid,
            runner=FakeRunner(garbage),
            pr_query=FakePrQuery(OPEN_PR),
            ops=FakeWorktreeOps(on_origin=[False, True]),
            repo_root=tmp_path / "checkout",
        )

    actuator = result["actuator"]
    assert actuator["report_status"] == dispatch_mod.REPORT_UNPARSEABLE
    assert actuator["raw_report_tail"] == garbage
    assert actuator["pr_url"] is None
    assert actuator["proof_status"] is None
    assert actuator["notes"] == ""
    assert actuator["g3_merge_check"] == dispatch_mod.G3_UNKNOWN
    assert "unparseable" in caplog.text

    on_disk, subdir = _reload(store, pid)
    assert on_disk.status == "applied"
    assert subdir == "applied"


def test_raw_report_tail_is_clamped(tmp_path):
    store = tmp_path / "store"
    pid = _seed_approved(store, 506)
    long_output = "x" * (dispatch_mod.RAW_TAIL_CHARS * 3) + "THE-END"

    result = _dispatch(
        store,
        pid,
        runner=FakeRunner(long_output),
        pr_query=FakePrQuery(OPEN_PR),
        ops=FakeWorktreeOps(on_origin=[False, True]),
        repo_root=tmp_path / "checkout",
    )
    tail = result["actuator"]["raw_report_tail"]
    assert len(tail) == dispatch_mod.RAW_TAIL_CHARS
    assert tail.endswith("THE-END")  # the TAIL, not the head


# ── 4. Nothing pushed → the approval stands (re-dispatch path) ───────────────


def test_no_pushed_branch_keeps_approval_and_records_attempt(
    tmp_path, caplog
):
    """The documented failure-path policy: no branch on origin after the
    session → nothing real exists to verify → status stays
    approved_human (re-dispatch is one command away) with the attempt
    recorded; a later successful dispatch applies with attempts=2."""
    store = tmp_path / "store"
    repo_root = tmp_path / "checkout"
    pid = _seed_approved(store, 507)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(507, proposal.human_title)

    with caplog.at_level("WARNING", logger="edr.bridge.dispatch"):
        result = _dispatch(
            store,
            pid,
            runner=FakeRunner("session died before the first push"),
            pr_query=FakePrQuery(OPEN_PR),
            ops=FakeWorktreeOps(on_origin=[False, False]),  # never pushed
            repo_root=repo_root,
        )

    assert result["status"] == "approved_human"
    assert result["actuator"]["branch_pushed"] is False
    assert result["actuator"]["attempts"] == 1
    assert "pushed NOTHING" in caplog.text

    on_disk, subdir = _reload(store, pid)
    assert on_disk.status == "approved_human"
    assert subdir == "pending"  # physically still in the dispatch lane
    assert len(on_disk.history) == 1  # approve only — no applied edge
    # Still dispatch-eligible: the duplicate guard passes again exactly
    # because nothing landed on origin.
    assert [p.id for p in lifecycle.eligible_for_dispatch(store)] == [pid]

    # Re-dispatch succeeds; the attempts counter survives the overwrite.
    result2 = _dispatch(
        store,
        pid,
        runner=FakeRunner(
            _report(branch, f"https://github.com/{REPO}/pull/7507")
        ),
        pr_query=FakePrQuery(OPEN_PR),
        ops=FakeWorktreeOps(on_origin=[False, True]),
        repo_root=repo_root,
    )
    assert result2["status"] == "applied"
    assert result2["actuator"]["attempts"] == 2
    on_disk2, subdir2 = _reload(store, pid)
    assert subdir2 == "applied"
    assert on_disk2.provenance.signals["actuator"]["attempts"] == 2


# ── 5. Guards: refusals are machine-readable, nothing launched ───────────────


def test_duplicate_branch_guard_refuses(tmp_path):
    store = tmp_path / "store"
    pid = _seed_approved(store, 508)
    ops = FakeWorktreeOps(on_origin=[True])  # branch already on origin
    runner = FakeRunner("should never run")

    snapshot = _store_snapshot(store)
    with pytest.raises(dispatch_mod.DispatchRefused) as excinfo:
        _dispatch(
            store,
            pid,
            runner=runner,
            pr_query=FakePrQuery(OPEN_PR),
            ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == dispatch_mod.REFUSAL_BRANCH_EXISTS
    assert runner.calls == []  # nothing launched
    assert [c[0] for c in ops.calls] == ["fetch", "exists"]  # no worktree
    assert _store_snapshot(store) == snapshot  # nothing mutated


def test_ineligible_and_unknown_refuse_before_any_seam_call(tmp_path):
    store = tmp_path / "store"
    ops = FakeWorktreeOps(on_origin=[])
    runner = FakeRunner("should never run")

    # Unknown id.
    with pytest.raises(dispatch_mod.DispatchRefused) as excinfo:
        _dispatch(
            store,
            "edr-triage-gh-999",
            runner=runner,
            pr_query=FakePrQuery(OPEN_PR),
            ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == dispatch_mod.REFUSAL_NOT_FOUND

    # Pending (never approved) — the eligibility RE-CHECK is load-bearing:
    # dispatch refuses even when handed an id directly.
    fields = issue_to_signal_fields(_issue(509))
    signals_store.observe(store, **fields)
    triage.run(store)
    pid = "edr-triage-gh-509"
    with pytest.raises(dispatch_mod.DispatchRefused) as excinfo:
        _dispatch(
            store,
            pid,
            runner=runner,
            pr_query=FakePrQuery(OPEN_PR),
            ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == dispatch_mod.REFUSAL_NOT_ELIGIBLE
    assert lifecycle.INELIGIBLE_STATUS in str(excinfo.value)

    # Approval revoked between list and dispatch (the re-check catches
    # the human's newer verdict).
    pid2 = _seed_approved(store, 510)
    proposal, subdir = _reload(store, pid2)
    import arbiter.state_machine as state_machine

    state_machine.transition(proposal, "rejected", actor="user")
    arbiter_store.move_proposal(proposal, store, from_subdir=subdir)
    with pytest.raises(dispatch_mod.DispatchRefused) as excinfo:
        _dispatch(
            store,
            pid2,
            runner=runner,
            pr_query=FakePrQuery(OPEN_PR),
            ops=ops,
            repo_root=tmp_path / "checkout",
        )
    assert excinfo.value.code == dispatch_mod.REFUSAL_NOT_ELIGIBLE

    assert runner.calls == []
    assert ops.calls == []  # refusals fired before ANY git operation


# ── 6. Dry run: plan + brief, zero mutation, zero seams ──────────────────────


def test_dry_run_prints_brief_and_mutates_nothing(tmp_path, capsys):
    store = tmp_path / "store"
    repo_root = tmp_path / "checkout"
    pid = _seed_approved(store, 511)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(511, proposal.human_title)

    snapshot = _store_snapshot(store)
    rc = bridge_main(
        [
            "dispatch",
            pid,
            "--shared-dir",
            str(store),
            "--repo",
            REPO,
            "--repo-root",
            str(repo_root),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The plan: eligibility verdict, deterministic branch, worktree path.
    assert "eligibility: eligible" in out
    assert branch in out
    assert str(dispatch_mod.worktree_path_for(repo_root, branch)) in out
    # The full brief, verbatim proof + G3 clause.
    assert PROOF in out
    assert "NEVER merge the PR" in out
    assert "nothing launched, nothing mutated" in out

    # ZERO mutation: every store file byte-identical.
    assert _store_snapshot(store) == snapshot
    # Zero seam calls is structural: dry_run takes NO seam parameters —
    # it cannot launch or shell out by construction.
    params = inspect.signature(dispatch_mod.dry_run).parameters
    assert "runner" not in params
    assert "pr_query" not in params
    assert "worktree_ops" not in params
    # And no worktree dir appeared.
    assert not (repo_root / ".edr").exists()


def test_dry_run_refuses_ineligible_with_verdict(tmp_path, capsys):
    store = tmp_path / "store"
    fields = issue_to_signal_fields(_issue(512))
    signals_store.observe(store, **fields)
    triage.run(store)  # pending — never approved

    rc = bridge_main(
        [
            "dispatch",
            "edr-triage-gh-512",
            "--shared-dir",
            str(store),
            "--repo",
            REPO,
            "--repo-root",
            str(tmp_path / "checkout"),
            "--dry-run",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert dispatch_mod.REFUSAL_NOT_ELIGIBLE in err
    assert lifecycle.INELIGIBLE_STATUS in err


# ── 7. parse_report unit behavior ────────────────────────────────────────────


def test_parse_report_last_occurrence_wins():
    """A worker that quotes the brief's template earlier in its output
    must not poison the parse — the FINAL block wins."""
    output = (
        "Per the brief I must end with:\n"
        "    branch: <placeholder>\n"
        "    pr_url: <the PR URL, or \"none\">\n"
        "    proof_status: green | red | not-run | scope-growth\n"
        "    notes: <one line>\n"
        "...work narrative...\n"
        "branch: edr/gh-1-real\n"
        "pr_url: https://github.com/o/r/pull/1\n"
        "proof_status: green\n"
        "notes: done\n"
    )
    report = dispatch_mod.parse_report(output)
    assert report == {
        "branch": "edr/gh-1-real",
        "pr_url": "https://github.com/o/r/pull/1",
        "proof_status": "green",
        "notes": "done",
    }


def test_parse_report_rejects_missing_or_invalid_fields():
    # Missing notes line → unparseable.
    assert (
        dispatch_mod.parse_report(
            "branch: b\npr_url: u\nproof_status: green\n"
        )
        is None
    )
    # proof_status outside PROOF_STATUS_VALUES → unparseable (this also
    # rejects a verbatim echo of the template's "green | red | ..." line).
    assert (
        dispatch_mod.parse_report(
            "branch: b\npr_url: u\nproof_status: amazing\nnotes: n"
        )
        is None
    )
    assert dispatch_mod.parse_report("") is None
    # Every documented proof status parses.
    for value in bridge_brief.PROOF_STATUS_VALUES:
        report = dispatch_mod.parse_report(
            f"branch: b\npr_url: none\nproof_status: {value}\nnotes: n"
        )
        assert report is not None and report["proof_status"] == value


def test_reported_branch_mismatch_is_logged_not_trusted(tmp_path, caplog):
    """The actuator block's branch is the PLANNED deterministic branch;
    a worker claiming a different one is logged at WARNING."""
    store = tmp_path / "store"
    pid = _seed_approved(store, 513)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(513, proposal.human_title)

    with caplog.at_level("WARNING", logger="edr.bridge.dispatch"):
        result = _dispatch(
            store,
            pid,
            runner=FakeRunner(
                _report("edr/gh-513-freelanced-elsewhere", "none")
            ),
            pr_query=FakePrQuery(OPEN_PR),
            ops=FakeWorktreeOps(on_origin=[False, True]),
            repo_root=tmp_path / "checkout",
        )
    assert result["actuator"]["branch"] == branch  # planned, not claimed
    assert "freelanced-elsewhere" in caplog.text


# ── CLI dispatch surface (real flow with refusal; happy path is covered
#    through dispatch() directly — the CLI wires default seams we must
#    never trigger in tests) ───────────────────────────────────────────────────


def test_cli_dispatch_refusal_exits_nonzero(tmp_path, capsys):
    rc = bridge_main(
        [
            "dispatch",
            "edr-triage-gh-999",
            "--shared-dir",
            str(tmp_path),
            "--repo",
            REPO,
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert dispatch_mod.REFUSAL_NOT_FOUND in capsys.readouterr().err


def test_actuator_block_is_json_round_trippable(tmp_path):
    """The persisted actuator block survives the store's JSON round-trip
    byte-faithfully (P1.4 reads it from disk, not from memory)."""
    store = tmp_path / "store"
    pid = _seed_approved(store, 514)
    proposal, _ = _reload(store, pid)
    branch = bridge_brief.branch_for(514, proposal.human_title)

    result = _dispatch(
        store,
        pid,
        runner=FakeRunner(
            _report(branch, f"https://github.com/{REPO}/pull/14")
        ),
        pr_query=FakePrQuery(OPEN_PR),
        ops=FakeWorktreeOps(on_origin=[False, True]),
        repo_root=tmp_path / "checkout",
    )
    raw = json.loads(
        (store / "proposals" / "applied" / f"{pid}.json").read_text()
    )
    assert raw["provenance"]["signals"]["actuator"] == result["actuator"]
    assert raw["status"] == "applied"

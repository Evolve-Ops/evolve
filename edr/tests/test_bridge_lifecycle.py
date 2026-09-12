"""edr/tests/test_bridge_lifecycle.py — P1.3a bridge proof (lifecycle + brief).

The actuator bridge's approval gate, eligibility filter, and dispatch
brief, proven against the REAL imported libraries on tmp_path stores.
Proposals are never hand-crafted: each test seeds a ``dev_issue`` Signal
through the real ``signals.store.observe()`` (using the P1.1 adapter's
own normalizer) and runs the real P1.2 triage ``run()`` — so the shapes
under test are exactly what production triage writes. No network, no
subprocess, no dispatching.

  1. approve         — pending → approved_human via the real lifecycle
                       (status + subdir + append-only history);
  2. refusals        — not-found / already-approved / archived /
                       human-routed / proof-less, each with NO mutation;
  3. eligibility     — pending not eligible; approved human-routed not
                       eligible; approved agent-able + proof eligible;
                       missing/malformed triage block defensively out;
  4. brief           — proof artifact VERBATIM, the G3 never-merge
                       clause, the deterministic branch name, the typed
                       report-back block, slug rules;
  5. CLI smoke       — list / approve / brief happy paths + refusal
                       exit codes via main(argv).

Design: internal/edr/design-edr-2026-06-11.md §5 (bridge contract), §5.2
(proof discipline), §5.3 (R1), §7 (G3 never-merge).
"""

from __future__ import annotations

import pytest

# Real library imports — same modules the shipped daemons load.
import arbiter.state_machine as state_machine
import arbiter.store as arbiter_store
import signals.store as signals_store
from edr.adapters.github_issues import issue_to_signal_fields
from edr.bridge import brief as bridge_brief
from edr.bridge import lifecycle
from edr.bridge.__main__ import main as bridge_main
from edr.triage import dev_issue as triage

PROOF = "pytest edr/tests/test_x.py::test_y goes red->green"
AGENT_BODY = f"It flakes on the second run.\nProof: {PROOF}\nMore context."
REPO = "example-org/example-repo"


# ── Seeding helpers (adapter normalizer + real store + real triage) ──────────


def _issue(number: int, **overrides) -> dict:
    """One fake GitHub issue in `gh issue list --json` shape."""
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


def _seed_triaged(shared_dir, number: int, **issue_overrides) -> str:
    """Signal → triage → one stored Proposal; returns its id.

    The default issue shape is agent-able (edr:agent-able label + a
    ``Proof:`` body line); override ``labels``/``body`` for the
    human-routed variants.
    """
    fields = issue_to_signal_fields(_issue(number, **issue_overrides))
    signals_store.observe(shared_dir, **fields)
    triage.run(shared_dir)
    return f"edr-triage-gh-{number}"


def _reload(shared_dir, proposal_id: str):
    """(proposal, subdir) re-read from disk — the authoritative state."""
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    assert located is not None
    proposal, _path, subdir = located
    return proposal, subdir


def _rewrite_in_place(shared_dir, proposal) -> None:
    """Persist an edited proposal through the real store helper."""
    located = arbiter_store.find_proposal(shared_dir, proposal.id)
    assert located is not None
    arbiter_store.move_proposal(proposal, shared_dir, from_subdir=located[2])


# ── 1. approve: pending → approved_human through the real lifecycle ──────────


def test_approve_moves_pending_to_approved_human(tmp_path):
    pid = _seed_triaged(tmp_path, 101)

    approved = lifecycle.approve(tmp_path, pid)
    assert approved.status == "approved_human"

    # Authoritative on-disk state: status approved_human; subdir stays
    # pending/ (the real _STATUS_TO_SUBDIR maps both approved_* statuses
    # there — status field authoritative, subdir is the physical index).
    on_disk, subdir = _reload(tmp_path, pid)
    assert on_disk.status == "approved_human"
    assert subdir == "pending"

    # Append-only history via the real state machine, human actor.
    assert len(on_disk.history) == 1
    entry = on_disk.history[0]
    assert (entry.from_status, entry.to_status) == ("pending", "approved_human")
    assert entry.actor == lifecycle.APPROVAL_ACTOR == "user"
    assert entry.reason == lifecycle.APPROVAL_REASON


def test_approve_refuses_unknown_proposal(tmp_path):
    with pytest.raises(lifecycle.ApprovalRefused) as excinfo:
        lifecycle.approve(tmp_path, "edr-triage-gh-999")
    assert excinfo.value.code == lifecycle.REFUSAL_NOT_FOUND
    # No store mutation: nothing was created anywhere.
    assert arbiter_store.find_proposal(tmp_path, "edr-triage-gh-999") is None


def test_approve_refuses_already_approved(tmp_path):
    """Approval is a one-way act on a *pending* item — re-approving an
    approved proposal is refused, and the first approval's record
    (status + single history entry) is untouched."""
    pid = _seed_triaged(tmp_path, 102)
    lifecycle.approve(tmp_path, pid)

    with pytest.raises(lifecycle.ApprovalRefused) as excinfo:
        lifecycle.approve(tmp_path, pid)
    assert excinfo.value.code == lifecycle.REFUSAL_NOT_APPROVABLE

    on_disk, _subdir = _reload(tmp_path, pid)
    assert on_disk.status == "approved_human"
    assert len(on_disk.history) == 1


def test_approve_refuses_archived(tmp_path):
    """A rejected (archived) proposal is not resurrectable via approve —
    the human's terminal verdict stands."""
    pid = _seed_triaged(tmp_path, 103)
    proposal, subdir = _reload(tmp_path, pid)
    state_machine.transition(proposal, "rejected", actor="user")
    arbiter_store.move_proposal(proposal, tmp_path, from_subdir=subdir)
    assert _reload(tmp_path, pid)[1] == "archived"

    with pytest.raises(lifecycle.ApprovalRefused) as excinfo:
        lifecycle.approve(tmp_path, pid)
    assert excinfo.value.code == lifecycle.REFUSAL_NOT_APPROVABLE
    on_disk, subdir_after = _reload(tmp_path, pid)
    assert on_disk.status == "rejected"
    assert subdir_after == "archived"


def test_approve_refuses_human_routed_without_mutation(tmp_path):
    """The load-bearing rule: a human cannot 'approve' a human-routed
    item into the agent lane — that's a triage re-run after relabeling,
    not an approval."""
    pid = _seed_triaged(tmp_path, 104, labels=[{"name": "bug"}])
    before, _ = _reload(tmp_path, pid)
    assert before.provenance.signals["triage"]["agent_able"] is False

    with pytest.raises(lifecycle.ApprovalRefused) as excinfo:
        lifecycle.approve(tmp_path, pid)
    assert excinfo.value.code == lifecycle.REFUSAL_HUMAN_ROUTED
    assert "re-run" in str(excinfo.value)  # points at the right surface

    # NO mutation: status, subdir, and history all untouched.
    after, subdir = _reload(tmp_path, pid)
    assert after.status == "pending"
    assert subdir == "pending"
    assert after.history == []


def test_approve_refuses_agent_able_without_proof(tmp_path):
    """Defensive gate: triage never emits agent_able=True without a
    proof, but if such a record existed, approving it would create a
    permanently un-dispatchable item — refused (§5.2)."""
    pid = _seed_triaged(tmp_path, 105)
    proposal, _ = _reload(tmp_path, pid)
    proposal.provenance.signals["triage"]["proof_artifact"] = None
    _rewrite_in_place(tmp_path, proposal)

    with pytest.raises(lifecycle.ApprovalRefused) as excinfo:
        lifecycle.approve(tmp_path, pid)
    assert excinfo.value.code == lifecycle.REFUSAL_NO_PROOF
    after, _ = _reload(tmp_path, pid)
    assert after.status == "pending"


# ── 2. Dispatch eligibility ──────────────────────────────────────────────────


def test_pending_agent_able_is_not_eligible(tmp_path):
    """Triage alone never feeds dispatch — the human gate is load-bearing."""
    _seed_triaged(tmp_path, 201)
    assert lifecycle.eligible_for_dispatch(tmp_path) == []


def test_approved_agent_able_with_proof_is_eligible(tmp_path):
    pid = _seed_triaged(tmp_path, 202)
    lifecycle.approve(tmp_path, pid)

    eligible = lifecycle.eligible_for_dispatch(tmp_path)
    assert [p.id for p in eligible] == [pid]
    ok, reason = lifecycle.dispatch_eligibility(eligible[0])
    assert ok is True
    assert reason == lifecycle.ELIGIBLE_OK


def test_approved_human_routed_is_not_eligible(tmp_path, caplog):
    """Even with approved_* status (forced through the real state
    machine), a human-routed verdict never dispatches — and the
    surprising approved-but-ineligible case is logged."""
    pid = _seed_triaged(tmp_path, 203, labels=[{"name": "bug"}])
    proposal, subdir = _reload(tmp_path, pid)
    state_machine.transition(proposal, "approved_human", actor="user")
    arbiter_store.move_proposal(proposal, tmp_path, from_subdir=subdir)

    with caplog.at_level("WARNING", logger="edr.bridge.lifecycle"):
        assert lifecycle.eligible_for_dispatch(tmp_path) == []
    assert lifecycle.INELIGIBLE_HUMAN_ROUTED in caplog.text


def test_missing_or_malformed_triage_block_is_not_eligible(tmp_path, caplog):
    """Defensive: an approved proposal whose triage block vanished or
    rotted is NOT eligible, with the reason logged."""
    pid_missing = _seed_triaged(tmp_path, 204)
    lifecycle.approve(tmp_path, pid_missing)
    proposal, _ = _reload(tmp_path, pid_missing)
    del proposal.provenance.signals["triage"]
    _rewrite_in_place(tmp_path, proposal)

    pid_malformed = _seed_triaged(tmp_path, 205)
    lifecycle.approve(tmp_path, pid_malformed)
    proposal, _ = _reload(tmp_path, pid_malformed)
    proposal.provenance.signals["triage"] = "not a dict"
    _rewrite_in_place(tmp_path, proposal)

    with caplog.at_level("WARNING", logger="edr.bridge.lifecycle"):
        assert lifecycle.eligible_for_dispatch(tmp_path) == []
    assert caplog.text.count(lifecycle.INELIGIBLE_NO_TRIAGE) == 2


def test_dispatch_eligibility_reasons_are_machine_readable(tmp_path):
    """The pure verdict exposes WHICH gate failed as data constants."""
    pid = _seed_triaged(tmp_path, 206)
    proposal, _ = _reload(tmp_path, pid)

    assert lifecycle.dispatch_eligibility(proposal) == (
        False,
        lifecycle.INELIGIBLE_STATUS,
    )

    proposal.status = "approved_human"  # in-memory only — pure function
    proposal.provenance.signals["triage"]["proof_artifact"] = "   "
    assert lifecycle.dispatch_eligibility(proposal) == (
        False,
        lifecycle.INELIGIBLE_NO_PROOF,
    )


# ── 3. The dispatch brief ────────────────────────────────────────────────────


def test_brief_carries_the_dispatch_contract(tmp_path):
    pid = _seed_triaged(tmp_path, 301)
    lifecycle.approve(tmp_path, pid)
    proposal, _ = _reload(tmp_path, pid)

    text = bridge_brief.brief_for(proposal, repo=REPO)

    # The proof artifact appears VERBATIM — the falsifiable close
    # condition (§5.2) must survive into the worker's instructions.
    assert PROOF in text
    # G3: never merge, stated emphatically, including the --auto ban.
    assert bridge_brief.G3_NEVER_MERGE.split(".")[0] in text  # "NEVER merge..."
    assert "NEVER merge the PR" in text
    assert "`--auto`" in text
    # The deterministic branch name.
    assert f"edr/gh-301-{bridge_brief.slug_for(proposal.human_title)}" in text
    # Issue reference + the read-the-full-issue instruction.
    assert f"gh issue view 301 --repo {REPO} --comments" in text
    assert f"https://github.com/{REPO}/issues/301" in text
    # Anti-wedge + one-bite + scrub clauses (the module-level contract).
    assert "--allow-empty" in text
    assert "scope-growth" in text
    assert "no operator names" in text
    # PR footer, exactly.
    assert bridge_brief.PR_FOOTER in text
    # The typed report-back block: every field present.
    for field in bridge_brief.REPORT_FIELDS:
        assert f"{field}:" in text
    # bug-classified work gets the red→green discipline.
    assert "FAILING test FIRST" in text


def test_brief_red_green_discipline_only_for_bugs(tmp_path):
    """The red→green line is classification-keyed data, not boilerplate."""
    pid = _seed_triaged(
        tmp_path,
        302,
        labels=[{"name": "enhancement"}, {"name": "edr:agent-able"}],
    )
    proposal, _ = _reload(tmp_path, pid)
    text = bridge_brief.brief_for(proposal, repo=REPO)
    assert "FAILING test FIRST" not in text
    assert PROOF in text


def test_brief_refuses_human_routed(tmp_path):
    pid = _seed_triaged(tmp_path, 303, labels=[{"name": "bug"}])
    proposal, _ = _reload(tmp_path, pid)
    with pytest.raises(bridge_brief.BriefUnavailable):
        bridge_brief.brief_for(proposal, repo=REPO)


def test_slug_rules():
    """Lowercase-hyphenated, triage prefix stripped, ≤40 chars, no
    trailing hyphen, deterministic fallback."""
    assert (
        bridge_brief.slug_for("EDR triage: Flaky test in the scheduler")
        == "flaky-test-in-the-scheduler"
    )
    long_title = "EDR triage: " + "word " * 20
    slug = bridge_brief.slug_for(long_title)
    assert len(slug) <= bridge_brief.SLUG_MAX_LEN
    assert not slug.endswith("-")
    assert bridge_brief.slug_for("") == bridge_brief.SLUG_FALLBACK
    assert (
        bridge_brief.branch_for(42, "EDR triage: Fix the Thing!")
        == "edr/gh-42-fix-the-thing"
    )


# ── 4. CLI smoke (main(argv) — no subprocess, no network) ────────────────────


def test_cli_approve_list_brief_happy_path(tmp_path, capsys):
    pid = _seed_triaged(tmp_path, 401)

    rc = bridge_main(["approve", pid, "--shared-dir", str(tmp_path)])
    assert rc == 0
    assert f"approved: {pid} pending -> approved_human" in (
        capsys.readouterr().out
    )

    rc = bridge_main(["list", "--shared-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert pid in out
    assert "issue=#401" in out
    assert PROOF in out
    assert "bridge: eligible 1" in out

    rc = bridge_main(
        ["brief", pid, "--shared-dir", str(tmp_path), "--repo", REPO]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert PROOF in out
    assert "NEVER merge the PR" in out


def test_cli_brief_previews_pending_proposals(tmp_path, capsys):
    """brief is the dry-run surface: it renders for a PENDING agent-able
    proposal so the operator reads the exact instructions before
    approving. Printing a brief dispatches nothing."""
    pid = _seed_triaged(tmp_path, 402)

    rc = bridge_main(
        ["brief", pid, "--shared-dir", str(tmp_path), "--repo", REPO]
    )
    assert rc == 0
    assert PROOF in capsys.readouterr().out
    # Still pending, still not eligible — preview did not approve.
    assert _reload(tmp_path, pid)[0].status == "pending"
    assert lifecycle.eligible_for_dispatch(tmp_path) == []


def test_cli_refusals_exit_nonzero(tmp_path, capsys):
    # approve: unknown id.
    rc = bridge_main(
        ["approve", "edr-triage-gh-999", "--shared-dir", str(tmp_path)]
    )
    assert rc == 1
    assert lifecycle.REFUSAL_NOT_FOUND in capsys.readouterr().err

    # brief: human-routed proposal.
    pid = _seed_triaged(tmp_path, 403, labels=[{"name": "bug"}])
    rc = bridge_main(
        ["brief", pid, "--shared-dir", str(tmp_path), "--repo", REPO]
    )
    assert rc == 1
    assert "human-routed" in capsys.readouterr().err

    # brief: archived proposal is outside the dispatch lifecycle.
    pid2 = _seed_triaged(tmp_path, 404)
    proposal, subdir = _reload(tmp_path, pid2)
    state_machine.transition(proposal, "rejected", actor="user")
    arbiter_store.move_proposal(proposal, tmp_path, from_subdir=subdir)
    rc = bridge_main(
        ["brief", pid2, "--shared-dir", str(tmp_path), "--repo", REPO]
    )
    assert rc == 1
    assert "rejected" in capsys.readouterr().err

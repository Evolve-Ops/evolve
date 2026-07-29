"""edr/tests/test_triage_dev_issue.py — P1.2 triage-generator proof.

The triage generator reads firing ``dev_issue`` Signals and writes routed
Proposals through the REAL imported ``arbiter.store``. These tests prove
the load-bearing properties on tmp_path stores only — Signals are seeded
through the real ``signals.store.observe()`` using the P1.1 adapter's own
normalizer (``issue_to_signal_fields``), so the field shapes are exactly
what production triage will see. No network, no LLM.

  1. round-trip      — Signal → Proposal with motivating_signals linkage
                       BOTH directions + machine-readable triage block;
  2. defaults-human  — unlabeled bug → agent_able=False (design §6.1);
  3. agent-able path — edr:agent-able label + Proof: line → True, proof
                       extracted verbatim;
  4. label, no proof — human + the documented reason;
  5. classification / severity / urgency / routing DATA tables;
  6. idempotency     — re-runs write nothing; archived (rejected) never
                       recreated;
  7. signal purity   — triage never mutates the Signal's severity;
  8. CLI summary line contract (P1.3 / operators read this).

Design: docs/edr/design-edr-2026-06-11.md §6 + §6.1;
docs/edr/design-edr-tiered-oversight-2026-06-11.md §4 Move 1.
"""

from __future__ import annotations

# Real library imports — same modules the shipped daemons load.
import arbiter.store as arbiter_store
import signals.store as signals_store
from edr.adapters.github_issues import issue_to_signal_fields
from edr.triage import dev_issue as triage


# ── Seeding helpers (adapter field shapes through the real store) ────────────


def _issue(number: int = 101, **overrides) -> dict:
    """One fake GitHub issue in `gh issue list --json` shape."""
    issue = {
        "number": number,
        "title": f"Flaky test in the scheduler suite ({number})",
        "body": "Repro: run the suite twice; second run fails.",
        "labels": [{"name": "bug"}, {"name": "ci"}],
        "author": {"login": "contributor-a"},
        "createdAt": "2026-06-10T00:00:00Z",
        "updatedAt": "2026-06-11T00:00:00Z",
        "url": f"https://github.com/example-org/example-repo/issues/{number}",
        "state": "OPEN",
    }
    issue.update(overrides)
    return issue


def _seed_signal(shared_dir, **issue_overrides):
    """Observe one issue-shaped Signal via the REAL adapter normalizer +
    real store — the exact field shapes production triage consumes."""
    fields = issue_to_signal_fields(_issue(**issue_overrides))
    return signals_store.observe(shared_dir, **fields)


def _labels(*names: str) -> list[dict]:
    return [{"name": name} for name in names]


def _triage_block(shared_dir, proposal_id: str) -> dict:
    """The machine-readable verdict from the STORED proposal (the P1.3
    parse surface): provenance.signals['triage'] after a disk round-trip."""
    located = arbiter_store.find_proposal(shared_dir, proposal_id)
    assert located is not None
    restored, _path, _subdir = located
    return restored.provenance.signals["triage"]


# ── 1. Round-trip: Signal → Proposal, linkage both directions ────────────────


def test_signal_to_proposal_round_trip(tmp_path):
    sig = _seed_signal(tmp_path, number=101)
    summary = triage.run(tmp_path)

    assert summary == {
        "signals": 1,
        "proposals_written": 1,
        "skipped_existing": 0,
        "agent_able": 0,
        "human": 1,
    }

    located = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-101")
    assert located is not None
    proposal, path, subdir = located
    assert subdir == "pending"
    assert path.exists()

    # Identity constants (the P1.2 contract).
    assert proposal.bot_id == "edr"
    assert proposal.generator_id == "edr_triage_dev_issue"
    assert proposal.dimension == "dev_remediation"
    assert proposal.provenance.technique == "edr_triage"

    # Forward link: the Proposal cites the motivating Signal.
    assert proposal.motivating_signals == [sig.id]
    assert proposal.trigger_observations == [sig.id]

    # Inverse link: write_proposal maintained the Signal backref.
    located_sig = signals_store.find_signal(tmp_path, sig.id)
    assert located_sig is not None
    sig_after, _spath, _ssubdir = located_sig
    assert "edr-triage-gh-101" in sig_after.motivated_proposals

    # Machine-readable triage block — every documented key present.
    block = proposal.provenance.signals["triage"]
    assert block["schema"] == 1
    assert block["classification"] == "bug"
    assert block["severity"] == "warn"
    assert block["agent_able"] is False
    assert block["route"] == "unrouted (operator)"
    assert block["proof_artifact"] is None
    assert block["reason"] == triage.REASON_DEFAULT_HUMAN
    assert block["issue_number"] == 101
    assert block["issue_url"].endswith("/issues/101")
    assert block["labels"] == ["bug", "ci"]


# ── 2. The agent-able verdict DEFAULTS TO HUMAN ──────────────────────────────


def test_unlabeled_bug_defaults_to_human(tmp_path):
    """An ordinary bug — even with a Proof: line in the body — routes
    human without the explicit human-applied edr:agent-able label."""
    _seed_signal(
        tmp_path,
        number=201,
        labels=_labels("bug"),
        body="Proof: pytest edr/tests/test_x.py::test_y goes red->green",
    )
    summary = triage.run(tmp_path)

    assert summary["agent_able"] == 0
    assert summary["human"] == 1
    block = _triage_block(tmp_path, "edr-triage-gh-201")
    assert block["agent_able"] is False
    assert block["proof_artifact"] is None
    assert block["reason"] == triage.REASON_DEFAULT_HUMAN


def test_agent_able_happy_path_label_plus_proof(tmp_path):
    _seed_signal(
        tmp_path,
        number=202,
        labels=_labels("bug", "edr:agent-able"),
        body=(
            "The scheduler test flakes on the second run.\n"
            "Proof: pytest packages/admin/tests/test_x.py::test_y "
            "goes red->green\n"
            "More context below."
        ),
    )
    summary = triage.run(tmp_path)

    assert summary["agent_able"] == 1
    assert summary["human"] == 0
    block = _triage_block(tmp_path, "edr-triage-gh-202")
    assert block["agent_able"] is True
    # Proof payload extracted verbatim (the P1.3 dispatch contract).
    assert block["proof_artifact"] == (
        "pytest packages/admin/tests/test_x.py::test_y goes red->green"
    )
    assert block["reason"] == triage.REASON_AGENT_ABLE


def test_label_without_proof_routes_human_with_reason(tmp_path):
    _seed_signal(
        tmp_path,
        number=203,
        labels=_labels("bug", "edr:agent-able"),
        body="Someone should fix this. No proof declared.",
    )
    summary = triage.run(tmp_path)

    assert summary["agent_able"] == 0
    assert summary["human"] == 1
    block = _triage_block(tmp_path, "edr-triage-gh-203")
    assert block["agent_able"] is False
    assert block["proof_artifact"] is None
    assert block["reason"] == triage.REASON_LABEL_NO_PROOF


def test_empty_proof_payload_is_no_declaration(tmp_path):
    """`Proof:` with nothing after it is not falsifiable — human."""
    _seed_signal(
        tmp_path,
        number=204,
        labels=_labels("edr:agent-able"),
        body="Proof:\n(blank)",
    )
    triage.run(tmp_path)
    block = _triage_block(tmp_path, "edr-triage-gh-204")
    assert block["agent_able"] is False
    assert block["reason"] == triage.REASON_LABEL_NO_PROOF


# ── 3. Classification / severity / urgency tables ────────────────────────────


def test_noise_classification_and_hygiene_urgency(tmp_path):
    _seed_signal(tmp_path, number=301, labels=_labels("duplicate"))
    triage.run(tmp_path)

    block = _triage_block(tmp_path, "edr-triage-gh-301")
    assert block["classification"] == "noise"
    located = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-301")
    assert located is not None
    assert located[0].urgency == "hygiene"


def test_noise_precedence_beats_bug():
    """An issue marked duplicate stays noise even when also labeled bug
    (documented precedence: noise > bug > feature)."""
    assert triage.classify(["bug", "duplicate"]) == "noise"


def test_feature_and_default_classification():
    assert triage.classify(["enhancement"]) == "feature"
    assert triage.classify(["question"]) == "needs-classification"
    assert triage.classify([]) == "needs-classification"


def test_severity_mapping_table(tmp_path):
    # security → alert + the security_critical urgency override.
    _seed_signal(tmp_path, number=302, labels=_labels("bug", "security"))
    # plain bug → warn, improvement.
    _seed_signal(tmp_path, number=303, labels=_labels("bug"))
    # unlabeled → conservative info floor.
    _seed_signal(tmp_path, number=304, labels=[])
    triage.run(tmp_path)

    sec = _triage_block(tmp_path, "edr-triage-gh-302")
    assert sec["severity"] == "alert"
    located = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-302")
    assert located is not None
    assert located[0].urgency == "security_critical"

    assert _triage_block(tmp_path, "edr-triage-gh-303")["severity"] == "warn"
    assert _triage_block(tmp_path, "edr-triage-gh-304")["severity"] == "info"


def test_triage_never_mutates_the_signal_severity(tmp_path):
    """Severity judgment lands in the Proposal; the Signal keeps the
    adapter's no-judgment info floor."""
    sig = _seed_signal(tmp_path, number=305, labels=_labels("security"))
    triage.run(tmp_path)

    located_sig = signals_store.find_signal(tmp_path, sig.id)
    assert located_sig is not None
    assert located_sig[0].severity == "info"
    assert _triage_block(tmp_path, "edr-triage-gh-305")["severity"] == "alert"


# ── 4. Routing table ─────────────────────────────────────────────────────────


def test_routing_label_hit(tmp_path):
    _seed_signal(tmp_path, number=401, labels=_labels("bug", "sudoers"))
    triage.run(tmp_path)
    assert (
        _triage_block(tmp_path, "edr-triage-gh-401")["route"]
        == "Diligence remediation (roadmap 80→100)"
    )


def test_routing_title_token_hit(tmp_path):
    _seed_signal(
        tmp_path,
        number=402,
        title="Add-bot wizard drops the consent step",
        labels=[],
    )
    triage.run(tmp_path)
    assert _triage_block(tmp_path, "edr-triage-gh-402")["route"] == (
        "User-value roadmap (functioning → astoundingly useful, U0–U5)"
    )


def test_routing_default_route(tmp_path):
    _seed_signal(
        tmp_path,
        number=403,
        title="Digest email renders twice on Sundays",
        labels=_labels("bug"),
    )
    triage.run(tmp_path)
    assert (
        _triage_block(tmp_path, "edr-triage-gh-403")["route"]
        == "unrouted (operator)"
    )


def test_routing_hyphen_token_multi_pod():
    """Title tokens keep internal hyphens, so 'multi-pod' matches as one
    deliberate token (a bare 'pod' substring would over-match)."""
    assert (
        triage.route_for([], "Multi-pod deposit route 500s")
        == 'Multi-pod hub ("Pods")'
    )
    assert triage.route_for([], "podcast support") == "unrouted (operator)"


# ── 5. Idempotency ───────────────────────────────────────────────────────────


def test_second_run_writes_nothing(tmp_path):
    _seed_signal(tmp_path, number=501)
    _seed_signal(tmp_path, number=502, labels=_labels("enhancement"))
    first = triage.run(tmp_path)
    second = triage.run(tmp_path)

    assert first["proposals_written"] == 2
    assert second == {
        "signals": 2,
        "proposals_written": 0,
        "skipped_existing": 2,
        "agent_able": 0,
        "human": 0,
    }
    # Still exactly two proposals in pending — no siblings minted.
    pending = list(arbiter_store.iter_proposals(tmp_path))
    assert sorted(p.id for p in pending) == [
        "edr-triage-gh-501",
        "edr-triage-gh-502",
    ]


def test_archived_rejected_proposal_is_not_recreated(tmp_path):
    """A human's rejection stands: the Signal may still fire, but triage
    never re-mints a proposal whose id exists in archived/."""
    _seed_signal(tmp_path, number=503)
    triage.run(tmp_path)

    located = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-503")
    assert located is not None
    proposal, _path, subdir = located
    proposal.status = "rejected"
    arbiter_store.move_proposal(proposal, tmp_path, from_subdir=subdir)
    relocated = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-503")
    assert relocated is not None and relocated[2] == "archived"

    summary = triage.run(tmp_path)
    assert summary["proposals_written"] == 0
    assert summary["skipped_existing"] == 1
    # Still archived, still rejected — not resurrected into pending/.
    final = arbiter_store.find_proposal(tmp_path, "edr-triage-gh-503")
    assert final is not None
    assert final[2] == "archived"
    assert final[0].status == "rejected"


def test_only_firing_dev_issue_signals_are_triaged(tmp_path):
    """Resolved Signals and foreign types are out of scope."""
    _seed_signal(tmp_path, number=504)
    # A foreign-type firing Signal in the same store.
    signals_store.observe(
        tmp_path,
        signature="edr:other_type:x",
        producer="edr_other",
        type="dev_ci_failure",
        flavor="maintenance",
        severity="info",
        scope="pod",
        title="A CI failure signal (P2 territory)",
        body="",
        details={},
    )
    summary = triage.run(tmp_path)
    assert summary["signals"] == 1
    assert summary["proposals_written"] == 1


# ── 6. CLI contract ──────────────────────────────────────────────────────────


def test_cli_summary_line(tmp_path, capsys):
    _seed_signal(tmp_path, number=601)
    rc = triage.main(["--shared-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "triage: signals 1, proposals_written 1, skipped_existing 0, "
        "agent_able 0, human 1"
    ) in out

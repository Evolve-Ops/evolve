"""fit_review.runner — Bite 3, the bot-side orchestrator (no real LLM spend).

Tests the five brakes in order (spec §8) and the candidate contract:

  * opt-out honored — an opted-out bot is not reviewed (no LLM, nothing written),
  * targeting floor gates the LLM — no_purpose / below-floor ⇒ NO model call,
  * cite-or-don't end-to-end — a declined reflection writes nothing,
  * the written candidate JSON matches the Bite-4 contract exactly,
  * deterministic value (Gate B),
  * an end-to-end pass off real on-disk substrate (tuples + network + capture).

Every LLM is a recording stub — ``llm.calls == []`` is how we prove the
expensive call did not fire on a gated path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fit_review import runner
from fit_review.candidate import parse_candidate
from fit_review.targeting import build_targeting_report
from observations.tuples import write_tuples
from schema.observation import ObservationTuple


# Injected gallery-catalog fixture. The catalog is an INJECTED input to
# build_targeting_report (see ``catalog=GALLERY`` below). These apps used to be
# shipped in packages/gallery/catalog.json, which is now a GENERATED projection
# of the real gallery (codebase-review 0.4) and no longer carries the
# document-generation / slack-comms tag combinations the pm_tuples fixture needs
# — so they live here, decoupled from the live gallery roster.
GALLERY = [
    {"pkg_id": "p-e5f6a7b8", "name": "Weekly Status Reporter",
     "application_tags": ["document-generation", "slack-comms", "task-management"]},
    {"pkg_id": "p-f6a7b8c9", "name": "Project Tracker",
     "application_tags": ["task-management", "calendar"]},
    # Task Manager is a REAL gallery app (task-management). The end-to-end
    # tests below drive the production poller/disk-build, which load the real
    # catalog internally — the suggested pkg must resolve there, so it is on
    # this injected shortlist AND in the shipped gallery.
    {"pkg_id": "p-9bfa1c84", "name": "Task Manager",
     "application_tags": ["task-management"]},
]

PKG_PROJECT_TRACKER = "p-f6a7b8c9"
PKG_WEEKLY_STATUS = "p-e5f6a7b8"
PKG_TASK_MANAGER = "p-9bfa1c84"   # real gallery app (task-management)

PM_PURPOSE = {
    "archetype": "project-manager",
    "mission": "Run the team's projects.",
    "captured": "declared",
    "confidence": 1.0,
}
PA_PURPOSE = {
    "archetype": "personal-assistant",
    "mission": "Help around the house.",
    "captured": "declared",
    "confidence": 1.0,
}

QUOTE = "Please keep track of these project tasks, I keep losing them in chat."


class RecordingLLM:
    def __init__(self, response: str = ""):
        self.response = response
        self.calls: list[tuple] = []

    def __call__(self, system, user, model, max_tokens):
        self.calls.append((system, user, model, max_tokens))
        return self.response


# ─────────────────────────────────────────────────────────────────────────────
# Tuple fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _mk(noun, verb, session, day, ts=None):
    uniq = f"{noun}-{verb}-{session}-{day}"
    ts = ts or f"{day}T10:00:00+00:00"
    return ObservationTuple(
        id=f"t-{abs(hash(uniq))}",
        bot_id="team-bot-a",
        session_id=session,
        segment_id=f"seg-{abs(hash(uniq))}",
        noun=noun,
        verb=verb,
        mood=None,
        engagement=1,
        timestamp_start=ts,
        timestamp_end=ts,
        source_hash=f"h-{abs(hash(uniq))}",
    )


def _spread(noun, verb, n, sessions, days):
    out = []
    for i in range(n):
        out.append(_mk(noun, verb, f"{noun}-s{i % sessions}", f"2026-06-{1 + (i % days):02d}"))
    return out


def pm_tuples():
    """The project-manager bot — task-management dominates, doc-gen + slack-comms
    also clear the floor. All in the project-manager playbook, all uncovered."""
    t = []
    t += _spread("task-management", "recording", 30, 20, 8)
    t += _spread("document-generation", "drafting", 13, 5, 3)
    t += _spread("slack-comms", "summarizing", 9, 4, 3)
    return t


def thin_tuples():
    """Negative-control shape — nothing clears the support floor."""
    return _spread("calendar", "scheduling", 1, 1, 1)


def pm_report():
    return build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=pm_tuples(), catalog=GALLERY
    )


def transcript_turns():
    return [
        {
            "session_id": "task-management-s0",
            "turn_index": 0,
            "ts": "2026-06-20T10:00:00+00:00",
            "text": QUOTE,
        }
    ]


def _suggest_json(pkg_id=PKG_WEEKLY_STATUS):
    return json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "The user keeps asking the bot to hold and track project tasks.",
            "suggested_gallery_pkg_id": pkg_id,
            "cited_evidence": [
                {"quote": QUOTE, "session_id": "task-management-s0", "ts": "2026-06-20T10:00:00+00:00"}
            ],
        }
    )


def _outbox_files(shared_dir):
    """Candidate files the Bite-4 poller would drain (now under shared_dir)."""
    outbox = runner._outbox_dir(shared_dir)
    if not outbox.exists():
        return []
    return [p for p in outbox.rglob("*.json")]


# ─────────────────────────────────────────────────────────────────────────────
# Brake 1: opt-out honored — no LLM, nothing written
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_out_via_absent_buffer_skips_before_any_llm(tmp_path):
    llm = RecordingLLM(_suggest_json())
    # shared_dir has no metrics/<bot>/recent-transcripts.json ⇒ opted out.
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=pm_report(),  # present, but opt-out short-circuits first
        transcript=None,  # forces the disk read → absent buffer → opted_out
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "opted_out"
    assert result["wrote_candidate"] is False
    assert llm.calls == []
    assert _outbox_files(tmp_path / "shared") == []


def test_opt_out_via_security_scanning_false_skips(tmp_path):
    llm = RecordingLLM(_suggest_json())
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {"team-bot-a": {"securityScanning": False}}},
        targeting_report=pm_report(),
        transcript=transcript_turns(),  # buffer present, but the flag opts out
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "opted_out"
    assert llm.calls == []
    assert _outbox_files(tmp_path / "shared") == []


# ─────────────────────────────────────────────────────────────────────────────
# Brake 2: targeting floor gates the expensive call
# ─────────────────────────────────────────────────────────────────────────────


def test_no_purpose_makes_no_llm_call_and_writes_nothing(tmp_path):
    no_purpose_report = build_targeting_report(
        bot_id="team-bot-a", purpose=None, tuples=pm_tuples(), catalog=GALLERY
    )
    llm = RecordingLLM(_suggest_json())
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=no_purpose_report,
        transcript=transcript_turns(),
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "no_purpose"
    assert llm.calls == []  # the LLM is escalation, not baseline
    assert _outbox_files(tmp_path / "shared") == []


def test_below_floor_makes_no_llm_call_and_writes_nothing(tmp_path):
    below_floor = build_targeting_report(
        bot_id="personal-bot", purpose=PA_PURPOSE, tuples=thin_tuples(), catalog=GALLERY
    )
    llm = RecordingLLM(_suggest_json())
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="personal-bot",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=below_floor,
        transcript=transcript_turns(),
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "no_candidate"
    assert llm.calls == []
    assert _outbox_files(tmp_path / "shared") == []


# ─────────────────────────────────────────────────────────────────────────────
# Brake 4: cite-or-don't — a declined reflection writes nothing
# ─────────────────────────────────────────────────────────────────────────────


def test_fabricated_quote_declines_and_writes_nothing(tmp_path):
    # The LLM fires (targeting passed) but its quote is not in the transcript.
    fabricated = json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "Track tasks.",
            "suggested_gallery_pkg_id": PKG_WEEKLY_STATUS,
            "cited_evidence": [{"quote": "a quote that never appears", "session_id": "x", "ts": "y"}],
        }
    )
    llm = RecordingLLM(fabricated)
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=pm_report(),
        transcript=transcript_turns(),
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "declined"
    assert result["wrote_candidate"] is False
    assert len(llm.calls) == 1  # the model DID fire; the post-hoc gate dropped it
    assert _outbox_files(tmp_path / "shared") == []


# ─────────────────────────────────────────────────────────────────────────────
# Brake 5 + contract: a grounded suggestion writes one contract-shaped candidate
# ─────────────────────────────────────────────────────────────────────────────

CONTRACT_KEYS = {
    "kind",
    "record_id",
    "bot_id",
    "archetype",
    "recommended_need",
    "suggested_gallery_pkg_id",
    "cited_evidence",
    "value_estimate",
    "altitude",
    "targeting_decision",
    "support",
    "run_id",
    "created_at",
}


def test_targets_found_writes_one_candidate_matching_contract(tmp_path):
    ws = tmp_path / "ws"
    llm = RecordingLLM(_suggest_json(PKG_WEEKLY_STATUS))
    result = runner.run_fit_review_for_bot(
        ws,
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=pm_report(),
        transcript=transcript_turns(),
        llm_call=llm,
        model=None,
    )
    assert result["decision"] == "suggest"
    assert result["wrote_candidate"] is True

    files = _outbox_files(tmp_path / "shared")
    assert len(files) == 1  # exactly one candidate — not zero, not a flood
    data = json.loads(files[0].read_text())

    # Exact contract shape.
    assert set(data.keys()) == CONTRACT_KEYS
    assert data["kind"] == "fit_review_candidate"
    assert data["bot_id"] == "team-bot-a"
    assert data["archetype"] == "project-manager"
    assert data["recommended_need"]
    assert data["altitude"] == 2
    assert data["targeting_decision"] == "targets_found"

    # pkg is a REAL gallery pkg_id.
    catalog_ids = {e["pkg_id"] for e in GALLERY}
    assert data["suggested_gallery_pkg_id"] in catalog_ids
    assert data["suggested_gallery_pkg_id"] == PKG_WEEKLY_STATUS

    # cited_evidence is verbatim, with provenance.
    assert len(data["cited_evidence"]) == 1
    ev = data["cited_evidence"][0]
    assert set(ev.keys()) == {"quote", "session_id", "ts"}
    assert ev["quote"] == QUOTE
    assert ev["session_id"] == "task-management-s0"

    # value_estimate is deterministic Gate B: strong support ⇒ high.
    ve = data["value_estimate"]
    assert set(ve.keys()) == {"tier", "basis", "evidence_refs"}
    assert ve["tier"] == "high"
    assert ve["basis"]
    assert ve["evidence_refs"] == ["task-management-s0"]

    # support carries the targeting numbers.
    assert data["support"]["distinct_sessions"] >= 8
    assert data["support"]["days"] >= 2

    # Integration: the merged Bite-4 reader parses it into a clean suggestion.
    cand = parse_candidate(data)
    assert cand.is_suggestion
    assert cand.pkg_id == PKG_WEEKLY_STATUS
    assert cand.citable_evidence  # ≥1 attestable quote survives for Gate A


def test_medium_tier_when_support_modest(tmp_path):
    # doc-gen alone: 5 sessions / 3 days — purpose-aligned, ≥3 but <8 ⇒ medium.
    modest = build_targeting_report(
        bot_id="team-bot-a",
        purpose=PM_PURPOSE,
        tuples=_spread("document-generation", "drafting", 13, 5, 3),
        catalog=GALLERY,
    )
    assert modest.decision == "targets_found"
    quote = "Draft the weekly report for me please."
    transcript = [
        {"session_id": "document-generation-s0", "turn_index": 0, "ts": "2026-06-20T10:00:00+00:00", "text": quote}
    ]
    # Pick whatever app the shortlist leads with so the pkg is valid.
    pkg = modest.primary.pkg_id
    payload = json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "User asks for weekly report drafting.",
            "suggested_gallery_pkg_id": pkg,
            "cited_evidence": [{"quote": quote, "session_id": "document-generation-s0", "ts": "2026-06-20T10:00:00+00:00"}],
        }
    )
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        targeting_report=modest,
        transcript=transcript,
        llm_call=RecordingLLM(payload),
        model=None,
    )
    assert result["decision"] == "suggest"
    assert result["value_tier"] == "medium"


# ─────────────────────────────────────────────────────────────────────────────
# Cadence wrapper
# ─────────────────────────────────────────────────────────────────────────────


def test_run_if_due_skips_when_recently_run(tmp_path):
    ws = tmp_path / "ws"
    now = datetime.now(timezone.utc)
    runner._stamp_sentinel(ws, now, {"decision": "no_candidate", "run_id": "x"})
    # 3 days later — inside the weekly window.
    out = runner.run_if_due(
        ws, bot_id="team-bot-a", shared_dir=tmp_path / "shared", now=now + timedelta(days=3)
    )
    assert out["ran"] is False
    assert "not due" in out["reason"]


def test_run_if_due_runs_when_overdue(tmp_path):
    ws = tmp_path / "ws"
    now = datetime.now(timezone.utc)
    runner._stamp_sentinel(ws, now, {"decision": "no_candidate", "run_id": "x"})
    # 8 days later — past the weekly window; opted out (no buffer) ⇒ runs + stamps.
    out = runner.run_if_due(
        ws,
        bot_id="team-bot-a",
        shared_dir=tmp_path / "shared",
        network={"bots": {}},
        now=now + timedelta(days=8),
    )
    assert out["ran"] is True
    assert out["decision"] == "opted_out"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: real on-disk substrate (tuples + network.json + capture buffer)
# ─────────────────────────────────────────────────────────────────────────────


def test_end_to_end_off_disk_writes_candidate(tmp_path):
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    bot = "team-bot-a"
    now = datetime.now(timezone.utc)

    # 1. Observation tuples in the 60-day window, written to the real store.
    by_day: dict[str, list] = {}
    for i in range(30):
        dt = now - timedelta(days=(i % 8) + 1)
        day = dt.date().isoformat()
        t = _mk("task-management", "recording", f"s{i % 20}", day, ts=dt.isoformat())
        by_day.setdefault(day, []).append((dt, t))
    for day, items in by_day.items():
        write_tuples([t for _dt, t in items], shared_dir=shared, bot_id=bot, day=items[0][0])

    # 2. Declared purpose in network.json (via the shared write path).
    from bot_purpose import set_bot_purpose

    network = {"bots": {bot: {}}}
    set_bot_purpose(
        network,
        bot,
        {"archetype": "project-manager", "mission": "Run the team's projects."},
        captured="declared",
        reviewed_at=now.isoformat(timespec="seconds"),
    )
    (shared).mkdir(parents=True, exist_ok=True)
    (shared / "network.json").write_text(json.dumps(network), encoding="utf-8")

    # 3. Recent-transcript capture buffer carrying the citation.
    metrics = shared / "metrics" / bot
    metrics.mkdir(parents=True, exist_ok=True)
    turns = [
        {"session_id": "s0", "turn_index": 0, "ts": (now - timedelta(days=1)).isoformat(), "text": QUOTE}
    ]
    (metrics / "recent-transcripts.json").write_text(json.dumps(turns), encoding="utf-8")

    # 4. Run with a stub LLM citing the verbatim quote + a real shortlisted pkg.
    payload = json.dumps(
        {
            "decision": "suggest",
            "recommended_need": "User repeatedly asks the bot to hold project tasks.",
            # network=None ⇒ the report is rebuilt from disk against the REAL
            # gallery catalog, so cite a real installable app.
            "suggested_gallery_pkg_id": PKG_TASK_MANAGER,
            "cited_evidence": [{"quote": QUOTE, "session_id": "s0", "ts": turns[0]["ts"]}],
        }
    )
    result = runner.run_fit_review_for_bot(
        ws,
        bot_id=bot,
        shared_dir=shared,
        llm_call=RecordingLLM(payload),
        model=None,  # network=None ⇒ loaded from disk; transcript=None ⇒ read buffer
    )

    assert result["decision"] == "suggest", result
    files = _outbox_files(shared)
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["suggested_gallery_pkg_id"] == PKG_TASK_MANAGER
    assert data["cited_evidence"][0]["quote"] == QUOTE
    assert data["targeting_decision"] == "targets_found"
    assert data["altitude"] == 2
    # The merged Bite-4 reader accepts the on-disk record.
    assert parse_candidate(data).is_suggestion


# ─────────────────────────────────────────────────────────────────────────────
# Cross-package seam: runner candidate → poller Gate A (cite-or-don't) end to end
#
# The other tests stop at ``parse_candidate`` — they prove the runner emits a
# STRUCTURED, parseable candidate, but not that it CLEARS the merged poller's
# Gate A. Bite 3 (this package) and Bite 4 (``evolve_admin``) are a cite-or-don't
# contract across a package boundary: the runner must emit structured
# ``cited_evidence: [{quote, session_id, ts}]`` whose ``session_id``s the bot's
# turn store attests to; the poller's Gate A RE-VERIFIES each quote against that
# store (verify, don't trust — spec §3.6). These two tests drive a real
# runner-produced candidate THROUGH the poller's acceptance path: the positive
# proves the contract holds end to end (a prose ``evidence: [str]`` candidate
# would silently fail here), the negative proves the poller re-verifies
# independently rather than trusting the runner's own citation.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_turn_store(tmp_path, bot_user, session_id, text):
    """Write the poller's INDEPENDENT Gate-A source: the bot's turns-*.jsonl."""
    d = tmp_path / "Users" / bot_user / ".openclaw" / "workspace" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / "turns-2026-06-20.jsonl").write_text(
        json.dumps({"session_id": session_id, "text": text})
    )
    return d


def _run_runner_to_outbox(tmp_path, *, shared):
    """Produce one real candidate citing session ``task-management-s0`` + QUOTE.

    The candidate feeds the PRODUCTION poller (``fit_review_poller.tick``), whose
    gate re-loads the real gallery catalog — so cite a real installable app
    (Task Manager, task-management) that is on both the injected shortlist and
    the shipped gallery.
    """
    result = runner.run_fit_review_for_bot(
        tmp_path / "ws",
        bot_id="team-bot-a",
        shared_dir=shared,
        network={"bots": {}},
        targeting_report=pm_report(),
        transcript=transcript_turns(),
        llm_call=RecordingLLM(_suggest_json(PKG_TASK_MANAGER)),
        model=None,
    )
    assert result["wrote_candidate"] is True
    assert len(_outbox_files(shared)) == 1
    return result


def test_runner_candidate_accepted_through_poller_gate_a(tmp_path, monkeypatch):
    """A runner candidate whose quote is attestable clears Gate A → one Proposal.

    This is the only check that proves the STRUCTURED-citation contract holds end
    to end: the runner-produced candidate is drained by the merged poller with the
    REAL transcript verifier (``quote_verifier_factory=None``) against a turn store
    seeded with the same OC session_id + verbatim quote, and a single InstallApp
    Proposal lands in pending/.
    """
    from evolve_admin.applications import fit_review_poller

    shared = tmp_path / "shared"
    _run_runner_to_outbox(tmp_path, shared=shared)

    # The cited (session_id, quote) IS present in the bot's turn store.
    turns_dir = _seed_turn_store(tmp_path, "team-bot-a", "task-management-s0", QUOTE)
    monkeypatch.setattr(fit_review_poller, "_turns_dir", lambda _bu: turns_dir)

    now = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
    res = fit_review_poller.tick(
        shared,
        network={"bots": {"team-bot-a": {"user": "team-bot-a"}}},
        now=now,
        build_report=lambda _b, *, network, shared_dir, now: pm_report(),
        quote_verifier_factory=None,  # the REAL make_transcript_quote_verifier
    )

    assert res.errors == []
    assert res.proposals_raised == 1

    from arbiter import store as arbiter_store

    pending = list(arbiter_store.iter_proposals(shared, subdirs=("pending",)))
    assert len(pending) == 1


def test_poller_gate_a_refutes_runner_candidate_when_quote_absent(tmp_path, monkeypatch):
    """Gate A re-verifies INDEPENDENTLY — it does not trust the runner's citation.

    Same runner candidate, but the bot's turn store does not contain the cited
    quote (what a hallucinated quote would look like). The poller drops it: no
    Proposal — and, critically, a refuted citation is a DROP, not an error, so it
    never reddens the shared scheduler tick.
    """
    from evolve_admin.applications import fit_review_poller

    shared = tmp_path / "shared"
    _run_runner_to_outbox(tmp_path, shared=shared)

    # Session present in the store, but WITHOUT the cited quote → not attestable.
    turns_dir = _seed_turn_store(
        tmp_path, "team-bot-a", "task-management-s0", "something else entirely"
    )
    monkeypatch.setattr(fit_review_poller, "_turns_dir", lambda _bu: turns_dir)

    now = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
    res = fit_review_poller.tick(
        shared,
        network={"bots": {"team-bot-a": {"user": "team-bot-a"}}},
        now=now,
        build_report=lambda _b, *, network, shared_dir, now: pm_report(),
        quote_verifier_factory=None,
    )

    assert res.proposals_raised == 0
    assert res.errors == []  # a refuted citation is a drop, never an error

    from arbiter import store as arbiter_store

    assert list(arbiter_store.iter_proposals(shared, subdirs=("pending",))) == []

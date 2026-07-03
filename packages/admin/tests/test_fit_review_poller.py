"""Integration tests for the admin-side fit_review_poller (Bite 4).

Constructs synthetic Fit Reviewer candidate outboxes in a temp shared_dir, runs
the poller end-to-end (gates → arbiter.store), and asserts:
  - a PASSing candidate becomes one InstallApp Proposal in pending/ that reaches
    the actionable Recommendations inbox (proposal_surfaces_in_pending_inbox)
  - a fabricated quote is dropped (no proposal)
  - a missing/unknown pkg yields no InstallApp
  - a below-floor candidate is skipped (and surfaced as a calmer Observation)
  - an already-operator-declined need is suppressed
  - processed candidates are archived into _ingested/
  - the default transcript quote verifier catches fabricated quotes/sessions
  - the clock is threaded (no wall-clock coupling)

Real arbiter.store on a temp shared_dir; no Anthropic calls, no LaunchDaemons,
no real bot users. The targeting recompute + transcript verifier are injected so
the tests don't stand up an observation store or a bot's transcript tree.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import fit_review_poller  # noqa: E402

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
PKG_PROJECT_TRACKER = "p-f6a7b8c9"
NETWORK = {"bots": {"team-bot-a": {"user": "team-bot-a"}}}

DECLARED_PM = {
    "archetype": "project-manager",
    "mission": "keep projects on track",
    "captured": "declared",
    "confidence": 1.0,
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_candidate(shared: Path, *, run_id: str, bot_id: str, **over) -> Path:
    rec = {
        "bot_id": bot_id,
        "targeting_decision": "suggest",
        "suggested_gallery_pkg_id": PKG_PROJECT_TRACKER,
        "recommended_need": "a place to track project tasks",
        "archetype": "project-manager",
        "cited_evidence": [
            {"quote": "track these 7 launch tasks", "session_id": "s1", "ts": "2026-06-10"},
        ],
        "value_estimate": {"tier": "high", "basis": "x", "evidence_refs": []},
        "support": {"distinct_sessions": 10, "days": 9},
        "run_id": run_id,
        "created_at": "2026-06-23T00:00:00+00:00",
    }
    rec.update(over)
    d = shared / "fit_review" / "outbox" / run_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{bot_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _strong_report(*, bot_id="team-bot-a", installed=()):
    """A faithfully-recomputed TargetingReport: task-management above floor with
    >= 8 distinct sessions → tier high, Project Tracker shortlisted."""
    from schema.observation import ObservationTuple
    from fit_review import targeting

    tuples = []
    for i in range(10):
        tuples.append(
            ObservationTuple(
                id=f"t-{i}",
                bot_id=bot_id,
                session_id=f"s{i}",
                segment_id=f"seg-{i}",
                noun="task-management",
                verb="recording",
                mood="neutral",
                engagement=1,
                timestamp_start=f"2026-06-{10 + (i % 9):02d}T10:00:00+00:00",
                timestamp_end=f"2026-06-{10 + (i % 9):02d}T10:05:00+00:00",
                source_hash=f"h-{i}",
            )
        )
    return targeting.build_targeting_report(
        bot_id=bot_id,
        purpose=DECLARED_PM,
        tuples=tuples,
        catalog=targeting._load_gallery_catalog(),
        installed_pkg_ids=installed,
    )


def _report_fn(report):
    def build(_bot_id, *, network, shared_dir, now):  # noqa: ARG001
        return report

    return build


def _verifier_factory(verdict_fn):
    def factory(_bot_user):
        return verdict_fn

    return factory


_TRUE = _verifier_factory(lambda b, s, q: True)
_FALSE = _verifier_factory(lambda b, s, q: False)


def _pending(shared: Path):
    from arbiter import store as arbiter_store

    return list(arbiter_store.iter_proposals(shared, subdirs=("pending",)))


# ── Tests ────────────────────────────────────────────────────────────────────


def test_pass_emits_one_install_app_in_pending(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    src = _write_candidate(shared, run_id="run-1", bot_id="team-bot-a")

    res = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(_strong_report()),
        quote_verifier_factory=_TRUE,
    )

    assert res.files_processed == 1
    assert res.proposals_raised == 1

    props = _pending(shared)
    assert len(props) == 1
    p = props[0]
    assert p.action.kind == "InstallApp"
    assert p.action.app_id == PKG_PROJECT_TRACKER
    assert p.altitude == 2
    assert p.surface == "improvement"
    assert p.value_estimate.tier == "high"
    assert p.created_at == NOW.isoformat()

    from proposal_routing import proposal_surfaces_in_pending_inbox

    assert proposal_surfaces_in_pending_inbox(p.to_dict())

    # Archived off the queue.
    assert not src.exists()
    archived = shared / "fit_review" / "_ingested" / "2026-06-23" / "run-1" / "team-bot-a.json"
    assert archived.exists()


def test_fabricated_quote_dropped_no_proposal(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(shared, run_id="run-1", bot_id="team-bot-a")

    res = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(_strong_report()),
        quote_verifier_factory=_FALSE,  # every quote refuted
    )
    assert res.proposals_raised == 0
    assert res.drops_by_reason.get("fabricated_quote") == 1
    assert _pending(shared) == []


def test_missing_pkg_no_install_app(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(
        shared, run_id="run-1", bot_id="team-bot-a", suggested_gallery_pkg_id=None
    )
    res = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(_strong_report()),
        quote_verifier_factory=_TRUE,
    )
    assert res.proposals_raised == 0
    assert res.drops_by_reason.get("no_pkg") == 1
    assert _pending(shared) == []


def test_below_floor_skipped_emits_observation(tmp_path: Path):
    from schema.observation import ObservationTuple
    from fit_review import targeting

    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(shared, run_id="run-1", bot_id="team-bot-a")

    # A report where task-management is below the support floor → pkg not on
    # the recomputed shortlist.
    thin = [
        ObservationTuple(
            id="t-1", bot_id="team-bot-a", session_id="s1", segment_id="seg-1",
            noun="task-management", verb="recording", mood="neutral", engagement=1,
            timestamp_start="2026-06-10T10:00:00+00:00",
            timestamp_end="2026-06-10T10:05:00+00:00", source_hash="h1",
        ),
    ]
    thin_report = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=DECLARED_PM, tuples=thin,
        catalog=targeting._load_gallery_catalog(),
    )

    res = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(thin_report),
        quote_verifier_factory=_TRUE,
    )
    assert res.proposals_raised == 0
    assert res.observations_raised == 1
    assert res.drops_by_reason.get("support_below_floor") == 1

    # The near-miss Observation is informational (altitude 0) → NOT in Pending.
    # Mirror the serve-time derivation: routes_arbiter.py computes
    # ``informational`` from the action kind before routing.
    from proposal_routing import proposal_surfaces_in_pending_inbox
    from arbiter.apply import is_informational_kind

    props = _pending(shared)
    assert len(props) == 1
    obs = props[0]
    assert obs.action.kind == "Investigation"
    assert obs.altitude == 0
    assert is_informational_kind(obs.action.kind)  # Investigation → Observations
    view = obs.to_dict()
    view["informational"] = is_informational_kind(obs.action.kind)
    assert not proposal_surfaces_in_pending_inbox(view)


def test_no_candidate_record_silently_dropped(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(
        shared, run_id="run-1", bot_id="team-bot-a",
        targeting_decision="no_candidate", suggested_gallery_pkg_id=None,
        cited_evidence=[],
    )
    res = fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    assert res.proposals_raised == 0
    assert res.observations_raised == 0
    assert res.drops_by_reason.get("not_a_suggestion") == 1
    assert _pending(shared) == []


def test_idempotent_second_tick_no_duplicate(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(shared, run_id="run-1", bot_id="team-bot-a")
    fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    # Re-drop the same candidate (new run dir) and re-run — the open pending
    # card should suppress a duplicate.
    _write_candidate(shared, run_id="run-2", bot_id="team-bot-a")
    res2 = fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    assert res2.proposals_raised == 0
    assert len(_pending(shared)) == 1


def test_operator_declined_suppressed_end_to_end(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_candidate(shared, run_id="run-1", bot_id="team-bot-a")

    # Force the cooldown predicate True regardless of history. (importlib gets
    # the real submodule — investigation/__init__ re-exports a same-named
    # function that would otherwise shadow it.)
    import importlib

    ph = importlib.import_module("investigation.proposal_history")
    monkeypatch.setattr(ph, "operator_already_declined", lambda *a, **k: True)

    res = fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    assert res.proposals_raised == 0
    assert res.drops_by_reason.get("operator_declined") == 1
    assert _pending(shared) == []


# ── Malformed / unreadable outbox files (quarantine, don't wedge the tick) ──


def test_malformed_candidate_quarantined_not_errored(tmp_path: Path):
    """A corrupt outbox file must be quarantined, not re-errored every tick.

    Regression for the review CONCERN on #3177: a malformed-JSON candidate used
    to be skipped (``continue``) WITHOUT archiving, so it re-failed on every tick
    — and because the audit-scheduler exits 1 on any ``result.errors`` it wedged
    the shared scheduler tick permanently red (the #3174 cron_exit pattern). The
    fix quarantines a PERMANENT read failure and counts it as a drop, NOT an
    error, so the tick stays green and the bad file is read only once.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    run_dir = shared / "fit_review" / "outbox" / "run-bad"
    run_dir.mkdir(parents=True)
    bad = run_dir / "team-bot-a.json"
    bad.write_text("{ this is not valid json …")  # malformed → permanent failure

    res = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(_strong_report()),
        quote_verifier_factory=_TRUE,
    )

    # Drained + counted as a DROP, not an error → the shared scheduler tick stays
    # green (cli.py audit-scheduler-tick: ``sys.exit(0 if not errors else 1)``).
    assert res.files_processed == 1
    assert res.errors == []
    assert res.as_dict()["errors"] == []
    assert res.drops_by_reason.get("malformed_quarantined") == 1
    assert res.proposals_raised == 0

    # Quarantined off the active outbox into _ingested/ …
    assert not bad.exists()
    archived = (
        shared / "fit_review" / "_ingested" / "2026-06-23" / "run-bad" / "team-bot-a.json"
    )
    assert archived.exists()

    # … and NOT re-processed on the next tick (nothing left to read, no new error).
    res2 = fit_review_poller.tick(
        shared,
        network=NETWORK,
        now=NOW,
        build_report=_report_fn(_strong_report()),
        quote_verifier_factory=_TRUE,
    )
    assert res2.files_processed == 0
    assert res2.errors == []


def test_stuck_transient_quarantined_past_age_cap(tmp_path: Path, monkeypatch):
    """A permanently-stuck TRANSIENT read can't keep the tick red forever.

    A transient failure (e.g. ACL not ready) is normally left for retry and DOES
    redden the tick while it persists — correct operational signal. But once it
    is older than ``_TRANSIENT_RETRY_MAX_AGE`` the poller quarantines it so it
    can't wedge the shared scheduler tick *indefinitely*.
    """
    import os

    shared = tmp_path / "shared"
    shared.mkdir()
    src = _write_candidate(shared, run_id="run-stuck", bot_id="team-bot-a")

    # Simulate a persistent transient read failure (read never succeeds).
    monkeypatch.setattr(fit_review_poller, "_read_json", lambda _p: None)

    # Fresh file → left for retry: an error, NOT a quarantine.
    res_fresh = fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    assert len(res_fresh.errors) == 1
    assert src.exists()

    # Age the file past the cap → quarantine; no longer an error.
    old = (
        NOW - fit_review_poller._TRANSIENT_RETRY_MAX_AGE - timedelta(hours=1)
    ).timestamp()
    os.utime(src, (old, old))
    res_stuck = fit_review_poller.tick(
        shared, network=NETWORK, now=NOW,
        build_report=_report_fn(_strong_report()), quote_verifier_factory=_TRUE,
    )
    assert res_stuck.errors == []
    assert res_stuck.drops_by_reason.get("unreadable_stuck_quarantined") == 1
    assert not src.exists()


# ── Default transcript quote verifier (the real anti-hallucination path) ─────


def _write_turns(tmp_root: Path, bot_user: str, session_id: str, texts: list[str]):
    d = tmp_root / "Users" / bot_user / ".openclaw" / "workspace" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"session_id": session_id, "text": t}) for t in texts]
    (d / "turns-2026-06-10.jsonl").write_text("\n".join(lines))


def test_default_verifier_verified_refuted_and_unavailable(tmp_path: Path, monkeypatch):
    bot_user = "team-bot-a"
    monkeypatch.setattr(
        fit_review_poller,
        "_turns_dir",
        lambda bu: tmp_path / "Users" / bu / ".openclaw" / "workspace" / "memory",
    )
    _write_turns(tmp_path, bot_user, "s1", ["please track these 7 launch tasks today"])
    verify = fit_review_poller.make_transcript_quote_verifier(bot_user)

    # VERIFIED — quote present in the claimed session (normalized substring).
    assert verify("team-bot-a", "s1", "track these 7 launch tasks") is True
    # REFUTED — quote NOT in that session → fabricated → drop.
    assert verify("team-bot-a", "s1", "buy milk and eggs") is False
    # REFUTED — session_id unknown to a present store → fabricated session.
    assert verify("team-bot-a", "s-unknown", "anything") is False


def test_default_verifier_degrades_when_store_absent(tmp_path: Path, monkeypatch):
    # No turns files at all → degrade to trusting the bot's session attestation
    # rather than dropping every candidate pod-wide.
    monkeypatch.setattr(
        fit_review_poller, "_turns_dir", lambda bu: tmp_path / "nope" / bu
    )
    verify = fit_review_poller.make_transcript_quote_verifier("team-bot-a")
    assert verify("team-bot-a", "s1", "anything at all") is True
    # ...but a structurally-empty citation never passes.
    assert verify("team-bot-a", "", "x") is False
    assert verify("team-bot-a", "s1", "  ") is False

"""fit_review gates + candidate parsing + proposal build — Bite 4 (pure).

The deterministic safety layer (spec-fit-reviewer-2026-06-12 §3.6). These tests
exercise each gate's PASS and each FAIL mode with NO I/O — the catalog, the
recomputed targeting report, the quote verifier and the cooldown predicate are
all injected. Role-placeholder bot ids only (``team-bot-a`` / ``personal-bot``),
never real bot names (the public-launch scrub guard).
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

from schema.observation import ObservationTuple

targeting = importlib.import_module("fit_review.targeting")
candidate_mod = importlib.import_module("fit_review.candidate")
gates = importlib.import_module("fit_review.gates")
proposal_routing = importlib.import_module("proposal_routing")

# Injected gallery-catalog fixture. The catalog is an INJECTED input to
# build_targeting_report / evaluate_candidate (see this module's docstring:
# "NO I/O — the catalog ... injected"). These three apps used to be shipped in
# packages/gallery/catalog.json, which is now a GENERATED projection of the real
# gallery (codebase-review 0.4) and no longer carries the document-generation /
# slack-comms tag combinations these algorithm tests need — so the fixtures live
# here, decoupled from the live gallery roster, not in the shipped catalog.
GALLERY = [
    {"pkg_id": "p-e5f6a7b8", "name": "Weekly Status Reporter",
     "application_tags": ["document-generation", "slack-comms", "task-management"]},
    {"pkg_id": "p-f6a7b8c9", "name": "Project Tracker",
     "application_tags": ["task-management", "calendar"]},
    {"pkg_id": "p-a1b2c3d4", "name": "Daily Health Tracker",
     "application_tags": ["health-nutrition", "health-fitness"]},
]
PKG_PROJECT_TRACKER = "p-f6a7b8c9"   # tags: task-management, calendar
PKG_WEEKLY_STATUS = "p-e5f6a7b8"     # tags: document-generation, slack-comms, task-management
PKG_DAILY_HEALTH = "p-a1b2c3d4"      # tags: health-nutrition, health-fitness

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)

DECLARED_PM = {
    "archetype": "project-manager",
    "mission": "keep the team's projects on track",
    "captured": "declared",
    "confidence": 1.0,
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _mk(noun: str, session: str, day: str, verb: str = "recording") -> ObservationTuple:
    uniq = f"{noun}-{session}-{day}-{verb}"
    return ObservationTuple(
        id=f"t-{abs(hash(uniq))}",
        bot_id="team-bot-a",
        session_id=session,
        segment_id=f"seg-{abs(hash(uniq))}",
        noun=noun,
        verb=verb,
        mood="neutral",
        engagement=1,
        timestamp_start=f"{day}T10:00:00+00:00",
        timestamp_end=f"{day}T10:05:00+00:00",
        source_hash=f"h-{abs(hash(uniq))}",
    )


def _strong_pm_tuples() -> list[ObservationTuple]:
    """task-management above the floor AND >= 8 distinct sessions (→ tier high)."""
    out: list[ObservationTuple] = []
    for i in range(10):
        out.append(_mk("task-management", session=f"s{i}", day=f"2026-06-{10 + (i % 9):02d}"))
    return out


def _report(tuples, *, purpose=DECLARED_PM, installed_pkg_ids=()):
    return targeting.build_targeting_report(
        bot_id="team-bot-a",
        purpose=purpose,
        tuples=tuples,
        catalog=GALLERY,
        installed_pkg_ids=installed_pkg_ids,
    )


def _candidate(**over):
    base = {
        "bot_id": "team-bot-a",
        "targeting_decision": "suggest",
        "suggested_gallery_pkg_id": PKG_PROJECT_TRACKER,
        "recommended_need": "a durable place to track project tasks",
        "archetype": "project-manager",
        "cited_evidence": [
            {"quote": "please track these 7 tasks for the launch", "session_id": "s1", "ts": "2026-06-10"},
        ],
        "value_estimate": {"tier": "high", "basis": "lots", "evidence_refs": ["s1: tasks"]},
        "support": {"distinct_sessions": 10, "days": 9},
        "run_id": "run-xyz",
        "created_at": "2026-06-23T00:00:00+00:00",
    }
    base.update(over)
    return candidate_mod.parse_candidate(base, run_id="run-xyz", bot_id="team-bot-a")


def _always_true(_bot, _sid, _quote):
    return True


def _always_false(_bot, _sid, _quote):
    return False


# ── Candidate parsing (contract ↔ spec §3.5 aliases) ─────────────────────────


def test_parse_accepts_contract_field_names():
    c = _candidate()
    assert c.bot_id == "team-bot-a"
    assert c.pkg_id == PKG_PROJECT_TRACKER
    assert c.is_suggestion
    assert c.run_id == "run-xyz"
    assert c.citable_evidence[0].quote.startswith("please track")
    assert c.citable_evidence[0].session_id == "s1"


def test_parse_accepts_spec_v35_field_names():
    raw = {
        "bot_id": "team-bot-a",
        "decision": "suggest",
        "app_pkg_id": PKG_WEEKLY_STATUS,
        "archetype_lens": "project-manager / status",
        "evidence": ["task-management was the #1 activity", "weekly status done by hand"],
        "value_tier": "medium",
        "value_basis": "purpose-aligned #1 domain",
        "targeting_support": {"distinct_sessions": 5, "distinct_days": 3},
    }
    c = candidate_mod.parse_candidate(raw, run_id="r", bot_id="team-bot-a")
    assert c.pkg_id == PKG_WEEKLY_STATUS
    assert c.is_suggestion
    assert c.claimed_value_tier == "medium"
    # Prose evidence lines carry no session → not citable on their own.
    assert c.citable_evidence == []
    assert c.claimed_evidence_refs  # but kept as readable refs


def test_no_candidate_decision_is_not_a_suggestion():
    c = _candidate(targeting_decision="no_candidate", suggested_gallery_pkg_id=None)
    assert not c.is_suggestion


# ── Gate: gallery-pkg-exists ─────────────────────────────────────────────────


def test_gate_pkg_missing_drops_no_install():
    report = _report(_strong_pm_tuples())
    c = _candidate(suggested_gallery_pkg_id=None)
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_true
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_NO_PKG


def test_gate_pkg_not_in_catalog_drops():
    report = _report(_strong_pm_tuples())
    c = _candidate(suggested_gallery_pkg_id="p-deadbeef")
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_true
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_NO_PKG


# ── Gate A: cited-quote-exists (anti-hallucination) ──────────────────────────


def test_gate_fabricated_quote_dropped():
    report = _report(_strong_pm_tuples())
    c = _candidate()
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_false
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_FABRICATED_QUOTE


def test_gate_passes_when_one_quote_verifies():
    report = _report(_strong_pm_tuples())
    c = _candidate(
        cited_evidence=[
            {"quote": "FABRICATED never said this", "session_id": "s9", "ts": "x"},
            {"quote": "please track these 7 tasks", "session_id": "s1", "ts": "y"},
        ]
    )

    def only_real(_bot, sid, quote):
        return "track these 7 tasks" in quote

    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=only_real
    )
    assert res.passed
    assert len(res.verified_evidence) == 1
    assert "track these 7 tasks" in res.verified_evidence[0].quote


# ── Gate: support-reconcile / already-installed ──────────────────────────────


def test_gate_support_below_floor_dropped():
    # Only 2 sessions → below SUPPORT_FLOOR_SESSIONS (3) → no candidate → pkg
    # not on the recomputed shortlist.
    thin = [
        _mk("task-management", session="s1", day="2026-06-10"),
        _mk("task-management", session="s2", day="2026-06-11"),
    ]
    report = _report(thin)
    c = _candidate()
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_true
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_SUPPORT_BELOW_FLOOR
    assert res.observation_worthy  # benign near-miss


def test_gate_already_installed_dropped():
    report = _report(_strong_pm_tuples(), installed_pkg_ids=[PKG_PROJECT_TRACKER])
    c = _candidate()
    res = gates.evaluate_candidate(
        c,
        catalog=GALLERY,
        targeting_report=report,
        installed_pkg_ids=[PKG_PROJECT_TRACKER],
        quote_verifier=_always_true,
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_ALREADY_INSTALLED


# ── Gate B: honest value ─────────────────────────────────────────────────────


def test_value_tier_computation():
    assert gates.compute_value_tier(
        distinct_sessions=9, purpose_aligned=True, no_current_coverage=True
    ) == "high"
    assert gates.compute_value_tier(
        distinct_sessions=4, purpose_aligned=True, no_current_coverage=True
    ) == "medium"
    assert gates.compute_value_tier(
        distinct_sessions=9, purpose_aligned=False, no_current_coverage=True
    ) == "low"


def test_value_recomputed_not_trusted_from_candidate():
    # Candidate self-asserts "high"; reconciled data gives only 4 sessions → the
    # poller must compute "medium" and never echo the LLM's inflated tier.
    tuples = [
        _mk("task-management", session=f"s{i}", day=f"2026-06-{10 + i:02d}")
        for i in range(4)
    ]
    report = _report(tuples)
    c = _candidate(value_estimate={"tier": "high", "basis": "trust me", "evidence_refs": []})
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_true
    )
    assert res.passed
    assert res.value_tier == "medium"  # NOT "high"


# ── Gate: cooldown / operator-declined ───────────────────────────────────────


def test_gate_operator_declined_suppressed():
    report = _report(_strong_pm_tuples())
    c = _candidate()
    res = gates.evaluate_candidate(
        c,
        catalog=GALLERY,
        targeting_report=report,
        quote_verifier=_always_true,
        operator_declined=lambda ck: True,
    )
    assert not res.passed
    assert res.reason_code == gates.REASON_OPERATOR_DECLINED
    assert not res.observation_worthy  # respect the cooldown — no resurface


# ── End-to-end: candidate → InstallApp proposal reaches Pending ──────────────


def test_pass_builds_install_app_proposal_in_pending_inbox():
    report = _report(_strong_pm_tuples())
    c = _candidate()
    res = gates.evaluate_candidate(
        c, catalog=GALLERY, targeting_report=report, quote_verifier=_always_true
    )
    assert res.passed
    assert res.value_tier == "high"

    proposal = gates.build_install_app_proposal(c, res, now=NOW)

    # Action shape
    assert proposal.action.kind == "InstallApp"
    assert proposal.action.app_id == PKG_PROJECT_TRACKER
    assert proposal.action.source == "gallery"
    # Surfacing contract
    assert proposal.surface == "improvement"
    assert proposal.altitude == gates.ALTITUDE_L2 == 2
    assert proposal.approval_audience == "bot_primary_user"
    assert proposal.dimension == "capabilities"
    # Value is the deterministic, computed tier with a real basis + cited refs
    assert proposal.value_estimate is not None
    assert proposal.value_estimate.tier == "high"
    assert proposal.value_estimate.evidence_refs
    assert proposal.value_estimate.basis  # the literal trace
    # Coalesce/cooldown lineage
    assert proposal.coalesce_key == "fit_review:team-bot-a:task-management"
    cause = proposal.provenance.signals["root_cause_attribution"]["cause_key"]
    assert cause == proposal.coalesce_key
    # Clock threaded — no wall-clock coupling
    assert proposal.created_at == NOW.isoformat()
    # The cited quote rides along as motivating evidence
    assert "track these 7 tasks" in proposal.problem

    # THE acceptance assertion (§6.4): it reaches the actionable Pending inbox.
    assert proposal_routing.proposal_surfaces_in_pending_inbox(proposal.to_dict())

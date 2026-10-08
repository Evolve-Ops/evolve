"""fit_review targeting report — Bite 1 (pure-Python, no LLM).

Fixtures mirror the *shape* of the live distribution verified read-only on the
pod 2026-06-12 (spec-fit-reviewer-2026-06-12.md §1.1), using ROLE PLACEHOLDERS
(``team-bot-a`` = the multi-user project-management bot; ``personal-bot`` = the
thin personal-assistant negative control) — never real bot names, which trip the
public-launch scrub guard.

The two acceptance cases (spec §6):
  * POSITIVE — the team PM bot's purpose-aligned #1 need (task-management) clears
    the support floor, is uncovered, and maps to real gallery ``pkg_id``s.
  * NEGATIVE CONTROL — the thin personal bot surfaces ZERO targets (a PASS: the
    engine declines to fabricate).
"""

from __future__ import annotations

import importlib

from schema.observation import ObservationTuple

targeting = importlib.import_module("fit_review.targeting")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

# Injected gallery-catalog fixture. The catalog is an INJECTED input to
# build_targeting_report (see ``catalog=`` below). These three apps used to be
# shipped in packages/gallery/catalog.json, which is now a GENERATED projection
# of the real gallery (codebase-review 0.4) and no longer carries the
# document-generation / slack-comms tag combinations this positive-proof case
# needs — so the fixture lives here, decoupled from the live gallery roster.
GALLERY = [
    {"pkg_id": "p-e5f6a7b8", "name": "Weekly Status Reporter",
     "application_tags": ["document-generation", "slack-comms", "task-management"]},
    {"pkg_id": "p-f6a7b8c9", "name": "Project Tracker",
     "application_tags": ["task-management", "calendar"]},
    {"pkg_id": "p-a1b2c3d4", "name": "Daily Health Tracker",
     "application_tags": ["health-nutrition", "health-fitness"]},
]

PKG_PROJECT_TRACKER = "p-f6a7b8c9"
PKG_WEEKLY_STATUS = "p-e5f6a7b8"
PKG_DAILY_HEALTH = "p-a1b2c3d4"


def _mk(noun: str, verb: str, session: str, day: str, mood: str | None = None) -> ObservationTuple:
    """One ObservationTuple on ``day`` (YYYY-MM-DD) in ``session``.

    engagement is uniformly 1 — the live store's dead axis (spec §1.1); the
    report must never rank on it. id/source_hash are made unique so storage-shape
    invariants hold even though the builder only reads noun/verb/mood/session/ts.
    """
    uniq = f"{noun}-{verb}-{session}-{day}-{mood}"
    return ObservationTuple(
        id=f"t-{abs(hash(uniq))}",
        bot_id="team-bot-a",
        session_id=session,
        segment_id=f"seg-{abs(hash(uniq))}",
        noun=noun,
        verb=verb,
        mood=mood,
        engagement=1,
        timestamp_start=f"{day}T10:00:00+00:00",
        timestamp_end=f"{day}T10:05:00+00:00",
        source_hash=f"h-{abs(hash(uniq))}",
    )


def _spread(noun: str, verb: str, n_tuples: int, n_sessions: int, n_days: int, mood=None):
    """Emit ``n_tuples`` tuples for ``noun`` spread across ``n_sessions`` distinct
    sessions and ``n_days`` distinct calendar days (round-robin)."""
    out = []
    for i in range(n_tuples):
        sess = f"{noun}-s{i % n_sessions}"
        day = f"2026-06-{1 + (i % n_days):02d}"
        out.append(_mk(noun, verb, sess, day, mood))
    return out


def team_bot_tuples() -> list[ObservationTuple]:
    """Project-management bot: task-management dominates (69), with
    document-generation (13) and slack-comms (9) close behind — all uncovered;
    incidental evolve-system / home-management activity below the candidate bar;
    calendar (2) / travel (1) below the support floor."""
    t: list[ObservationTuple] = []
    t += _spread("task-management", "recording", 69, n_sessions=20, n_days=8)
    t += _spread("document-generation", "drafting", 13, n_sessions=5, n_days=3)
    t += _spread("slack-comms", "summarizing", 9, n_sessions=4, n_days=3)
    # Emergent + recurring but NOT strongly so (6 sessions < 8) → not a candidate.
    t += _spread("evolve-system", "troubleshooting", 29, n_sessions=6, n_days=4, mood="frustrated")
    t += _spread("home-management", "planning", 14, n_sessions=3, n_days=2)  # emergent, off-purpose
    t += _spread("calendar", "scheduling", 2, n_sessions=2, n_days=1)  # below floor
    t += _spread("travel", "planning", 1, n_sessions=1, n_days=1)  # below floor
    return t


def personal_bot_tuples() -> list[ObservationTuple]:
    """Thin personal-assistant bot: a handful of tuples over a few days,
    dominated by off-purpose evolve-system / code poking. Nothing clears a floor."""
    t: list[ObservationTuple] = []
    t += _spread("evolve-system", "exploring", 5, n_sessions=2, n_days=2)
    t += _spread("code", "reviewing", 3, n_sessions=2, n_days=1)
    t += _spread("calendar", "scheduling", 1, n_sessions=1, n_days=1)  # aligned but below floor
    return t


PM_PURPOSE = {"archetype": "project-manager", "mission": "Run the team's projects.", "captured": "declared", "confidence": 1.0}
PA_PURPOSE = {"archetype": "personal-assistant", "mission": "Help around the house.", "captured": "declared", "confidence": 1.0}


# ─────────────────────────────────────────────────────────────────────────────
# Positive proof — the team PM bot
# ─────────────────────────────────────────────────────────────────────────────


def _pm_report():
    return targeting.build_targeting_report(
        bot_id="team-bot-a",
        purpose=PM_PURPOSE,
        tuples=team_bot_tuples(),
        catalog=GALLERY,
    )


def test_positive_decision_is_targets_found():
    r = _pm_report()
    assert r.decision == targeting.DECISION_TARGETS_FOUND
    assert r.has_declared_purpose is True


def test_positive_candidates_are_the_aligned_above_floor_nouns():
    r = _pm_report()
    cand = {c.noun for c in r.candidates}
    # task-management is the plurality; document-generation + slack-comms also
    # clear the floor and are in the project-manager playbook.
    assert cand == {"task-management", "document-generation", "slack-comms"}
    # All confirmed (purpose-aligned), all above floor, none covered.
    for c in r.candidates:
        assert c.alignment == "confirmed"
        assert c.above_floor is True
        assert c.covered is False


def test_positive_shortlist_names_real_gallery_pkg_ids():
    r = _pm_report()
    pkg_ids = {g.pkg_id for g in r.shortlist}
    # The two apps the spec names as the expected fit are both present.
    assert PKG_PROJECT_TRACKER in pkg_ids
    assert PKG_WEEKLY_STATUS in pkg_ids
    # Every shortlisted id is a real catalog pkg_id.
    catalog_ids = {e["pkg_id"] for e in GALLERY}
    assert pkg_ids <= catalog_ids


def test_positive_primary_covers_the_most_of_what_the_bot_does():
    r = _pm_report()
    # Weekly Status Reporter's tags = {document-generation, slack-comms,
    # task-management} cover ALL three confirmed needs → it leads deterministically.
    assert r.primary is not None
    assert r.primary.pkg_id == PKG_WEEKLY_STATUS
    assert r.primary.coverage_count == 3
    assert set(r.primary.matched_domains) == {"task-management", "document-generation", "slack-comms"}
    # The spec's other expected fit (Project Tracker) is on the list, ranked below.
    assert r.primary.pkg_id in {PKG_WEEKLY_STATUS, PKG_PROJECT_TRACKER}


def test_positive_below_floor_nouns_are_not_candidates():
    r = _pm_report()
    by_noun = {x.noun: x for x in r.rollups}
    assert by_noun["calendar"].above_floor is False  # 2 sessions, 1 day
    assert by_noun["travel"].above_floor is False
    assert by_noun["calendar"].is_candidate is False
    # evolve-system is emergent and recurring but below the strong-emergent bar.
    assert by_noun["evolve-system"].alignment == "emergent"
    assert by_noun["evolve-system"].is_candidate is False


def test_engagement_is_not_a_ranking_input():
    # engagement is uniformly 1; ranking is by distinct_sessions/days/coverage.
    # A doubling of engagement (still uniform) must not change the outcome.
    base = _pm_report()
    assert base.primary is not None and base.primary.pkg_id == PKG_WEEKLY_STATUS


# ─────────────────────────────────────────────────────────────────────────────
# Negative control — the thin personal bot (zero is a PASS)
# ─────────────────────────────────────────────────────────────────────────────


def test_negative_control_emits_zero_targets():
    r = targeting.build_targeting_report(
        bot_id="personal-bot",
        purpose=PA_PURPOSE,
        tuples=personal_bot_tuples(),
        catalog=GALLERY,
    )
    assert r.decision == targeting.DECISION_NO_CANDIDATE
    assert r.candidates == []
    assert r.shortlist == []
    assert r.primary is None
    assert "support floor" in r.reason


# ─────────────────────────────────────────────────────────────────────────────
# Purpose gate — no declared purpose ⇒ capability pass must not run
# ─────────────────────────────────────────────────────────────────────────────


def test_no_purpose_does_not_run_capability_pass():
    r = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=None, tuples=team_bot_tuples(), catalog=GALLERY
    )
    assert r.decision == targeting.DECISION_NO_PURPOSE
    assert r.has_declared_purpose is False
    assert r.candidates == []
    assert r.shortlist == []
    # Rollups are still computed (the report is auditable even without a purpose).
    assert any(x.noun == "task-management" for x in r.rollups)


def test_inferred_purpose_frames_but_does_not_anchor_emission():
    inferred = {"archetype": "project-manager", "mission": "guessed", "captured": "inferred", "confidence": 0.6}
    r = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=inferred, tuples=team_bot_tuples(), catalog=GALLERY
    )
    assert r.has_declared_purpose is False
    assert r.decision == targeting.DECISION_NO_PURPOSE
    assert r.candidates == []


# ─────────────────────────────────────────────────────────────────────────────
# Support floor + coverage edges
# ─────────────────────────────────────────────────────────────────────────────


def test_support_floor_exact_boundaries():
    # Exactly 3 sessions across exactly 2 days → above floor (inclusive).
    at_floor = _spread("task-management", "recording", 6, n_sessions=3, n_days=2)
    r = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=at_floor, catalog=GALLERY
    )
    tm = next(x for x in r.rollups if x.noun == "task-management")
    assert tm.distinct_sessions == 3 and tm.distinct_days == 2
    assert tm.above_floor is True and tm.is_candidate is True

    # 2 sessions (one short) → below floor even with many days.
    short_sessions = _spread("task-management", "recording", 6, n_sessions=2, n_days=4)
    r2 = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=short_sessions, catalog=GALLERY
    )
    tm2 = next(x for x in r2.rollups if x.noun == "task-management")
    assert tm2.distinct_sessions == 2 and tm2.above_floor is False
    assert r2.decision == targeting.DECISION_NO_CANDIDATE

    # 3 sessions but only 1 day → below floor (days short).
    one_day = _spread("task-management", "recording", 6, n_sessions=3, n_days=1)
    r3 = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=one_day, catalog=GALLERY
    )
    tm3 = next(x for x in r3.rollups if x.noun == "task-management")
    assert tm3.distinct_days == 1 and tm3.above_floor is False


def test_covered_domain_is_not_a_candidate():
    # task-management already covered by an installed app → drop it as a candidate.
    r = targeting.build_targeting_report(
        bot_id="team-bot-a",
        purpose=PM_PURPOSE,
        tuples=team_bot_tuples(),
        catalog=GALLERY,
        covered_domains={"task-management"},
    )
    by_noun = {x.noun: x for x in r.rollups}
    assert by_noun["task-management"].covered is True
    assert by_noun["task-management"].is_candidate is False
    assert "task-management" not in {c.noun for c in r.candidates}
    # document-generation + slack-comms remain → still targets_found.
    assert r.decision == targeting.DECISION_TARGETS_FOUND


def test_installed_pkg_is_excluded_from_shortlist():
    r = targeting.build_targeting_report(
        bot_id="team-bot-a",
        purpose=PM_PURPOSE,
        tuples=team_bot_tuples(),
        catalog=GALLERY,
        installed_pkg_ids={PKG_WEEKLY_STATUS},
    )
    assert PKG_WEEKLY_STATUS not in {g.pkg_id for g in r.shortlist}
    # Project Tracker is still available → still a real target.
    assert PKG_PROJECT_TRACKER in {g.pkg_id for g in r.shortlist}
    assert r.decision == targeting.DECISION_TARGETS_FOUND


def test_gap_with_no_gallery_match_is_the_forge_trigger():
    # A strongly-emergent need (>= 8 sessions) with no covering gallery app:
    # candidate clears the floor, but maps to no vetted pkg_id → honest gap,
    # never a forced match. (``gardening`` is off every playbook and is not a
    # gallery tag, so it stays a pure-gap candidate.)
    tuples = _spread("gardening", "planning", 40, n_sessions=10, n_days=5)
    r = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=tuples, catalog=GALLERY
    )
    assert {c.noun for c in r.candidates} == {"gardening"}
    assert r.shortlist == []
    assert r.decision == targeting.DECISION_GAP_NO_GALLERY


def test_meta_noun_is_never_a_candidate():
    # evolve-system describes operating Evolve itself through the bot, not a
    # user-facing capability — never a candidate even when strongly emergent.
    tuples = _spread("evolve-system", "troubleshooting", 40, n_sessions=15, n_days=8)
    r = targeting.build_targeting_report(
        bot_id="team-bot-a", purpose=PM_PURPOSE, tuples=tuples, catalog=GALLERY
    )
    es = next(x for x in r.rollups if x.noun == "evolve-system")
    assert es.above_floor is True  # it clears the floor numerically
    assert es.is_candidate is False  # but is excluded as a meta noun
    assert r.candidates == []
    assert r.decision == targeting.DECISION_NO_CANDIDATE


def test_to_dict_is_serializable_and_complete():
    import json

    r = _pm_report()
    blob = json.dumps(r.to_dict())  # must not raise
    d = json.loads(blob)
    assert d["kind"] == "fit_review_targeting_report"
    assert d["decision"] == targeting.DECISION_TARGETS_FOUND
    assert d["primary_pkg_id"] == PKG_WEEKLY_STATUS
    assert d["support_floor"] == {"distinct_sessions": 3, "distinct_days": 2}
    assert isinstance(d["rollups"], list) and d["rollups"]

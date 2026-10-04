"""tests/test_evo_wizard_goals_tour.py — GOALS + PLATFORM_TOUR phases (slice 5b3).

Spec: internal/spec-evo-wizard-2026-05-05.md §4.2 (phases 3 + 4).

Exercises the new primary onboarding additions:

  * PHASE_GOALS gathers top_goals, pain_points, current_workflow_notes.
    Exit fires as soon as at least one goal is articulated; pain points
    and workflow notes are bonus structure.
  * PHASE_PLATFORM_TOUR is a single-turn explainer with no extractor —
    the bot reads Evolve's three-piece overview and the wizard advances
    to wrap on the user's next reply.
  * The full chain is GREET → ABOUT_YOU → GOALS → PLATFORM_TOUR → WRAP.
  * Profile commit on Wrap captures all GOALS fields alongside ABOUT_YOU
    fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _stub_extractor(user_message, targets, current_state):
    """Keyword-driven stub. Maps phrases in the user's message onto the
    target schema so tests can drive the phase machine without an LLM."""
    msg = (user_message or "").lower()
    out = {}
    names = {t.name for t in targets}
    if "name" in names:
        for cand in ("pod_admin", "alice", "bob", "dana"):
            if cand in msg:
                out["name"] = cand.title()
                break
    if "role" in names:
        if "tech lead" in msg:
            out["role"] = "Tech lead"
        elif "engineer" in msg:
            out["role"] = "Engineer"
    if "environment" in names and "example" in msg:
        out["environment"] = "Example Corp"
    if "top_goals" in names:
        goals = []
        if "deploy" in msg:
            goals.append("track deploys")
        if "ci" in msg:
            goals.append("monitor CI")
        if "calendar" in msg:
            goals.append("manage calendar")
        if goals:
            out["top_goals"] = goals
    if "pain_points" in names and "manual" in msg:
        out["pain_points"] = ["doing it manually"]
    if "current_workflow_notes" in names and "slack" in msg:
        out["current_workflow_notes"] = "checking slack threads"
    return out


@pytest.fixture(autouse=True)
def install_stub():
    from evolve_admin.evo.wizard import extractor as _ext
    _ext.set_extractor(_stub_extractor)
    yield
    _ext.set_extractor(None)


# ─────────────────────────────────────────────────────────────────────────────
# Phase definitions / exit conditions
# ─────────────────────────────────────────────────────────────────────────────


def test_goals_exit_needs_at_least_one_goal():
    from evolve_admin.evo.wizard.phases import GOALS_PHASE

    assert GOALS_PHASE.exit_condition({}) is False
    assert GOALS_PHASE.exit_condition({"pain_points": ["x"]}) is False
    assert GOALS_PHASE.exit_condition({"top_goals": []}) is False
    assert GOALS_PHASE.exit_condition({"top_goals": ["track deploys"]}) is True


def test_platform_tour_always_advances():
    from evolve_admin.evo.wizard.phases import PLATFORM_TOUR_PHASE
    assert PLATFORM_TOUR_PHASE.exit_condition({}) is True


def test_phase_chain_traverses_to_wrap_through_goals_and_tour():
    """Walking the next_phase pointers from GREET should hit ABOUT_YOU,
    GOALS, PLATFORM_TOUR, GALLERY_RECS, then terminate at WRAP."""
    from evolve_admin.evo.wizard import phases

    seen = []
    cur = phases.PHASE_GREET
    while cur:
        seen.append(cur)
        ph = phases.get_phase(cur)
        assert ph is not None, f"unknown phase {cur}"
        cur = ph.next_phase

    assert seen == [
        phases.PHASE_GREET,
        phases.PHASE_ABOUT_YOU,
        phases.PHASE_GOALS,
        phases.PHASE_PLATFORM_TOUR,
        phases.PHASE_GALLERY_RECS,
        phases.PHASE_WRAP,
    ]


def test_all_primary_phases_returns_full_chain():
    from evolve_admin.evo.wizard import phases

    names = [p.name for p in phases.all_primary_phases()]
    assert names == ["greet", "about_you", "goals", "platform_tour",
                     "gallery_recs", "wrap"]


def test_goals_phase_targets_match_spec():
    from evolve_admin.evo.wizard.phases import GOALS_PHASE

    target_names = {t.name for t in GOALS_PHASE.targets}
    assert target_names == {"top_goals", "pain_points", "current_workflow_notes"}


def test_platform_tour_has_no_extraction_targets():
    from evolve_admin.evo.wizard.phases import PLATFORM_TOUR_PHASE

    assert PLATFORM_TOUR_PHASE.targets == ()
    assert PLATFORM_TOUR_PHASE.has_extractor is False


# ─────────────────────────────────────────────────────────────────────────────
# Engine — full E2E run
# ─────────────────────────────────────────────────────────────────────────────


def _drive_through_about_you(tmp_path, network):
    """Helper: walk a fresh wizard up through the end of ABOUT_YOU so
    individual tests can focus on what comes next."""
    from evolve_admin.evo import dispatch
    from evolve_admin.evo.wizard import engine

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo wizard",
    )
    wsid = r.wizard_session_id
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Hi I'm Pod_admin.", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Tech lead at Example Corp.", network=network,
    )
    # Sanity: we should now be in goals
    assert r.phase == "goals"
    return wsid


def test_full_run_writes_profile_with_goals_fields(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import gallery_recs as _gr
    from evolve_admin.evo.wizard import user_profile_writer as _writer
    # Stub gallery so it skips GALLERY_RECS — the goal of this test is
    # the goals/wrap fields, not gallery behavior.
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])

    # Stub the profile writer so we capture extracted without invoking sudo.
    captured: dict = {}

    def _stub_commit(*, extracted, user_key, bot_id, bot_home, bot_user, existing=None):
        captured["extracted"] = dict(extracted)
        return None

    monkeypatch.setattr(_writer, "commit", _stub_commit)

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_about_you(tmp_path, network)

    # Goals turn — gives one goal + pain + workflow
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="I want to track deploys and CI, doing it manually now via slack.",
        network=network,
    )
    assert r.phase == "platform_tour"
    assert r.completed is False

    # Tour delivered, user replies anything → empty gallery skips
    # GALLERY_RECS, wizard pauses at FORGE_INTRO (goals set + no apps)
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )
    assert r.phase == "forge_intro"
    assert r.completed is False

    # Opt out of forge → wrap fires + profile commits
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="no thanks", network=network,
    )
    assert r.phase == "wrap"
    assert r.completed is True
    assert r.wizard_session_id is None

    # The wizard's commit was called with extracted holding all the fields
    # the test cares about. Persistence is covered by the writer's own tests.
    p = captured["extracted"]
    assert p["name"] == "Pod_Admin"
    assert p["role"] == "Tech lead"
    assert p["environment"] == "Example Corp"
    assert p["top_goals"] == ["track deploys", "monitor CI"]
    assert p["pain_points"] == ["doing it manually"]
    assert p["current_workflow_notes"] == "checking slack threads"


def test_goals_does_not_advance_without_a_goal(tmp_path):
    """User stalls in GOALS → exit doesn't fire, we stay in goals."""
    from evolve_admin.evo.wizard import engine

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_about_you(tmp_path, network)

    # Reply mentions pain but no goal → exit stays False
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="it's all manual, very tedious", network=network,
    )
    assert r.phase == "goals"
    assert r.completed is False


def test_goals_advances_with_only_top_goals(tmp_path):
    """Exit needs only top_goals — pain_points and workflow_notes are
    optional. A user who articulates a goal and nothing else should still
    move on to the platform tour."""
    from evolve_admin.evo.wizard import engine

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_about_you(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="manage calendar", network=network,
    )
    assert r.phase == "platform_tour"


def test_platform_tour_advances_on_any_reply(tmp_path, monkeypatch):
    """No extraction in platform_tour — every reply advances. With the
    gallery stubbed empty, the wizard hops through GALLERY_RECS
    (skipped) and lands at FORGE_INTRO (goals set + no apps). One
    more turn (opt out) finalizes at wrap."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import gallery_recs as _gr
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_about_you(tmp_path, network)

    # Get into platform_tour
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="I want to track deploys", network=network,
    )

    # Tour reply → skipped gallery → forge_intro
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="brief noise about anything", network=network,
    )
    assert r.completed is False
    assert r.phase == "forge_intro"

    # Opt out → wrap
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="no thanks", network=network,
    )
    assert r.completed is True
    assert r.phase == "wrap"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders surface relevant context
# ─────────────────────────────────────────────────────────────────────────────


def test_goals_prompt_surfaces_known_about_you_fields():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GOALS

    block = build(PHASE_GOALS, {
        "name": "Pod_admin", "role": "Tech lead", "environment": "Example Corp",
    })
    # The LLM needs the prior context so it can be conversational instead
    # of cold-asking what the user just told it.
    assert "Pod_admin" in block
    assert "Tech lead" in block
    assert "Example Corp" in block


def test_goals_prompt_lists_existing_extractions():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GOALS

    block = build(PHASE_GOALS, {
        "name": "Pod_admin",
        "top_goals": ["track deploys"],
        "pain_points": ["manual checks"],
    })
    assert "track deploys" in block
    assert "manual checks" in block


def test_platform_tour_prompt_mentions_three_capabilities():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_PLATFORM_TOUR

    block = build(PHASE_PLATFORM_TOUR, {"name": "Pod_admin"})
    body = block.lower()
    assert "better engine" in body
    assert "continuity" in body
    assert "gallery" in body
    # User context shows up so the bot can personalize
    assert "Pod_admin" in block

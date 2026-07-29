"""tests/test_evo_wizard_gallery_recs.py — gallery recommendations phase (5b5).

Spec: docs/spec-evo-wizard-2026-05-05.md §4.2 phase 5.

Exercises:
  * Candidate loader: empty gallery returns []; installed-on-this-bot
    pkgs filtered out; profile-keyword scoring orders top-K correctly.
  * Reply classifier: accept (named), accept (only-one shortcut),
    accept-all, dismiss-all, ambiguous; phrase vs whole-word matching
    so "no" inside "not sure" doesn't false-classify.
  * Engine: ambiguous reply re-renders prompt; accept advances and
    finalizes same turn; dismiss-all advances and finalizes; full E2E
    captures apps_accepted into the user profile.
  * Skip-when-empty: PLATFORM_TOUR → empty gallery → straight to WRAP.
  * Phase chain: GREET → ABOUT_YOU → GOALS → PLATFORM_TOUR →
    GALLERY_RECS → WRAP.
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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────────


def _stub(user_message, targets, current_state):
    """Drive the primary chain through to platform_tour for E2E tests."""
    msg = (user_message or "").lower()
    out = {}
    names = {t.name for t in targets}
    if "name" in names and "pod_admin" in msg:
        out["name"] = "Pod_admin"
    if "role" in names and "tech lead" in msg:
        out["role"] = "Tech lead"
    if "environment" in names and "palace" in msg:
        out["environment"] = "Example Corp"
    if "top_goals" in names and "deploy" in msg:
        out["top_goals"] = ["track deploys"]
    if "current_tooling" in names and "github" in msg:
        out["current_tooling"] = ["github"]
    return out


@pytest.fixture(autouse=True)
def install_stub():
    from evolve_admin.evo.wizard import extractor as _ext
    _ext.set_extractor(_stub)
    yield
    _ext.set_extractor(None)


def _fake_gallery(*pkgs):
    """Build a fake gallery loader that returns the given pkg dicts."""
    def _loader(shared_dir, bot_ids):
        return [dict(p) for p in pkgs]
    return _loader


def _seed_pkgs():
    return [
        {"pkg_id": "p-cal", "display_name": "Calendar Sync",
         "description": "Syncs Google Calendar every 15 min",
         "application_tags": ["calendar", "scheduling"], "installed_on": []},
        {"pkg_id": "p-ci", "display_name": "CI Monitor",
         "description": "Watches GitHub Actions for failed CI runs",
         "application_tags": ["ci", "github", "deploy"], "installed_on": []},
        {"pkg_id": "p-task", "display_name": "Task Manager",
         "description": "Tag-based task tracking with CLI",
         "application_tags": ["productivity"], "installed_on": []},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Candidate loader
# ─────────────────────────────────────────────────────────────────────────────


def test_load_candidates_empty_when_gallery_empty(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import gallery_recs
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages", lambda d, b: [])
    assert gallery_recs.load_candidates(tmp_path, "team_bot_a", {}, top_k=3) == []


def test_load_candidates_filters_installed(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import gallery_recs
    import evolve_admin.applications.gallery as _g

    pkgs = _seed_pkgs()
    pkgs[0]["installed_on"] = ["team_bot_a"]  # already installed → excluded
    monkeypatch.setattr(_g, "list_gallery_packages", lambda d, b: pkgs)
    out = gallery_recs.load_candidates(tmp_path, "team_bot_a", {}, top_k=3)
    pkg_ids = [c["pkg_id"] for c in out]
    assert "p-cal" not in pkg_ids
    assert "p-ci" in pkg_ids
    assert "p-task" in pkg_ids


def test_load_candidates_orders_by_profile_match(tmp_path, monkeypatch):
    """Profile keyword overlap should bubble matching apps to the top."""
    from evolve_admin.evo.wizard import gallery_recs
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages", lambda d, b: _seed_pkgs())
    profile = {
        "top_goals": ["track deploys"],
        "current_tooling": ["github"],
    }
    out = gallery_recs.load_candidates(tmp_path, "team_bot_a", profile, top_k=3)
    # CI Monitor matches "github" + "deploy" → should be first
    assert out[0]["pkg_id"] == "p-ci"


def test_load_candidates_top_k_caps(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import gallery_recs
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages", lambda d, b: _seed_pkgs())
    out = gallery_recs.load_candidates(tmp_path, "team_bot_a", {}, top_k=2)
    assert len(out) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Reply classifier
# ─────────────────────────────────────────────────────────────────────────────


def _candidates():
    return [
        {"pkg_id": "p-cal", "display_name": "Calendar Sync",
         "description": "Syncs Google Calendar"},
        {"pkg_id": "p-ci", "display_name": "CI Monitor",
         "description": "Watches GitHub Actions"},
        {"pkg_id": "p-task", "display_name": "Task Manager",
         "description": "Tag-based task tracking"},
    ]


@pytest.mark.parametrize("text,expected_intent,expected_accepted", [
    ("yes calendar one", "accept", ["p-cal"]),
    ("the CI monitor please", "accept", ["p-ci"]),
    ("calendar and ci", "accept", {"p-cal", "p-ci"}),
    ("install all of them", "accept", {"p-cal", "p-ci", "p-task"}),
    ("yes all three", "accept", {"p-cal", "p-ci", "p-task"}),
    ("none of these", "dismiss_all", []),
    ("skip", "dismiss_all", []),
    ("not really", "dismiss_all", []),
    ("maybe later", "dismiss_all", []),
    ("hmm not sure", "ambiguous", []),  # 'no' substring shouldn't false-cancel
    ("hmm", "ambiguous", []),
])
def test_classify_reply(text, expected_intent, expected_accepted):
    from evolve_admin.evo.wizard.gallery_recs import classify_reply

    r = classify_reply(text, _candidates())
    assert r["intent"] == expected_intent
    if isinstance(expected_accepted, list):
        assert r["accepted"] == expected_accepted
    else:
        assert set(r["accepted"]) == expected_accepted


def test_classify_only_one_candidate_yes_means_that_one():
    """When there's only one candidate on offer, "yes" without naming
    is unambiguous."""
    from evolve_admin.evo.wizard.gallery_recs import classify_reply

    one = [_candidates()[0]]
    r = classify_reply("yes please", one)
    assert r["intent"] == "accept"
    assert r["accepted"] == ["p-cal"]


def test_classify_named_accept_dismisses_others():
    """Picking one app implicitly dismisses the others on the same prompt."""
    from evolve_admin.evo.wizard.gallery_recs import classify_reply

    r = classify_reply("the calendar one please", _candidates())
    assert r["accepted"] == ["p-cal"]
    assert set(r["dismissed"]) == {"p-ci", "p-task"}


# ─────────────────────────────────────────────────────────────────────────────
# Phase chain + Phase definition
# ─────────────────────────────────────────────────────────────────────────────


def test_phase_chain_includes_gallery_recs_before_wrap():
    from evolve_admin.evo.wizard import phases

    seen = []
    cur = phases.PHASE_GREET
    while cur:
        seen.append(cur)
        cur = phases.get_phase(cur).next_phase

    i_pt = seen.index(phases.PHASE_PLATFORM_TOUR)
    i_gr = seen.index(phases.PHASE_GALLERY_RECS)
    i_wrap = seen.index(phases.PHASE_WRAP)
    assert i_pt < i_gr < i_wrap


def test_gallery_recs_phase_has_no_targets():
    from evolve_admin.evo.wizard.phases import GALLERY_RECS_PHASE
    assert GALLERY_RECS_PHASE.targets == ()
    assert GALLERY_RECS_PHASE.has_extractor is False


# ─────────────────────────────────────────────────────────────────────────────
# Engine — full E2E with non-empty gallery
# ─────────────────────────────────────────────────────────────────────────────


def _drive_through_platform_tour(tmp_path, network):
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
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Tech lead at Example Corp.", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="track deploys with github", network=network,
    )
    return wsid


def test_full_run_with_accept_writes_apps_to_profile(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine, state
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages",
                        _fake_gallery(*_seed_pkgs()))
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_platform_tour(tmp_path, network)

    # platform_tour reply → advance to gallery_recs (rendered with candidates)
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )
    assert r.phase == "gallery_recs"
    assert r.completed is False
    body = r.system_append or ""
    # Candidates surfaced in the prompt
    assert "Calendar Sync" in body or "CI Monitor" in body

    # Accept a specific app → advance + finalize same turn
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="install the CI monitor please", network=network,
    )
    assert r.phase == "wrap"
    assert r.completed is True
    assert r.wizard_session_id is None

    # apps_accepted/dismissed are gallery state, captured in wizard state
    # (not persisted to the v2 user profile — those are gallery decisions,
    # not user-identity facts).
    st = state.read_state(tmp_path, "team_bot_a", wsid)
    assert "CI Monitor" in (st.extracted.get("apps_accepted") or [])
    assert "p-cal" in (st.extracted.get("apps_dismissed") or [])
    assert "p-task" in (st.extracted.get("apps_dismissed") or [])


def test_dismiss_all_finalizes_with_no_apps(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine, state
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages",
                        _fake_gallery(*_seed_pkgs()))
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_platform_tour(tmp_path, network)

    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )  # → gallery_recs
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="none thanks", network=network,
    )
    # No apps accepted + goals set → forge_intro offered, then opt out
    assert r.phase == "forge_intro"
    assert r.completed is False
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="no thanks", network=network,
    )
    assert r.phase == "wrap"
    assert r.completed is True

    st = state.read_state(tmp_path, "team_bot_a", wsid)
    assert (st.extracted.get("apps_accepted") or []) == []
    # dismissed should hold all candidate pkg_ids
    assert set(st.extracted.get("apps_dismissed") or []) >= {"p-cal", "p-ci", "p-task"}


def test_ambiguous_reply_re_renders_gallery_recs(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages",
                        _fake_gallery(*_seed_pkgs()))
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_platform_tour(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )  # → gallery_recs
    # Ambiguous reply — stays in phase, re-renders with same candidates
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="hmm not sure", network=network,
    )
    assert r.phase == "gallery_recs"
    assert r.completed is False


def test_install_all_accepts_every_candidate(tmp_path, monkeypatch):
    from evolve_admin.evo.wizard import engine, state
    import evolve_admin.applications.gallery as _g

    monkeypatch.setattr(_g, "list_gallery_packages",
                        _fake_gallery(*_seed_pkgs()))
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_platform_tour(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="install all of them", network=network,
    )
    assert r.completed is True
    st = state.read_state(tmp_path, "team_bot_a", wsid)
    accepted = set(st.extracted.get("apps_accepted") or [])
    assert {"Calendar Sync", "CI Monitor", "Task Manager"} <= accepted


# ─────────────────────────────────────────────────────────────────────────────
# Skip-when-empty optimization
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_gallery_skips_to_wrap(tmp_path, monkeypatch):
    """When platform_tour exits and the gallery is empty, we should
    skip past GALLERY_RECS rather than burning a turn on a 'no recs'
    prompt — but if the user has goals + no apps installed, we still
    pause at FORGE_INTRO before finalizing. Opting out gets us to
    wrap on the next turn."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import gallery_recs as _gr
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    wsid = _drive_through_platform_tour(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="ok cool", network=network,
    )
    # Skipped GALLERY_RECS → forge_intro
    assert r.phase == "forge_intro"
    assert r.completed is False
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="no thanks", network=network,
    )
    assert r.phase == "wrap"
    assert r.completed is True


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────


def test_gallery_recs_prompt_renders_candidates():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GALLERY_RECS

    block = build(
        PHASE_GALLERY_RECS,
        {"name": "Pod_admin", "top_goals": ["track deploys"]},
        context={"bot_id": "team_bot_a", "gallery_candidates": _candidates()},
    )
    assert "Calendar Sync" in block
    assert "CI Monitor" in block
    assert "Task Manager" in block
    assert "track deploys" in block  # profile signals surfaced


def test_gallery_recs_prompt_handles_no_candidates():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GALLERY_RECS

    block = build(
        PHASE_GALLERY_RECS, {"name": "Pod_admin"},
        context={"bot_id": "team_bot_a", "gallery_candidates": []},
    )
    # The handler skips before this prompt is rendered in practice, but
    # the builder itself should degrade gracefully if it ever is.
    assert "no" in block.lower() or "no recommendations" in block.lower()

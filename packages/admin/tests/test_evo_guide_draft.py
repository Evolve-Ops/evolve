"""tests/test_evo_guide_draft.py — conversational bot-guide drafting (slice 5b6).

Spec: docs/spec-evo-wizard-2026-05-05.md §5.5.

Exercises:
  * `evo guide` dispatch — primary-only (secondary blocked); starts a
    `guide_drafter` session at GUIDE_GATHER; pre-populates from existing
    guide when one exists.
  * GUIDE_GATHER extracts audience/tone/do_say/dont_say/body_outline.
    Exit fires on audience + tone OR audience + body. Preview keywords
    ("show me", "preview", "see it") force advance to GUIDE_CONFIRM.
  * GUIDE_CONFIRM gates save on user reply: save / edit / cancel /
    unclear (re-render).
  * Save writes the guide via `evo.guide.write_guide` and emits a
    `guide_save` audit event.
  * Cancel does not write.
  * Edit kicks back to GUIDE_GATHER preserving extracted fields.
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


def _stub(user_message, targets, current_state):
    """Keyword-driven extractor; covers the guide_drafter target schema."""
    msg = (user_message or "").lower()
    out = {}
    names = {t.name for t in targets}
    if "audience" in names:
        if "engineering" in msg:
            out["audience"] = "engineering team"
        elif "household" in msg:
            out["audience"] = "household"
    if "tone" in names:
        if "direct" in msg:
            out["tone"] = "direct, no emoji"
        elif "warm" in msg:
            out["tone"] = "warm and chatty"
    if "do_say" in names and "ping" in msg:
        out["do_say"] = ["ping on CI failures"]
    if "dont_say" in names and "hr" in msg:
        out["dont_say"] = ["no HR or payroll"]
    if "body_outline" in names:
        if "ci" in msg or "deploy" in msg:
            out["body_outline"] = "Team_bot_a handles CI and deploy notifications."
        elif "household" in msg:
            out["body_outline"] = "Bot for household chores."
    return out


@pytest.fixture(autouse=True)
def install_stub():
    from evolve_admin.evo.wizard import extractor as _ext
    _ext.set_extractor(_stub)
    yield
    _ext.set_extractor(None)


def _admin_network(tmp_path: Path, *, admin_id: str = "12345") -> dict:
    """Network where the test caller resolves to admin (skips CHALLENGE)."""
    return {
        "members": ["team_bot_a"], "sharedDir": str(tmp_path),
        "pod": {
            "admin_passphrase": "charles",
            "primary_passphrase": "darwin",
            "admins": {"external_ids": {"telegram": [admin_id]}},
        },
        "bots": {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase definitions
# ─────────────────────────────────────────────────────────────────────────────


def test_guide_gather_exit_needs_audience_plus_tone_or_body():
    from evolve_admin.evo.wizard.phases import GUIDE_GATHER_PHASE

    assert GUIDE_GATHER_PHASE.exit_condition({}) is False
    assert GUIDE_GATHER_PHASE.exit_condition({"audience": "team"}) is False
    assert GUIDE_GATHER_PHASE.exit_condition({"tone": "direct"}) is False
    # audience + tone
    assert GUIDE_GATHER_PHASE.exit_condition(
        {"audience": "team", "tone": "direct"}
    ) is True
    # audience + body
    assert GUIDE_GATHER_PHASE.exit_condition(
        {"audience": "team", "body_outline": "...prose..."}
    ) is True


def test_guide_confirm_is_terminal():
    from evolve_admin.evo.wizard.phases import GUIDE_CONFIRM_PHASE

    assert GUIDE_CONFIRM_PHASE.next_phase is None
    assert GUIDE_CONFIRM_PHASE.has_extractor is False


def test_all_guide_draft_phases():
    from evolve_admin.evo.wizard import phases

    names = [p.name for p in phases.all_guide_draft_phases()]
    assert names == ["guide_gather", "guide_confirm"]


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_guide_starts_session_for_admin(tmp_path):
    from evolve_admin.evo import dispatch

    network = _admin_network(tmp_path)
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo guide",
    )
    assert r.subcommand == "guide"
    assert r.role == "admin"
    assert r.wizard_session_id == "ext:telegram:12345"
    assert "guide_gather" in (r.system_append or "").lower()


def test_dispatch_guide_starts_session_for_primary(tmp_path):
    from evolve_admin.evo import dispatch

    # Primary (recorded) — admin pod field absent
    network = {
        "members": ["team_bot_a"], "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {
            "team_bot_a": {"primary_user": {"external_ids": {"telegram": "OWNER"}}}
        },
    }
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="OWNER", raw_text="evo guide",
    )
    assert r.role == "primary"
    assert r.wizard_session_id == "ext:telegram:OWNER"


def test_dispatch_guide_blocks_secondary(tmp_path):
    from evolve_admin.evo import dispatch

    network = {
        "members": ["team_bot_a"], "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {
            "team_bot_a": {"primary_user": {"external_ids": {"telegram": "OWNER"}}}
        },
    }
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="STRANGER", raw_text="evo guide",
    )
    assert r.role == "secondary"
    # The registry's available_to=PRIMARY check fires before our custom
    # `evo guide` handler, so secondaries get the standard "isn't
    # available" rejection rather than the custom primary-only stub.
    # Either is fine for v1 — the point is no session started.
    assert r.wizard_session_id is None
    assert "isn't available" in (r.system_append or "")


def test_dispatch_guide_pre_populates_from_existing_guide(tmp_path):
    """If a guide already exists, dispatch seeds the gather state with
    the existing fields so the flow doubles as an editor."""
    from evolve_admin.evo import dispatch
    from evolve_admin.evo import guide as _guide
    from evolve_admin.evo.wizard import state as _state

    # Plant a pre-existing guide
    _guide.write_guide(
        tmp_path, "team_bot_a",
        frontmatter={
            "audience": "engineering team",
            "tone": "direct, no emoji",
            "do_say": ["ping on CI failures"],
            "dont_say": ["no HR"],
        },
        body="# Team_bot_a\n\nTeam_bot_a handles CI.\n",
        authored_by="pod_admin_user",
    )

    network = _admin_network(tmp_path)
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo guide",
    )
    wsid = r.wizard_session_id
    assert wsid is not None

    st = _state.read_state(tmp_path, "team_bot_a", wsid)
    # Existing fields pre-loaded
    assert st.extracted["audience"] == "engineering team"
    assert st.extracted["tone"] == "direct, no emoji"
    assert st.extracted["do_say"] == ["ping on CI failures"]
    assert st.extracted["dont_say"] == ["no HR"]
    # body_outline holds the existing prose body
    assert "Team_bot_a handles CI" in st.extracted["body_outline"]


# ─────────────────────────────────────────────────────────────────────────────
# Engine — gather + confirm + save
# ─────────────────────────────────────────────────────────────────────────────


def _start(tmp_path, network):
    from evolve_admin.evo import dispatch
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo guide",
    )
    return r.wizard_session_id


def test_full_save_flow_writes_guide_and_audits(tmp_path):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import guide as _guide
    from evolve_admin.evo import audit as _audit

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)

    # Audience → still in gather
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="for the engineering team", network=network,
    )
    assert r.phase == "guide_gather"

    # Tone → exit fires, advance to confirm
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="direct, no emoji", network=network,
    )
    assert r.phase == "guide_confirm"
    # The confirm prompt renders the proposed guide
    assert "audience" in (r.system_append or "").lower()

    # Save
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="save", network=network,
    )
    assert r.completed is True
    assert r.wizard_session_id is None
    assert "saved" in (r.system_append or "").lower()

    # Guide written
    g = _guide.read_guide(tmp_path, "team_bot_a")
    assert g is not None
    assert g.frontmatter["audience"] == "engineering team"
    assert g.frontmatter["tone"] == "direct, no emoji"

    # Audit event
    events = _audit.read_events(tmp_path)
    assert any(e["action"] == "guide_save" for e in events)


def test_cancel_does_not_write_guide(tmp_path):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import guide as _guide

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)

    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="for the engineering team", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="direct, no emoji", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="cancel", network=network,
    )
    assert r.completed is True
    assert r.wizard_session_id is None
    assert "not saved" in (r.system_append or "").lower()
    # No guide on disk
    assert _guide.read_guide(tmp_path, "team_bot_a") is None


def test_edit_kicks_back_to_gather_preserving_state(tmp_path):
    from evolve_admin.evo.wizard import engine, state

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="for the engineering team", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="direct, no emoji", network=network,
    )
    # In confirm now — say edit
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="edit", network=network,
    )
    assert r.phase == "guide_gather"
    assert r.completed is False

    # State preserved
    st = state.read_state(tmp_path, "team_bot_a", wsid)
    assert st.extracted["audience"] == "engineering team"
    assert st.extracted["tone"] == "direct, no emoji"


def test_unclear_reply_re_renders_confirm(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="engineering team direct tone", network=network,
    )
    # Should be in confirm now (audience+tone extracted in one turn)
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="hmm not sure", network=network,
    )
    assert r.phase == "guide_confirm"
    assert r.completed is False  # still gating save


def test_preview_keyword_force_advances_from_gather(tmp_path):
    """Even with only audience filled, 'show me' should advance to
    confirm so the user can see the working draft."""
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="for the engineering team", network=network,
    )
    # Audience only — exit wouldn't fire normally. Preview kicks us to confirm.
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="show me", network=network,
    )
    assert r.phase == "guide_confirm"
    assert r.completed is False


@pytest.mark.parametrize("save_word", [
    "save", "yes", "ok", "go ahead", "looks good", "ship it", "do it",
    "approve", "confirm",
])
def test_various_save_keywords_trigger_write(tmp_path, save_word):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import guide as _guide

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="engineering team direct tone", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message=save_word, network=network,
    )
    assert r.completed is True
    assert _guide.read_guide(tmp_path, "team_bot_a") is not None


@pytest.mark.parametrize("cancel_word", [
    "cancel", "no", "don't save", "discard", "abort",
])
def test_various_cancel_keywords_skip_write(tmp_path, cancel_word):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import guide as _guide

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="engineering team direct tone", network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message=cancel_word, network=network,
    )
    assert r.completed is True
    assert _guide.read_guide(tmp_path, "team_bot_a") is None


def test_state_audience_is_guide_drafter(tmp_path):
    from evolve_admin.evo.wizard import state

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    st = state.read_state(tmp_path, "team_bot_a", wsid)
    assert st.audience == "guide_drafter"


def test_save_overwrites_existing_guide(tmp_path):
    """Editing-and-saving updates the guide on disk rather than refusing
    or appending. The write_guide implementation handles this — we just
    confirm the engine-driven save path goes through."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import guide as _guide

    # Start with an existing guide
    _guide.write_guide(
        tmp_path, "team_bot_a",
        frontmatter={"audience": "old audience", "tone": "old tone"},
        body="# Team_bot_a\n\nOld body.\n",
        authored_by="alice",
    )
    original = _guide.read_guide(tmp_path, "team_bot_a")
    assert original.frontmatter["audience"] == "old audience"

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    # Update audience + tone (seeded from existing, but the user's
    # extraction overrides via stub)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="actually for the household, with warm tone",
        network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="save", network=network,
    )

    updated = _guide.read_guide(tmp_path, "team_bot_a")
    assert updated.frontmatter["audience"] == "household"
    assert updated.frontmatter["tone"] == "warm and chatty"


def test_post_completion_returns_none(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _admin_network(tmp_path)
    wsid = _start(tmp_path, network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="engineering team direct tone", network=network,
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="save", network=network,
    )
    # Further turns return None
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="thanks", network=network,
    )
    assert r is None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────


def test_gather_prompt_lists_missing_fields():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GUIDE_GATHER

    block = build(
        PHASE_GUIDE_GATHER, {"audience": "engineering team"},
        context={"bot_id": "team_bot_a"},
    )
    assert "engineering team" in block
    # Tone, do_say, dont_say, body should be flagged as missing
    body = block.lower()
    assert "tone" in body
    assert "summary" in body or "body" in body


def test_gather_prompt_signals_editing_when_seed_present():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GUIDE_GATHER

    block = build(
        PHASE_GUIDE_GATHER, {"audience": "engineering team"},
        context={"bot_id": "team_bot_a", "editing_existing": True},
    )
    assert "edit" in block.lower()


def test_confirm_prompt_renders_draft():
    from evolve_admin.evo.wizard.prompts import build
    from evolve_admin.evo.wizard.phases import PHASE_GUIDE_CONFIRM

    block = build(
        PHASE_GUIDE_CONFIRM, {
            "audience": "engineering team",
            "tone": "direct, no emoji",
            "do_say": ["ping on CI failures"],
            "dont_say": ["no HR"],
            "body_outline": "Team_bot_a handles CI.",
        },
        context={"bot_id": "team_bot_a"},
    )
    # Frontmatter shape
    assert "audience" in block
    assert "engineering team" in block
    assert "ping on CI failures" in block
    assert "no HR" in block
    # Body
    assert "Team_bot_a handles CI" in block
    # Save options surfaced for the bot
    body = block.lower()
    assert "save" in body and "cancel" in body and "edit" in body

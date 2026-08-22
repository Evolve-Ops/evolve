"""tests/test_evo_wizard.py — wizard engine MVP (slice 5a).

Spec: docs/spec-evo-wizard-2026-05-05.md §4.

Exercises:
  * state file round-trip (atomic write, status lifecycle)
  * phase definitions and exit conditions
  * extractor test seam + JSON parsing tolerance
  * engine turn loop end-to-end with a deterministic stub extractor
  * dispatch's `evo wizard` branch (primary path + secondary stub)
  * /api/evo/wizard/turn route happy path + 404 + 400
  * profile commit on Wrap
  * plugin user-key derivation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Test extractor — deterministic stub used end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def _stub_extractor(user_message, targets, current_state):
    """Crude keyword-matcher; good enough for the engine integration tests."""
    msg = (user_message or "").lower()
    out = {}
    target_names = {t.name for t in targets}
    if "name" in target_names:
        for cand in ("pod_admin", "alice", "bob", "dana"):
            if cand in msg:
                out["name"] = cand.title()
                break
    if "role" in target_names:
        if "tech lead" in msg:
            out["role"] = "Tech lead"
        elif "engineer" in msg:
            out["role"] = "Engineer"
        elif "designer" in msg:
            out["role"] = "Designer"
    if "environment" in target_names:
        if "example" in msg:
            out["environment"] = "Example Corp"
        elif "household" in msg:
            out["environment"] = "household"
    if "current_tooling" in target_names and "slack" in msg:
        out["current_tooling"] = ["slack"]
    # Slice 5b3 fields — only used by tests that walk through the GOALS
    # phase. Other tests don't care about these targets.
    if "top_goals" in target_names:
        goals = []
        if "deploy" in msg:
            goals.append("track deploys")
        if "calendar" in msg:
            goals.append("manage calendar")
        if goals:
            out["top_goals"] = goals
    if "pain_points" in target_names and "manual" in msg:
        out["pain_points"] = ["doing it manually"]
    return out


@pytest.fixture(autouse=True)
def install_stub_extractor():
    from evolve_admin.evo.wizard import extractor as _ext

    _ext.set_extractor(_stub_extractor)
    yield
    _ext.set_extractor(None)  # restore default


# ─────────────────────────────────────────────────────────────────────────────
# State module
# ─────────────────────────────────────────────────────────────────────────────


def test_state_initialize_writes_in_progress(tmp_path):
    from evolve_admin.evo.wizard import state

    st = state.initialize(
        tmp_path,
        bot_id="team_bot_a",
        user_key="ext:telegram:12345",
        audience="primary",
        initial_phase="greet",
    )
    assert st.status == "in_progress"
    assert st.current_phase == "greet"
    assert st.created_at != ""
    assert st.updated_at != ""

    # Round-trip
    on_disk = state.read_state(tmp_path, "team_bot_a", "ext:telegram:12345")
    assert on_disk is not None
    assert on_disk.bot_id == "team_bot_a"
    assert on_disk.audience == "primary"
    assert on_disk.is_active() is True


def test_state_path_encodes_colons(tmp_path):
    """Filename must be portable — colons in user_key shouldn't survive."""
    from evolve_admin.evo.wizard import state

    p = state.state_path(tmp_path, "team_bot_a", "ext:telegram:12345")
    assert ":" not in p.name


def test_state_read_returns_none_for_absent(tmp_path):
    from evolve_admin.evo.wizard import state
    assert state.read_state(tmp_path, "team_bot_a", "nope") is None


def test_state_read_returns_none_for_corrupt(tmp_path):
    from evolve_admin.evo.wizard import state

    p = state.state_path(tmp_path, "team_bot_a", "ext:telegram:12345")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-json")
    assert state.read_state(tmp_path, "team_bot_a", "ext:telegram:12345") is None


def test_state_mark_completed(tmp_path):
    from evolve_admin.evo.wizard import state

    st = state.initialize(
        tmp_path, bot_id="team_bot_a", user_key="u", audience="primary",
        initial_phase="greet",
    )
    state.mark_completed(tmp_path, st)
    on_disk = state.read_state(tmp_path, "team_bot_a", "u")
    assert on_disk.status == "completed"
    assert on_disk.completed_at


# ─────────────────────────────────────────────────────────────────────────────
# Phases
# ─────────────────────────────────────────────────────────────────────────────


def test_about_you_exit_requires_name_plus_role_or_env():
    from evolve_admin.evo.wizard.phases import ABOUT_YOU_PHASE

    assert ABOUT_YOU_PHASE.exit_condition({}) is False
    assert ABOUT_YOU_PHASE.exit_condition({"name": "Pod_admin"}) is False
    assert ABOUT_YOU_PHASE.exit_condition({"name": "Pod_admin", "role": "TL"}) is True
    assert ABOUT_YOU_PHASE.exit_condition({"name": "Pod_admin", "environment": "X"}) is True


def test_greet_always_advances():
    from evolve_admin.evo.wizard.phases import GREET_PHASE
    assert GREET_PHASE.exit_condition({}) is True


def test_phase_chain_terminates_at_wrap():
    from evolve_admin.evo.wizard import phases

    seen = set()
    cur = phases.initial_phase()
    while cur:
        assert cur not in seen, "phase graph must be acyclic"
        seen.add(cur)
        ph = phases.get_phase(cur)
        assert ph is not None
        cur = ph.next_phase
    assert "wrap" in seen


# ─────────────────────────────────────────────────────────────────────────────
# Extractor (JSON parsing only — Anthropic call mocked via test seam)
# ─────────────────────────────────────────────────────────────────────────────


def test_extractor_parse_strips_fences():
    from evolve_admin.evo.wizard.extractor import _parse_json_object

    raw = "```json\n{\"name\": \"Pod_admin\"}\n```"
    parsed = _parse_json_object(raw)
    assert parsed == {"name": "Pod_admin"}


def test_extractor_parse_recovers_object_from_prose():
    from evolve_admin.evo.wizard.extractor import _parse_json_object

    raw = "Sure, here's the data: {\"name\": \"Pod_admin\", \"role\": \"TL\"}. hope this helps."
    parsed = _parse_json_object(raw)
    assert parsed["name"] == "Pod_admin"
    assert parsed["role"] == "TL"


def test_extractor_coerce_drops_unknown_fields():
    from evolve_admin.evo.wizard.extractor import _coerce_to_targets
    from evolve_admin.evo.wizard.phases import FieldSpec

    targets = (FieldSpec("name", "first name", "string"),)
    out = _coerce_to_targets({"name": "Pod_admin", "fingerprint": "evil"}, targets)
    assert out == {"name": "Pod_admin"}


def test_extractor_coerce_string_list_handles_string_input():
    from evolve_admin.evo.wizard.extractor import _coerce_to_targets
    from evolve_admin.evo.wizard.phases import FieldSpec

    targets = (FieldSpec("tools", "tools", "string_list"),)
    # Model returns a single string — we tolerate and lift to a 1-list.
    out = _coerce_to_targets({"tools": "Slack"}, targets)
    assert out == {"tools": ["Slack"]}


def test_extractor_set_seam_overrides_default():
    from evolve_admin.evo.wizard import extractor

    def stub(_msg, _targets, _state):
        return {"name": "Override"}

    extractor.set_extractor(stub)
    try:
        out = extractor.extract_fields("anything", ((extractor.FieldSpec("name", "n", "string")),))
        assert out == {"name": "Override"}
    finally:
        extractor.set_extractor(None)


# ─────────────────────────────────────────────────────────────────────────────
# Engine — end-to-end turn loop
# ─────────────────────────────────────────────────────────────────────────────


def test_engine_full_run_completes_with_extracted_state(tmp_path, monkeypatch):
    """Full primary onboarding chain: GREET → ABOUT_YOU → GOALS →
    PLATFORM_TOUR → GALLERY_RECS → FORGE_INTRO → WRAP. With the
    gallery stubbed empty, GALLERY_RECS is skipped; the user opts
    out of forge to reach wrap — six turns to complete.

    Profile-write side effects are isolated by stubbing the user_profile
    writer's commit; the writer's own tests (test_evo_user_profile_writer.py)
    cover the v2 file path and sudo-cp sequence end to end. This test is
    about the engine flow and the resulting in-memory ``state.extracted``.
    """
    from evolve_admin.evo.wizard import engine, state
    from evolve_admin.evo.wizard import gallery_recs as _gr
    from evolve_admin.evo.wizard import user_profile_writer as _writer
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])

    # Capture wizard commits so we can assert on extracted contents
    # without invoking the sudo path or touching a bot user's home dir.
    captured: dict = {}

    def _stub_commit(*, extracted, user_key, bot_id, bot_home, bot_user, existing=None):
        captured["extracted"] = dict(extracted)
        captured["user_key"] = user_key
        captured["bot_id"] = bot_id
        return None

    monkeypatch.setattr(_writer, "commit", _stub_commit)

    # Turn 1 — start (greet)
    r1 = engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345", audience="primary"
    )
    assert r1.phase == "greet"
    assert r1.completed is False
    assert r1.wizard_session_id == "ext:telegram:12345"

    # Turn 2 — user provides name → advance to about_you
    r2 = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345",
        user_message="Hi, I'm Pod_admin.",
    )
    assert r2.phase == "about_you"
    assert r2.completed is False

    # Turn 3 — user provides role+environment → advance to goals
    r3 = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345",
        user_message="I'm a tech lead at Example Corp.",
    )
    assert r3.phase == "goals"
    assert r3.completed is False

    # Turn 4 — user articulates a goal → advance to platform_tour
    r4 = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345",
        user_message="I want to track deploys, doing it manually now.",
    )
    assert r4.phase == "platform_tour"
    assert r4.completed is False

    # Turn 5 — empty gallery → skip GALLERY_RECS, route to FORGE_INTRO
    # (top_goals set + apps_accepted empty fires the offer)
    r5 = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345",
        user_message="ok cool",
    )
    assert r5.phase == "forge_intro"
    assert r5.completed is False

    # Turn 6 — opt out of forge → finalize at wrap
    r6 = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:12345",
        user_message="no thanks",
    )
    assert r6.phase == "wrap"
    assert r6.completed is True
    assert r6.wizard_session_id is None

    # State on disk → completed
    st = state.read_state(tmp_path, "team_bot_a", "ext:telegram:12345")
    assert st.status == "completed"
    assert st.current_phase == "wrap"
    assert st.extracted["name"] == "Pod_Admin"
    assert st.extracted["role"] == "Tech lead"
    assert st.extracted["environment"] == "Example Corp"
    assert st.extracted["top_goals"] == ["track deploys"]
    assert st.extracted["pain_points"] == ["doing it manually"]

    # The wizard called user_profile_writer.commit with the right inputs.
    # The writer itself is tested separately.
    assert captured["bot_id"] == "team_bot_a"
    assert captured["user_key"] == "ext:telegram:12345"
    assert captured["extracted"]["name"] == "Pod_Admin"
    assert captured["extracted"]["role"] == "Tech lead"
    assert captured["extracted"]["top_goals"] == ["track deploys"]


def test_engine_post_completion_returns_none(tmp_path, monkeypatch):
    """After wrap, further turns return None — plugin clears its session
    ref. Walks the full chain to completion before testing."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import gallery_recs as _gr
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])

    engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key="u", audience="primary",
    )
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key="u", user_message="Pod_admin")
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="u",
        user_message="tech lead at Example Corp",
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="u",
        user_message="track deploys",
    )
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="u", user_message="ok",
    )
    # Empty gallery skipped → forge_intro; opt out → wrap
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="u", user_message="no thanks",
    )
    # After wrap, further turns return None
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key="u", user_message="hi")
    assert r is None


def test_engine_resume_restores_in_progress(tmp_path):
    from evolve_admin.evo.wizard import engine, state

    r1 = engine.start_session(tmp_path, bot_id="team_bot_a", user_key="u", audience="primary")
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key="u", user_message="Pod_admin")
    # Now imagine a process restart — the plugin loses its in-memory session
    # but the on-disk state is still in_progress at about_you.
    st = state.read_state(tmp_path, "team_bot_a", "u")
    assert st.current_phase == "about_you"

    # Re-starting picks up the same in-progress state, doesn't reset.
    r2 = engine.start_session(tmp_path, bot_id="team_bot_a", user_key="u", audience="primary")
    assert r2.phase == "about_you"

    # And the next turn carries on with previously-extracted fields.
    st2 = state.read_state(tmp_path, "team_bot_a", "u")
    assert st2.extracted.get("name") == "Pod_Admin"


def test_engine_short_responses_dont_advance_about_you(tmp_path):
    """If the extractor pulls nothing useful, ABOUT_YOU shouldn't exit."""
    from evolve_admin.evo.wizard import engine

    engine.start_session(tmp_path, bot_id="team_bot_a", user_key="u", audience="primary")
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key="u", user_message="Pod_admin")
    # User stalls — empty extraction, exit condition unmet
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="u", user_message="hmm not sure",
    )
    assert r is not None
    assert r.phase == "about_you"
    assert r.completed is False


# ─────────────────────────────────────────────────────────────────────────────
# Profile commit
#
# The wizard now writes v2 user profiles to the bot user's home via the
# sudo cp pattern. The mapping logic and the sudo path are tested in
# test_evo_user_profile_writer.py — the legacy {shared_dir}/profiles/users/
# JSON store has been removed (spec docs/spec-user-profile-2026-05-07.md §D7).
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# user-key derivation
# ─────────────────────────────────────────────────────────────────────────────


def test_derive_user_key_prefers_pod_user_for_primary():
    from evolve_admin.evo.identity import derive_user_key

    network = {
        "members": ["team_bot_a"],
        "bots": {
            "team_bot_a": {
                "primary_user": {
                    "pod_user": "pod_admin_user",
                    "external_ids": {"telegram": "12345"},
                }
            }
        },
    }
    assert derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="12345"
    ) == "pod:pod_admin_user"


def test_derive_user_key_uses_ext_for_secondary():
    from evolve_admin.evo.identity import derive_user_key

    network = {
        "members": ["team_bot_a"],
        "bots": {
            "team_bot_a": {
                "primary_user": {
                    "pod_user": "pod_admin_user",
                    "external_ids": {"telegram": "12345"},  # primary = 12345
                }
            }
        },
    }
    # Different sender → secondary
    assert derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="99999"
    ) == "ext:telegram:99999"


def test_derive_user_key_anon_fallback():
    from evolve_admin.evo.identity import derive_user_key

    network = {"members": ["team_bot_a"]}
    assert derive_user_key(
        network, bot_id="team_bot_a", channel=None, sender_external_id=None
    ) == "anon:team_bot_a"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch — `evo wizard` branch
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_wizard_starts_session_for_primary(tmp_path):
    from evolve_admin.evo import dispatch

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo wizard",
    )
    assert r.subcommand == "wizard"
    assert r.mode == "speak"
    assert r.wizard_session_id == "ext:telegram:12345"
    assert "[EVO WIZARD" in (r.system_append or "")


def test_dispatch_wizard_returns_stub_for_secondary(tmp_path):
    from evolve_admin.evo import dispatch

    # Recorded primary on slack; sender is on slack but a different ID → secondary.
    # As of slice 5b2, secondaries no longer get a "coming soon" stub — they
    # enter the wizard at the CHALLENGE phase to verify their identity via
    # passphrase. Detailed challenge behavior lives in
    # test_evo_wizard_challenge.py; here we just confirm the routing.
    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {"admin_passphrase": "charles", "primary_passphrase": "darwin"},
        "bots": {
            "team_bot_a": {
                "primary_user": {"external_ids": {"slack": "U-OWNER"}}
            }
        },
    }
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="slack",
        sender_external_id="U-MEMBER", raw_text="evo wizard",
    )
    assert r.subcommand == "wizard"
    assert r.mode == "speak"
    assert r.role == "secondary"
    # Wizard session is started — secondary lands at CHALLENGE, not at a stub
    assert r.wizard_session_id == "ext:slack:U-MEMBER"
    assert "challenge" in (r.system_append or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    def _atomic_save(data, path=None):
        target = Path(path) if path is not None else network_path
        target.write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(evo_routes, "save_network", _atomic_save)
    monkeypatch.setattr(_cfg, "save_network", _atomic_save)

    app = Flask(__name__)
    app.config["TESTING"] = True
    evo_routes.register_evo_routes(app, network_path)
    return app, tmp_path


def test_route_wizard_start_then_turn_then_complete(evo_app, monkeypatch):
    """End-to-end via the /api/evo/wizard/turn route. With the gallery
    stubbed empty, GALLERY_RECS is skipped and we reach wrap in five
    turns."""
    from evolve_admin.evo.wizard import gallery_recs as _gr
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])
    app, _ = evo_app
    client = app.test_client()

    # Turn 1 — dispatch starts wizard
    r1 = client.post(
        "/api/evo/dispatch",
        json={"bot_id": "team_bot_a", "channel": "telegram",
              "sender_external_id": "12345", "raw_text": "evo wizard"},
    )
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["subcommand"] == "wizard"
    wsid = d1["wizard_session_id"]
    assert wsid == "ext:telegram:12345"

    # Turn 2 — user replies via wizard turn
    r2 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid,
              "user_message": "I'm Pod_admin."},
    )
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["phase"] == "about_you"
    assert d2["completed"] is False
    assert d2["wizard_session_id"] == wsid

    # Turn 3 — user supplies role + env → advance to goals
    r3 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid,
              "user_message": "tech lead at Example Corp"},
    )
    assert r3.status_code == 200
    d3 = r3.get_json()
    assert d3["phase"] == "goals"
    assert d3["completed"] is False

    # Turn 4 — user articulates a goal → advance to platform_tour
    r4 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid,
              "user_message": "track deploys"},
    )
    assert r4.status_code == 200
    d4 = r4.get_json()
    assert d4["phase"] == "platform_tour"
    assert d4["completed"] is False

    # Turn 5 — empty gallery → skip GALLERY_RECS, route to forge_intro
    r5 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid,
              "user_message": "ok cool"},
    )
    assert r5.status_code == 200
    d5 = r5.get_json()
    assert d5["phase"] == "forge_intro"
    assert d5["completed"] is False

    # Turn 6 — opt out of forge → finalize at wrap
    r6 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid,
              "user_message": "no thanks"},
    )
    assert r6.status_code == 200
    d6 = r6.get_json()
    assert d6["phase"] == "wrap"
    assert d6["completed"] is True
    assert d6["wizard_session_id"] is None

    # Turn 7 — post-completion returns 404, plugin clears session
    r7 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid, "user_message": "thanks"},
    )
    assert r7.status_code == 404


def test_route_wizard_turn_validates_inputs(evo_app):
    app, _ = evo_app
    client = app.test_client()
    r = client.post("/api/evo/wizard/turn", json={"bot_id": "team_bot_a"})
    assert r.status_code == 400
    r = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "", "wizard_session_id": "x", "user_message": "y"},
    )
    assert r.status_code == 400


def test_route_wizard_state_get(evo_app):
    app, _ = evo_app
    client = app.test_client()

    # No state yet → 404
    r = client.get("/api/evo/wizard/state?bot_id=team_bot_a&user_key=u")
    assert r.status_code == 404

    # Start a wizard
    client.post(
        "/api/evo/dispatch",
        json={"bot_id": "team_bot_a", "channel": "telegram",
              "sender_external_id": "12345", "raw_text": "evo wizard"},
    )
    r = client.get(
        "/api/evo/wizard/state?bot_id=team_bot_a&user_key=ext:telegram:12345",
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "in_progress"
    assert data["current_phase"] == "greet"


# ─────────────────────────────────────────────────────────────────────────────
# /api/evo/wizard/active — stateless wizard recovery for the plugin
# ─────────────────────────────────────────────────────────────────────────────
#
# Used by the plugin to find an active wizard's session_id when its
# in-memory BetterSessionState was wiped (gateway restart, plugin
# reload). Replaces the previous design where the plugin relied
# entirely on its own in-memory state.wizardSessionId, which was lost
# across restarts and stranded users mid-wizard.


def test_route_wizard_active_returns_null_when_no_wizard(evo_app):
    """No wizard for this caller → 200 with wizard_session_id: null.
    Plugin treats this as 'no wizard, continue normal flow.' Always
    200 (not 404) so the plugin doesn't have to distinguish a 404
    from a transport error."""
    app, _ = evo_app
    client = app.test_client()
    r = client.get(
        "/api/evo/wizard/active"
        "?bot_id=team_bot_a&channel=telegram&sender_external_id=99999",
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["wizard_session_id"] is None


def test_route_wizard_active_finds_in_flight_wizard(evo_app):
    """A wizard started via /api/evo/dispatch is findable via
    /api/evo/wizard/active using the same (bot, channel, sender)
    triple. Returns the session_id (== user_key) and current phase."""
    app, _ = evo_app
    client = app.test_client()

    # Start the wizard
    client.post(
        "/api/evo/dispatch",
        json={"bot_id": "team_bot_a", "channel": "telegram",
              "sender_external_id": "12345", "raw_text": "evo wizard"},
    )

    # Same triple → finds the wizard
    r = client.get(
        "/api/evo/wizard/active"
        "?bot_id=team_bot_a&channel=telegram&sender_external_id=12345",
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["wizard_session_id"] == "ext:telegram:12345"
    assert data["phase"] == "greet"


def test_route_wizard_active_validates_bot_id(evo_app):
    """``bot_id`` is required; the rest is optional (falls through to
    ``derive_user_key``'s anon: keying when channel/sender are missing)."""
    app, _ = evo_app
    client = app.test_client()
    r = client.get("/api/evo/wizard/active")
    assert r.status_code == 400


def test_route_wizard_active_returns_null_for_completed_state(evo_app):
    """A completed wizard is not 'active' — its state is marked
    ``completed`` on disk. The plugin must NOT route subsequent user
    messages through wizardTurn for completed sessions. Mark completion
    directly via the state-store API rather than driving every phase to
    actually complete (the primary chain has phase-specific extraction
    requirements that a turn-bashing loop can't satisfy)."""
    from evolve_admin.evo.wizard import state as _wizard_state
    app, shared_dir = evo_app
    client = app.test_client()

    # Start a wizard
    client.post(
        "/api/evo/dispatch",
        json={"bot_id": "team_bot_a", "channel": "telegram",
              "sender_external_id": "12345", "raw_text": "evo wizard"},
    )
    wsid = "ext:telegram:12345"

    # Sanity: while in_progress, active returns the session id
    r = client.get(
        "/api/evo/wizard/active"
        "?bot_id=team_bot_a&channel=telegram&sender_external_id=12345",
    )
    assert r.get_json()["wizard_session_id"] == wsid

    # Force-complete the state file (mark_completed mutates st.status
    # and rewrites the file).
    st = _wizard_state.read_state(shared_dir, "team_bot_a", wsid)
    assert st is not None
    _wizard_state.mark_completed(shared_dir, st)

    # Now active should return null — the wizard isn't active anymore
    r = client.get(
        "/api/evo/wizard/active"
        "?bot_id=team_bot_a&channel=telegram&sender_external_id=12345",
    )
    assert r.status_code == 200
    assert r.get_json()["wizard_session_id"] is None

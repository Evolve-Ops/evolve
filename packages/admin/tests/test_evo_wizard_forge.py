"""tests/test_evo_wizard_forge.py — wizard forge-via-messaging flow (slice 5b7b).

Spec: docs/spec-forge-via-messaging-2026-05-07.md.

Exercises the full FORGE_INTRO → FORGE_DESIGN → FORGE_CONFIRM chain
appended to the primary wizard, plus the manifest/job construction and
the notification emission. Forge code generation itself is stubbed
(monkeypatch on `forge_handlers.set_kickoff_runner`) so tests don't
need an Anthropic key.
"""

from __future__ import annotations

import json
import re
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


def _stub_extractor(user_message, targets, current_state):
    msg = (user_message or "").lower()
    out = {}
    names = {t.name for t in targets}
    if "name" in names and "pod_admin" in msg:
        out["name"] = "Pod_admin"
    if "role" in names and "tech lead" in msg:
        out["role"] = "Tech lead"
    if "environment" in names and "palace" in msg:
        out["environment"] = "Example Corp"
    if "top_goals" in names and "track" in msg:
        out["top_goals"] = ["track PR review status"]
    if "forge_app_name" in names:
        if "pr-watcher" in msg or ("pr" in msg and "watcher" in msg):
            out["forge_app_name"] = "pr-watcher"
        elif "deploy" in msg and "tracker" in msg:
            out["forge_app_name"] = "deploy-tracker"
    if "forge_description" in names:
        if "pr" in msg or "watcher" in msg:
            out["forge_description"] = "Watches PRs and pings when reviews are overdue"
        elif "deploy" in msg:
            out["forge_description"] = "Tracks deployments across environments"
    if "forge_capabilities" in names and "github" in msg:
        out["forge_capabilities"] = ["github"]
    if "forge_example_behaviors" in names and "ping" in msg:
        out["forge_example_behaviors"] = ["ping when review pending >24h"]
    if "forge_conversational_summary" in names and len(msg) > 30:
        out["forge_conversational_summary"] = msg[:200]
    return out


@pytest.fixture(autouse=True)
def install_extractor_stub():
    from evolve_admin.evo.wizard import extractor as _ext
    _ext.set_extractor(_stub_extractor)
    yield
    _ext.set_extractor(None)


@pytest.fixture(autouse=True)
def stub_gallery_empty(monkeypatch):
    """Empty gallery so GALLERY_RECS skip-when-empty fires; the wizard
    walks straight from PLATFORM_TOUR to FORGE_INTRO when forge trigger
    conditions hold."""
    from evolve_admin.evo.wizard import gallery_recs as _gr
    monkeypatch.setattr(_gr, "load_candidates", lambda *a, **kw: [])
    yield


@pytest.fixture
def kickoff_complete():
    """Substitute a stub runner that records the call AND emits a
    forge_complete notification synchronously."""
    from evolve_admin.evo.wizard import forge_handlers as _fh
    from evolve_admin.evo import notifications as _n

    captured = {}

    def runner(shared_dir, job_id, bot_id, user_key, app_name, app_id, conv_summary):
        captured["shared_dir"] = shared_dir
        captured["job_id"] = job_id
        captured["bot_id"] = bot_id
        captured["user_key"] = user_key
        captured["app_name"] = app_name
        captured["app_id"] = app_id
        captured["conv_summary"] = conv_summary
        _fh._emit_complete(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id, conv_summary=conv_summary,
        )

    _fh.set_kickoff_runner(runner)
    yield captured
    _fh.set_kickoff_runner(None)


@pytest.fixture
def kickoff_failed():
    from evolve_admin.evo.wizard import forge_handlers as _fh

    def runner(shared_dir, job_id, bot_id, user_key, app_name, app_id, conv_summary):
        _fh._emit_failed(
            shared_dir, user_key, bot_id=bot_id,
            app_name=app_name, app_id=app_id, conv_summary=conv_summary,
            reason="tests failed: ImportError on stub",
        )

    _fh.set_kickoff_runner(runner)
    yield
    _fh.set_kickoff_runner(None)


def _read_unread_eventually(tmp_path, user_key, kind, timeout=5.0):
    """Poll read_unread until an event of ``kind`` appears (or timeout).

    kick_off_build runs the (stubbed) runner on a daemon thread, so the
    notification lands asynchronously — asserting immediately after
    process_turn returns is a race the 2-core CI runners sometimes lose.
    read_unread is non-consuming, so polling is safe. Returns whatever
    events are visible at the end either way; the caller's assertions
    produce the real failure message."""
    import time
    from evolve_admin.evo import notifications as _n

    deadline = time.monotonic() + timeout
    while True:
        events = _n.read_unread(tmp_path, user_key)
        if any(e.get("kind") == kind for e in events) or time.monotonic() > deadline:
            return events
        time.sleep(0.02)


def _start_wizard(tmp_path, sender_external_id="12345"):
    from evolve_admin.evo import dispatch
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id=sender_external_id, raw_text="evo wizard",
    )
    return r.wizard_session_id, network


def _drive_to_forge_intro(tmp_path, network, wsid):
    """Walk wizard from greet to forge_intro (assumes empty gallery and
    goals→no apps_accepted → forge trigger)."""
    from evolve_admin.evo.wizard import engine
    msgs = [
        "Hi I'm Pod_admin.",
        "Tech lead at Palace.",
        "track PR review status",
        "ok cool",  # tour
    ]
    for m in msgs:
        engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message=m, network=network)


# ─────────────────────────────────────────────────────────────────────────────
# Phase chain + definitions
# ─────────────────────────────────────────────────────────────────────────────


def test_phase_chain_includes_forge_phases():
    from evolve_admin.evo.wizard import phases

    seen = []
    cur = phases.PHASE_GREET
    while cur:
        seen.append(cur)
        cur = phases.get_phase(cur).next_phase
    # FORGE_CONFIRM is terminal (next_phase=None) so the natural chain
    # walk doesn't reach WRAP through it; that's intentional. Forge phases
    # are reached as a branch from GALLERY_RECS, not the linear chain.
    assert phases.PHASE_GALLERY_RECS in seen


def test_all_forge_phases_returned():
    from evolve_admin.evo.wizard import phases

    names = [p.name for p in phases.all_forge_phases()]
    assert names == ["forge_intro", "forge_design", "forge_confirm"]


def test_forge_design_exit_needs_name_plus_description():
    from evolve_admin.evo.wizard.phases import FORGE_DESIGN_PHASE

    assert FORGE_DESIGN_PHASE.exit_condition({}) is False
    assert FORGE_DESIGN_PHASE.exit_condition({"forge_app_name": "x"}) is False
    assert FORGE_DESIGN_PHASE.exit_condition(
        {"forge_app_name": "x", "forge_description": "y"}
    ) is True


# ─────────────────────────────────────────────────────────────────────────────
# Trigger logic — forge_intro fires only when goals && no apps_accepted
# ─────────────────────────────────────────────────────────────────────────────


def test_should_offer_forge_with_goals_no_accepts():
    from evolve_admin.evo.wizard.engine import _should_offer_forge

    assert _should_offer_forge({"top_goals": ["x"]}) is True
    assert _should_offer_forge(
        {"top_goals": ["x"], "apps_accepted": ["something"]}
    ) is False
    assert _should_offer_forge({}) is False
    assert _should_offer_forge({"top_goals": []}) is False


def test_forge_intro_fires_when_gallery_empty_and_goals(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    from evolve_admin.evo.wizard import state
    st = state.read_state(tmp_path, "team_bot_a", wsid)
    assert st.current_phase == "forge_intro"


def test_no_forge_when_apps_accepted(tmp_path, monkeypatch):
    """If user accepts gallery apps, the forge trigger should not fire —
    gallery satisfied them, no need to build custom."""
    from evolve_admin.evo.wizard import engine, state, gallery_recs

    # Stub gallery candidates so user can accept one
    monkeypatch.setattr(gallery_recs, "load_candidates", lambda *a, **kw: [
        {"pkg_id": "p-cal", "display_name": "Calendar Sync",
         "description": "Calendar"},
    ])

    wsid, network = _start_wizard(tmp_path)
    for m in ["Hi I'm Pod_admin.", "Tech lead at Palace.",
              "track PR review status", "ok cool"]:
        engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message=m, network=network)
    # Now in gallery_recs — accept the candidate
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="yes the calendar one", network=network)
    # Goes straight to wrap, not forge_intro (apps_accepted satisfied)
    assert r.phase == "wrap"
    assert r.completed is True


def test_no_forge_when_no_goals(tmp_path):
    """User with no goals shouldn't be offered a custom build — they
    haven't articulated a need."""
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    msgs = [
        "Hi I'm Pod_admin.",
        "Tech lead at Palace.",
        "stuck — no goals",  # extractor returns nothing for top_goals
    ]
    # We'll never exit goals phase since no goals extracted → user stalled
    for m in msgs:
        r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                               user_message=m, network=network)
    # Stuck in goals — not in forge_intro
    assert r.phase == "goals"


# ─────────────────────────────────────────────────────────────────────────────
# Forge intro classifier + handler
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("yes please", "opt_in"),
    ("sure thing", "opt_in"),
    ("let's try", "opt_in"),
    ("go ahead", "opt_in"),
    ("build it", "opt_in"),
    ("no thanks", "opt_out"),
    ("not now", "opt_out"),
    ("maybe later", "opt_out"),
    ("skip", "opt_out"),
    ("cancel", "opt_out"),
    ("hmm", "ambiguous"),
    ("", "ambiguous"),
])
def test_classify_forge_intro(text, expected):
    from evolve_admin.evo.wizard.engine import _classify_forge_intro
    assert _classify_forge_intro(text) == expected


def test_opt_in_advances_to_forge_design(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="yes let's try", network=network)
    assert r.phase == "forge_design"
    assert r.completed is False


def test_opt_out_wraps_immediately(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="not now", network=network)
    assert r.phase == "wrap"
    assert r.completed is True
    assert r.wizard_session_id is None


# ─────────────────────────────────────────────────────────────────────────────
# Forge design + confirm gate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("build it", "build"),
    ("yes", "build"),
    ("ok", "build"),
    ("approve", "build"),
    ("cancel", "cancel"),
    ("don't build", "cancel"),
    ("shelve it", "cancel"),
    ("skip", "cancel"),
    ("edit", "edit"),
    ("change", "edit"),
    ("one more thing", "edit"),
    ("show me the spec", "show_spec"),
    ("hmm not sure", "ambiguous"),
])
def test_classify_forge_confirm(text, expected):
    from evolve_admin.evo.wizard.engine import _classify_forge_confirm
    assert _classify_forge_confirm(text) == expected


def test_design_extracts_app_name_and_description(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    # Design turn — provides both name and description in one go
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.",
        network=network,
    )
    assert r.phase == "forge_confirm"


def test_design_preview_keyword_force_advances(tmp_path):
    """Even with only partial state, 'show me' from FORGE_DESIGN should
    jump to FORGE_CONFIRM."""
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    # Provide only name — exit normally wouldn't fire
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher",  # only name — no description
        network=network,
    )
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="show me", network=network,
    )
    assert r.phase == "forge_confirm"


def test_confirm_edit_kicks_back_to_design(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="one more thing", network=network)
    assert r.phase == "forge_design"
    assert r.completed is False


def test_confirm_cancel_wraps_without_kickoff(tmp_path, kickoff_complete):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="cancel", network=network)
    assert r.phase == "wrap"
    assert r.completed is True
    # Stub runner shouldn't have been called
    assert kickoff_complete == {}


def test_confirm_show_spec_re_renders(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="show me the spec", network=network)
    assert r.phase == "forge_confirm"
    assert r.completed is False


def test_confirm_ambiguous_re_renders(tmp_path):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="hmm not sure", network=network)
    assert r.phase == "forge_confirm"
    assert r.completed is False


# ─────────────────────────────────────────────────────────────────────────────
# Build kickoff — manifest construction + notification emission
# ─────────────────────────────────────────────────────────────────────────────


def test_build_writes_manifest_and_emits_complete(tmp_path, kickoff_complete):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    r = engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                           user_message="build it!", network=network)
    assert r.phase == "wrap"
    assert r.completed is True

    # Manifest persisted
    mp = tmp_path / "applications" / "team_bot_a" / "pr-watcher.json"
    assert mp.exists()
    m = json.loads(mp.read_text())
    assert m["display_name"] == "pr-watcher"
    assert m["bot_id"] == "team_bot_a"
    assert m["source"] == "bot_created"
    assert m["context_snapshot"]["created_via"] == "chat"
    assert m["context_snapshot"]["requested_by_user_key"] == "ext:telegram:12345"
    assert re.match(r"^p-[a-f0-9]{8}$", m["pkg_id"])  # canonical form since Slice 3a

    # Notification was emitted (runner runs on a daemon thread — wait for it;
    # the stub fills `kickoff_complete` before emitting, so seeing the event
    # also orders the captured-shape assertions below)
    events = _read_unread_eventually(tmp_path, "ext:telegram:12345", "forge_complete")
    assert any(e["kind"] == "forge_complete" for e in events)

    # Stub runner was called with the right shape
    assert kickoff_complete["bot_id"] == "team_bot_a"
    assert kickoff_complete["user_key"] == "ext:telegram:12345"
    assert kickoff_complete["app_name"] == "pr-watcher"
    assert kickoff_complete["app_id"] == "pr-watcher"


def test_build_emits_failed_notification_on_runner_failure(tmp_path, kickoff_failed):
    from evolve_admin.evo.wizard import engine

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="build it!", network=network)

    # Runner runs on a daemon thread — wait for the notification to land.
    events = _read_unread_eventually(tmp_path, "ext:telegram:12345", "forge_failed")
    fail_events = [e for e in events if e["kind"] == "forge_failed"]
    assert len(fail_events) == 1
    assert "ImportError" in fail_events[0]["detail"]


def test_profile_excludes_forge_scratch_fields(tmp_path, kickoff_complete, monkeypatch):
    """Forge-design extracted fields are about a specific in-flight
    build, not properties of the user — they shouldn't make it onto the
    user-profile sections.

    In v2 this is structurally guaranteed by the mapper's whitelist
    (only name/role/environment/current_tooling/current_workflow_notes/
    top_goals/pain_points map into sections); this test is a regression
    guard against that whitelist drifting."""
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo.wizard import user_profile_writer as _writer

    captured: dict = {}

    def _stub_commit(*, extracted, user_key, bot_id, bot_home, bot_user, existing=None):
        captured["extracted"] = dict(extracted)
        # Build the profile to verify no forge_/_ fields leaked into sections
        captured["profile"] = _writer.build_profile_from_extracted(
            extracted=extracted, user_key=user_key, bot_id=bot_id
        )
        return None

    monkeypatch.setattr(_writer, "commit", _stub_commit)

    wsid, network = _start_wizard(tmp_path)
    _drive_to_forge_intro(tmp_path, network, wsid)
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="yes", network=network)
    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="Call it pr-watcher. It watches PRs.", network=network,
    )
    engine.process_turn(tmp_path, bot_id="team_bot_a", user_key=wsid,
                       user_message="build it!", network=network)

    # Wizard captured the user-level fields
    extracted = captured["extracted"]
    assert extracted.get("name") == "Pod_admin"
    assert extracted.get("top_goals") == ["track PR review status"]
    # And forge_* fields stayed in extracted (they drive the forge handoff)
    forge_keys = [k for k in extracted if k.startswith("forge_")]
    assert forge_keys, "expected forge_* fields in wizard state"

    # But the mapped profile has no forge_*/scratch keys anywhere in
    # sections — they don't survive the section mapping.
    profile = captured["profile"]
    for section_name, section_body in profile.sections.items():
        body = section_body or ""
        assert "forge_" not in body, (
            f"forge_ leaked into section {section_name!r}: {body!r}"
        )
        assert "pr-watcher" not in body, (
            f"forge build name leaked into section {section_name!r}"
        )


def test_kickoff_runs_async_by_default(tmp_path, kickoff_complete):
    """kick_off_build with run_async=True (default) spawns a daemon
    thread; the call returns immediately. We can't easily test thread
    semantics here, but we can confirm the call completes quickly and
    the runner observed the expected payload."""
    from evolve_admin.evo.wizard import forge_handlers

    extracted = {
        "name": "Pod_admin",
        "top_goals": ["x"],
        "forge_app_name": "test-app",
        "forge_description": "Does the test thing",
        "forge_conversational_summary": "A test app for testing",
    }
    result = forge_handlers.kick_off_build(
        tmp_path, "team_bot_a", "ext:telegram:99999", extracted,
        run_async=False,  # easier to verify in tests
    )
    assert result.ok is True
    assert result.app_id == "test-app"
    assert result.app_name == "test-app"
    assert result.pkg_id and re.match(r"^p-[a-f0-9]{8}$", result.pkg_id)  # canonical form since Slice 3a
    assert result.job_id and result.job_id.startswith("j-")


# ─────────────────────────────────────────────────────────────────────────────
# forge_handlers helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_slugify_app_name():
    from evolve_admin.evo.wizard.forge_handlers import slugify_app_name

    assert slugify_app_name("PR Watcher") == "pr-watcher"
    # Spaces become hyphens; non-alphanumerics like "!" are stripped.
    assert slugify_app_name("  Hello World!  ") == "hello-world"
    assert slugify_app_name("") == "untitled-app"
    assert slugify_app_name("   ") == "untitled-app"
    assert slugify_app_name("My_Cool_App") == "my-cool-app"


def test_synthetic_pkg_id_format():
    from evolve_admin.evo.wizard.forge_handlers import synthetic_pkg_id

    # Canonical p-<8hex> since manifest-v7 Slice 3a — the pkg_id becomes
    # the spec_id at the native-write conversion, so it must conform.
    pid = synthetic_pkg_id()
    assert re.match(r"^p-[a-f0-9]{8}$", pid)


def test_build_manifest_skeleton():
    from evolve_admin.evo.wizard.forge_handlers import build_manifest_from_state

    extracted = {
        "forge_app_name": "PR Watcher",
        "forge_description": "Watches PRs",
        "forge_capabilities": ["github", "slack"],
        "forge_example_behaviors": ["ping when overdue"],
        "forge_conversational_summary": "Bot that watches PRs.",
    }
    m = build_manifest_from_state("team_bot_a", extracted, "ext:telegram:12345")
    assert m["id"] == "pr-watcher"
    assert m["bot_id"] == "team_bot_a"
    assert m["display_name"] == "PR Watcher"
    assert m["description"] == "Watches PRs"
    assert m["conversational_summary"] == "Bot that watches PRs."
    assert "github" in m["build_spec"].lower()
    assert "ping when overdue" in m["build_spec"].lower()
    assert m["context_snapshot"]["created_via"] == "chat"
    assert m["context_snapshot"]["requested_by_user_key"] == "ext:telegram:12345"

"""tests/test_evo_wizard_challenge.py — wizard CHALLENGE phase (slice 5b2).

Spec: docs/spec-evo-wizard-2026-05-05.md §4.6.

Exercises the passphrase-driven front door of the wizard:

  * Dispatch routing by role:
      - admin → GREET
      - primary → GREET
      - secondary → CHALLENGE
      - unknown caller on unconfigured bot → GREET (v1 fallback)

  * CHALLENGE turn handling:
      - admin passphrase → claim_admin + advance to GREET
      - primary passphrase + no recorded primary → claim_primary + advance
      - primary passphrase + existing different primary → no claim, end
      - both passphrases → both claims (where possible)
      - decline ("skip", "no", …) → end gracefully
      - unrecognized text → end gracefully
      - audit log records the claim event

  * Route integration:
      - /api/evo/wizard/turn persists network mutations on dirty
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

from evolve_admin import external_ids as ext_ids  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def stub_extractor():
    """Replace the LLM extractor with a deterministic stub. CHALLENGE
    bypasses the extractor entirely, but later phases (GREET, ABOUT_YOU)
    invoke it on resume — we don't want those tests reaching the
    Anthropic API."""
    from evolve_admin.evo.wizard import extractor as _ext

    _ext.set_extractor(lambda *a, **kw: {})
    yield
    _ext.set_extractor(None)


def _network(shared_dir: Path, *, primary_recorded: str | None = None,
             admin_recorded: list[str] | None = None) -> dict:
    pod = {
        "admin_passphrase": "charles",
        "primary_passphrase": "darwin",
    }
    if admin_recorded is not None:
        pod["admins"] = {"external_ids": {"telegram": list(admin_recorded)}}
    bots: dict = {}
    if primary_recorded is not None:
        bots["team_bot_a"] = {
            "primary_user": {"external_ids": {"telegram": primary_recorded}}
        }
    return {
        "members": ["team_bot_a"], "sharedDir": str(shared_dir),
        "pod": pod, "bots": bots,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phases / initial routing
# ─────────────────────────────────────────────────────────────────────────────


def test_initial_phase_secondary_starts_at_challenge():
    from evolve_admin.evo.wizard.phases import (
        initial_phase, PHASE_CHALLENGE, PHASE_GREET,
    )
    assert initial_phase("primary", "secondary") == PHASE_CHALLENGE
    assert initial_phase("primary", "primary") == PHASE_GREET
    assert initial_phase("primary", "admin") == PHASE_GREET


def test_phase_chain_includes_challenge():
    from evolve_admin.evo.wizard import phases
    assert phases.get_phase(phases.PHASE_CHALLENGE) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch routing
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_secondary_starts_challenge(tmp_path):
    from evolve_admin.evo import dispatch

    network = _network(tmp_path, primary_recorded="OWNER")
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="STRANGER", raw_text="evo wizard",
    )
    assert r.subcommand == "wizard"
    assert r.wizard_session_id == "ext:telegram:STRANGER"
    assert "challenge" in (r.system_append or "").lower()
    # Old "coming soon" stub is gone for secondaries
    assert "coming soon" not in (r.system_append or "").lower()


def test_dispatch_admin_starts_greet(tmp_path):
    from evolve_admin.evo import dispatch

    network = _network(
        tmp_path, primary_recorded="OWNER", admin_recorded=["MY_ADMIN"],
    )
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="MY_ADMIN", raw_text="evo wizard",
    )
    assert r.role == "admin"
    assert "greet" in (r.system_append or "").lower()


def test_dispatch_primary_starts_greet(tmp_path):
    from evolve_admin.evo import dispatch

    network = _network(tmp_path, primary_recorded="OWNER")
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="OWNER", raw_text="evo wizard",
    )
    assert r.role == "primary"
    assert "greet" in (r.system_append or "").lower()


def test_dispatch_unconfigured_bot_falls_back_to_greet(tmp_path):
    """No primary recorded → resolve_role returns 'primary' (v1 fallback)
    → wizard starts at GREET, not CHALLENGE."""
    from evolve_admin.evo import dispatch

    network = _network(tmp_path)  # no primary_recorded
    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="OWNER", raw_text="evo wizard",
    )
    assert r.role == "primary"
    assert "greet" in (r.system_append or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# Challenge turn handling
# ─────────────────────────────────────────────────────────────────────────────


def _start_challenge(tmp_path, network):
    """Helper: kick off a wizard for a secondary caller and return the
    wizard_session_id ready for process_turn."""
    from evolve_admin.evo import dispatch

    r = dispatch.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="STRANGER", raw_text="evo wizard",
    )
    assert r.wizard_session_id == "ext:telegram:STRANGER"
    return r.wizard_session_id


def test_challenge_admin_passphrase_advances_to_greet(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="charles", network=network,
    )
    assert r is not None
    assert r.phase == "greet"
    assert r.completed is False
    assert r.network_dirty is True
    assert r.wizard_session_id == wsid
    assert "pod admin" in (r.system_append or "").lower()
    # Admin recorded
    admins = network["pod"]["admins"]["external_ids"]["telegram"]
    assert "STRANGER" in admins


def test_challenge_primary_passphrase_no_existing_primary(tmp_path):
    """Edge case: state ended up at CHALLENGE for a user (e.g. they
    started while a primary was recorded) and by the time they reply,
    the primary slot is open (admin reassigned mid-wizard). The primary
    claim should now succeed and advance them to GREET."""
    from evolve_admin.evo.wizard import engine, state, phases

    network = _network(tmp_path)  # no primary recorded
    state.initialize(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        audience="primary", initial_phase=phases.PHASE_CHALLENGE,
    )

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        user_message="darwin", network=network,
    )
    assert r is not None
    assert r.phase == "greet"
    assert r.network_dirty is True
    assert ext_ids.ids_for(
        network["bots"]["team_bot_a"]["primary_user"], "telegram"
    ) == ["STRANGER"]


def test_challenge_primary_passphrase_conflict_ends_wizard(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="darwin", network=network,
    )
    assert r is not None
    assert r.completed is True  # Wizard ends on no-claim outcome
    assert r.wizard_session_id is None
    assert r.network_dirty is False
    assert (
        network["bots"]["team_bot_a"]["primary_user"]["external_ids"]["telegram"]
        == "OWNER"
    )


def test_challenge_both_passphrases(tmp_path):
    """Both passphrases in one message: admin claim is pod-wide (always
    succeeds), primary claim depends on slot availability. Plant CHALLENGE
    state on a bot with no primary recorded so primary also lands."""
    from evolve_admin.evo.wizard import engine, state, phases

    network = _network(tmp_path)  # no primary recorded
    state.initialize(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        audience="primary", initial_phase=phases.PHASE_CHALLENGE,
    )

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        user_message="charles darwin", network=network,
    )
    assert r is not None
    assert r.phase == "greet"
    assert r.network_dirty is True
    body = (r.system_append or "").lower()
    assert "pod admin" in body
    assert "primary user" in body
    # Both stored
    assert ext_ids.has_id(network["pod"]["admins"], "telegram", "STRANGER")
    assert ext_ids.ids_for(
        network["bots"]["team_bot_a"]["primary_user"], "telegram"
    ) == ["STRANGER"]


def test_challenge_decline_ends_wizard(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="skip", network=network,
    )
    assert r.completed is True
    assert r.wizard_session_id is None
    assert r.network_dirty is False


@pytest.mark.parametrize("decline_word", ["no", "nope", "pass", "later", "cancel", "stop", "SKIP"])
def test_challenge_recognizes_various_declines(tmp_path, decline_word):
    from evolve_admin.evo.wizard import engine

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)
    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message=decline_word, network=network,
    )
    assert r.completed is True
    assert r.network_dirty is False


def test_challenge_unrecognized_text_ends_wizard(tmp_path):
    from evolve_admin.evo.wizard import engine

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="who am I anyway", network=network,
    )
    assert r.completed is True
    assert r.network_dirty is False
    # Body framing distinguishes attempt vs decline
    assert "didn't recognize" in (r.system_append or "")


def test_challenge_writes_audit_log_on_admin_claim(tmp_path):
    from evolve_admin.evo.wizard import engine
    from evolve_admin.evo import audit

    network = _network(tmp_path, primary_recorded="OWNER")
    wsid = _start_challenge(tmp_path, network)

    engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key=wsid,
        user_message="charles", network=network,
    )
    events = audit.read_events(tmp_path)
    assert any(e["action"] == "claim_admin" for e in events)
    admin_event = next(e for e in events if e["action"] == "claim_admin")
    assert admin_event["to"] == "STRANGER"


def test_challenge_idempotent_when_already_admin(tmp_path):
    """A user who's already admin shouldn't re-claim through the
    challenge — they wouldn't normally hit CHALLENGE since their role
    resolves to admin, but if state somehow points there the handler
    should be a clean no-op rather than re-recording or erroring."""
    from evolve_admin.evo.wizard import engine, state, phases

    network = _network(
        tmp_path, primary_recorded="OWNER", admin_recorded=["STRANGER"],
    )
    # Manually plant a CHALLENGE state for this user (skipping dispatch).
    state.initialize(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        audience="primary", initial_phase=phases.PHASE_CHALLENGE,
    )

    r = engine.process_turn(
        tmp_path, bot_id="team_bot_a", user_key="ext:telegram:STRANGER",
        user_message="charles", network=network,
    )
    # No new claim recorded (already admin), so wizard ends with no claim.
    assert r.completed is True
    assert r.network_dirty is False


# ─────────────────────────────────────────────────────────────────────────────
# Route integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def evo_app(tmp_path, monkeypatch):
    from flask import Flask
    from evolve_admin.web import evo_routes
    from evolve_admin import config as _cfg

    network = _network(tmp_path, primary_recorded="OWNER")
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
    return app, tmp_path, network_path


def test_route_secondary_wizard_then_admin_claim_persists(evo_app):
    app, _, network_path = evo_app
    client = app.test_client()

    # Turn 1 — secondary starts wizard, lands at CHALLENGE
    r1 = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a", "channel": "telegram",
            "sender_external_id": "STRANGER", "raw_text": "evo wizard",
        },
    )
    assert r1.status_code == 200
    d1 = r1.get_json()
    wsid = d1["wizard_session_id"]
    assert wsid == "ext:telegram:STRANGER"

    # Turn 2 — types admin passphrase via wizard turn endpoint
    r2 = client.post(
        "/api/evo/wizard/turn",
        json={
            "bot_id": "team_bot_a", "wizard_session_id": wsid,
            "user_message": "charles",
        },
    )
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["phase"] == "greet"
    assert d2["completed"] is False

    # Network persisted to disk
    on_disk = json.loads(network_path.read_text())
    assert "STRANGER" in on_disk["pod"]["admins"]["external_ids"]["telegram"]


def test_route_secondary_decline_does_not_persist(evo_app):
    app, _, network_path = evo_app
    client = app.test_client()

    r1 = client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "team_bot_a", "channel": "telegram",
            "sender_external_id": "STRANGER", "raw_text": "evo wizard",
        },
    )
    wsid = r1.get_json()["wizard_session_id"]

    r2 = client.post(
        "/api/evo/wizard/turn",
        json={"bot_id": "team_bot_a", "wizard_session_id": wsid, "user_message": "skip"},
    )
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["completed"] is True
    assert d2["wizard_session_id"] is None

    on_disk = json.loads(network_path.read_text())
    # Pod block has no admins key (or empty) — no claim was applied
    admins = on_disk.get("pod", {}).get("admins", {}).get("external_ids", {})
    assert "STRANGER" not in admins.get("telegram", [])

"""tests/test_evo_wizard_render_modes.py — verbatim vs agenda render-mode
dispatch on wizard TurnResults.

Covers the structural fix that prevents the LLM from being asked to
render canonical-body wizard prompts. See prompts.py / engine.py /
phases.py changes in the wizard verbatim render-mode PR.

Asserts:
  * Phase declarations: each canonical-body phase is render_mode="verbatim";
    each conversational phase is render_mode="agenda"
  * TurnResult auto-routing: verbatim phases populate direct_send_message
    AND wrap system_append with the LLM-fallback "respond verbatim" framing;
    agenda phases leave direct_send_message=None
  * End-to-end via setup-google: every turn returns a clean direct_send body
    with no LLM-instruction overhead bleeding into it
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Phase declarations — render_mode is the source of truth
# ─────────────────────────────────────────────────────────────────────────────


def test_default_render_mode_is_agenda():
    """Phases that don't declare render_mode default to agenda — matches
    the wizard's original LLM-driven design. Verbatim is an explicit
    opt-in for canonical-body phases."""
    from evolve_admin.evo.wizard import phases as _p
    assert _p.GREET_PHASE.render_mode == "agenda"
    assert _p.ABOUT_YOU_PHASE.render_mode == "agenda"
    assert _p.GOALS_PHASE.render_mode == "agenda"
    assert _p.PLATFORM_TOUR_PHASE.render_mode == "agenda"
    assert _p.GALLERY_RECS_PHASE.render_mode == "agenda"
    assert _p.WRAP_PHASE.render_mode == "agenda"
    assert _p.CHALLENGE_PHASE.render_mode == "agenda"
    assert _p.SECONDARY_GREET_PHASE.render_mode == "agenda"
    assert _p.SECONDARY_ABOUT_YOU_PHASE.render_mode == "agenda"
    assert _p.HOW_TO_USE_PHASE.render_mode == "agenda"
    assert _p.SECONDARY_WRAP_PHASE.render_mode == "agenda"
    assert _p.GUIDE_GATHER_PHASE.render_mode == "agenda"
    assert _p.FORGE_INTRO_PHASE.render_mode == "agenda"
    assert _p.FORGE_DESIGN_PHASE.render_mode == "agenda"


def test_canonical_body_phases_are_verbatim():
    """Phases whose render is canonical user-facing text (URLs, exact
    Console steps, drafted spec previews, guide previews) opt into
    verbatim mode — the plugin direct-sends and the LLM stays silent."""
    from evolve_admin.evo.wizard import phases as _p
    # All 5 setup-google phases — every turn is exact text
    assert _p.GOOGLE_SETUP_INTRO_PHASE.render_mode == "verbatim"
    assert _p.GOOGLE_SETUP_ADMIN_URL_PHASE.render_mode == "verbatim"
    assert _p.GOOGLE_SETUP_CONSOLE_PHASE.render_mode == "verbatim"
    assert _p.GOOGLE_SETUP_CLIENT_ID_PHASE.render_mode == "verbatim"
    assert _p.GOOGLE_SETUP_CLIENT_SECRET_PHASE.render_mode == "verbatim"
    # Spec / preview / guide renders — exact text matters for the
    # downstream classifier (approve / iterate / cancel etc.)
    assert _p.APP_CREATE_DESCRIBE_PHASE.render_mode == "verbatim"
    assert _p.APP_CREATE_REVIEW_PHASE.render_mode == "verbatim"
    assert _p.GUIDE_CONFIRM_PHASE.render_mode == "verbatim"
    assert _p.FORGE_CONFIRM_PHASE.render_mode == "verbatim"
    # rec_pending pitch/clarify/context/push/all_caught_up render as
    # deterministic user-facing text (2026-05-17). Verbatim under the
    # suppress-LLM regime — agenda mode lost the compliance fight
    # against the conversation-history pattern from prior `evo X` turns.
    assert _p.REC_PENDING_PHASE.render_mode == "verbatim"


# ─────────────────────────────────────────────────────────────────────────────
# TurnResult.__post_init__ auto-routing
# ─────────────────────────────────────────────────────────────────────────────


def test_verbatim_phase_populates_direct_send_and_wraps_system_append():
    """Constructing a TurnResult with a verbatim phase and a body in
    system_append: __post_init__ copies the body to direct_send_message
    AND wraps system_append with LLM-fallback framing."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases

    from evolve_admin.evo import _delimiters as _evo_delim

    body = "**Step 1 of 4 — admin base URL**\n\nPaste the URL:"
    r = _engine.TurnResult(
        system_append=body,
        wizard_session_id="ext:telegram:1",
        phase=_phases.PHASE_GOOGLE_SETUP_ADMIN_URL,
        completed=False,
    )
    # Body is the direct-send payload — wrapped with evo delimiters so
    # the user can distinguish it from any LLM hallucinations alongside
    assert _evo_delim.EVO_DELIMITER_OPEN in r.direct_send_message
    assert _evo_delim.EVO_DELIMITER_CLOSE in r.direct_send_message
    assert body in r.direct_send_message  # the canonical body is in there
    # system_append is wrapped with the verbatim framing for LLM fallback
    assert r.system_append != body
    assert "respond only with the following message, verbatim" in r.system_append.lower()
    # Body is appended after the wrapper so substring assertions still work
    assert body in r.system_append


def test_agenda_phase_leaves_direct_send_none():
    """Constructing a TurnResult with an agenda phase: direct_send_message
    stays None, system_append is unmodified — the LLM engages as today."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases

    agenda = "[EVO WIZARD]\nPhase: greet.\nGoal: ask the user's name."
    r = _engine.TurnResult(
        system_append=agenda,
        wizard_session_id="ext:telegram:1",
        phase=_phases.PHASE_GREET,
        completed=False,
    )
    assert r.direct_send_message is None
    assert r.system_append == agenda  # untouched


def test_explicit_direct_send_message_skips_verbatim_wrap_but_still_delimited():
    """If a caller explicitly sets ``direct_send_message``,
    __post_init__ skips the verbatim auto-route logic (no copy from
    system_append, no system_append wrap). BUT it still applies the
    evo visual delimiters — those are universal for any direct-send,
    not tied to verbatim auto-routing."""
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import phases as _phases
    from evolve_admin.evo import _delimiters as _evo_delim

    body = "verbatim body"
    explicit = "different explicit body"
    r = _engine.TurnResult(
        system_append=body,
        direct_send_message=explicit,
        wizard_session_id=None,
        phase=_phases.PHASE_GOOGLE_SETUP_INTRO,
        completed=False,
    )
    # Explicit direct_send_message preserved (just wrapped with delimiters)
    assert explicit in r.direct_send_message
    assert _evo_delim.EVO_DELIMITER_OPEN in r.direct_send_message
    # No verbatim auto-route happened — system_append untouched (no wrap)
    assert r.system_append == body


def test_unknown_phase_does_not_crash_post_init():
    """Defensive — constructing a TurnResult with a phase the registry
    doesn't know shouldn't crash __post_init__. Makes test mocks easier."""
    from evolve_admin.evo.wizard import engine as _engine

    r = _engine.TurnResult(
        system_append="anything",
        wizard_session_id=None,
        phase="phase_that_does_not_exist",
        completed=False,
    )
    # Unknown phase → no auto-routing, no wrap, untouched
    assert r.direct_send_message is None
    assert r.system_append == "anything"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: setup-google chain → every turn direct-sends a clean body
# ─────────────────────────────────────────────────────────────────────────────


VALID_CLIENT_ID = (
    "1234567890-abcdefghijklmnopqrstuvwxyzABCDEF.apps.googleusercontent.com"
)
VALID_CLIENT_SECRET = "GOCSPX-AbCdEfGhIjKlMnOpQrStUvWx"
VALID_ADMIN_URL = "https://evolve.example.com"


@pytest.fixture
def network(tmp_path):
    return {
        "sharedDir": str(tmp_path),
        "members": ["evolve"],
        "primary": "evolve",
        "bots": {
            "evolve": {"primary_user": {
                "external_ids": {"telegram": "12345"},
            }},
        },
    }


def _dispatch(network, raw, bot_id="evolve"):
    from evolve_admin.evo.dispatch import dispatch
    return dispatch(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345", raw_text=raw,
    )


def _turn(network, msg, bot_id="evolve"):
    from evolve_admin.evo.wizard.engine import process_turn
    from evolve_admin.evo.identity import derive_user_key
    shared = Path(network["sharedDir"])
    user_key = derive_user_key(
        network, bot_id=bot_id, channel="telegram",
        sender_external_id="12345",
    )
    return process_turn(
        shared, bot_id=bot_id, user_key=user_key,
        user_message=msg, network=network,
    )


def test_setup_google_dispatch_returns_clean_direct_send_body(network):
    """First turn: `evo setup-google` dispatches and returns a
    DispatchResult with direct_send_message populated — clean body, no
    LLM-instruction overhead. The plugin will send this verbatim."""
    r = _dispatch(network, "evo setup-google")
    assert r.direct_send_message is not None
    body = r.direct_send_message
    # The user-facing canonical body
    assert "Setting up Google Workspace access" in body
    assert "Reply `ready`" in body
    # No LLM-instruction framing should bleed into the user-facing body
    assert "Phase:" not in body
    assert "Goal:" not in body
    assert "[EVO WIZARD" not in body
    assert "Render this verbatim" not in body
    assert "Render verbatim" not in body


def test_setup_google_every_phase_yields_clean_direct_send(network):
    """Walk the entire setup-google chain. Every phase is verbatim, so
    every turn must populate direct_send_message with a clean body."""
    _dispatch(network, "evo setup-google")
    # Phase 1: ready → admin_url ask
    r = _turn(network, "ready")
    assert r.direct_send_message is not None
    assert "admin base URL" in r.direct_send_message
    assert "Phase:" not in r.direct_send_message
    # Phase 2: paste URL → console checklist
    r = _turn(network, VALID_ADMIN_URL)
    assert r.direct_send_message is not None
    assert "Google Cloud Console" in r.direct_send_message
    assert f"{VALID_ADMIN_URL}/api/admin/onboard/google/callback" in r.direct_send_message
    assert "Phase:" not in r.direct_send_message
    # Phase 3: done → client_id ask
    r = _turn(network, "done")
    assert r.direct_send_message is not None
    assert "Client ID" in r.direct_send_message
    assert "Phase:" not in r.direct_send_message
    # Phase 4: paste client_id → client_secret ask
    r = _turn(network, VALID_CLIENT_ID)
    assert r.direct_send_message is not None
    assert "Client Secret" in r.direct_send_message
    assert "Phase:" not in r.direct_send_message


def test_setup_google_validation_error_is_also_verbatim(network):
    """A validation failure (bad URL) re-renders the same phase — that
    re-render must also be a clean direct-send body, not an LLM agenda."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    r = _turn(network, "localhost:5050")  # invalid URL
    assert r.direct_send_message is not None
    body = r.direct_send_message
    assert "didn't validate" in body
    # No LLM-instruction overhead in the validation re-render
    assert "Phase:" not in body
    assert "Goal:" not in body
    assert "Render verbatim" not in body


def test_setup_google_cancel_is_clean_direct_send(network):
    """Cancel terminal — also a verbatim phase, also direct-sends."""
    _dispatch(network, "evo setup-google")
    r = _turn(network, "cancel")
    assert r.direct_send_message is not None
    assert "Cancelled" in r.direct_send_message
    assert "no credentials saved" in r.direct_send_message.lower()
    assert "Phase:" not in r.direct_send_message

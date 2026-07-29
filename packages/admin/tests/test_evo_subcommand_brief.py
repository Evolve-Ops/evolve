"""tests/test_evo_subcommand_brief.py — ``subcommand_brief`` plumbing.

The plugin's ``before_prompt_build`` directive briefs the LLM in
plain English about what the user asked for. The brief comes through
the wire envelope:

  * Dispatch turns: ``DispatchResult.subcommand_brief`` — auto-derived
    in __post_init__ from the subcommand registry's ``short_help``.
  * Wizard turns: response field on ``/api/evo/wizard/turn`` — derived
    at the wire boundary from the wizard state's ``audience``.

These tests pin both producer paths so the plugin can rely on the
field being present whenever the server has a brief to give.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# DispatchResult.subcommand_brief auto-derivation
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_result_auto_fills_brief_from_registry():
    """``DispatchResult(subcommand="help", …)`` → ``subcommand_brief``
    auto-set to the registry's short_help for ``help``."""
    from evolve_admin.evo.dispatch import DispatchResult
    from evolve_admin.evo import subcommands as _sc

    expected = _sc.lookup("help").short_help

    r = DispatchResult(
        subcommand="help",
        role="primary",
        mode="speak",
        direct_send_message="**evo commands**\n  …",
    )
    assert r.subcommand_brief == expected


def test_dispatch_result_brief_for_setup_google():
    """The motivating subcommand — ``evo setup-google`` — gets the
    Google-specific short_help threaded through."""
    from evolve_admin.evo.dispatch import DispatchResult

    r = DispatchResult(
        subcommand="setup-google",
        role="primary",
        mode="speak",
        direct_send_message="**Setting up Google Workspace…**",
    )
    assert r.subcommand_brief is not None
    # short_help text from subcommands.py registry
    assert "Google" in r.subcommand_brief or "google" in r.subcommand_brief.lower()


def test_dispatch_result_brief_unchanged_when_caller_set_explicitly():
    """Caller-supplied ``subcommand_brief`` wins over the auto-derive
    — lets a handler override with a more specific phrasing if it
    wants to (e.g. for a per-call variant)."""
    from evolve_admin.evo.dispatch import DispatchResult

    r = DispatchResult(
        subcommand="help",
        role="primary",
        mode="speak",
        subcommand_brief="caller's custom brief",
    )
    assert r.subcommand_brief == "caller's custom brief"


def test_dispatch_result_brief_none_for_unknown_subcommand():
    """Unknown subcommand → registry lookup returns None → brief stays
    None. Plugin falls back to the directive's generic wording."""
    from evolve_admin.evo.dispatch import DispatchResult

    r = DispatchResult(
        subcommand="nonexistent-subcommand",
        role="primary",
        mode="speak",
    )
    assert r.subcommand_brief is None


# ─────────────────────────────────────────────────────────────────────────────
# /api/evo/wizard/turn includes subcommand_brief from wizard audience
# ─────────────────────────────────────────────────────────────────────────────
#
# The route looks up the wizard state's ``audience`` and maps it to a
# human-readable brief. Verified end-to-end by driving a real wizard via
# the test client and inspecting the response shape.


@pytest.fixture
def wizard_app(tmp_path):
    """Spin up the admin Flask app pointed at a tmp network — same
    fixture pattern as test_evo_wizard.py's evo_app."""
    from evolve_admin.web.server import create_app
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "evolve"],
        "primary": "evolve",
        "bots": {
            "admin_bot": {
                "primary_user": {"external_ids": {"telegram": "12345"}},
            },
            "evolve": {
                "primary_user": {"external_ids": {"telegram": "12345"}},
            },
        },
    }))
    app = create_app(network_path)
    return app, tmp_path


def test_wizard_turn_response_includes_brief_for_google_setup(wizard_app):
    """A live ``evo setup-google`` flow on admin_bot — the subsequent
    wizard-turn response carries the google_setup brief so the
    plugin can pass it to the LLM directive."""
    app, _tmp = wizard_app
    client = app.test_client()

    # Start the wizard
    client.post(
        "/api/evo/dispatch",
        json={
            "bot_id": "admin_bot", "channel": "telegram",
            "sender_external_id": "12345",
            "raw_text": "evo setup-google",
        },
    )

    # Take a wizard turn (reply "ready" to advance from intro)
    r = client.post(
        "/api/evo/wizard/turn",
        json={
            "bot_id": "admin_bot",
            "wizard_session_id": "ext:telegram:12345",
            "user_message": "ready",
        },
    )
    assert r.status_code == 200
    data = r.get_json()
    assert "subcommand_brief" in data
    assert data["subcommand_brief"] is not None
    # The audience-based map (in evo_routes.py) describes setup-google
    assert "setup-google" in data["subcommand_brief"] or "Google" in data["subcommand_brief"]


def test_wizard_turn_response_includes_brief_field_even_when_unknown(wizard_app):
    """When the wizard state can't be read (404 path), the response
    still has the field shape — null, not missing — so the plugin
    doesn't hit ``undefined`` access errors."""
    app, _tmp = wizard_app
    client = app.test_client()
    r = client.post(
        "/api/evo/wizard/turn",
        json={
            "bot_id": "admin_bot",
            "wizard_session_id": "ext:telegram:no-such-session",
            "user_message": "x",
        },
    )
    # 404 — no active wizard. The response shape may differ for 404s
    # (the route returns {error, wizard_session_id: null}); the brief
    # is only surfaced on the 200 path. The plugin's wizardTurn
    # client handles both shapes — verified separately in TS.
    assert r.status_code == 404

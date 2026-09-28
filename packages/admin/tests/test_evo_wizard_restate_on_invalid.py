"""tests/test_evo_wizard_restate_on_invalid.py — pin the "restate on
invalid + always offer cancel" wizard UX principle.

Motivating bug: typing ``evo help`` during ``setup-google`` admin_url
hit the URL validator, which surfaced ``"must start with http://"`` —
factually correct, but the user couldn't tell from the message that
``cancel`` was an option without scrolling back to the original ask.
They had to manually look up the cancel keyword.

Fix is small + structural: every ``*_invalid`` re-render restates the
full original prompt (which itself ends with the cancel hint), so the
re-render carries the same escape route + examples that the first ask
did. Every initial-ask renderer also includes the cancel hint so the
user sees it from turn one.

These tests pin the contract so we don't regress.
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
# Initial-ask renders: every one mentions ``cancel`` so the escape
# route is visible from turn one.
# ─────────────────────────────────────────────────────────────────────────────


def test_google_setup_initial_asks_all_mention_cancel():
    """Each Step N of setup-google should mention ``cancel`` — keeps
    the escape route consistent across the chain so the user never has
    to scroll back to find out how to abort."""
    from evolve_admin.evo.wizard import prompts

    # Step 0 (intro) — already had it; pin it
    assert "cancel" in prompts.render_google_setup_intro().lower()
    # Step 1 (admin URL) — previously the only one MISSING the cancel hint
    assert "cancel" in prompts.render_google_setup_admin_url().lower()
    # Step 2 (console checklist)
    assert "cancel" in prompts.render_google_setup_console(
        "https://example.com",
    ).lower()
    # Step 3 (client ID)
    assert "cancel" in prompts.render_google_setup_client_id().lower()
    # Step 4 (client secret)
    assert "cancel" in prompts.render_google_setup_client_secret().lower()


def test_app_create_initial_asks_all_mention_cancel():
    from evolve_admin.evo.wizard import prompts

    assert "cancel" in prompts.render_app_create_describe_intro().lower()
    # Too-short re-prompt — also an "ask" from the user's POV
    assert "cancel" in prompts.render_app_create_describe_too_short().lower()
    # Draft-failed retry — same
    assert "cancel" in prompts.render_app_create_draft_failed().lower()
    # Review (approve/iterate/cancel) — already pins cancel
    sample_draft = {"display_name": "Demo", "description": "test"}
    assert "cancel" in prompts.render_app_create_review(sample_draft).lower()
    # Iterate-empty — re-prompt with cancel option
    assert "cancel" in prompts.render_app_create_iterate_empty(sample_draft).lower()


# ─────────────────────────────────────────────────────────────────────────────
# *_invalid re-renders: restate the original prompt so the user has
# the full ask in front of them (examples + cancel hint), not just an
# error message.
# ─────────────────────────────────────────────────────────────────────────────


def test_admin_url_invalid_restates_original_prompt():
    """Validation failure → user sees the full original prompt again
    underneath the error. No scrollback needed to remember what was
    expected (or how to cancel)."""
    from evolve_admin.evo.wizard import prompts

    body = prompts.render_google_setup_admin_url_invalid(
        "URL must start with http:// or https://",
    )
    # Error context
    assert "didn't validate" in body
    assert "must start with http" in body
    # Original prompt restated — pin a few distinctive substrings from
    # render_google_setup_admin_url() so we know the full ask is there
    # (not just the cancel hint reflexively appended)
    assert "Step 1 of 4" in body
    assert "tailscale" in body.lower()
    assert "ngrok" in body.lower()
    # Cancel escape (comes from the restated prompt)
    assert "cancel" in body.lower()


def test_client_id_invalid_restates_original_prompt():
    from evolve_admin.evo.wizard import prompts

    body = prompts.render_google_setup_client_id_invalid(
        "missing apps.googleusercontent.com suffix",
    )
    assert "didn't validate" in body
    assert "missing apps.googleusercontent.com" in body
    # Restated prompt
    assert "Step 3 of 4" in body
    assert "apps.googleusercontent.com" in body
    assert "cancel" in body.lower()


def test_client_secret_invalid_restates_original_prompt_with_bot_id():
    """The secret prompt is bot-scoped (it mentions where the secret
    will be stored), so the *_invalid must pass bot_id through to
    the restated prompt — not fall back to 'this bot' when the
    caller knew which bot."""
    from evolve_admin.evo.wizard import prompts

    body = prompts.render_google_setup_client_secret_invalid(
        "secrets shouldn't start with that", bot_id="admin_bot",
    )
    assert "didn't validate" in body
    # Restated prompt — bot-scoped storage explanation
    assert "Step 4 of 4" in body
    assert "admin_bot" in body  # bot_id threaded through
    assert "auth-profiles.json" in body
    assert "cancel" in body.lower()


def test_client_secret_invalid_defaults_bot_id_for_back_compat():
    """Callers that don't pass bot_id get a sensible default rather
    than an exception — keeps the renderer safe to call from tests
    and from any future code path that doesn't have the bot_id at hand."""
    from evolve_admin.evo.wizard import prompts

    body = prompts.render_google_setup_client_secret_invalid("nope")
    assert "this bot" in body
    assert "Step 4 of 4" in body


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: process_turn drives the wizard, an invalid input
# produces a re-render that's self-contained (prompt + cancel) so the
# user has everything in one message.
# ─────────────────────────────────────────────────────────────────────────────


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


def test_evo_help_during_admin_url_phase_now_self_explanatory(network):
    """The motivating bug end-to-end. Type ``evo help`` during the
    admin_url phase — the URL validator rejects it, the re-render
    includes the full original ask + the cancel hint, so the user
    can recover without scrolling back."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")  # advance to admin_url phase
    r = _turn(network, "evo help")
    assert r is not None
    body = r.direct_send_message
    assert body is not None
    # Validation error surfaced
    assert "didn't validate" in body
    # Full original ask restated underneath (examples, formatting hints)
    assert "Step 1 of 4" in body
    assert "tailscale" in body.lower() or "ngrok" in body.lower()
    # Cancel escape visible without scrollback
    assert "cancel" in body.lower()


def test_invalid_client_secret_re_render_threads_bot_id(network):
    """Walk to the client_secret phase and submit something invalid —
    the re-render mentions the actual bot (``evolve``), not the
    generic ``this bot`` fallback."""
    _dispatch(network, "evo setup-google")
    _turn(network, "ready")
    _turn(network, "https://example.com")  # valid URL, advances
    _turn(network, "done")  # advance past console checklist
    _turn(network,
          "1234567890-abc.apps.googleusercontent.com")  # client_id ok
    r = _turn(network, "not a real secret")
    assert r is not None
    body = r.direct_send_message
    assert body is not None
    assert "didn't validate" in body
    # bot_id is "evolve" in this fixture
    assert "evolve" in body

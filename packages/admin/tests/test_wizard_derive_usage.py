"""tests/test_wizard_derive_usage.py — _derive_usage_from_state shape.

The chat wizard synthesizes a usage block from the operator's natural-
language description rather than asking field-by-field. This file pins
the shape so the wizard never ships an app with an empty
bot_voice_examples list — which previously rendered an empty "Sample
bot replies" section in INSTALLED_APPS.md and left the LLM without a
voice anchor when invoking the app.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))

from evolve_admin.evo.wizard.forge_handlers import _derive_usage_from_state  # noqa: E402


def test_derive_emits_at_least_one_voice_example() -> None:
    """The wizard used to hardcode bot_voice_examples=[], which meant every
    wizard-built app shipped with an empty "Sample bot replies" section
    in INSTALLED_APPS.md. Now it emits parametric placeholders so the
    rendered entry always has a voice anchor."""
    usage = _derive_usage_from_state(
        display_name="Test App",
        description="A test app for testing.",
        capabilities=["test"],
        behaviors=["run tests"],
        conversational_summary="When user asks, run tests.",
    )
    assert usage["bot_voice_examples"], (
        "wizard must emit at least one bot_voice_example — empty list "
        "renders to an empty 'Sample bot replies' section in INSTALLED_APPS.md"
    )
    assert all(isinstance(s, str) and s.strip() for s in usage["bot_voice_examples"])


def test_voice_examples_reference_app_name() -> None:
    """Examples should mention the app by name so the renderer's voice
    section reads as actual app voice, not generic filler. Catches the
    case where someone replaces the parametric template with a static
    string and the same line shows up for every app on the bot."""
    usage = _derive_usage_from_state(
        display_name="MyUniqueApp",
        description="x",
        capabilities=[],
        behaviors=[],
        conversational_summary="x",
    )
    joined = " ".join(usage["bot_voice_examples"]).lower()
    assert "myuniqueapp" in joined


def test_empty_display_name_doesnt_crash() -> None:
    """Defensive: a wizard run with no display_name yet should still
    produce a usage block rather than crashing on f-string substitution."""
    usage = _derive_usage_from_state(
        display_name="",
        description="x",
        capabilities=[],
        behaviors=[],
        conversational_summary="",
    )
    assert isinstance(usage["bot_voice_examples"], list)
    assert usage["bot_voice_examples"]


def test_other_usage_fields_unchanged_by_this_fix() -> None:
    """Regression guard: changing the bot_voice_examples derivation
    shouldn't drift the other fields the wizard emits."""
    usage = _derive_usage_from_state(
        display_name="Task Tracker",
        description="Tracks tasks.",
        capabilities=["track", "remind"],
        behaviors=["add task", "list tasks"],
        conversational_summary="When the user wants to track work.",
    )
    assert usage["model"] == "user-initiated"
    assert usage["how_to_use"]
    assert usage["trigger_recognition"]["hint_words"]
    assert usage["auto_capture"]["enabled"] is False

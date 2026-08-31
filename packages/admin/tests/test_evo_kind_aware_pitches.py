"""tests/test_evo_kind_aware_pitches.py — Per-action-kind framing.

Step 2 of the evo unify plan. The pitch the bot reads to the user
should match what "yes" actually means. ConfigPatch yes = run applier
now; Investigation yes = take this on, do the work, come back; BuildApp
yes = dispatch to forge.

This suite verifies:

1. Each known action_kind has distinct semantics (verb + yes_means)
2. Manual-completion kinds (Investigation, WorkflowInstruction) include
   the "comes to In Process queue" follow-up framing
3. External-completion kinds (BuildApp) include the "forge owns the
   remainder" follow-up framing
4. Recs without action_kind (non-proposal adapters: onboarding /
   scoreboard / compliance / whimsy) fall back to generic semantics
5. The pitch block actually emits the kind-specific text
"""

from __future__ import annotations

import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ──────────────────────────────────────────────────────────────────────────
# Semantics map
# ──────────────────────────────────────────────────────────────────────────


def test_known_action_kinds_have_distinct_verbs():
    """Each kind's verb should be different so the user can tell them
    apart in chat. ConfigPatch's 'apply this patch' is a different
    commitment than Investigation's 'take this on'."""
    from evolve_admin.evo.wizard.prompts import _KIND_PITCH_SEMANTICS

    verbs = {
        kind: spec["verb"] for kind, spec in _KIND_PITCH_SEMANTICS.items()
    }
    # Every kind has a non-empty verb
    for kind, verb in verbs.items():
        assert verb, f"{kind} has empty verb"

    # Investigation and WorkflowInstruction share 'take this on' (same
    # cognitive commitment) but every other kind is distinct.
    shared_verb_kinds = {
        k for k, v in verbs.items() if v == "take this on"
    }
    assert shared_verb_kinds == {"Investigation", "WorkflowInstruction"}


def test_manual_completion_kinds_carry_follow_up_manual():
    from evolve_admin.evo.wizard.prompts import _KIND_PITCH_SEMANTICS

    assert _KIND_PITCH_SEMANTICS["Investigation"]["follow_up"] == "manual"
    assert (
        _KIND_PITCH_SEMANTICS["WorkflowInstruction"]["follow_up"] == "manual"
    )


def test_external_completion_kinds_carry_follow_up_external():
    from evolve_admin.evo.wizard.prompts import _KIND_PITCH_SEMANTICS

    assert _KIND_PITCH_SEMANTICS["BuildApp"]["follow_up"] == "external"


def test_auto_applicable_kinds_have_no_follow_up():
    """ConfigPatch / TierAdjustment / etc. complete autonomously — the
    operator shouldn't see follow-up framing in chat."""
    from evolve_admin.evo.wizard.prompts import _KIND_PITCH_SEMANTICS

    for kind in (
        "ConfigPatch",
        "TierAdjustment",
        "ManifestUpdate",
        "DeprecateApp",
        "SoulEdit",
        "AgentsAppend",
        "ThrottleGenerator",
        "PauseGenerator",
        "InstallApp",
        "VetoAnnotation",
    ):
        assert _KIND_PITCH_SEMANTICS[kind]["follow_up"] is None, (
            f"{kind} should have follow_up=None (autonomous)"
        )


def test_kind_semantics_falls_back_to_generic_for_missing_kind():
    """Recs from non-proposal adapters (onboarding, scoreboard,
    compliance, whimsy) don't carry action_kind. The lookup must
    return generic semantics rather than crashing."""
    from evolve_admin.evo.wizard.prompts import (
        _DEFAULT_PITCH_SEMANTICS,
        _kind_pitch_semantics,
    )

    # No action_kind → default
    assert _kind_pitch_semantics({}) is _DEFAULT_PITCH_SEMANTICS
    assert _kind_pitch_semantics({"action_kind": None}) is _DEFAULT_PITCH_SEMANTICS

    # Unknown kind → default
    assert _kind_pitch_semantics({"action_kind": "FutureKind"}) is _DEFAULT_PITCH_SEMANTICS


def test_kind_semantics_returns_specific_spec_for_known_kind():
    from evolve_admin.evo.wizard.prompts import _kind_pitch_semantics

    semantics = _kind_pitch_semantics({"action_kind": "Investigation"})
    assert semantics["verb"] == "take this on"
    assert semantics["follow_up"] == "manual"

    semantics = _kind_pitch_semantics({"action_kind": "BuildApp"})
    assert semantics["verb"] == "build this app"
    assert semantics["follow_up"] == "external"


# ──────────────────────────────────────────────────────────────────────────
# Pitch block emission
# ──────────────────────────────────────────────────────────────────────────


def _make_rec(action_kind: str | None = None, **overrides) -> dict:
    base = {
        "id": "rec_test",
        "title": "Test recommendation",
        "detail": "Detail body",
        "context": "",
        "tags": [],
    }
    if action_kind is not None:
        base["action_kind"] = action_kind
    base.update(overrides)
    return base


def test_pitch_for_configpatch_describes_immediate_apply():
    """rec_pending pitch is user-facing text (2026-05-17 verbatim
    conversion). For ConfigPatch the pitch surfaces the "what yes
    does" semantic so the user knows it's an immediate write — no
    In Process / forge framing because the kind is autonomous."""
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec(action_kind="ConfigPatch")
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    assert "writes to the bot's config immediately" in text
    assert "In Process queue" not in text
    assert "forge" not in text.lower()


def test_pitch_for_investigation_describes_in_process():
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec(action_kind="Investigation")
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    assert "In Process queue" in text
    assert "handled it" in text


def test_pitch_for_workflowinstruction_describes_file_write_and_in_process():
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec(action_kind="WorkflowInstruction")
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    assert "markdown" in text.lower() or "instruction file" in text
    assert "In Process queue" in text


def test_pitch_for_buildapp_describes_forge_dispatch():
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec(action_kind="BuildApp")
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    assert "forge" in text.lower()
    # External-completion: surface that the forge owns close-out
    assert "reports back" in text or "forge takes over" in text


def test_pitch_for_unknown_kind_uses_generic_semantics():
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec()
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    # The default yes_means ("the engine records your acceptance and
    # follows up") is surfaced in the user-facing pitch.
    assert "engine records your acceptance" in text
    assert "In Process queue" not in text
    assert "forge" not in text.lower()


def test_pitch_emits_reply_options_as_a_line():
    """Under the verbatim-render conversion the pitch shows reply
    options as a single short line, not a bulleted menu — keeps the
    message tight on Telegram / Slack / Discord."""
    from evolve_admin.evo.wizard.prompts import _rec_pending_pitch_block

    rec = _make_rec(action_kind="ConfigPatch")
    text = _rec_pending_pitch_block(rec, bot_id="team_bot_a")

    assert "Reply: yes / no / snooze / next / why?" in text
    # Options not rendered as a bulleted menu list
    assert "\n  - say yes" not in text
    assert "say 'snooze' / 'remind me later' to defer" not in text
